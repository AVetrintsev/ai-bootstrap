"""Run on the target VM as root; creates and removes a temporary browser session."""
import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
import uuid

from playwright.sync_api import sync_playwright

if os.geteuid() != 0 or len(sys.argv) not in (3, 4, 5):
    sys.exit("Usage: sudo PYTHON tests/accept_canvas.py PROJECT CHROMIUM [qwen|codex] [shell|mcp|project-tools|memory]")
project, chromium = sys.argv[1:3]
agent = sys.argv[3] if len(sys.argv) >= 4 else 'qwen'
tool = sys.argv[4] if len(sys.argv) == 5 else 'shell'
if agent not in ('qwen', 'codex'):
    sys.exit('Expected qwen or codex')
if tool not in ('shell', 'mcp', 'project-tools', 'memory') or (tool == 'mcp' and agent != 'codex'):
    sys.exit('MCP acceptance requires Codex and its temporary probe fixture')
session = secrets.token_urlsafe(32)
token_hash = hashlib.sha256(session.encode()).digest()
database = "/var/lib/ai-portal/portal.sqlite3"
now = int(time.time())
marker = "ai-canvas-accept-" + secrets.token_hex(8)
marker_path = "/workspace/repo/." + marker
fixture_symbol = 'canvas_tools_' + secrets.token_hex(8)
fixture_path = '/workspace/repo/' + fixture_symbol + '.ts'
executor_stopped = False
test_conversation = None
context = None
memory = None
with sqlite3.connect(database) as db:
    db.execute("INSERT INTO sessions(token_hash,expires_at,name,registered_at,last_active_at) VALUES(?,?,?,?,?)",
               (token_hash, now + 1800, "Canvas acceptance", now, now))
