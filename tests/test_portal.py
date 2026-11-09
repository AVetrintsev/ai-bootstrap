import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
import re
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

    def request(self, path, *, form=None, cookie="", ip="192.0.2.1"):
        environ = {}
        setup_testing_defaults(environ)
        environ.update(PATH_INFO=path, HTTP_COOKIE=cookie, HTTP_X_PORTAL_CLIENT_IP=ip)
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

        body = b"".join(self.portal(environ, start_response))
        return *result, body

    def code(self, secret):
        # Independent RFC 4226 calculation as an authenticator would perform it.
        import hashlib
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
