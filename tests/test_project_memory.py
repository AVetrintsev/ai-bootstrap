"""Exercise project memory over MCP with real Graphiti and FalkorDB on Linux."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import project_memory


class Provider(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        schema = body['response_format']['json_schema']['schema']
        fields = schema.get('properties', {})
        # External model boundary: an episode without named entities is valid.
        result = {name: [] for name, field in fields.items() if field.get('type') == 'array'}
        payload = json.dumps({'id': 'completion', 'object': 'chat.completion', 'created': 1,
            'model': 'test', 'choices': [{'index': 0, 'finish_reason': 'stop',
                'message': {'role': 'assistant', 'content': json.dumps(result)}}]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_):
        pass


class MemoryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        threading.Thread(target=self.provider.serve_forever, daemon=True).start()
        self.addCleanup(self.provider.server_close)
        self.addCleanup(self.provider.shutdown)
        self.settings = {'base_url': f'http://127.0.0.1:{self.provider.server_port}/v1',
            'model': 'test', 'embedding_model': 'test', 'embedding_dimensions': 3}
        self.memory = project_memory.Memory('gh-12', self.root / 'data', self.settings, 'private-provider-key')
        await self.memory.start()
        self.addAsyncCleanup(self.memory.close)
        self.socket_path = self.root / 'memory.sock'
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(self.socket_path))
        self.listener.listen()
        self.server = await project_memory.serve(self.listener, self.memory, os.getuid())
        self.addAsyncCleanup(self.stop_server)

    async def stop_server(self):
        self.server.close()
        await self.server.wait_closed()

    async def call(self, name, arguments):
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        try:
            writer.write(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                'params': {'protocolVersion': '2025-03-26', 'capabilities': {},
                           'clientInfo': {'name': 'test', 'version': '1'}}}).encode() + b'\n')
            await writer.drain()
            initialized = json.loads(await reader.readline())
            self.assertNotIn('error', initialized)
            writer.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            writer.write(json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                'params': {'name': name, 'arguments': arguments}}).encode() + b'\n')
            await writer.drain()
            response = json.loads(await asyncio.wait_for(reader.readline(), 30))
            return response['result']
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_memory_written_by_one_client_is_visible_to_another(self):
        added = await self.call('add_memory', {'name': 'Decision', 'episode_body': 'Use purple buttons.'})
        self.assertFalse(added.get('isError'), added)
        retrieved = await self.call('get_episodes', {})
        self.assertFalse(retrieved.get('isError'), retrieved)
        episodes = json.loads(retrieved['content'][0]['text'])['episodes']
        self.assertEqual([(item['name'], item['content']) for item in episodes],
                         [('Decision', 'Use purple buttons.')])

    async def test_reading_by_uuid_and_rejecting_project_override(self):
        added = await self.call('add_memory', {'name': 'Decision', 'episode_body': 'Use purple buttons.'})
        identity = json.loads(added['content'][0]['text'])['uuid']
        found = await self.call('get_episode', {'uuid': identity})
        self.assertFalse(found.get('isError'), found)
        self.assertEqual(json.loads(found['content'][0]['text'])['content'], 'Use purple buttons.')
        for name, args in (
            ('add_memory', {'name': 'foreign', 'episode_body': 'secret', 'group_id': 'gh-99'}),
            ('get_episodes', {'group_ids': ['gh-99']}),
            ('get_episode', {'uuid': identity, 'project_id': 'gh-99'}),
        ):
            with self.subTest(name=name):
                denied = await self.call(name, args)
                self.assertTrue(denied.get('isError'), denied)
        self.assertEqual(len(json.loads((await self.call('get_episodes', {}))['content'][0]['text'])['episodes']), 1)


if __name__ == '__main__':
    unittest.main()