try:
    if tool == 'memory':
        from memory_probe import MemoryClient
        memory = MemoryClient(project)
        seed = memory.call('add_memory', name=fixture_symbol,
                           episode_body='The project acceptance color is purple.')
    if tool == 'project-tools':
        subprocess.run(['ai-sandbox', 'shell', project, 'python3', '-c',
            'import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])', fixture_path,
            f'export function {fixture_symbol}() {{ return 42; }}\n'], check=True, stdin=subprocess.DEVNULL)
        time.sleep(5)
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
        row.get_by_role("link", name="Открыть" if agent == 'codex' else "Qwen Code", exact=True).click()
        page.get_by_test_id("chat-input").wait_for(timeout=90000)
        if tool in ('project-tools', 'memory'):
            # Keep test runs independent of existing conversations and their pending turns.
            profiles = page.request.get(f'http://localhost:8080/agent/{project}/api/agent-profiles').json()['profiles']
            profile = next(p for p in profiles if p['name'] == ('codex-subscription' if agent == 'codex' else 'qwen-code'))
            test_conversation = str(uuid.uuid4())
            created = page.request.post(f'http://localhost:8080/agent/{project}/api/conversations', data={
                'conversation_id': test_conversation, 'agent_profile_id': profile['id'],
                'workspace': {'kind': 'LocalWorkspace', 'working_dir': '/workspace/repo'},
                'worktree': False, 'autotitle': False})
            assert created.ok, created.text()
            page.goto(f'http://localhost:8080/conversations/{test_conversation}?backend={project}')
            page.get_by_test_id('chat-input').wait_for(timeout=90000)
        print("Canvas URL:", page.url)
        prompt = (f"Use the shell tool to run exactly: test \"$PWD\" = /workspace/repo && printf '%s' '{marker}' > '{marker_path}'\n"
                  "Do not read or change any other files. Then reply DONE.")
        if tool == 'mcp':
            prompt = f'Call the MCP acceptance probe tool with file="{marker_path}" and text="{marker}". Use only that MCP tool, not shell. Then reply DONE.'
        if tool == 'project-tools':
            prompt = (f'Acceptance test. First call Serena initial_instructions, then get_symbols_overview on {fixture_symbol}.ts, then '
                      f'CodeGraphContext find_code with query {fixture_symbol}. Both must find the function. '
                      f'Then use the shell tool to run: cd /workspace/repo && rg {fixture_symbol} {fixture_symbol}.ts '
                      f"&& ast-grep run --lang typescript --pattern 'function $NAME() {{ $$$BODY }}' {fixture_symbol}.ts "
                      f"&& printf '%s' '{marker}' > '{marker_path}'. Do not change other files. Reply DONE.")
        if tool == 'memory':
            prompt = (f'Call Graphiti get_episode with uuid="{seed["uuid"]}". Then call Graphiti add_memory '
                      f'with name="{fixture_symbol}" and episode_body="Canvas memory reference: {marker}.". '
                      'Use only these MCP tools, then reply DONE.')
        def has_symbol(value):
            if isinstance(value, str):
                try:
                    return has_symbol(json.loads(value))
                except ValueError:
                    return (any(has_symbol(line) for line in value.splitlines())
                            if len(value.splitlines()) > 1 else value == fixture_symbol)
            if isinstance(value, dict):
                return not value.get('isError') and any(has_symbol(v) for k, v in value.items() if k not in ('query', 'search_term'))
            return isinstance(value, list) and any(has_symbol(v) for v in value)

        def has_tool(transcript):
            if tool in ('project-tools', 'memory'):
                expected = {'get_episode', 'add_memory'} if tool == 'memory' else {'get_symbols_overview', 'find_code'}
                completed = set()
                def inspect(value):
                    if isinstance(value, dict):
                        if (value.get('tool_call_id') and value.get('source') == 'agent' and
                                value.get('status') == 'completed' and not value.get('is_error')):
                            output = value.get('raw_output') or value.get('content')
                            if has_symbol(output):
                                for name in expected:
                                    if name in value.get('title', ''):
                                        completed.add(name)
                        for child in value.values():
                            inspect(child)
                    elif isinstance(value, list):
                        for child in value:
                            inspect(child)
                for frame in frames:
                    try:
                        inspect(json.loads(frame))
                    except (ValueError, TypeError):
                        continue
                return completed == expected
            return (any(line.strip() == 'probe' for line in transcript.splitlines()) if tool == 'mcp' else
                    any(marker in line and ('Running Shell:' in line or '/bin/bash' in line) for line in transcript.splitlines()))
        page.get_by_test_id("chat-input").fill(prompt)
        frames.clear()
        page.get_by_test_id("chat-input").press("Enter")
        # Startup may take 120s; the Codex bridge allows a 600s model turn.
        deadline = time.monotonic() + (720 if tool in ('project-tools', 'memory') else 240)
        while time.monotonic() < deadline:
            if memory:
                confirmed = any(marker in item['content'] for item in memory.call('get_episodes')['episodes'])
            else:
                result = subprocess.run(["ai-sandbox", "shell", project, "cat", marker_path],
                                        capture_output=True, text=True, stdin=subprocess.DEVNULL)
                confirmed = result.returncode == 0 and result.stdout == marker
            if confirmed:
                transcript = page.locator("body").inner_text()
                if has_tool(transcript):
                    break
            approval = page.get_by_role("button", name=re.compile(r"^(Allow once|Approve|Allow|Разрешить)$", re.I))
            if approval.count() and approval.first.is_visible():
                approval.first.click()
            page.wait_for_timeout(2000)
        else:
            if tool in ('project-tools', 'memory'):
                with open('/tmp/' + marker + '-events.json', 'w') as output:
                    json.dump([frame.decode() if isinstance(frame, bytes) else frame for frame in frames], output)
            print("Canvas failure:", page.locator("body").inner_text())
            raise AssertionError(f"{agent} result was not confirmed in both the checkout and Canvas tool output")
        page.wait_for_timeout(2000)
        transcript = page.locator("body").inner_text()
        print("Canvas result:", transcript)
        assert has_tool(transcript), "No matching agent tool in Canvas"
        assert frames, "No live WebSocket events"
        print("WebSocket events:", len(frames))
        print(f"OK {agent} read and wrote project memory via MCP" if memory else
              f"OK {agent} wrote the marker in the same checkout used by ai-sandbox shell")
        screenshot = "/tmp/" + marker + ".png"
        page.screenshot(path=screenshot, full_page=True)
        print("Screenshot:", screenshot)
        if agent == 'codex' and tool == 'shell':
            subprocess.run(['systemctl', 'stop', f'ai-bootstrap-codex-executor@{project}.socket',
                            f'ai-bootstrap-codex-executor@{project}.service'], check=True)
            executor_stopped = True
            page.get_by_test_id('chat-input').fill(f'Write the word blocked to {marker_path}-blocked using the shell tool.')
            page.get_by_test_id('chat-input').press('Enter')
            page.get_by_text(re.compile('Исполнитель проекта недоступен')).first.wait_for(timeout=60000)
            absent = subprocess.run(['ai-sandbox', 'shell', project, 'test', '!', '-e', marker_path + '-blocked'])
            assert absent.returncode == 0
            assert not os.path.exists(marker_path + '-blocked')
            print('OK Canvas shows an explicit executor error without writing on the host or in the sandbox')
            subprocess.run(['systemctl', 'start', f'ai-bootstrap-codex-executor@{project}.socket'], check=True)
            executor_stopped = False
            # Reload the backend to prove a rejected message is not pending work.
            import pwd
            account = pwd.getpwnam('aisbx-' + project)
            subprocess.run(['runuser', '-u', account.pw_name, '--', 'env', f'XDG_RUNTIME_DIR=/run/user/{account.pw_uid}',
                            'systemctl', '--user', 'restart', 'ai-bootstrap-agent.service'], check=True, cwd='/')
            # systemctl returns before the Python server has finished starting.
            for _ in range(60):
                ready = page.request.get(f'http://localhost:8080/agent/{project}/server_info', timeout=15000)
                if ready.ok:
                    break
                page.wait_for_timeout(1000)
            else:
                raise AssertionError('Agent Server did not recover')
            page.goto(f'http://localhost:8080/canvas/open/{project}')
            page.get_by_test_id('chat-input').wait_for(timeout=90000)
            page.wait_for_timeout(5000)
            absent = subprocess.run(['ai-sandbox', 'shell', project, 'test', '!', '-e', marker_path + '-blocked'])
            assert absent.returncode == 0, 'Rejected request replayed after recovery'
            print('OK failed Canvas request remains rejected after service recovery')
        if test_conversation:
            page.request.post(f'http://localhost:8080/agent/{project}/api/conversations/{test_conversation}/pause')
            deleted = page.request.delete(f'http://localhost:8080/agent/{project}/api/conversations/{test_conversation}')
            assert deleted.ok, deleted.text()
            test_conversation = None
        browser.close()
