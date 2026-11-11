import http.client
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import model_gateway


class Provider(BaseHTTPRequestHandler):
    requests = []
    fail = False

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append((self.path, self.headers.get("Authorization"), body))
        if self.fail:
            response = b'test-provider-secret: upstream failure details'
            self.send_response(401)
            self.end_headers()
            self.wfile.write(response)
            return
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"run_shell_command","arguments":"{}"}}]}}]}\n\ndata: [DONE]\n\n')
            return
        response = json.dumps({"choices": [{"message": {"role": "assistant", "content": "ready"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, *_):
        pass


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        certificate, private_key = self.root / "cert.pem", self.root / "tls.key"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
            "-keyout", str(private_key), "-out", str(certificate),
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        previous = os.environ.get("SSL_CERT_FILE")
        os.environ["SSL_CERT_FILE"] = str(certificate)
        self.addCleanup(lambda: os.environ.pop("SSL_CERT_FILE", None) if previous is None
                        else os.environ.__setitem__("SSL_CERT_FILE", previous))
        Provider.requests = []
        Provider.fail = False
        self.provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, private_key)
        self.provider.socket = context.wrap_socket(self.provider.socket, server_side=True)
        self.start(self.provider)
        (self.root / "provider.key").write_text("test-provider-secret")
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({
            "providers": {"test": {
                "base_url": f"https://localhost:{self.provider.server_port}/compatible-mode/v1",
                "key_file": str(self.root / "provider.key"),
            }},
            "models": {"coder": {"provider": "test", "model": "upstream-coder"}},
            "defaults": {"models": ["coder"], "requests_per_day": 2, "max_output_tokens": 256},
            "projects": {},
        }))
        self.state = self.root / "state"
        self.client = model_gateway.provision(self.config, self.state, "alpha")
        self.gateway = model_gateway.make_server(self.config, self.state, ("127.0.0.1", 0))
        self.start(self.gateway)

    def start(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

    def request(self, body=None, path="/v1/chat/completions", token=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=5)
        headers = {"Authorization": "Bearer " + (token or self.client["api_key"])}
        connection.request("POST" if body is not None else "GET", path,
                           json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        self.content_type = response.getheader("Content-Type")
        result = response.status, response.read()
        connection.close()
        return result

    def test_allowed_model_preserves_tool_calls_and_keeps_provider_key_on_host(self):
        tool = {"type": "function", "function": {"name": "run_shell_command", "parameters": {"type": "object"}}}
        status, body = self.request({"model": "coder", "messages": [{"role": "user", "content": "hello"}],
                                     "tools": [tool], "max_tokens": 1024})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["choices"][0]["message"]["content"], "ready")
        path, authorization, upstream = Provider.requests[0]
        self.assertEqual(path, "/compatible-mode/v1/chat/completions")
        self.assertEqual(authorization, "Bearer test-provider-secret")
        self.assertEqual(upstream["model"], "upstream-coder")
        self.assertEqual(upstream["tools"], [tool])
        self.assertEqual(upstream["max_tokens"], 256)
        self.assertNotIn("test-provider-secret", json.dumps(self.client))

    def test_project_quota_survives_restart_and_does_not_charge_denied_models(self):
        request = {"model": "coder", "messages": [{"role": "user", "content": "hello"}]}
        status, _ = self.request(dict(request, model="forbidden"))
        self.assertEqual(status, 403)
        self.assertEqual(self.request(request)[0], 200)
        self.assertEqual(self.request(request)[0], 200)
        self.gateway.shutdown()
        self.gateway = model_gateway.make_server(self.config, self.state, ("127.0.0.1", 0))
        self.start(self.gateway)
        status, body = self.request(request)
        self.assertEqual(status, 429, body)
        self.assertEqual(json.loads(body)["error"]["code"], "project_quota_exceeded")
        beta = model_gateway.provision(self.config, self.state, "beta")
        self.assertEqual(self.request(request, token=beta["api_key"])[0], 200)
        self.assertEqual(len(Provider.requests), 3)

    def test_streaming_tool_calls_and_output_limits_cannot_be_overridden(self):
        status, body = self.request({"model": "coder", "messages": [], "stream": True,
                                     "max_tokens": 128, "max_completion_tokens": 999999, "n": 5})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.content_type, "text/event-stream")
        self.assertIn(b'"tool_calls"', body)
        self.assertIn(b'data: [DONE]', body)
        upstream = Provider.requests[0][2]
        self.assertEqual(upstream["n"], 1)
        self.assertEqual(upstream["max_tokens"], 128)
        self.assertNotIn("max_completion_tokens", upstream)

    def test_project_can_only_discover_and_call_its_allowed_models(self):
        settings = json.loads(self.config.read_text())
        settings["models"]["other"] = {"provider": "test", "model": "other-upstream"}
        settings["projects"]["beta"] = {"models": ["other"], "requests_per_day": 1, "max_output_tokens": 64}
        self.config.write_text(json.dumps(settings))
        beta = model_gateway.provision(self.config, self.state, "beta")
        status, body = self.request(path="/v1/models", token=beta["api_key"])
        self.assertEqual(status, 200)
        self.assertEqual([m["id"] for m in json.loads(body)["data"]], ["other"])
        self.assertNotIn("provider", body.decode())
        self.assertEqual(self.request({"model": "coder", "messages": [], "project": "alpha"}, token=beta["api_key"])[0], 403)
        self.assertEqual(self.request(path="/v1/models", token="invalid")[0], 401)
        self.assertEqual(self.request(path="/config")[0], 404)
        self.assertEqual(self.request({"model": "other", "messages": []})[0], 403)

    def test_provider_errors_do_not_reveal_credentials_and_failed_attempts_use_quota(self):
        Provider.fail = True
        request = {"model": "coder", "messages": []}
        for _ in range(2):
            status, body = self.request(request)
            self.assertEqual(status, 502)
            self.assertNotIn(b"test-provider-secret", body)
        self.assertEqual(self.request(request)[0], 429)

    def test_rejects_insecure_provider_configuration_before_serving(self):
        settings = json.loads(self.config.read_text())
        settings["providers"]["test"]["base_url"] = "http://example.com/v1"
        self.config.write_text(json.dumps(settings))
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            model_gateway.make_server(self.config, self.state, ("127.0.0.1", 0))

    def test_concurrent_requests_cannot_overrun_the_project_quota(self):
        request = {"model": "coder", "messages": []}
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.request(request)[0], range(6)))
        self.assertEqual(sorted(results), [200, 200, 429, 429, 429, 429])
        self.assertEqual(len(Provider.requests), 2)

    def test_key_rotation_takes_effect_without_changing_the_sandbox_key(self):
        request = {"model": "coder", "messages": []}
        self.assertEqual(self.request(request)[0], 200)
        (self.root / "provider.key").write_text("rotated-provider-secret")
        self.assertEqual(self.request(request)[0], 200)
        self.assertEqual(Provider.requests[-1][1], "Bearer rotated-provider-secret")
        self.assertEqual(model_gateway.provision(self.config, self.state, "alpha")["api_key"], self.client["api_key"])

    def test_revoke_invalidates_an_old_project_key(self):
        result = subprocess.run([
            sys.executable, str(ROOT / "lib/model_gateway.py"), "--config", str(self.config),
            "--state", str(self.state), "revoke", "alpha",
        ], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.request(path="/v1/models")[0], 401)
        replacement = model_gateway.provision(self.config, self.state, "alpha")
        self.assertNotEqual(replacement["api_key"], self.client["api_key"])


if __name__ == "__main__":
    unittest.main()
