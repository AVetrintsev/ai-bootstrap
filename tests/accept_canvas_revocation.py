"""Revoke a real Canvas browser while its Qwen tool is running on the target VM.

Usage: sudo BROWSER_PYTHON tests/accept_canvas_revocation.py PROJECT CHROMIUM
Creates and removes two fixture sessions, one conversation and two marker files.
"""
import hashlib
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
import uuid

from playwright.sync_api import sync_playwright


def sandbox(*command, check=True):
    return subprocess.run(['ai-sandbox', 'shell', project, *command], check=check,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)


if os.geteuid() != 0 or len(sys.argv) != 3 or not re.fullmatch(r'gh-[1-9][0-9]{0,18}', sys.argv[1]):
    sys.exit('Usage: sudo BROWSER_PYTHON tests/accept_canvas_revocation.py PROJECT CHROMIUM')
project, chromium = sys.argv[1:]
database = '/var/lib/ai-portal/portal.sqlite3'
tokens = [secrets.token_urlsafe(32) for _ in range(2)]
hashes = [hashlib.sha256(token.encode()).digest() for token in tokens]
marker = 'revoke-' + secrets.token_hex(8)
started = '/workspace/repo/.' + marker + '-started'
finished = '/workspace/repo/.' + marker + '-finished'
conversation = None
now = int(time.time())
with sqlite3.connect(database) as db:
    db.executemany('INSERT INTO sessions(token_hash,expires_at,name,registered_at,last_active_at) VALUES(?,?,?,?,?)',
                   [(value, now + 1800, marker + str(index), now, now) for index, value in enumerate(hashes)])
try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium, args=['--no-sandbox'])
        contexts = [browser.new_context() for _ in tokens]
        for context, token in zip(contexts, tokens):
            context.set_default_timeout(90000)
            context.add_cookies([{'name': 'ai_session', 'value': token, 'url': 'http://localhost:8080',
                                 'httpOnly': True, 'sameSite': 'Strict'}])
        actor, owner = contexts
        page = actor.new_page()
        page.goto(f'http://localhost:8080/canvas/open/{project}?agent=qwen')
        page.get_by_test_id('chat-input').wait_for()
        api = f'http://localhost:8080/agent/{project}/api/conversations'
        profiles = actor.request.get(f'http://localhost:8080/agent/{project}/api/agent-profiles').json()['profiles']
        profile = next(item for item in profiles if item['name'] == 'qwen-code')
        conversation = str(uuid.uuid4())
        response = actor.request.post(api, data={
            'conversation_id': conversation, 'agent_profile_id': profile['id'],
            'workspace': {'kind': 'LocalWorkspace', 'working_dir': '/workspace/repo'},
            'worktree': False, 'autotitle': False})
        assert response.ok, 'Could not create fixture conversation'
        connections = []

        def connected(websocket):
            record = {'closed': False}
            connections.append(record)
            websocket.on('close', lambda: record.update(closed=True))

        page.on('websocket', connected)
        page.goto(f'http://localhost:8080/conversations/{conversation}?backend={project}')
        page.get_by_test_id('chat-input').wait_for()
        page.get_by_test_id('chat-input').fill(
            f'Use the shell tool with timeout 60000 to run exactly: '
            f"printf started > '{started}' && sleep 20 && printf finished > '{finished}'. "
            'Do not change any other files. After the command completes reply DONE.')
        page.get_by_test_id('chat-input').press('Enter')
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            if sandbox('test', '-f', started, check=False).returncode == 0:
                break
            approval = page.get_by_role('button', name=re.compile(r'^(Allow once|Approve|Allow|Разрешить)$', re.I))
            if approval.count() and approval.first.is_visible():
                approval.first.click()
            page.wait_for_timeout(500)
        else:
            raise AssertionError('Agent did not start the long-running tool')
        assert sandbox('test', '!', '-e', finished, check=False).returncode == 0, 'Tool finished before revocation'
        live = [record for record in connections if not record['closed']]
        assert live, 'No open Canvas WebSocket at revocation time'
        management = owner.new_page()
        management.goto('http://localhost:8080/')
        row = management.locator('tr').filter(has=management.locator(f'input[value="{marker}0"]'))
        row.get_by_role('button', name='Отозвать сессию').click()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not all(record['closed'] for record in live):
            page.wait_for_timeout(200)
        assert all(record['closed'] for record in live), 'Revocation did not close the existing Canvas connection'
        assert actor.request.get(api + '/' + conversation).status == 404
        print('PASS owner revocation closes the open Canvas connection and denies new requests', flush=True)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            response = owner.request.get(api + '/' + conversation)
            assert response.ok, 'Surviving browser lost access'
            status = response.json()['execution_status']
            assert status not in ('error', 'stuck'), 'Agent failed after browser revocation'
            if status == 'finished':
                break
            page.wait_for_timeout(1000)
        else:
            raise AssertionError('Agent did not finish after browser revocation')
        assert sandbox('cat', finished).stdout == 'finished'
        print('PASS the already-running agent task completes after its browser is revoked', flush=True)
        browser.close()
finally:
    try:
        if conversation:
            cleanup = '''import json,pathlib,sys,urllib.request,urllib.error
config=json.loads(pathlib.Path('/root/.config/ai-bootstrap/canvas.json').read_text())
for suffix,method in [('/pause','POST'),('','DELETE')]:
 request=urllib.request.Request('http://127.0.0.1:18000/api/conversations/'+sys.argv[1]+suffix,
  method=method,headers={'X-Session-API-Key':config['key']})
 try: urllib.request.urlopen(request,timeout=20).close()
 except urllib.error.HTTPError as error:
  if error.code!=404: raise
'''
            sandbox('python3', '-c', cleanup, conversation)
    finally:
        try:
            sandbox('rm', '-f', '--', started, finished)
        finally:
            with sqlite3.connect(database) as db:
                db.executemany('DELETE FROM sessions WHERE token_hash=?', [(value,) for value in hashes])
