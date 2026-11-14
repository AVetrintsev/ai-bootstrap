"""Read-only startup instructions over the project's existing private socket."""
import json
import socket


def read_rules(socket_path='/run/ai-bootstrap-git/codex.sock'):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(15)
        connection.connect(str(socket_path))
        connection.sendall(b'{"jsonrpc":"2.0","id":1,"method":"platform/rules","params":{}}\n')
        with connection.makefile('rb') as reader:
            message = json.loads(reader.readline(262144))
        text = message.get('result', {}).get('text')
        if message.get('id') != 1 or not isinstance(text, str):
            raise ValueError('Shared rules unavailable')
        return text
