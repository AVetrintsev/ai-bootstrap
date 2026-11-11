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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import model_gateway


class Provider(BaseHTTPRequestHandler):
    requests = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append((self.path, self.headers.get("Authorization"), body))
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


if __name__ == "__main__":
    unittest.main()
