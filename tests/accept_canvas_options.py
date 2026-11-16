"""Verify real Codex and Qwen controls via the deployed portal on the target VM."""
import hashlib
import json
import os
import secrets
import sqlite3
import sys
import time
from urllib.parse import urlsplit
import uuid

from playwright.sync_api import sync_playwright


if os.geteuid() != 0 or len(sys.argv) != 4:
    sys.exit("Usage: sudo PYTHON tests/accept_canvas_options.py PROJECT CHROMIUM ORIGIN")
project, chromium, origin = sys.argv[1:]
origin = origin.rstrip("/")
address = urlsplit(origin)
token = secrets.token_urlsafe(32)
token_hash = hashlib.sha256(token.encode()).digest()
database = "/var/lib/ai-portal/portal.sqlite3"
now = int(time.time())
with sqlite3.connect(database) as db:
    db.execute(
        "INSERT INTO sessions(token_hash,expires_at,name,registered_at,last_active_at) VALUES(?,?,?,?,?)",
        (token_hash, now + 900, "Canvas options acceptance", now, now),
    )


def api(page, path, method="GET", data=None):
    result = page.evaluate(
        """async ({path,method,data}) => {
            const response = await fetch(path, {
                method,
                headers: data ? {"Content-Type":"application/json"} : {},
                body: data ? JSON.stringify(data) : undefined,
            });
            return {ok: response.ok, status: response.status, body: await response.text()};
        }""",
        {"path": f"/agent/{project}/api/{path}", "method": method, "data": data},
    )
    assert result["ok"], (method, result["status"])
    return result["body"]


def check(page, profile_id, name):
    identifier = str(uuid.uuid4())
    endpoint = f"conversations/{identifier}"
    api(page, "conversations", "POST", {
        "conversation_id": identifier,
        "agent_profile_id": profile_id,
        "workspace": {"kind": "LocalWorkspace", "working_dir": "/workspace/repo"},
        "worktree": False,
        "autotitle": False,
    })
    try:
        page.goto(f"{origin}/conversations/{identifier}?backend={project}")
        editor = page.get_by_test_id("chat-input")
        editor.fill("Reply exactly READY. Do not use tools.")
        editor.press("Enter")
        page.get_by_text("READY", exact=True).last.wait_for(timeout=150000)
        picker = page.get_by_test_id("ai-bootstrap-reasoning")
        picker.wait_for(timeout=30000)
        model = page.get_by_test_id("chat-input-llm-model")
        model.wait_for(timeout=30000)
        info = json.loads(api(page, endpoint))
        assert info["available_models"], name
        effort = next(option["value"] for option in info["available_efforts"]
                      if option["value"] in ("low", "medium", "high") and option["value"] != info["current_effort"])
        picker.select_option(effort)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if json.loads(api(page, endpoint))["current_effort"] == effort:
                break
            page.wait_for_timeout(350)
        else:
            raise AssertionError(f"{name}: thinking level was not applied")
        page.reload()
        picker.wait_for(timeout=30000)
        assert picker.input_value() == effort, name
        print(f"{name}: native model picker and reasoning change after reload verified", flush=True)
    finally:
        api(page, endpoint + "/pause", "POST")
        api(page, endpoint, "DELETE")


try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium, headless=True, args=[
            "--no-sandbox", f"--host-resolver-rules=MAP {address.hostname} 127.0.0.1",
        ])
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        context.add_cookies([{"name": "ai_session", "value": token, "url": origin,
                              "secure": address.scheme == "https", "httpOnly": True, "sameSite": "Strict"}])
        page = context.new_page()
        page.set_default_timeout(30000)
        page.goto(f"{origin}/canvas/open/{project}")
        page.get_by_test_id("chat-input").wait_for()
        profiles = json.loads(api(page, "agent-profiles"))["profiles"]
        for name in ("codex-subscription", "qwen-code"):
            check(page, next(profile["id"] for profile in profiles if profile["name"] == name), name)
finally:
    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
