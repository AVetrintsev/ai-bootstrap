import asyncio
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from portal import OwnerAccess, OWNER_PREFIX, SESSION_LIFETIME, digest, totp
from canvas import create_app


class CanvasTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.now = 1_700_000_010
        self.owner = OwnerAccess(self.root, bytes(range(32)), lambda: self.now)
        self.owner.initialize()
        token = self.owner.provision().removeprefix(OWNER_PREFIX).encode()
        _, secret = self.owner.registration_page(token)
        _, self.session = self.owner.register(token, totp(secret, self.now // 30), "127.0.0.1")
        (self.root / "index.html").write_text("<!doctype html><head></head><body>Canvas</body>")
        self.server = TestServer(create_app(self.owner, self.root, self.root,
                                            socket_dir=self.root, check_interval=0.02))
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.client = ClientSession()
        self.addAsyncCleanup(self.client.close)

    async def test_canvas_assets_api_and_websocket_need_a_live_owner_session(self):
        paths = ["/canvas/open/gh-7", "/conversations", "/assets/main.js", "/agent/gh-7/api/settings", "/agent/gh-7/sockets/123"]
        for cookie in ("", "ai_session=invalid"):
            for path in paths:
                async with self.client.get(self.server.make_url(path), headers={"Cookie": cookie}) as response:
                    self.assertEqual(response.status, 404, path)
        async with self.client.get(self.server.make_url("/conversations"),
                                   cookies={"ai_session": self.session}) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("Canvas", await response.text())
        self.now += SESSION_LIFETIME
        async with self.client.get(self.server.make_url("/conversations"),
                                   cookies={"ai_session": self.session}) as response:
            self.assertEqual(response.status, 404)

    async def test_project_api_supplies_its_key_and_revocation_closes_live_websocket(self):
        async def api(request):
            return web.json_response({"key": request.headers.get("X-Session-API-Key"),
                                      "cookie": request.headers.get("Cookie"), "body": await request.json()})

        async def stream(request):
            websocket = web.WebSocketResponse()
            await websocket.prepare(request)
            async for message in websocket:
                await websocket.send_str(message.data)
            return websocket

        backend = web.Application()
        backend.router.add_post("/api/echo", api)
        backend.router.add_get("/sockets/123", stream)
        runner = web.AppRunner(backend)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        await web.UnixSite(runner, str(self.root / "gh-7.sock")).start()
        (self.root / "gh-7.json").write_text(json.dumps({"key": "private-project-key"}))
        cookie = {"ai_session": self.session}
        async with self.client.post(self.server.make_url("/agent/gh-7/api/echo"),
                                    cookies=cookie, json={"message": "hello"},
                                    headers={"X-Session-API-Key": "portal-session"}) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.json(), {"key": "private-project-key", "cookie": None,
                                                      "body": {"message": "hello"}})
        async with self.client.ws_connect(self.server.make_url("/agent/gh-7/sockets/123"),
                                          headers={"Cookie": "ai_session=" + self.session}) as websocket:
            await websocket.send_str("hello")
            self.assertEqual((await websocket.receive(timeout=2)).data, "hello")
            self.owner.change_browser(digest(self.session), digest(self.session), "revoke", "")
            message = await websocket.receive(timeout=2)
            self.assertNotEqual(message.type.name, "TEXT")

    async def test_user_activity_renews_session_but_background_requests_do_not(self):
        original_expiry = self.now + SESSION_LIFETIME
        cookie = {"ai_session": self.session}
        self.now += 100
        async with self.client.get(self.server.make_url("/conversations"), cookies=cookie) as response:
            self.assertEqual(response.status, 200)
            self.assertNotIn("Set-Cookie", response.headers)
        with self.owner.connect() as db:
            self.assertEqual(db.execute("SELECT expires_at FROM sessions").fetchone()[0], original_expiry)
        async with self.client.post(self.server.make_url("/canvas/activity"), cookies=cookie) as response:
            self.assertEqual(response.status, 403)
        origin = str(self.server.make_url("/")).rstrip("/")
        async with self.client.post(self.server.make_url("/canvas/activity"), cookies=cookie,
                                    headers={"Origin": origin}) as response:
            self.assertEqual(response.status, 204)
            self.assertIn("ai_session=", response.headers["Set-Cookie"])
        with self.owner.connect() as db:
            self.assertEqual(db.execute("SELECT expires_at FROM sessions").fetchone()[0], self.now + SESSION_LIFETIME)

    async def test_expiry_closes_idle_event_stream_without_waiting_for_an_agent_event(self):
        finished = asyncio.Event()

        async def stream(request):
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(b"data: ready\n\n")
            try:
                while request.transport and not request.transport.is_closing():
                    await asyncio.sleep(0.01)
            finally:
                finished.set()
            return response

        backend = web.Application()
        backend.router.add_get("/api/events", stream)
        runner = web.AppRunner(backend)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        await web.UnixSite(runner, str(self.root / "gh-7.sock")).start()
        (self.root / "gh-7.json").write_text(json.dumps({"key": "private-project-key"}))
        async with self.client.get(self.server.make_url("/agent/gh-7/api/events"),
                                   cookies={"ai_session": self.session}) as response:
            self.assertEqual(await response.content.readuntil(b"\n\n"), b"data: ready\n\n")
            self.now += SESSION_LIFETIME
            await asyncio.wait_for(finished.wait(), 2)
