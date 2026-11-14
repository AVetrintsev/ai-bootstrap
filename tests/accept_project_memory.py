"""Run as root on the target VM: MCP isolation, persistence and sandbox boundaries."""
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys

from memory_probe import MemoryClient

if (os.geteuid() != 0 or len(sys.argv) not in (3, 4)
        or any(not re.fullmatch(r'gh-[1-9][0-9]{0,18}', p) for p in sys.argv[1:3])
        or sys.argv[1] == sys.argv[2]):
    sys.exit('Usage: sudo python3 tests/accept_project_memory.py PROJECT OTHER [INSTALLER]')
project, other = sys.argv[1:3]
marker = 'memory-' + secrets.token_hex(8)
identity = None


def run(*command):
    return subprocess.run(command, check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL)


try:
    with MemoryClient(project) as writer, MemoryClient(project) as reader, MemoryClient(other) as outsider:
        record = writer.call('add_memory', name=marker,
            episode_body=f'The Artemis project uses the deployment code {marker}.')
        identity = record['uuid']
        assert marker in reader.call('get_episode', uuid=identity)['content']
        assert any(marker in fact['fact'] for fact in reader.call('search_memory_facts', query=marker)['facts'])
        for name, args in [('get_episode', {'uuid': identity}), ('delete_episode', {'uuid': identity}),
                           ('get_episodes', {'group_ids': [project]}),
                           ('add_memory', {'name': marker, 'episode_body': marker, 'group_id': project}),
                           ('search_memory_facts', {'query': marker, 'project_id': project})]:
            result = outsider.request('tools/call', {'name': name, 'arguments': args})
            assert result.get('isError'), f'Cross-project access accepted: {name}'
        assert not any(marker in item['content'] for item in outsider.call('get_episodes')['episodes'])
        assert not any(marker in item['fact'] for item in outsider.call('search_memory_facts', query=marker)['facts'])
    run('systemctl', 'restart', f'ai-bootstrap-memory@{project}.service')
    with MemoryClient(project) as reader:
        assert marker in reader.call('get_episode', uuid=identity)['content']
    if len(sys.argv) == 4:
        run('bash', str(Path(sys.argv[3]).resolve()), '--config', '/etc/ai-bootstrap/config.yaml')
        with MemoryClient(project) as reader:
            assert marker in reader.call('get_episode', uuid=identity)['content']
        print('OK acknowledged project memory survived reinstall')
    probe = '''import socket
from pathlib import Path
for path in ['/etc/ai-bootstrap/memory.key', '/etc/ai-bootstrap/model-keys/openrouter.key',
             '/var/lib/ai-memory', '/run/credentials/ai-bootstrap-memory@PROJECT.service/provider.key']:
    try:
        Path(path).read_bytes()
    except (PermissionError, FileNotFoundError, IsADirectoryError):
        pass
    else:
        raise AssertionError('Host memory credential is exposed')
for host in ('127.0.0.1', '10.0.2.2'):
    connection = socket.socket()
    connection.settimeout(2)
    try:
        connection.connect((host, 6379))
    except OSError:
        pass
    else:
        raise AssertionError('Direct FalkorDB access is exposed')
    finally:
        connection.close()
'''.replace('PROJECT', project)
    run('ai-sandbox', 'shell', project, 'python3', '-I', '-c', probe)
    print('OK same-project sharing, semantic search, cross-project denial, restart and secret isolation')
finally:
    if identity:
        with MemoryClient(project) as client:
            client.call('delete_episode', uuid=identity)
