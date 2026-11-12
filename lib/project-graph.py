"""One persistent CGC process per sandbox, shared by local stdio MCP clients."""
import json
import os
from pathlib import Path
import select
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time

SOCKET = '/run/ai-codegraph.sock'
PID = Path('/run/ai-codegraph.pid')


def client():
    with socket.socket(socket.AF_UNIX) as connection:
        connection.connect(SOCKET)
        with connection.makefile('rb') as reader:
            for line in sys.stdin.buffer:
                message = json.loads(line)
                connection.sendall(line)
                if 'id' in message:
                    answer = reader.readline()
                    if not answer:
                        raise RuntimeError('Project graph disconnected')
                    sys.stdout.buffer.write(answer)
                    sys.stdout.buffer.flush()


class Graph:
    def __init__(self):
        environment = dict(os.environ,
            DEFAULT_DATABASE='falkordb', CGC_RUNTIME_DB_TYPE='falkordb',
            CGC_RUNTIME_DB_PATH='/root/.local/share/ai-codegraph/graph.rdb',
            FALKORDB_PORT='0', FALKORDB_SOCKET_PATH='/run/ai-codegraph-db.sock', PARALLEL_WORKERS='2',
            CGC_IGNORE_PROJECT_ENV='1', CGC_ALLOWED_ROOTS='/workspace/repo',
            CGC_OUTPUT_FORMAT='json', OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='2')
        self.process = subprocess.Popen(['/opt/ai-codegraph/bin/python', '-I',
            '/opt/ai-project-tools/codegraph-worker.py'],
            cwd='/workspace/repo', env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            bufsize=0)
        self.lock = threading.Lock()
        self.sequence = 0

    def request(self, message, timeout=180):
        with self.lock:
            self.sequence += 1
            identity = self.sequence
            self.process.stdin.write((json.dumps(dict(message, jsonrpc='2.0', id=identity)) + '\n').encode())
            deadline = time.monotonic() + timeout if timeout is not None else None
            # Unbuffered byte reads avoid hiding a second line behind select().
            line = bytearray()
            while deadline is None or time.monotonic() < deadline:
                remaining = max(0, deadline - time.monotonic()) if deadline is not None else None
                if not select.select([self.process.stdout], [], [], remaining)[0]:
                    break
                char = self.process.stdout.read(1)
                if not char:
                    raise RuntimeError('CodeGraphContext exited')
                line.extend(char)
                if char != b'\n':
                    continue
                try:
                    response = json.loads(line)
                except ValueError:
                    line.clear()
                    continue
                line.clear()
                if isinstance(response, dict) and response.get('id') == identity:
                    response['id'] = message.get('id')
                    return response
            raise TimeoutError('CodeGraphContext did not respond')

    def tool(self, name, arguments, timeout=180):
        response = self.request({'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}}, timeout)
        if 'error' in response or response.get('result', {}).get('isError'):
            raise RuntimeError(f'CodeGraphContext {name} failed: {response}')
        result = json.loads(response['result']['content'][0]['text'])
        if result.get('error') or result.get('success') is False:
            raise RuntimeError(f'CodeGraphContext {name} failed: {result}')
        return result

    def prepare(self):
        self.request({'method': 'initialize', 'params': {'protocolVersion': '2025-03-26',
            'capabilities': {}, 'clientInfo': {'name': 'ai-bootstrap', 'version': '1'}}})
        # CGC synchronizes existing repositories here and starts an index job for new ones.
        # Existing large repositories synchronize synchronously in this pinned CGC release.
        # Keep that work alive when a provisioning request times out; a retry reuses it.
        job = self.tool('watch_directory', {'path': '/workspace/repo'}, timeout=None)
        previous = None
        while job.get('job_id'):
            result = self.tool('check_job_status', {'job_id': job['job_id']})
            status = result.get('job', result)
            state = status.get('status')
            if state != previous:
                print(f'Project graph index: {state}', file=sys.stderr, flush=True)
                previous = state
            if state == 'completed':
                break
            if state not in ('pending', 'running'):
                raise RuntimeError(f'Initial graph index failed: {status}')
            time.sleep(1)

    def close(self):
        # EOF runs CGC's own watcher shutdown and database checkpoint.
        self.process.stdin.close()
        try:
            self.process.wait(timeout=90)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


def serve():
    PID.write_text(str(os.getpid()))
    graph = Graph()
    def stop(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    try:
        graph.prepare()
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                for line in self.rfile:
                    message = json.loads(line)
                    if 'id' not in message:
                        continue
                    try:
                        response = graph.request(message)
                    except (OSError, RuntimeError, TimeoutError):
                        # A broken shared worker must be replaced by systemd.
                        os.kill(os.getpid(), signal.SIGTERM)
                        return
                    self.wfile.write((json.dumps(response) + '\n').encode())
                    self.wfile.flush()
        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True
        Path(SOCKET).unlink(missing_ok=True)
        with Server(SOCKET, Handler) as server:
            os.chmod(SOCKET, 0o600)
            server.serve_forever()
    finally:
        graph.close()
        Path(SOCKET).unlink(missing_ok=True)
        PID.unlink(missing_ok=True)


if __name__ == '__main__':
    mode = sys.argv[1]
    if mode == 'client':
        client()
    elif mode == 'run':
        serve()
    elif mode == 'stop':
        if PID.exists():
            pid = int(PID.read_text())
            process = Path(f'/proc/{pid}/cmdline')
            if process.exists() and b'/opt/ai-project-tools/graph.py' in process.read_bytes():
                os.kill(pid, signal.SIGTERM)
    elif mode == 'ready':
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(10)
            connection.connect(SOCKET)
            connection.sendall(b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n')
            with connection.makefile('rb') as reader:
                assert json.loads(reader.readline())['result']['tools']
    else:
        sys.exit('Expected run, stop, ready or client')