finally:
    if memory:
        try:
            for episode in memory.call('get_episodes', num_results=20)['episodes']:
                if episode['name'] == fixture_symbol:
                    memory.call('delete_episode', uuid=episode['uuid'])
        finally:
            memory.close()
    if test_conversation:
        # The browser may already be closed on an exception. Use the existing
        # authenticated project API inside the sandbox to stop/delete only our fixture.
        cleanup = '''import json,pathlib,sys,urllib.request,urllib.error
c=json.loads(pathlib.Path('/root/.config/ai-bootstrap/canvas.json').read_text())
for suffix,method in [('/pause','POST'),('','DELETE')]:
 r=urllib.request.Request('http://127.0.0.1:18000/api/conversations/'+sys.argv[1]+suffix,
   method=method,headers={'X-Session-API-Key':c['key']})
 try: urllib.request.urlopen(r,timeout=20).close()
 except urllib.error.HTTPError as e:
  if e.code != 404: raise
'''
        subprocess.run(['ai-sandbox', 'shell', project, 'python3', '-c', cleanup, test_conversation],
                       stdin=subprocess.DEVNULL, timeout=60, check=True)
    if executor_stopped:
        subprocess.run(['systemctl', 'start', f'ai-bootstrap-codex-executor@{project}.socket'], check=True)
    subprocess.run(["ai-sandbox", "shell", project, "rm", "-f", "--", marker_path, marker_path + '-blocked'],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if tool == 'project-tools':
        subprocess.run(['ai-sandbox', 'shell', project, 'rm', '-f', '--', fixture_path],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
