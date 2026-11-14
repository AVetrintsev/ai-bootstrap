"""MCP client for acceptance tests against an installed project sandbox."""
import json
import select
import subprocess


def has_memory_record(value, expected):
    """Find an exact record in completed ACP/Canvas tool output, including JSON text."""
    if isinstance(value, str):
        try:
            return has_memory_record(json.loads(value), expected)
        except ValueError:
            return (any(has_memory_record(line, expected) for line in value.splitlines())
                    if len(value.splitlines()) > 1 else False)
    if isinstance(value, dict):
        if value.get('isError') or value.get('is_error'):
            return False
        return (all(value.get(key) == expected[key] for key in ('uuid', 'name', 'content'))
                or any(has_memory_record(child, expected) for child in value.values()))
    return isinstance(value, list) and any(has_memory_record(child, expected) for child in value)


class MemoryClient:
    def __init__(self, project):
        self.process = subprocess.Popen(['ai-sandbox', 'shell', project, 'python3', '-I',
            '/usr/local/lib/ai-bootstrap/memory-client'], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.sequence = 0
        self.request('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                                   'clientInfo': {'name': 'acceptance', 'version': '1'}})
        self.process.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        self.process.stdin.flush()

    def request(self, method, params):
        self.sequence += 1
        self.process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': self.sequence,
                                             'method': method, 'params': params}) + '\n')
        self.process.stdin.flush()
        if not select.select([self.process.stdout], [], [], 300)[0]:
            raise AssertionError('Memory MCP timed out')
        response = json.loads(self.process.stdout.readline())
        assert response.get('id') == self.sequence and 'error' not in response, 'MCP protocol failure'
        return response['result']

    def call(self, tool_name, **arguments):
        result = self.request('tools/call', {'name': tool_name, 'arguments': arguments})
        assert not result.get('isError'), f'Memory tool failed: {tool_name}'
        return json.loads(result['content'][0]['text'])

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=10)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
