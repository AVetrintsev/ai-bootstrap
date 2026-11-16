"""Pinned Agent Server launch and idempotent Qwen ACP configuration, inside a sandbox."""
import json
import os
from pathlib import Path
import signal
import sys
import time
import urllib.error
import urllib.request

config = Path("/root/.config/ai-bootstrap/canvas.json")
pidfile = Path("/run/ai-agent-server.pid")
mode = sys.argv[1]
if mode == "stop":
    if pidfile.exists():
        pid = int(pidfile.read_text())
        process = Path(f"/proc/{pid}/cmdline")
        if process.exists() and b"openhands.agent_server" in process.read_bytes():
            os.kill(pid, signal.SIGTERM)
    sys.exit(0)

settings = json.loads(config.read_text())
if mode == "run":
    root = Path("/root/.local/share/ai-canvas")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chdir("/workspace/repo")
    environment = dict(os.environ, OH_SESSION_API_KEYS_0=settings["key"], OH_SECRET_KEY=settings["key"],
        OH_PERSISTENCE_DIR=str(root), OH_CONVERSATIONS_PATH=str(root / "conversations"),
        OH_BASH_EVENTS_DIR=str(root / "bash_events"), OH_WORKSPACE_PATH="/workspace/repo",
        OH_CONVERSATION_WORKTREE_ROOT="/workspace/worktrees", OH_ENABLE_VSCODE="false", DO_NOT_TRACK="1")
    pidfile.write_text(str(os.getpid()))
    python = "/opt/ai-agent/bin/python"
    os.execve(python, [python, "-m", "openhands.agent_server", "--host", "127.0.0.1", "--port", "18000"], environment)

if mode != "configure":
    sys.exit("Expected run, stop or configure")

def request(path, method="GET", body=None):
    request = urllib.request.Request("http://127.0.0.1:18000" + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Session-API-Key": settings["key"], "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)

deadline = time.monotonic() + 90
while time.monotonic() < deadline:
    try:
        current = request("/api/settings")
        break
    except (OSError, ValueError):
        time.sleep(1)
else:
    sys.exit("Agent Server did not become ready")

# Profiles are managed through the public Agent Server API. Conversations and
# their worktrees are created by the native launcher, never by provisioning.
profiles = request("/api/agent-profiles")["profiles"]
names = {profile["name"] for profile in profiles}
gateway = config.with_name("model-gateway.json")
if gateway.exists() and "qwen-code" not in names:
    model = json.loads(gateway.read_text())["model"]
    request("/api/agent-profiles/qwen-code", "POST", {
        "agent_kind": "acp", "acp_server": "custom", "acp_command": "/usr/local/bin/qwen",
        "acp_args": ["--acp"], "acp_model": model,
    })
if "codex-subscription" not in names:
    request("/api/agent-profiles/codex-subscription", "POST", {
        "agent_kind": "acp", "acp_server": "custom", "acp_command": "/usr/local/bin/codex",
        "acp_args": ["--acp"],
    })
marker = config.with_name("native-canvas-configured")
if not marker.exists():
    profile = next(item for item in request("/api/agent-profiles")["profiles"] if item["name"] == "codex-subscription")
    request("/api/agent-profiles/" + profile["id"] + "/activate", "POST")
    request("/api/settings", "PATCH", {
        "misc_settings_diff": {"app_preferences": {"language": "ru", "user_consents_to_analytics": False}},
    })
    marker.write_text("1\n")
print("OK Agent Server and native ACP profiles")
