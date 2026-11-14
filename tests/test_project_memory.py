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
        if getattr(self.server, 'fail', False):
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'private-provider-key: confidential upstream details')
            return
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path.endswith('/embeddings'):
            response = {'data': [{'index': 0, 'embedding': [0.1, 0.2, 0.3], 'object': 'embedding'}],
                        'model': 'test', 'object': 'list', 'usage': {'prompt_tokens': 1, 'total_tokens': 1}}
        else:
            schema = body['response_format']['json_schema']['schema']
            fields = schema.get('properties', {})
            # External model boundary: an episode without named entities is valid.
            result = {name: [] for name, field in fields.items() if field.get('type') == 'array'}
            response = {'id': 'completion', 'object': 'chat.completion', 'created': 1,
                'model': 'test', 'choices': [{'index': 0, 'finish_reason': 'stop',
                    'message': {'role': 'assistant', 'content': json.dumps(result)}}]}
        payload = json.dumps(response).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
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
        self.addAsyncCleanup(self.close_memory)
        self.socket_path = self.root / 'memory.sock'
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(self.socket_path))
        self.listener.listen()
        self.server = await project_memory.serve(self.listener, self.memory, os.getuid())
        self.addAsyncCleanup(self.stop_server)

    async def stop_server(self):
        self.server.close()
        await self.server.wait_closed()

    async def close_memory(self):
        await self.memory.close()

    async def call(self, name, arguments, socket_path=None):
        reader, writer = await asyncio.open_unix_connection(str(socket_path or self.socket_path))
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

    async def test_deleting_a_memory_makes_it_unavailable(self):
        added = await self.call('add_memory', {'name': 'Temporary', 'episode_body': 'Discard this note.'})
        identity = json.loads(added['content'][0]['text'])['uuid']
        deleted = await self.call('delete_episode', {'uuid': identity})
        self.assertFalse(deleted.get('isError'), deleted)
        self.assertTrue((await self.call('get_episode', {'uuid': identity})).get('isError'))
        self.assertEqual(json.loads((await self.call('get_episodes', {}))['content'][0]['text'])['episodes'], [])

    async def test_other_project_cannot_read_or_delete_memory_even_with_its_uuid(self):
        other = project_memory.Memory('gh-99', self.root / 'other', self.settings, 'private-provider-key')
        await other.start()
        self.addAsyncCleanup(other.close)
        other_socket = self.root / 'other.sock'
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(other_socket))
        listener.listen()
        other_server = await project_memory.serve(listener, other, os.getuid())
        self.addCleanup(other_server.close)
        added = await self.call('add_memory', {'name': 'Private', 'episode_body': 'Purple is our secret color.'})
        identity = json.loads(added['content'][0]['text'])['uuid']
        for name in ('get_episode', 'delete_episode'):
            with self.subTest(name=name):
                self.assertTrue((await self.call(name, {'uuid': identity}, other_socket)).get('isError'))
        self.assertEqual(json.loads((await self.call('get_episodes', {}, other_socket))['content'][0]['text'])['episodes'], [])
        self.assertFalse((await self.call('get_episode', {'uuid': identity})).get('isError'))

    async def test_acknowledged_memory_survives_service_restart(self):
        added = await self.call('add_memory', {'name': 'Persistent', 'episode_body': 'Keep purple across restarts.'})
        identity = json.loads(added['content'][0]['text'])['uuid']
        await self.stop_server()
        await self.memory.close()
        self.memory = project_memory.Memory('gh-12', self.root / 'data', self.settings, 'private-provider-key')
        await self.memory.start()
        self.socket_path.unlink(missing_ok=True)
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(self.socket_path))
        listener.listen()
        self.server = await project_memory.serve(listener, self.memory, os.getuid())
        found = await self.call('get_episode', {'uuid': identity})
        self.assertFalse(found.get('isError'), found)
        self.assertEqual(json.loads(found['content'][0]['text'])['content'], 'Keep purple across restarts.')

    async def test_search_is_scoped_to_project_and_rejects_namespace_override(self):
        empty = await self.call('search_memory_facts', {'query': 'button color'})
        self.assertFalse(empty.get('isError'), empty)
        self.assertEqual(json.loads(empty['content'][0]['text']), {'facts': []})
        denied = await self.call('search_memory_facts', {'query': 'secret', 'group_ids': ['gh-99']})
        self.assertTrue(denied.get('isError'), denied)

    async def test_provider_failure_does_not_expose_credentials_or_claim_success(self):
        setattr(self.provider, 'fail', True)
        failed = await self.call('search_memory_facts', {'query': 'anything'})
        self.assertTrue(failed.get('isError'), failed)
        self.assertNotIn('private-provider-key', json.dumps(failed))
        self.assertNotIn('confidential', json.dumps(failed))

    async def test_socket_rejects_a_different_project_user(self):
        await self.stop_server()
        self.socket_path.unlink(missing_ok=True)
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(self.socket_path))
        listener.listen()
        self.server = await project_memory.serve(listener, self.memory, os.getuid() + 1)
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        try:
            self.assertEqual(await asyncio.wait_for(reader.read(), 3), b'')
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_official_mcp_client_can_discover_and_call_memory_tools(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        async with stdio_client(StdioServerParameters(command='/usr/bin/socat',
                args=['STDIO', 'UNIX-CONNECT:' + str(self.socket_path)])) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                listing = await client.list_tools()
                self.assertIn('add_memory', [tool.name for tool in listing.tools])
                added = await client.call_tool('add_memory', {'name': 'SDK', 'episode_body': 'Shared via standard MCP.'})
                self.assertFalse(added.isError)
                records = await client.call_tool('get_episodes', {})
                self.assertFalse(records.isError)
                self.assertIn('Shared via standard MCP.', str(records.content))


if __name__ == '__main__':
    unittest.main()
