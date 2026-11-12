"""Session-gated Canvas frontend and per-project Agent Server transport."""

import asyncio
from contextlib import suppress
import json
import mimetypes
from pathlib import Path
import re
import socket
import time

from aiohttp import ClientError, ClientSession, ClientTimeout, UnixConnector, WSMsgType, web

from portal import KEY, STATE, SESSION_LIFETIME, OwnerAccess, digest, foreground_navigation, github_request


PROJECT = re.compile(r"gh-[1-9][0-9]{0,18}")
SAFE_HEADERS = {"content-type", "accept", "range", "if-none-match", "x-expose-secrets"}
RESPONSE_HEADERS = {"content-type", "content-range", "accept-ranges", "etag"}


def create_app(owner, assets, connections, *, socket_dir=Path("/run/ai-canvas"), check_interval=1):
    @web.middleware
    async def authenticated(request, handler):
        token = request.cookies.get("ai_session", "")
        if not await asyncio.to_thread(owner.has_session, token):
            raise web.HTTPNotFound()
        origin = request.headers.get("Origin")
        if origin and origin != f"{request.scheme}://{request.host}":
            raise web.HTTPForbidden()
        request["session"] = token
        response = await handler(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    async def open_project(request):
        project = request.match_info["project"]
        config = connection(project)
        try:
            async with ClientSession(connector=UnixConnector(path=str(socket_dir / f"{project}.sock")),
                                     timeout=ClientTimeout(total=15)) as client:
                async with client.get("http://localhost/api/settings", headers={"X-Session-API-Key": config["key"]}) as ready:
                    if ready.status != 200:
                        raise OSError("Agent Server is not ready")
        except (ClientError, OSError):
            await asyncio.to_thread(github_request, "canvas-failed", repository=int(project[3:]))
            return web.Response(status=503, content_type="text/html", text=
                '<html lang="ru"><meta charset="utf-8"><h1>Подключение Canvas не завершено</h1>'
                '<p><a href="/projects">К проектам — повторить подключение</a></p></html>')
        # Canvas 1.39.0 registry format and URL selection are pinned together.
        # The real Agent Server key stays on the platform; the authenticated proxy supplies it.
        value = json.dumps(project)
        script = """const id=PROJECT;
const entry={id,name:id,host:location.origin+'/agent/'+id,apiKey:'portal-session',kind:'local'};
let entries=[];
try { entries=JSON.parse(localStorage.getItem('openhands-backends')||'[]'); } catch {}
if (!Array.isArray(entries)) entries=[];
localStorage.setItem('openhands-backends',JSON.stringify([...entries.filter(x=>x.id!==id),entry]));
sessionStorage.setItem('openhands-active-backend',JSON.stringify({backendId:id,orgId:null}));
location.replace('/conversations?backend='+encodeURIComponent(id));""".replace("PROJECT", value)
        return web.Response(content_type="text/html", text=
            '<!doctype html><html lang="ru"><meta charset="utf-8"><title>Открытие проекта</title>'
            '<p>Открываем Canvas…</p><script>' + script + '</script></html>')

    def connection(project):
        if not PROJECT.fullmatch(project):
            raise web.HTTPNotFound()
        try:
            return json.loads((connections / f"{project}.json").read_text())
        except (OSError, ValueError):
            raise web.HTTPServiceUnavailable(text="Canvas ещё не подключён. Вернитесь к проектам и повторите подключение.")

    async def watch_session(request):
        while True:
            await asyncio.sleep(check_interval)
            if not await asyncio.to_thread(owner.has_session, request["session"]):
                if request.transport:
                    request.transport.close()
                return

    async def copy_messages(source, target):
        async for message in source:
            if message.type == WSMsgType.TEXT:
                await target.send_str(message.data)
            elif message.type == WSMsgType.BINARY:
                await target.send_bytes(message.data)
            else:
                break

    async def proxy(request):
        project = request.match_info["project"]
        config = connection(project)
        path = request.match_info["path"]
        if not (path.startswith("api/") or path.startswith("sockets/") or path in ("sockets", "alive", "ready", "health")):
            raise web.HTTPNotFound()
        headers = {key: value for key, value in request.headers.items() if key.lower() in SAFE_HEADERS}
        headers["X-Session-API-Key"] = config["key"]
        # Neither the portal cookie nor a caller-selected destination/key reaches the sandbox.
        query = [(key, value) for key, value in request.query.items() if key not in ("session_api_key", "api_key")]
        url = "http://localhost/" + path
        watcher = asyncio.create_task(watch_session(request))
        try:
            connector = UnixConnector(path=str(socket_dir / f"{project}.sock"))
            async with ClientSession(connector=connector, timeout=ClientTimeout(total=None, sock_connect=10),
                                     auto_decompress=False) as upstream:
                if request.headers.get("Upgrade", "").lower() == "websocket":
                    async with upstream.ws_connect(url, params=query, headers=headers, max_msg_size=16 * 1024 * 1024) as remote:
                        browser = web.WebSocketResponse(max_msg_size=16 * 1024 * 1024)
                        await browser.prepare(request)
                        tasks = [asyncio.create_task(copy_messages(browser, remote)),
                                 asyncio.create_task(copy_messages(remote, browser))]
                        try:
                            await asyncio.wait([*tasks, watcher], return_when=asyncio.FIRST_COMPLETED)
                        finally:
                            for task in tasks:
                                task.cancel()
                            await asyncio.gather(*tasks, return_exceptions=True)
                            await browser.close()
                        return browser
                async with upstream.request(request.method, url, params=query, headers=headers,
                                            data=request.content, allow_redirects=False) as remote:
                    response = web.StreamResponse(status=remote.status,
                        headers={key: value for key, value in remote.headers.items() if key.lower() in RESPONSE_HEADERS})
                    response.headers["Cache-Control"] = "no-store"
                    await response.prepare(request)
                    async for chunk in remote.content.iter_chunked(65536):
                        if not await asyncio.to_thread(owner.has_session, request["session"]):
                            break
                        await response.write(chunk)
                    await response.write_eof()
                    return response
        except (ClientError, OSError):
            raise web.HTTPServiceUnavailable(text="Agent Server недоступен. Повторите подключение из портала.")
        finally:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher

    async def static(request):
        if request.method not in ("GET", "HEAD"):
            raise web.HTTPNotFound()
        relative = request.path.lstrip("/")
        path = (assets / relative).resolve()
        if not path.is_relative_to(assets.resolve()) or "server" in Path(relative).parts:
            raise web.HTTPNotFound()
        if path.is_file():
            return web.FileResponse(path)
        if "." in Path(relative).name or relative.startswith(("agent/", "api/", "sockets", "canvas/")):
            raise web.HTTPNotFound()
        content = (assets / "index.html").read_text()
        content = content.replace("</head>", "<script>window.__AGENT_CANVAS_DO_NOT_TRACK__=true;</script></head>", 1)
        response = web.Response(text=content, content_type="text/html")
        environ = {"HTTP_" + key.upper().replace("-", "_"): value for key, value in request.headers.items()}
        if foreground_navigation(environ):
            await asyncio.to_thread(owner.dashboard, digest(request["session"]), renew=True)
            response.set_cookie("ai_session", request["session"], max_age=SESSION_LIFETIME,
                                httponly=True, samesite="Strict", secure=request.scheme == "https")
        return response

    app = web.Application(middlewares=[authenticated], client_max_size=16 * 1024 * 1024)
    app.router.add_get("/canvas/open/{project}", open_project)
    app.router.add_route("*", "/agent/{project}/{path:.*}", proxy)
    app.router.add_route("*", "/{path:.*}", static)
    return app


if __name__ == "__main__":
    owner = OwnerAccess(STATE, KEY.read_bytes(), time.time)
    app = create_app(owner, Path("/opt/ai-canvas/build"), Path("/var/lib/ai-canvas/connections"))
    web.run_app(app, sock=socket.socket(fileno=3), access_log=None, print=None)
