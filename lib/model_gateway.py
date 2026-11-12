"""Project-scoped OpenAI-compatible model access; provider credentials stay on the host."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import ssl
import sys
from urllib.parse import urlsplit


CONFIG = Path("/etc/ai-bootstrap/model-gateway.json")
STATE = Path("/var/lib/ai-models")
PORT = 18081
MAX_BODY = 2 * 1024 * 1024
PROJECT = re.compile(r"[a-z][a-z0-9-]{0,23}")


def configuration(path):
    return validate_settings(json.loads(Path(path).read_text(encoding="utf-8")))


def validate_settings(settings):
    if not isinstance(settings, dict):
        raise ValueError("model_gateway must be a mapping")
    providers, models = settings.get("providers"), settings.get("models")
    if not isinstance(providers, dict) or not providers or not isinstance(models, dict) or not models:
        raise ValueError("model_gateway requires providers and models")
    for name, provider in providers.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name) or not isinstance(provider, dict):
            raise ValueError("invalid model_gateway provider")
        try:
            endpoint = urlsplit(provider.get("base_url", ""))
            if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
                raise ValueError
            if endpoint.port is not None and not 1 <= endpoint.port <= 65535:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError("model_gateway providers require an HTTPS base_url without credentials, query or fragment") from None
        key_file = provider.get("key_file")
        if not isinstance(key_file, str) or not key_file.startswith("/") or "\n" in key_file or "\x00" in key_file:
            raise ValueError("model_gateway provider key_file must be an absolute host path")
    for alias, model in models.items():
        if not isinstance(alias, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9/_.:-]{0,127}", alias) or not isinstance(model, dict):
            raise ValueError("invalid model_gateway model alias")
        if not isinstance(model.get("provider"), str) or model["provider"] not in providers or not isinstance(model.get("model"), str) or not model["model"].strip():
            raise ValueError("model_gateway model requires a known provider and upstream model")
    projects = settings.get("projects", {})
    if not isinstance(projects, dict) or any(not isinstance(name, str) or not PROJECT.fullmatch(name) for name in projects):
        raise ValueError("invalid model_gateway projects")
    for policy in [settings.get("defaults"), *projects.values()]:
        if not isinstance(policy, dict):
            raise ValueError("model_gateway requires default and project policies")
        allowed = policy.get("models")
        if not isinstance(allowed, list) or not allowed or any(not isinstance(model, str) or model not in models for model in allowed):
            raise ValueError("model_gateway policy must allow known models")
        for field, minimum, maximum in (("requests_per_day", 0, 1000000), ("max_output_tokens", 1, 65536)):
            if type(policy.get(field)) is not int or not minimum <= policy[field] <= maximum:
                raise ValueError(f"model_gateway policy {field} is outside the permitted range")
    return settings


@contextmanager
def database(state):
    state = Path(state)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(state / "gateway.sqlite3", timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE IF NOT EXISTS projects (name TEXT PRIMARY KEY, token TEXT NOT NULL, digest TEXT UNIQUE NOT NULL)")
    connection.execute("CREATE TABLE IF NOT EXISTS usage (project TEXT NOT NULL, day TEXT NOT NULL, requests INTEGER NOT NULL, PRIMARY KEY (project, day))")
    connection.commit()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def reserve_request(state, project, limit):
    day = datetime.now(timezone.utc).date().isoformat()
    with database(state) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT OR IGNORE INTO usage VALUES (?, ?, 0)", (project, day))
        return connection.execute(
            "UPDATE usage SET requests = requests + 1 WHERE project = ? AND day = ? AND requests < ?",
            (project, day, limit),
        ).rowcount == 1


def provision(config, state, project):
    if not PROJECT.fullmatch(project):
        raise ValueError("invalid project name")
    settings = configuration(config)
    policy = settings.get("projects", {}).get(project, settings["defaults"])
    with database(state) as connection:
        token = secrets.token_urlsafe(32)
        connection.execute("INSERT OR IGNORE INTO projects VALUES (?, ?, ?)",
                           (project, token, hashlib.sha256(token.encode()).hexdigest()))
        row = connection.execute("SELECT token FROM projects WHERE name = ?", (project,)).fetchone()
    return {"api_key": row["token"], "model": policy["models"][0],
            "models": policy["models"], "max_output_tokens": policy["max_output_tokens"]}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "AIModelGateway"
    sys_version = ""

    def setup(self):
        self.request.settimeout(30)
        super().setup()

    def log_message(self, *_):
        # Requests may contain workspace text or credentials. Do not log them.
        pass

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def error(self, status, code, message):
        self.reply(status, {"error": {"type": code, "code": code, "message": message}})

    def authorize(self):
        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("Bearer ") or len(authorization) > 256:
            self.error(401, "invalid_api_key", "A project gateway key is required.")
            return None
        digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
        with database(self.server.state) as connection:
            row = connection.execute("SELECT name FROM projects WHERE digest = ?", (digest,)).fetchone()
        if row is None:
            self.error(401, "invalid_api_key", "Unknown project gateway key.")
            return None
        settings = configuration(self.server.config)
        policy = settings.get("projects", {}).get(row["name"], settings["defaults"])
        return row["name"], settings, policy

    def do_GET(self):
        authorized = self.authorize()
        if authorized is None:
            return
        if self.path != "/v1/models":
            self.error(404, "not_found", "Unknown gateway endpoint.")
            return
        _, _, policy = authorized
        self.reply(200, {"object": "list", "data": [
            {"id": model, "object": "model", "owned_by": "project"} for model in policy["models"]
        ]})

    def do_POST(self):
        authorized = self.authorize()
        if authorized is None:
            return
        if self.path != "/v1/chat/completions":
            self.error(404, "not_found", "Unknown gateway endpoint.")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if self.headers.get("Transfer-Encoding") or not 0 < length <= MAX_BODY:
                raise ValueError
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, UnicodeError):
            self.error(400, "invalid_request", "Expected a JSON request of at most 2 MiB.")
            return
        project, settings, policy = authorized
        alias = body.get("model")
        if not isinstance(alias, str) or alias not in policy["models"]:
            self.error(403, "model_not_allowed", "This model is not enabled for the project.")
            return
        model = settings["models"][alias]
        provider = settings["providers"][model["provider"]]
        tokens = body.get("max_tokens", body.get("max_completion_tokens", policy["max_output_tokens"]))
        if type(tokens) is not int or tokens < 1 or not isinstance(body.get("messages"), list) or type(body.get("stream", False)) is not bool:
            self.error(400, "invalid_request", "Invalid messages, stream or output token limit.")
            return
        if not reserve_request(self.server.state, project, policy["requests_per_day"]):
            self.error(429, "project_quota_exceeded", "The project's daily request limit is exhausted; it resets at 00:00 UTC.")
            return
        outgoing = {key: value for key, value in body.items() if key in {
            "messages", "tools", "tool_choice", "temperature", "top_p", "stop", "seed",
            "frequency_penalty", "presence_penalty", "parallel_tool_calls", "response_format",
            "stream", "stream_options", "enable_thinking", "reasoning_effort",
        }}
        outgoing["model"] = model["model"]
        outgoing["max_tokens"] = min(tokens, policy["max_output_tokens"])
        outgoing["n"] = 1
        endpoint = urlsplit(provider["base_url"])
        upstream = http.client.HTTPSConnection(endpoint.hostname, endpoint.port or 443,
                                               timeout=120, context=ssl.create_default_context())
        started = False
        try:
            key = Path(provider["key_file"]).read_text(encoding="utf-8").strip()
            upstream.request("POST", endpoint.path.rstrip("/") + "/chat/completions",
                             json.dumps(outgoing).encode(),
                             {"Authorization": "Bearer " + key, "Content-Type": "application/json"})
            response = upstream.getresponse()
            if response.status != 200:
                status = 403 if response.status in (401, 403) else response.status if 400 <= response.status < 500 else 502
                self.error(status, "provider_error", f"The model provider rejected the request (HTTP {response.status}); check provider access and configuration.")
                return
            if outgoing.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                started = True
                total = 0
                while chunk := response.read1(65536):
                    total += len(chunk)
                    if total > 32 * 1024 * 1024:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
                return
            data = response.read(32 * 1024 * 1024 + 1)
            if len(data) > 32 * 1024 * 1024:
                self.error(502, "provider_error", "The provider response exceeded the size limit.")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            started = True
            self.wfile.write(data)
        except (OSError, ValueError, http.client.HTTPException):
            if not started:
                self.error(502, "provider_unavailable", "The model provider is unavailable.")
        finally:
            upstream.close()


def make_server(config, state, address):
    configuration(config)
    server = ThreadingHTTPServer(address, Handler)
    server.config = Path(config)
    server.state = Path(state)
    return server


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--state", type=Path, default=STATE)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve")
    project = commands.add_parser("provision")
    project.add_argument("project")
    revoke = commands.add_parser("revoke")
    revoke.add_argument("project")
    args = parser.parse_args()
    if args.command == "provision":
        print(json.dumps(provision(args.config, args.state, args.project)))
    elif args.command == "revoke":
        with database(args.state) as connection:
            connection.execute("DELETE FROM projects WHERE name = ?", (args.project,))
    else:
        make_server(args.config, args.state, ("0.0.0.0", PORT)).serve_forever()


if __name__ == "__main__":
    main()
