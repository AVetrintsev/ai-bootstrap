import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "lib/public-portal.py"
SPEC = importlib.util.spec_from_file_location("public_portal", SOURCE)
public_portal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(public_portal)
DOMAIN = "portal.example.org"
IPV4 = "8.8.4.4"
PATH = "/_ai-bootstrap-probe/one-time-path"
BODY = "fresh control string"


class PublicPortalTests(unittest.TestCase):
    @patch.object(public_portal, "fetch_json")
    def test_public_dns_requires_exact_a_and_no_aaaa(self, fetch):
        correct_a = {"Status": 0, "Answer": [{"type": 1, "data": IPV4}]}
        fetch.side_effect = [correct_a, {"Status": 0}]
        public_portal.verify_dns(DOMAIN, IPV4)
        fetch.side_effect = [{"Status": 0, "Answer": [{"type": 1, "data": "8.8.8.8"}]}]
        with self.assertRaisesRegex(SystemExit, "public DNS A .* expected only"):
            public_portal.verify_dns(DOMAIN, IPV4)
        fetch.side_effect = [correct_a, {"Status": 0, "Answer": [{"type": 28, "data": "2001:db8::1"}]}]
        with self.assertRaisesRegex(SystemExit, "unexpected public DNS AAAA"):
            public_portal.verify_dns(DOMAIN, IPV4)

    @patch.object(public_portal, "fetch_json")
    def test_external_probe_requests_https_443_and_requires_exact_response(self, fetch):
        complete = {"status": "finished", "resolvedAddress": IPV4, "tls": {"authorized": True},
                    "statusCode": 200, "truncated": False, "rawBody": BODY}
        fetch.side_effect = [{"id": "probe-id"}, {"status": "finished", "results": [{"result": complete}]}]
        public_portal.verify_web(DOMAIN, IPV4, PATH, BODY, "HTTPS", 443)
        request = json.loads(fetch.call_args_list[0].args[-1])
        self.assertEqual(request["target"], DOMAIN)
        self.assertEqual(request["measurementOptions"], {
            "protocol": "HTTPS", "port": 443, "ipVersion": 4, "request": {"method": "GET", "path": PATH},
        })
        for overrides, message in (
            ({"resolvedAddress": "8.8.8.8"}, "public DNS"),
            ({"status": "failed", "resolvedAddress": "8.8.8.8"}, "public DNS"),
            ({"tls": {"authorized": False}}, "public TLS"),
            ({"statusCode": 302}, "public inbound HTTPS"),
            ({"rawBody": BODY + "\n"}, "public inbound HTTPS"),
            ({"truncated": True}, "public inbound HTTPS"),
            ({"status": "failed", "failureSource": "target", "rawOutput": "connect ETIMEDOUT"}, "inbound TCP 443"),
        ):
            with self.subTest(overrides=overrides):
                fetch.side_effect = [
                    {"id": "probe-id"},
                    {"status": "finished", "results": [{"result": {**complete, **overrides}}]},
                ]
                with self.assertRaisesRegex(SystemExit, message):
                    public_portal.verify_web(DOMAIN, IPV4, PATH, BODY, "HTTPS", 443)

    @patch.object(public_portal, "fetch_json")
    def test_external_probe_also_checks_http_80_without_tls(self, fetch):
        complete = {"status": "finished", "resolvedAddress": IPV4, "statusCode": 200, "rawBody": BODY}
        fetch.side_effect = [{"id": "probe-id"}, {"status": "finished", "results": [{"result": complete}]}]
        public_portal.verify_web(DOMAIN, IPV4, PATH, BODY, "HTTP", 80)
        request = json.loads(fetch.call_args_list[0].args[-1])
        self.assertEqual(request["measurementOptions"]["protocol"], "HTTP")
        self.assertEqual(request["measurementOptions"]["port"], 80)

    @patch.object(public_portal, "fetch_json", side_effect=subprocess.CalledProcessError(28, "curl"))
    def test_globalping_api_failure_is_reported_separately(self, fetch):
        with self.assertRaisesRegex(SystemExit, "Globalping unavailable"):
            public_portal.verify_web(DOMAIN, IPV4, PATH, BODY, "HTTPS", 443)
