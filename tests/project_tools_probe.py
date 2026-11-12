"""Runs inside the sandbox: verifies the actual MCP servers and live graph changes."""
import json
from pathlib import Path
import queue
import subprocess
import threading
import time
import uuid


class MCP:
    def __init__(self, command):
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, cwd='/workspace/repo')
        self.messages = queue.Queue()
        self.sequence = 0
        threading.Thread(target=self.read, daemon=True).start()
        self.request('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
            'clientInfo': {'name': 'acceptance', 'version': '1'}})
        self.process.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        self.process.stdin.flush()

    def read(self):
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            self.messages.put(message)
        self.messages.put({'error': 'MCP process exited'})

    def request(self, method, params):
        self.sequence += 1
        self.process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': self.sequence,
                                             'method': method, 'params': params}) + '\n')
        self.process.stdin.flush()
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            result = self.messages.get(timeout=max(0.1, deadline - time.monotonic()))
            assert 'error' not in result, result
            if result.get('id') == self.sequence:
                assert not result['result'].get('isError'), result
                return result['result']
        raise AssertionError('MCP response timed out')

    def call(self, name, **arguments):
        return self.request('tools/call', {'name': name, 'arguments': arguments})

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=10)


graph_command = ['python3', '-I', '/opt/ai-project-tools/graph.py', 'client']
graph = MCP(graph_command)
second = MCP(graph_command)
serena = None
name = 'ai_tools_accept_' + uuid.uuid4().hex
path = Path('/workspace/repo') / (name + '.py')


def wait_symbol(symbol, present):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        result = graph.call('find_code', query=symbol)
        value = json.loads(result['content'][0]['text'])
        assert 'error' not in value, value
        # Match returned symbol records, not the echoed search query.
        def found(item):
            if isinstance(item, dict):
                return item.get('name') == symbol or any(found(v) for k, v in item.items() if k not in ('query', 'search_term'))
            if isinstance(item, list):
                return any(found(v) for v in item)
            return False
        if found(value) == present:
            return
        time.sleep(1)
    raise AssertionError((symbol, present, value))


try:
    tools = graph.request('tools/list', {})['tools']
    assert any(tool['name'] == 'find_code' for tool in tools)
    assert second.call('list_indexed_repositories')['content']
    print('OK concurrent MCP clients share the project graph')
    path.write_text(f'def {name}():\n    return 42\n')
    wait_symbol(name, True)
    serena = MCP(['/opt/ai-serena/bin/serena', 'start-mcp-server', '--project', '/workspace/repo',
        '--context', 'ide', '--open-web-dashboard', 'false', '--enable-gui-log-window', 'false'])
    serena.call('initial_instructions')
    result = serena.call('get_symbols_overview', relative_path=path.name)
    assert name in json.dumps(result), result
    print('OK Serena resolves a symbol in the same checkout')
    path.write_text(f'def {name}_changed():\n    return 43\n')
    wait_symbol(name + '_changed', True)
    wait_symbol(name, False)
    path.unlink()
    wait_symbol(name + '_changed', False)
    print('OK graph reflects additions, edits and deletions')
finally:
    path.unlink(missing_ok=True)
    graph.close()
    second.close()
    if serena:
        serena.close()
