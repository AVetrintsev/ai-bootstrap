"""Installed acceptance: real agents, browser approval, two projects and reinstall.

Run as root on VM AI using the existing browser Python environment. All fixture
sessions, proposals and published records are removed, including on failure.
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

from playwright.sync_api import sync_playwright
from memory_probe import MemoryClient


if (os.geteuid() != 0 or len(sys.argv) not in (4, 5)
        or any(not re.fullmatch(r'gh-[1-9][0-9]{0,18}', value) for value in sys.argv[1:3])
        or sys.argv[1] == sys.argv[2]):
    sys.exit('Usage: sudo BROWSER_PYTHON tests/accept_shared_memory.py PROJECT OTHER CHROMIUM [INSTALLER]')
project, other, chromium = sys.argv[1:4]
tests = Path(__file__).resolve().parent
names = ['shared-' + secrets.token_hex(8) for _ in range(2)]
session = secrets.token_urlsafe(32)
token_hash = hashlib.sha256(session.encode()).digest()
database = '/var/lib/ai-portal/portal.sqlite3'
private = None


def run(*command, **kwargs):
    return subprocess.run(command, check=True, stdin=subprocess.DEVNULL, **kwargs)


def agent_call(project_name, agent, name, identity):
    run(sys.executable, str(tests / 'accept_project_agents.py'), project_name, agent,
        'shared-memory', name, identity, timeout=420)


def proposal_pages(page):
    page.goto('http://localhost:8080/memory')
    while True:
        yield page.locator('article')
        following = page.get_by_role('link', name='Следующие предложения')
        if not following.count():
            return
        following.click()


def fixture_rows(page, name):
    for rows in proposal_pages(page):
        matches = rows.filter(has=page.get_by_role('heading', name=name, exact=True))
        if matches.count():
            return matches
    raise AssertionError('Agent proposal was not found in the owner portal')


def pending_fixture_ids(page, name):
    identities = []
    for rows in proposal_pages(page):
        matches = rows.filter(has=page.get_by_role('heading', name=name, exact=True))
        identities.extend(matches.nth(index).locator('input[name="uuid"]').input_value()
                          for index in range(matches.count()))
    return identities


def check_published(identities):
    for project_name in (project, other):
        with MemoryClient(project_name) as memory:
            for name, identity in zip(names, identities):
                record = memory.call('get_shared_memory', uuid=identity)
                assert record['name'] == name
                assert record['content'] == f'Shared memory acceptance: {name}.'


now = int(time.time())
with sqlite3.connect(database) as db:
    db.execute('INSERT INTO sessions(token_hash,expires_at,name,registered_at,last_active_at) VALUES(?,?,?,?,?)',
               (token_hash, now + 3600, 'Shared memory acceptance', now, now))
try:
    agent_call(project, 'codex', names[0], 'propose')
    agent_call(other, 'qwen', names[1], 'propose')
    with MemoryClient(project) as memory:
        private = memory.call('add_memory', name=names[0], episode_body='Private acceptance color is purple.')['uuid']
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium, args=['--no-sandbox'])
        context = browser.new_context(viewport={'width': 1200, 'height': 800})
        context.add_cookies([{'name': 'ai_session', 'value': session, 'url': 'http://localhost:8080',
                             'httpOnly': True, 'sameSite': 'Strict'}])
        page = context.new_page()
        page.set_default_timeout(30000)
        anonymous = browser.new_context()
        assert anonymous.request.get('http://localhost:8080/memory').status == 404
        identities = []
        for name in names:
            row = fixture_rows(page, name).first
            text = row.locator('pre code').text_content()
            assert text == f'Shared memory acceptance: {name}.', 'Portal changed the proposed text'
            fields = {key: row.locator(f'input[name="{key}"]').input_value() for key in ('csrf', 'uuid', 'revision')}
            fields['confirm'] = 'yes'
            identity = fields['uuid']
            assert anonymous.request.post('http://localhost:8080/memory/approve', form=fields).status == 404
            assert context.request.post('http://localhost:8080/memory/approve', form={**fields, 'csrf': 'wrong'}).status == 403
            assert context.request.post('http://localhost:8080/memory/approve', form={**fields, 'revision': '0' * 64}).status == 503
            for project_name in (project, other):
                with MemoryClient(project_name) as memory:
                    for tool, arguments in (
                        ('get_shared_memory', {'uuid': identity}),
                        ('approve_proposal', {'uuid': identity, 'revision': fields['revision']}),
                        ('propose_shared_memory', {'name': name, 'episode_body': 'Forged', 'project_id': other}),
                    ):
                        assert memory.request('tools/call', {'name': tool, 'arguments': arguments}).get('isError')
                    assert identity not in [record['uuid'] for record in memory.call('get_shared_memories')['episodes']]
            row.get_by_role('button', name='Опубликовать этот текст').click()
            assert page.get_by_role('heading', name='Запись опубликована', exact=True).is_visible()
            assert context.request.post('http://localhost:8080/memory/approve', form=fields).ok
            identities.append(identity)
        anonymous.close()
        print('OK exact browser text, owner-only confirmation, denied agent publication and retry', flush=True)
        check_published(identities)
        with MemoryClient(other) as memory:
            for tool in ('get_episode', 'delete_episode', 'get_shared_memory'):
                assert memory.request('tools/call', {'name': tool, 'arguments': {'uuid': private}}).get('isError')
            assert private not in [item['uuid'] for item in memory.call('get_episodes')['episodes']]
        for project_name in (project, other):
            for agent in ('codex', 'qwen'):
                agent_call(project_name, agent, names[0], identities[0])
        print('OK both real agents read approved memory in both projects; private memory remains isolated', flush=True)
        for agent in ('codex', 'qwen'):
            log = Path('/tmp') / (names[0] + '-' + agent + '-canvas.log')
            with log.open('w') as output:
                run(sys.executable, str(tests / 'accept_canvas.py'), project, chromium,
                    agent, 'shared-memory', identities[0], stdout=output, stderr=subprocess.STDOUT, timeout=900)
            print(f'OK {agent} Canvas completed shared read and proposal; {log}', flush=True)
        pending = pending_fixture_ids(page, names[0])
        assert len(pending) >= 2, 'Canvas proposals are absent from the portal'
        with MemoryClient(other) as memory:
            for identity in pending:
                assert memory.request('tools/call', {'name': 'get_shared_memory', 'arguments': {'uuid': identity}}).get('isError')
        run('systemctl', 'restart', 'ai-bootstrap-shared-memory.service')
        check_published(identities)
        assert set(pending_fixture_ids(page, names[0])) == set(pending)
        if len(sys.argv) == 5:
            log = Path('/tmp') / (names[0] + '-reinstall.log')
            with log.open('w') as output:
                run('bash', str(Path(sys.argv[4]).resolve()), '--config', '/etc/ai-bootstrap/config.yaml',
                    stdout=output, stderr=subprocess.STDOUT, timeout=1200)
            check_published(identities)
            assert set(pending_fixture_ids(page, names[0])) == set(pending)
            print(f'OK published records and pending proposals survived reinstall; {log}', flush=True)
        with MemoryClient(project) as memory:
            assert memory.call('get_episode', uuid=private)['content'] == 'Private acceptance color is purple.'
        probe = '''from pathlib import Path
import socket
for path in ('/var/lib/ai-memory/shared/proposals.sqlite3', '/var/lib/ai-memory/shared/memory.rdb'):
    try: Path(path).read_bytes()
    except (OSError, PermissionError): pass
    else: raise AssertionError('Shared memory storage is exposed')
for path in ('/run/ai-bootstrap-shared-memory.sock', '/run/ai-bootstrap-shared-memory-owner.sock'):
    with socket.socket(socket.AF_UNIX) as connection:
        try: connection.connect(path)
        except OSError: pass
        else: raise AssertionError('Trusted shared-memory socket is exposed')
'''
        for project_name in (project, other):
            run('ai-sandbox', 'shell', project_name, 'python3', '-I', '-c', probe)
        print('OK shared storage and approval sockets are outside both sandboxes', flush=True)
        browser.close()
finally:
    cleanup_errors = []
    try:
        if private:
            with MemoryClient(project) as memory:
                memory.call('delete_episode', uuid=private)
    except Exception as error:
        cleanup_errors.append(error)
    try:
        with sqlite3.connect(database) as db:
            db.execute('DELETE FROM sessions WHERE token_hash = ?', (token_hash,))
    except Exception as error:
        cleanup_errors.append(error)
    # Fixture cleanup only: stop the shared writer before opening its database.
    # Match unpredictable names created by this run; never remove other records.
    cleanup = '''import asyncio, json, sys
sys.path.insert(0, '/usr/local/libexec/ai-bootstrap')
from shared_memory import SharedMemory, EpisodicNode
from graphiti_core.errors import NodeNotFoundError
async def main():
    memory = SharedMemory('/var/lib/ai-memory/shared')
    await memory.start()
    try:
        with memory.connect() as db:
            rows = db.execute('SELECT uuid FROM proposals WHERE name IN (?, ?)', json.loads(sys.argv[1])).fetchall()
            for row in rows:
                try:
                    episode = await EpisodicNode.get_by_uuid(memory.driver, row['uuid'])
                except NodeNotFoundError:
                    continue
                await episode.delete(memory.driver)
            await memory.db.connection.save()
            db.execute('DELETE FROM proposals WHERE name IN (?, ?)', json.loads(sys.argv[1]))
    finally:
        await memory.close()
asyncio.run(main())
'''
    try:
        run('systemctl', 'stop', 'ai-bootstrap-shared-memory.socket',
            'ai-bootstrap-shared-memory-owner.socket', 'ai-bootstrap-shared-memory.service')
        run('runuser', '-u', 'ai-memory', '--', '/opt/ai-memory/bin/python', '-I', '-c', cleanup, json.dumps(names), cwd='/')
    except Exception as error:
        cleanup_errors.append(error)
    finally:
        try:
            run('systemctl', 'start', 'ai-bootstrap-shared-memory.socket', 'ai-bootstrap-shared-memory-owner.socket')
        except Exception as error:
            cleanup_errors.append(error)
    if cleanup_errors:
        raise ExceptionGroup('Acceptance fixture cleanup failed', cleanup_errors)
