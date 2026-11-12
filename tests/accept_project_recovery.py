"""Target VM: preserve the graph and sandbox across restart and repeat installation."""
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import subprocess
import sys
import time

if os.geteuid() != 0 or len(sys.argv) != 3 or not re.fullmatch(r'gh-[1-9][0-9]{0,18}', sys.argv[1]):
    sys.exit('Usage: sudo python3 tests/accept_project_recovery.py PROJECT INSTALL_SH')
project, installer = sys.argv[1:]
account = pwd.getpwnam('aisbx-' + project)
symbol = 'recovery_tools_' + secrets.token_hex(8)
fixture = '/workspace/repo/' + symbol + '.ts'
unit = 'ai-bootstrap-project-tools.service'


def run(*command, **kwargs):
    kwargs.setdefault('stdin', subprocess.DEVNULL)
    return subprocess.run(command, check=True, text=True, capture_output=True, cwd='/', **kwargs).stdout


def project_run(*command, **kwargs):
    return run('runuser', '-u', account.pw_name, '--', 'env', f'XDG_RUNTIME_DIR=/run/user/{account.pw_uid}',
               *command, **kwargs)


def shell(*command, **kwargs):
    return run('ai-sandbox', 'shell', project, *command, **kwargs)


def ready():
    deadline = time.monotonic() + 800
    while time.monotonic() < deadline:
        try:
            shell('python3', '-I', '/opt/ai-project-tools/graph.py', 'ready', timeout=15)
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            time.sleep(2)
    raise AssertionError('Graph did not recover')


def indexed(name, present):
    messages = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {'name': 'recovery', 'version': '1'}}},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'find_code', 'arguments': {'query': name}}},
    ]
    def found(value):
        if isinstance(value, dict):
            return value.get('name') == name or any(found(v) for k, v in value.items() if k not in ('query', 'search_term'))
        return isinstance(value, list) and any(found(v) for v in value)
    for _ in range(60):
        output = shell('python3', '-I', '/opt/ai-project-tools/graph.py', 'client',
                       stdin=None, input=''.join(json.dumps(m) + '\n' for m in messages), timeout=180)
        response = next(json.loads(line) for line in output.splitlines() if json.loads(line).get('id') == 2)
        assert not response['result'].get('isError'), response
        result = json.loads(response['result']['content'][0]['text'])
        assert 'error' not in result, result
        if found(result) == present:
            return
        time.sleep(1)
    raise AssertionError((name, present, result))


try:
    ready()
    identity = project_run('podman', 'inspect', '--format', '{{.Id}}', 'project')
    shell('python3', '-c', 'import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])',
          fixture, f'export function {symbol}() {{ return 42; }}\n')
    indexed(symbol, True)
    project_run('systemctl', '--user', 'stop', unit, timeout=130)
    shell('python3', '-c',
          'from pathlib import Path; p=Path("/root/.local/share/ai-codegraph/graph.rdb"); '
          'assert p.stat().st_size>100; assert p.read_bytes()[:5]==b"REDIS"')
    shell('python3', '-c', 'import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])',
          fixture, f'export function {symbol}_changed() {{ return 43; }}\n')
    run('systemctl', 'restart', f'user@{account.pw_uid}.service', timeout=180)
    ready()
    assert project_run('podman', 'inspect', '--format', '{{.Id}}', 'project') == identity
    indexed(symbol + '_changed', True)
    indexed(symbol, False)
    print('OK persistent FalkorDB file, same sandbox and offline edits after project restart', flush=True)
    run('bash', str(Path(installer).resolve()), '--config', '/etc/ai-bootstrap/config.yaml', timeout=1800)
    ready()
    assert project_run('podman', 'inspect', '--format', '{{.Id}}', 'project') == identity
    indexed(symbol + '_changed', True)
    print('OK repeat installation preserves the sandbox, working copy and available graph', flush=True)
finally:
    project_run('systemctl', '--user', 'start', unit, timeout=60)
    shell('rm', '-f', '--', fixture)
