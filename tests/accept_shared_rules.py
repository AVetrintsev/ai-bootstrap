"""Installed rules acceptance with real terminal agents and Canvas in two projects.

Run as root with browser Python, PROJECT OTHER CHROMIUM [INSTALLER]. Only this
run's temporary rule, browser session and Canvas conversations are removed.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
import time
import uuid

from playwright.sync_api import sync_playwright


if (os.geteuid() != 0 or len(sys.argv) not in (4, 5)
        or any(not re.fullmatch(r'gh-[1-9][0-9]{0,18}', value) for value in sys.argv[1:3])
        or sys.argv[1] == sys.argv[2]):
    sys.exit('Usage: sudo BROWSER_PYTHON tests/accept_shared_rules.py PROJECT OTHER CHROMIUM [INSTALLER]')
projects = sys.argv[1:3]
chromium = sys.argv[3]
marker = 'rules-' + secrets.token_hex(8)
prompt = (f'Rule check {marker}. If an owner instruction defines the response to this check, follow it. '
          'Otherwise reply exactly NO_SHARED_RULE. Do not use tools.')
session = secrets.token_urlsafe(32)
session_hash = hashlib.sha256(session.encode()).digest()
database = '/var/lib/ai-portal/portal.sqlite3'
identity = None
conversations = []


def run(*command, **kwargs):
    kwargs.setdefault('stdin', subprocess.DEVNULL)
    return subprocess.run(command, check=True, **kwargs)


def terminal(project, agent, expected):
    command = ['codex', 'exec', prompt] if agent == 'codex' else ['qwen', '-p', prompt, '--output-format', 'json']
    result = run('ai-sandbox', 'shell', project, 'timeout', '--kill-after=10', '300',
                 'sh', '-c', 'cd /workspace/repo && exec "$@"', 'rules-acceptance', *command,
                 capture_output=True, text=True, timeout=330)
    if agent == 'codex':
        answer = result.stdout.strip()
    else:
        # Qwen's JSON result is either one document or the final JSON line.
        try:
            payload = json.loads(result.stdout)
        except ValueError:
            payload = next(json.loads(line) for line in reversed(result.stdout.splitlines()) if line.startswith('{'))
        if isinstance(payload, list):
            payload = next(item for item in reversed(payload) if item.get('type') == 'result')
        answer = payload.get('result', payload.get('response', '')).strip()
    assert answer == expected, f'{project} {agent} terminal returned {answer!r}, expected {expected!r}'
    print(f'OK {project} {agent} terminal {expected}', flush=True)


def canvas(context, project, agent, expected):
    # The portal launch route registers the backend in this browser's Canvas state.
    page = context.new_page()
    page.goto(f'http://localhost:8080/canvas/open/{project}?agent={agent}')
    page.get_by_test_id('chat-input').wait_for(timeout=90000)
    profiles = context.request.get(f'http://localhost:8080/agent/{project}/api/agent-profiles').json()['profiles']
    profile = next(item for item in profiles if item['name'] == ('codex-subscription' if agent == 'codex' else 'qwen-code'))
    conversation = str(uuid.uuid4())
    base = f'http://localhost:8080/agent/{project}/api/conversations/{conversation}'
    response = context.request.post(f'http://localhost:8080/agent/{project}/api/conversations', data={
        'conversation_id': conversation, 'agent_profile_id': profile['id'],
        'workspace': {'kind': 'LocalWorkspace', 'working_dir': '/workspace/repo'},
        'worktree': False, 'autotitle': False})
    assert response.ok, response.text()
    conversations.append((project, conversation))
    try:
        page.goto(f'http://localhost:8080/conversations/{conversation}?backend={project}')
        page.get_by_test_id('chat-input').wait_for(timeout=90000)
        page.get_by_test_id('chat-input').fill(prompt)
        page.get_by_test_id('chat-input').press('Enter')
        deadline = time.monotonic() + 360
        while time.monotonic() < deadline:
            state = context.request.get(base).json()['execution_status']
            if state == 'finished':
                break
            assert state not in ('error', 'stuck'), f'Canvas ended with {state}'
            page.wait_for_timeout(1000)
        else:
            raise AssertionError(f'{project} {agent} Canvas did not finish')
        events = context.request.get(base + '/events/search?source=agent&kind=MessageEvent&limit=100').json()['items']
        answers = [''.join(part.get('text', '') for part in event['llm_message']['content']).strip()
                   for event in events if event['llm_message']['role'] == 'assistant']
        assert answers == [expected], f'Unexpected completed Canvas messages: {answers!r}'
        page.get_by_text(expected, exact=True).first.wait_for(timeout=10000)
        print(f'OK {project} {agent} Canvas {expected}', flush=True)
    except BaseException:
        page.screenshot(path=f'/tmp/{marker}-{project}-{agent}-failure.png', full_page=True)
        raise
    finally:
        try:
            context.request.post(base + '/pause')
            deleted = context.request.delete(base)
            if deleted.ok:
                conversations.remove((project, conversation))
        finally:
            page.close()


def sandbox_isolation(project):
    probe = '''import json, pathlib, socket, urllib.request, urllib.error
assert not pathlib.Path('/var/lib/ai-rules/rules.sqlite3').exists()
settings=json.loads(pathlib.Path('/root/.config/ai-bootstrap/model-gateway.json').read_text())
url=settings['base_url'].rstrip('/')+'/platform/rules'
request=urllib.request.Request(url, data=b'{"content":"forged"}', method='POST',
    headers={'Authorization':'Bearer '+settings['api_key']})
try: urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=10)
except urllib.error.HTTPError as error: assert error.code==404
else: raise AssertionError('Gateway accepted a write')
with socket.socket(socket.AF_UNIX) as connection:
 connection.connect('/run/ai-bootstrap-git/codex.sock')
 connection.sendall(b'{"id":1,"method":"rules/save","params":{"content":"forged"}}\\n')
 with connection.makefile('rb') as reader: assert 'error' in json.loads(reader.readline())
'''
    run('ai-sandbox', 'shell', project, 'python3', '-I', '-c', probe, timeout=30)
    print(f'OK {project} cannot access rule storage or change rules as an agent', flush=True)


now = int(time.time())
with sqlite3.connect(database) as db:
    db.execute('INSERT INTO sessions(token_hash,expires_at,name,registered_at,last_active_at) VALUES(?,?,?,?,?)',
               (session_hash, now + 7200, 'Shared rules acceptance', now, now))
try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium, args=['--no-sandbox'])
        context = browser.new_context(viewport={'width': 1200, 'height': 900})
        context.set_default_timeout(30000)
        context.add_cookies([{'name': 'ai_session', 'value': session, 'url': 'http://localhost:8080',
                             'httpOnly': True, 'sameSite': 'Strict'}])
        page = context.new_page()
        page.goto('http://localhost:8080/rules')
        content = f'For rule check {marker}, reply exactly {marker}-v1. Do not use tools or any other text.'
        page.get_by_label('Текст нового правила').fill(content)
        page.get_by_role('button', name='Добавить правило', exact=True).click()
        row = page.locator('article').filter(has=page.locator('textarea', has_text=content))
        identity = row.locator('input[name="id"]').first.input_value()
        assert row.locator('textarea').input_value() == content
        print('OK owner added the exact rule in the browser', flush=True)
        for width, height in [(1200, 900), (390, 844)]:
            page.set_viewport_size({'width': width, 'height': height})
            page.screenshot(path=f'/tmp/{marker}-{width}.png', full_page=True)
        page.set_viewport_size({'width': 1200, 'height': 900})
        for project in projects:
            sandbox_isolation(project)
        for phase in ('v1', 'v2', 'deleted'):
            if phase == 'v2':
                page.goto('http://localhost:8080/rules')
                row = page.locator('article').filter(has=page.locator(f'input[name="id"][value="{identity}"]'))
                content = f'For rule check {marker}, reply exactly {marker}-v2. Do not use tools or any other text.'
                row.locator('textarea').fill(content)
                row.get_by_role('button', name='Сохранить изменения').click()
                run('systemctl', 'restart', 'ai-bootstrap-portal.service', 'ai-bootstrap-models.service')
                for project in projects:
                    run('systemctl', 'restart', f'ai-bootstrap-codex-project@{project}.service')
                if len(sys.argv) == 5:
                    with open('/tmp/' + marker + '-reinstall.log', 'w') as output:
                        run('bash', str(Path(sys.argv[4]).resolve()), '--config', '/etc/ai-bootstrap/config.yaml',
                            stdout=output, stderr=subprocess.STDOUT, timeout=1200)
                page.goto('http://localhost:8080/rules')
                row = page.locator('article').filter(has=page.locator(f'input[name="id"][value="{identity}"]'))
                assert row.locator('textarea').input_value() == content
                print('OK edited rule survived service restarts' + (' and reinstall' if len(sys.argv) == 5 else ''), flush=True)
            elif phase == 'deleted':
                page.goto('http://localhost:8080/rules')
                row = page.locator('article').filter(has=page.locator(f'input[name="id"][value="{identity}"]'))
                row.locator('summary').click()
                row.get_by_role('button', name='Удалить правило', exact=True).click()
                identity = None
                print('OK owner deleted the rule in the browser', flush=True)
            expected = marker + '-' + phase if phase != 'deleted' else 'NO_SHARED_RULE'
            for project in projects:
                for agent in ('codex', 'qwen'):
                    terminal(project, agent, expected)
                    canvas(context, project, agent, expected)
        browser.close()
finally:
    cleanup_errors = []
    # Delete only this run's fixture, with its current revision, as the owner.
    cleanup = '''import sys
sys.path.insert(0, '/usr/local/libexec/ai-bootstrap')
from shared_rules import Rules
rules=Rules()
for item in rules.list():
 if item['content'].startswith('For rule check '+sys.argv[1]+', reply exactly '+sys.argv[1]):
  rules.change('delete',identity=item['id'],revision=item['revision'])
'''
    try:
        run('runuser', '-u', 'ai-portal', '--', 'python3', '-I', '-c', cleanup, marker, timeout=20)
    except Exception as error:
        cleanup_errors.append(error)
    for project, conversation in conversations:
        cleanup = '''import json,pathlib,sys,urllib.request,urllib.error
config=json.loads(pathlib.Path('/root/.config/ai-bootstrap/canvas.json').read_text())
for suffix,method in [('/pause','POST'),('','DELETE')]:
 request=urllib.request.Request('http://127.0.0.1:18000/api/conversations/'+sys.argv[1]+suffix,
  method=method,headers={'X-Session-API-Key':config['key']})
 try: urllib.request.urlopen(request,timeout=20).close()
 except urllib.error.HTTPError as error:
  if error.code!=404: raise
'''
        try:
            run('ai-sandbox', 'shell', project, 'python3', '-c', cleanup, conversation, timeout=60)
        except Exception as error:
            cleanup_errors.append(error)
    try:
        with sqlite3.connect(database) as db:
            db.execute('DELETE FROM sessions WHERE token_hash = ?', (session_hash,))
    except Exception as error:
        cleanup_errors.append(error)
    if cleanup_errors:
        raise ExceptionGroup('Shared rules fixture cleanup failed', cleanup_errors)
