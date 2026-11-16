"""Verify real Codex and Qwen controls via the deployed portal on the target VM."""
import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import time
from urllib.parse import urlsplit
import uuid

from playwright.sync_api import TimeoutError as BrowserTimeout, sync_playwright


profiles_by_agent = {"codex": "codex-subscription", "qwen": "qwen-code"}
if (
    os.geteuid() != 0
    or len(sys.argv) not in (4, 5)
    or (len(sys.argv) == 5 and sys.argv[4] not in profiles_by_agent)
):
    sys.exit("Usage: sudo PYTHON tests/accept_canvas_options.py PROJECT CHROMIUM ORIGIN [codex|qwen]")
project, chromium, origin = sys.argv[1:4]
profile_names = [profiles_by_agent[sys.argv[4]]] if len(sys.argv) == 5 else profiles_by_agent.values()
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


def wait_for_value(page, endpoint, field, value):
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if json.loads(api(page, endpoint))[field] == value:
            return
        page.wait_for_timeout(350)
    raise AssertionError(f"Conversation did not apply {field}")


def prompt(page, endpoint, editor, word):
    editor.fill(f"Reply exactly {word}. Do not use tools.")
    editor.press("Enter")
    try:
        reply = re.compile(rf"^{re.escape(word)}[.!]?$", re.IGNORECASE)
        page.get_by_text(reply).last.wait_for(timeout=150000)
    except BrowserTimeout:
        events = json.loads(api(page, f"{endpoint}/events/search?limit=20"))["items"]
        event_types = [(event.get("kind"), event.get("source")) for event in events]
        raise AssertionError(f"{word}: no expected reply. Events: {event_types}") from None


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
        picker = page.get_by_test_id("ai-bootstrap-reasoning")
        picker.wait_for(timeout=60000)
        model = page.get_by_test_id("chat-input-llm-model")
        model.wait_for(timeout=30000)
        page.set_viewport_size({"width": 390, "height": 850})
        picker.wait_for()
        assert picker.evaluate(
            "el => { const bounds = el.getBoundingClientRect(); "
            "return bounds.width > 0 && bounds.left >= 0 && bounds.right <= innerWidth; }"
        ), name
        page.set_viewport_size({"width": 1440, "height": 1000})
        prompt(page, endpoint, editor, "READY")
        print(f"{name}: controls appeared before the first message and first response received", flush=True)
        info = json.loads(api(page, endpoint))
        assert info["available_models"], name
        original_model = info["current_model_id"]
        offered = [item["model_id"] for item in info["available_models"]]
        others = [value for value in offered if value != original_model]
        if others:
            choices = (others[0], original_model) if original_model in offered else (others[0],)
            for value in choices:
                model.click()
                page.get_by_test_id(f"chat-input-acp-model-option-{value}").click()
                wait_for_value(page, endpoint, "current_model_id", value)
            info = json.loads(api(page, endpoint))
        offered_efforts = {option["value"] for option in info["available_efforts"]}
        if name == "qwen-code":
            assert "high" in offered_efforts, offered_efforts
        effort = next(value for value in ("high", "medium", "low")
                      if value in offered_efforts and value != info["current_effort"])
        picker.select_option(effort)
        wait_for_value(page, endpoint, "current_effort", effort)
        page.reload()
        picker.wait_for(timeout=30000)
        assert picker.input_value() == effort, name
        prompt(page, endpoint, editor, "APPLIED")
        print(f"{name}: model catalog, reasoning change after reload and subsequent reply verified", flush=True)
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
        for name in profile_names:
            check(page, next(profile["id"] for profile in profiles if profile["name"] == name), name)
finally:
    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
