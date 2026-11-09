import base64
import hashlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from io import BytesIO, StringIO
from itertools import chain
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit
from wsgiref.util import setup_testing_defaults

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import portal
from portal import Portal, print_activation_url


class PortalTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.state = Path(self.work.name)
        self.key = bytes(range(32))
        self.time = 1_700_000_010
        self.portal = self.new_portal()

    def new_portal(self, *, secure=False):
        return Portal(self.state, self.key, clock=lambda: self.time, secure=secure)

    def activation(self, base="http://localhost:8080"):
        output = StringIO()
        with redirect_stdout(output):
            print_activation_url(self.portal.owner, base)
        return output.getvalue().strip()

    def request(self, path, *, streaming=False, headers=None, form=None, cookie="", ip="192.0.2.1"):
        environ = {}
        setup_testing_defaults(environ)
        environ.update(PATH_INFO=path, HTTP_COOKIE=cookie, HTTP_X_PORTAL_CLIENT_IP=ip)
        environ.update(headers or {})
        if form is not None:
            data = urlencode(form).encode()
            environ.update(
                REQUEST_METHOD="POST",
                CONTENT_LENGTH=str(len(data)),
                CONTENT_TYPE="application/x-www-form-urlencoded",
            )
            environ["wsgi.input"] = BytesIO(data)
        result = []

        def start_response(status, headers):
            result.extend((int(status.split()[0]), dict(headers)))

        body = self.portal(environ, start_response)
        if streaming:
            iterator = iter(body)
            first = next(iterator)
            self.addCleanup(iterator.close)
            return *result, chain((first,), iterator)
        data = b"".join(body)
        return *result, data

    def code(self, secret):
        # Independent RFC 4226 calculation as an authenticator would perform it.
        import hmac

        message = (self.time // 30).to_bytes(8, "big")
        digest = hmac.new(base64.b32decode(secret), message, hashlib.sha1).digest()
        offset = digest[-1] % 16
        return f"{(int.from_bytes(digest[offset:offset + 4], 'big') & 0x7fffffff) % 1_000_000:06d}"

    def enroll(self, path=None):
        path = path or urlsplit(self.activation()).path
        status, headers, body = self.request(path)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn(b"data:image/svg+xml;base64,", body)
        secret = re.search(rb"<code>([A-Z2-7]+)</code>", body).group(1)
        status, headers, _ = self.request(path, form={"code": self.code(secret)})
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertEqual("Secure" in headers["Set-Cookie"], self.portal.secure)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.activation(), "")
        self.assertEqual(self.request("/_session", cookie=cookie)[0], 204)
        status, _, body = self.request("/", cookie=cookie)
        self.assertEqual(status, 200)
        registration = re.search(rb'href="(/_owner/[^"]+)"', body).group(1).decode()
        return cookie, registration, secret

    def test_activation_expires_and_installer_retries_keep_pending_link(self):
        link = self.activation()
        self.assertTrue(link.startswith("http://localhost:8080/_owner/"))
        self.portal = self.new_portal()
        self.assertEqual(link, self.activation())
        self.time += 24 * 60 * 60
        self.assertEqual(self.request(urlsplit(link).path)[0], 404)
        self.assertNotEqual(link, self.activation())
        self.assertEqual(self.request(urlsplit(link).path)[0], 404)

    def test_secure_transport_marks_cookie_secure(self):
        self.portal = self.new_portal(secure=True)
        self.assertTrue(self.activation("https://portal.example").startswith("https://portal.example/"))
        self.enroll()

    def test_one_code_creates_one_browser_even_with_parallel_submissions(self):
        old_cookie, registration, secret = self.enroll()
        self.assertEqual(self.request(registration, form={"code": self.code(secret)})[0], 400)
        self.time += 30
        code = self.code(secret)
        ready = Barrier(2)

        def register(ip):
            ready.wait(timeout=5)
            return self.request(registration, form={"code": code}, ip=ip)

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(register, ("192.0.2.2", "192.0.2.3")))
        self.assertEqual(sorted(result[0] for result in results), [303, 400])
        new_cookie = next(headers["Set-Cookie"].split(";", 1)[0]
                          for status, headers, _ in results if status == 303)
        self.assertNotEqual(new_cookie, old_cookie)
        self.portal = self.new_portal()
        for cookie in (new_cookie, old_cookie):
            self.assertEqual(self.request("/_session", cookie=cookie)[0], 204)

    def test_global_hour_limit_survives_restart_and_keeps_existing_session(self):
        cookie, registration, secret = self.enroll()
        self.time += 30
        for last_octet in range(1, 4):
            result = self.request(registration, form={"code": "invalid"}, ip=f"192.0.2.{last_octet}")
            self.assertEqual(result[0], 400)
            self.time += 30
        self.portal = self.new_portal()
        self.assertEqual(self.request("/_session", cookie=cookie)[0], 204)
        self.time += 3509
        result = self.request(registration, form={"code": self.code(secret)}, ip="192.0.2.4")
        self.assertEqual(result[0], 429)
        self.time += 1
        result = self.request(registration, form={"code": self.code(secret)}, ip="192.0.2.4")
        self.assertEqual(result[0], 303)

    def admin(self, command):
        output = StringIO()
        key = self.state / "portal.key"
        key.write_bytes(self.key)
        with (
            redirect_stdout(output),
            patch.object(portal, "STATE", self.state),
            patch.object(portal, "KEY", key),
            patch.object(portal.time, "time", lambda: self.time),
            patch.object(portal.os, "umask"),
        ):
            portal.main([command, "http://localhost:8080"])
        return urlsplit(output.getvalue().strip()).path

    def test_recovery_revokes_access_and_replaces_totp_and_registration(self):
        cookie, registration, secret = self.enroll()
        self.time += 30
        for _ in range(3):
            self.assertEqual(self.request(registration, form={"code": "invalid"})[0], 400)

        superseded = self.admin("recover")
        recovery = self.admin("recover")
        self.portal = self.new_portal()
        self.assertEqual(self.request("/_session", cookie=cookie)[0], 403)
        self.assertEqual(self.request(registration)[0], 404)
        self.assertEqual(self.request(superseded)[0], 404)
        replacement_cookie, replacement_registration, replacement_secret = self.enroll(recovery)
        self.assertNotEqual(replacement_secret, secret)
        self.assertNotEqual(replacement_registration, registration)
        self.portal = self.new_portal()
        self.assertEqual(self.request("/_session", cookie=replacement_cookie)[0], 204)
        self.assertEqual(self.request("/_session", cookie=cookie)[0], 403)
        self.assertEqual(self.request(recovery)[0], 404)
        self.assertEqual(self.request(registration)[0], 404)

    def test_rotation_only_replaces_registration_url(self):
        cookie, registration, secret = self.enroll()
        replacement = self.admin("rotate-registration")
        self.assertNotEqual(replacement, registration)
        self.portal = self.new_portal()
        self.assertEqual(self.request(registration)[0], 404)
        self.assertEqual(self.request("/_session", cookie=cookie)[0], 204)
        self.assertIn(replacement.encode(), self.request("/", cookie=cookie)[2])
        self.time += 30
        self.assertEqual(self.request(replacement, form={"code": self.code(secret)})[0], 303)

    def browser_forms(self, body):
        csrf = re.search(rb'name="csrf" value="([0-9a-f]+)"', body).group(1).decode()
        paths = re.findall(rb'action="(/browsers/[0-9a-f]+/rename)"', body)
        return csrf, [path.decode() for path in paths]

    def test_browser_rename_and_revoke_are_protected_and_persist(self):
        owner, registration, secret = self.enroll()
        self.time += 30
        status, headers, _ = self.request(registration, form={"code": self.code(secret)})
        self.assertEqual(status, 303)
        other = headers["Set-Cookie"].split(";", 1)[0]
        csrf, (_, rename) = self.browser_forms(self.request("/", cookie=owner)[2])
        self.assertEqual(self.request(rename, cookie=owner, form={"name": "Rejected"})[0], 403)

        name = 'Firefox <desktop>'
        status, headers, _ = self.request(rename, cookie=owner, form={"csrf": csrf, "name": name})
        self.assertEqual(status, 303)
        self.assertIn("Max-Age=2592000", headers["Set-Cookie"])
        self.portal = self.new_portal()
        page = self.request("/", cookie=owner)[2]
        self.assertIn(b"Firefox &lt;desktop&gt;", page)
        self.assertIn(b"2023-11-14", page)
        self.assertIn(b"2023-12-14", page)

        revoke = rename.removesuffix("rename") + "revoke"
        self.assertEqual(self.request(revoke, cookie=owner, form={"csrf": csrf})[0], 303)
        self.portal = self.new_portal()
        self.assertEqual(self.request("/", cookie=other)[0], 404)
        self.assertEqual(self.request("/", cookie=owner)[0], 200)
        self.assertNotIn(b"Firefox", self.request("/", cookie=owner)[2])

    def test_only_foreground_navigation_renews_session_and_cookie(self):
        cookie, _, _ = self.enroll()
        self.time += 29 * 86400
        navigation = {"HTTP_SEC_FETCH_MODE": "navigate", "HTTP_SEC_FETCH_DEST": "document"}
        status, headers, _ = self.request("/", cookie=cookie, headers=navigation)
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=2592000", headers["Set-Cookie"])

        self.time += 29 * 86400
        for path, fetch_headers in (
            ("/", {}),
            ("/", {**navigation, "HTTP_SEC_PURPOSE": "prefetch"}),
            ("/api/status", {}),
            ("/_session", {}),
        ):
            self.assertNotIn("Set-Cookie", self.request(path, cookie=cookie, headers=fetch_headers)[1])
        self.time += 86400
        self.portal = self.new_portal()
        self.assertEqual(self.request("/", cookie=cookie, headers=navigation)[0], 404)

    def test_upgrade_keeps_existing_browser_and_recovers_registration_date(self):
        cookie, _, _ = self.enroll()
        # Recreate the previous release's sessions table and its fixed expiry.
        with sqlite3.connect(self.state / "portal.sqlite3") as db:
            db.executescript("""
                DROP TABLE sessions;
                CREATE TABLE sessions (token_hash BLOB PRIMARY KEY, expires_at INTEGER NOT NULL);
            """)
            token_hash = hashlib.sha256(cookie.split("=", 1)[1].encode()).digest()
            db.execute("INSERT INTO sessions VALUES (?, ?)", (token_hash, self.time + 30 * 86400))
        db.close()

        self.activation()  # The installer initializes the schema before starting the new service.
        self.activation()
        self.portal = self.new_portal()
        status, _, page = self.request("/", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(page.count(b"2023-11-14 22:13:30 UTC"), 2)
        self.assertIn(b"2023-12-14 22:13:30 UTC", page)

    def test_open_stream_closes_after_revocation(self):
        cookie, _, _ = self.enroll()
        status, _, stream = self.request("/api/test-stream", cookie=cookie, streaming=True)
        self.assertEqual(status, 200)
        self.assertEqual(next(iter(stream)), b"data: ready\n\n")

        csrf, (rename,) = self.browser_forms(self.request("/", cookie=cookie)[2])
        revoke = rename.removesuffix("rename") + "revoke"
        status, headers, _ = self.request(revoke, cookie=cookie, form={"csrf": csrf})
        self.assertEqual(status, 303)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(list(stream), [])
        self.assertEqual(self.request("/api/test-stream", cookie=cookie)[0], 404)

    def test_open_stream_closes_at_expiry_without_renewal(self):
        cookie, _, _ = self.enroll()
        self.time += 29 * 86400
        status, headers, stream = self.request("/api/test-stream", cookie=cookie, streaming=True)
        self.assertEqual(status, 200)
        self.assertNotIn("Set-Cookie", headers)
        self.assertEqual(next(iter(stream)), b"data: ready\n\n")

        self.time += 86400
        self.assertEqual(list(stream), [])
        self.assertEqual(self.request("/api/test-stream", cookie=cookie)[0], 404)
