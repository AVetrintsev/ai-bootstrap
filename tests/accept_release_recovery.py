"""Target VM: verify state across reinstall, rejected config and corrected retry.

Run after both projects have real Qwen history and accept_sandbox.sh before:
sudo python3 tests/accept_release_recovery.py PROJECT OTHER INSTALLER
The second project also receives the harmless hello package as a persistence probe.
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
import tempfile
import time
from urllib.request import build_opener, ProxyHandler, Request

import yaml
from memory_probe import MemoryClient


def run(*command, **kwargs):
    if 'input' not in kwargs:
        kwargs['stdin'] = subprocess.DEVNULL
    return subprocess.run(command, check=True, text=True, capture_output=True,
                          timeout=1800, **kwargs).stdout


def shell(project, *command, **kwargs):
    return run('ai-sandbox', 'shell', project, *command, **kwargs)


def snapshot(project):
    probe = '''import hashlib,json,pathlib,subprocess,sys
def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()
history={str(path):digest(path) for path in pathlib.Path('/root/.qwen/projects').rglob('chats/*.jsonl') if path.stat().st_size}
assert history, 'Expected real Qwen conversation history'
print(json.dumps({'history':history,
 'file':digest(pathlib.Path(sys.argv[1])),
 'head':subprocess.check_output(['git','-C','/workspace/repo','rev-parse','HEAD'],text=True),
 'package':subprocess.check_output(['dpkg-query','-W','-f=${Status}:${Version}','hello'],text=True),
 'keys':{name:digest(pathlib.Path('/root/.config/ai-bootstrap')/name) for name in ('canvas.json','model-gateway.json')}}))
'''
    return json.loads(shell(project, 'python3', '-I', '-c', probe, fixture))


def indexed(project):
    messages = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {'name': 'release-acceptance', 'version': '1'}}},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'find_code', 'arguments': {'query': marker}}},
    ]

    def found(value):
        if isinstance(value, dict):
            return value.get('name') == marker or any(found(child) for key, child in value.items() if key not in ('query', 'search_term'))
        return isinstance(value, list) and any(found(child) for child in value)

    for _ in range(30):
        output = shell(project, 'python3', '-I', '/opt/ai-project-tools/graph.py', 'client',
                       input=''.join(json.dumps(message) + '\n' for message in messages))
        response = next(json.loads(line) for line in output.splitlines() if json.loads(line).get('id') == 2)
        assert not response['result'].get('isError'), 'Graph query failed'
        result = json.loads(response['result']['content'][0]['text'])
        if found(result):
            return
        time.sleep(2)
    raise AssertionError('Persistent working-copy symbol is missing from the index')


def credentials():
    paths = ['mihomo.yaml', 'github-app.pem', 'github-app-id', 'model-keys/openrouter.key', 'memory.key', 'portal.key']
    return {name: hashlib.sha256((Path('/etc/ai-bootstrap') / name).read_bytes()).digest() for name in paths}


def owner_state():
    with sqlite3.connect('/var/lib/ai-portal/portal.sqlite3') as db:
        return db.execute('SELECT * FROM owner').fetchall(), db.execute('SELECT * FROM sessions ORDER BY token_hash').fetchall()


def verify():
    assert credentials() == original_credentials, 'A service secret changed'
    assert owner_state() == original_owner, 'Owner identity or browser sessions changed'
    client = build_opener(ProxyHandler({}))
    with client.open(Request('http://localhost:8080/api/status',
                            headers={'Cookie': 'ai_session=' + session}), timeout=15) as response:
        assert response.status == 200, 'Existing browser session stopped working'
    for project in projects:
        after, before = snapshot(project), snapshots[project]
        for key in ('file', 'head', 'package', 'keys'):
            assert after[key] == before[key], f'{project}: {key} changed'
        assert all(after['history'].get(path) == value for path, value in before['history'].items()), 'Conversation history changed'
        indexed(project)
        with MemoryClient(project) as memory:
            assert memory.call('get_episode', uuid=records[project])['content'] == marker
    print('PASS working copies, packages, history, indices, private memory, sessions and secrets preserved', flush=True)


def install(config, label, expected_success=True):
    log = Path('/home/owner') / (marker + '-' + label + '.log')
    with log.open('w') as output:
        result = subprocess.run(['bash', installer, '--config', str(config)], stdin=subprocess.DEVNULL,
                                stdout=output, stderr=subprocess.STDOUT, timeout=1800)
    if expected_success:
        assert result.returncode == 0, f'{label} failed; see private VM log {log}'
    else:
        assert result.returncode != 0, 'Invalid configuration was accepted'
        assert 'ERROR install/preflight:' in log.read_text(), 'Invalid configuration reached a mutation step'
    print('PASS ' + label, flush=True)


if __name__ == '__main__':
    if os.geteuid() != 0 or len(sys.argv) != 4 or sys.argv[1] == sys.argv[2] or any(
            not re.fullmatch(r'gh-[1-9][0-9]{0,18}', project) for project in sys.argv[1:3]):
        sys.exit('Usage: sudo python3 accept_release_recovery.py PROJECT OTHER INSTALLER')
    projects = sys.argv[1:3]
    installer = str(Path(sys.argv[3]).resolve())
    os.umask(0o077)
    marker = 'release_recovery_' + secrets.token_hex(8)
    fixture = '/workspace/repo/' + marker + '.ts'
    config_path = Path('/etc/ai-bootstrap/config.yaml')
    original_config = config_path.read_bytes()
    original_resources = Path('/etc/ai-bootstrap/sandbox-resources').read_bytes()
    session = secrets.token_urlsafe(32)
    session_hash = hashlib.sha256(session.encode()).digest()
    now = int(time.time())
    with sqlite3.connect('/var/lib/ai-portal/portal.sqlite3') as db:
        db.execute('INSERT INTO sessions(token_hash,expires_at,name,registered_at,last_active_at) VALUES(?,?,?,?,?)',
                   (session_hash, now + 7200, marker, now, now))
    original_credentials, original_owner = credentials(), owner_state()
    snapshots, records = {}, {}
    changed = False
    try:
        for project in projects:
            shell(project, 'apt-get', '-qy', '--no-install-recommends', 'install', 'hello')
            shell(project, 'python3', '-c', 'import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])',
                  fixture, f'export function {marker}() {{ return 42; }}\n')
            indexed(project)
            snapshots[project] = snapshot(project)
            with MemoryClient(project) as memory:
                records[project] = memory.call('add_memory', name=marker, episode_body=marker)['uuid']
        install(config_path, 'same-release-reinstall')
        verify()
        with tempfile.TemporaryDirectory(prefix=marker + '-', dir='/root') as directory:
            candidate = Path(directory) / 'config.yaml'
            config = yaml.safe_load(original_config)
            config['sandbox']['memory_percent'] = 101
            candidate.write_text(yaml.safe_dump(config))
            install(candidate, 'invalid-config-rejected', expected_success=False)
            assert Path('/etc/ai-bootstrap/sandbox-resources').read_bytes() == original_resources
            verify()
            config = yaml.safe_load(original_config)
            cpu = config['sandbox'].get('cpu_percent', 80)
            config['sandbox']['cpu_percent'] = cpu - 1 if cpu > 1 else 2
            candidate.write_text(yaml.safe_dump(config))
            changed = True
            install(candidate, 'corrected-config-applied')
            assert int(Path('/etc/ai-bootstrap/sandbox-resources').read_text().split()[1]) == config['sandbox']['cpu_percent']
            run('bash', str(Path(__file__).with_name('accept_resources.sh')), *projects)
            verify()
        install(config_path, 'original-config-restored')
        changed = False
        assert config_path.read_bytes() == original_config
        assert Path('/etc/ai-bootstrap/sandbox-resources').read_bytes() == original_resources
        verify()
    finally:
        try:
            if changed:
                install(config_path, 'cleanup-restore-original-config')
        finally:
            errors = []
            for project in projects:
                try:
                    if project in records:
                        with MemoryClient(project) as memory:
                            memory.call('delete_episode', uuid=records[project])
                except Exception as error:
                    errors.append(error)
                try:
                    shell(project, 'rm', '-f', '--', fixture)
                except Exception as error:
                    errors.append(error)
            with sqlite3.connect('/var/lib/ai-portal/portal.sqlite3') as db:
                db.execute('DELETE FROM sessions WHERE token_hash=?', (session_hash,))
            if errors:
                raise ExceptionGroup('Release acceptance cleanup failed', errors)
