"""Run on the target VM as root; creates and removes a temporary browser session."""
import hashlib
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

if os.geteuid() != 0 or len(sys.argv) != 3:
    sys.exit("Usage: sudo PYTHON tests/accept_canvas.py PROJECT CHROMIUM")
project, chromium = sys.argv[1:]
session = secrets.token_urlsafe(32)
token_hash = hashlib.sha256(session.encode()).digest()
database = "/var/lib/ai-portal/portal.sqlite3"
now = int(time.time())
marker = "ai-canvas-accept-" + secrets.token_hex(8)
marker_path = "/workspace/repo/." + marker
with sqlite3.connect(database) as db:
    db.execute("INSERT INTO sessions(token_hash,expires_at,name,registered_at,last_active_at) VALUES(?,?,?,?,?)",
               (token_hash, now + 600, "Canvas acceptance", now, now))
try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium, headless=True, args=["--no-sandbox"])
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        context.set_default_timeout(120000)
        context.add_cookies([{"name": "ai_session", "value": session, "url": "http://localhost:8080",
                              "httpOnly": True, "sameSite": "Strict"}])
        page = context.new_page()
        frames = []
        page.on("websocket", lambda websocket: websocket.on("framereceived", lambda frame: frames.append(frame)))
        page.on("pageerror", lambda error: print("Browser error:", str(error)[:300]))
        page.goto("http://localhost:8080/projects")
        print("Portal:", page.locator("body").inner_text())
        row = page.locator("li").filter(has_text=project)
        if row.get_by_role("button", name="Подключить Canvas").count():
            row.get_by_role("button", name="Подключить Canvas").click()
        for _ in range(60):
            if row.get_by_role("link", name="Открыть", exact=True).count():
                break
            page.wait_for_timeout(2000)
            page.reload()
        row.get_by_role("link", name="Открыть", exact=True).click()
        page.get_by_test_id("chat-input").wait_for(timeout=90000)
        print("Canvas URL:", page.url)
        prompt = (f"Use the shell tool to run exactly: test \"$PWD\" = /workspace/repo && printf '%s' '{marker}' > '{marker_path}'\n"
                  "Do not read or change any other files. Then reply DONE.")
        page.get_by_test_id("chat-input").fill(prompt)
        page.get_by_test_id("chat-input").press("Enter")
        for _ in range(90):
            result = subprocess.run(["ai-sandbox", "shell", project, "cat", marker_path],
                                    capture_output=True, text=True)
            if result.returncode == 0 and result.stdout == marker:
                transcript = page.locator("body").inner_text()
                if any(line.startswith("Running Shell:") and marker in line for line in transcript.splitlines()):
                    break
            approval = page.get_by_role("button", name=re.compile(r"^(Allow once|Approve|Allow|Разрешить)$", re.I))
            if approval.count() and approval.first.is_visible():
                approval.first.click()
            page.wait_for_timeout(2000)
        else:
            print("Canvas failure:", page.locator("body").inner_text())
            raise AssertionError("Qwen result was not confirmed in both the checkout and Canvas tool output")
        page.wait_for_timeout(2000)
        transcript = page.locator("body").inner_text()
        print("Canvas result:", transcript)
        assert any(line.startswith("Running Shell:") and marker in line for line in transcript.splitlines()), "No matching Qwen tool in Canvas"
        assert frames, "No live WebSocket events"
        print("WebSocket events:", len(frames))
        print("OK Qwen wrote the marker in the same checkout used by ai-sandbox shell")
        screenshot = "/tmp/" + marker + ".png"
        page.screenshot(path=screenshot, full_page=True)
        print("Screenshot:", screenshot)
        browser.close()
finally:
    subprocess.run(["ai-sandbox", "shell", project, "rm", "-f", "--", marker_path],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
