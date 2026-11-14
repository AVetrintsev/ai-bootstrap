"""Exercise agent MCP and owner HTTP boundaries with real Graphiti/FalkorDB."""
import asyncio
import base64
from html import unescape
from http.server import ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import threading
import unittest
from urllib.parse import urlencode
from wsgiref.util import setup_testing_defaults

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import portal
import project_memory
import shared_memory
from test_project_memory import Provider


class SharedMemoryTest(unittest.IsolatedAsyncioTestCase):
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
        self.projects = {}
        self.shared = shared_memory.SharedMemory(self.root / 'shared')
        await self.shared.start()
        self.addAsyncCleanup(self.close_shared)
        self.agent_socket = self.root / 'agents.sock'
        self.owner_socket = self.root / 'owner.sock'
        self.shared_servers = []
        await self.start_shared_servers()
        self.addAsyncCleanup(self.stop_shared_servers)
        (self.root / 'portal').mkdir()
        self.clock = 1_700_000_010
        self.portal = portal.Portal(self.root / 'portal', bytes(range(32)), clock=lambda: self.clock,
                                    shared_memory_socket=str(self.owner_socket))
        self.portal.owner.initialize()
        activation = self.portal.owner.provision()
        _, _, page = await self.http(activation)
        secret = re.search(r'<code>([A-Z2-7]+)</code>', page).group(1).encode()
        status, headers, _ = await self.http(activation,
            form={'code': portal.totp(base64.b32decode(secret), self.clock // 30)})
        self.assertEqual(status, 303)
        self.cookie = headers['Set-Cookie'].split(';', 1)[0]

    def listener(self, path):
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(path))
        listener.listen()
        return listener

    async def start_shared_servers(self):
        for path, owner in ((self.agent_socket, False), (self.owner_socket, True)):
            server = await shared_memory.serve(self.listener(path), self.shared, os.getuid(), owner=owner)
            self.shared_servers.append(server)

    async def stop_shared_servers(self):
        for server in self.shared_servers:
            server.close()
            await server.wait_closed()
        self.shared_servers = []
        for path in (self.agent_socket, self.owner_socket):
            path.unlink(missing_ok=True)

    async def close_shared(self):
        await self.shared.close()

    async def mcp(self, project_name, tool, **arguments):
        if project_name not in self.projects:
            memory = project_memory.Memory(project_name, self.root / project_name, self.settings, 'private-test-key',
                                            shared_socket=str(self.agent_socket))
            await memory.start()
            self.addAsyncCleanup(memory.close)
            server = await project_memory.serve(self.listener(self.root / (project_name + '.sock')), memory, os.getuid())
            async def stop():
                server.close()
                await server.wait_closed()
            self.addAsyncCleanup(stop)
            self.projects[project_name] = memory
        reader, writer = await asyncio.open_unix_connection(str(self.root / (project_name + '.sock')), limit=1048576)
        try:
            for identity, method, params in (
                (1, 'initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                                  'clientInfo': {'name': 'acceptance', 'version': '1'}}),
                (2, 'tools/call', {'name': tool, 'arguments': arguments}),
            ):
                writer.write(json.dumps({'jsonrpc': '2.0', 'id': identity,
                                         'method': method, 'params': params}).encode() + b'\n')
                await writer.drain()
                response = json.loads(await asyncio.wait_for(reader.readline(), 30))
                self.assertNotIn('error', response)
            result = response['result']
            return result if result.get('isError') else json.loads(result['content'][0]['text'])
        finally:
            writer.close()
            await writer.wait_closed()

    async def http(self, path, *, form=None, cookie='', headers=None):
        def request():
            environ = {}
            setup_testing_defaults(environ)
            route, _, query = path.partition('?')
            environ.update(PATH_INFO=route, QUERY_STRING=query, HTTP_COOKIE=cookie,
                           HTTP_X_PORTAL_CLIENT_IP='127.0.0.1', **(headers or {}))
            if form is not None:
                body = urlencode(form).encode()
                environ.update(REQUEST_METHOD='POST', CONTENT_TYPE='application/x-www-form-urlencoded',
                               CONTENT_LENGTH=str(len(body)))
                environ['wsgi.input'] = BytesIO(body)
            response = []
            body = self.portal(environ, lambda status, headers: response.extend(
                (int(status.split()[0]), dict(headers))))
            return *response, b''.join(body).decode()
        return await asyncio.to_thread(request)

    def proposal_form(self, page, identity):
        article = re.search(r'<article aria-labelledby="proposal-' + identity + r'">(.*?)</article>',
                            page, re.S).group(1)
        fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]+)"', article))
        return {**fields, 'confirm': 'yes'}

    async def test_owner_publishes_exact_proposal_then_both_projects_can_read_it(self):
        text = '\nСохраняйте  два пробела,\n\tпереносы и <script>& "🙂" дословно.\n'
        proposed = await self.mcp('gh-12', 'propose_shared_memory', name='Решение', episode_body=text)
        self.assertEqual(proposed['status'], 'pending')
        identity = proposed['uuid']
        for project in ('gh-12', 'gh-99'):
            self.assertEqual((await self.mcp(project, 'get_shared_memories'))['episodes'], [])
            self.assertTrue((await self.mcp(project, 'get_shared_memory', uuid=identity))['isError'])
        status, _, page = await self.http('/memory', cookie=self.cookie)
        self.assertEqual(status, 200)
        displayed = unescape(re.search(r'<pre><code>(.*?)</code></pre>', page, re.S).group(1))
        self.assertEqual(displayed, text)
        self.assertIn('Опубликовать этот текст', page)
        fields = self.proposal_form(page, identity)
        self.assertEqual((await self.http('/memory/approve', cookie=self.cookie, form=fields))[0], 200)
        for project in ('gh-12', 'gh-99'):
            self.assertEqual(await self.mcp(project, 'get_shared_memory', uuid=identity),
                             {'uuid': identity, 'name': 'Решение', 'content': text})
        self.assertEqual((await self.http('/memory/approve', cookie=self.cookie, form=fields))[0], 200)
        records = (await self.mcp('gh-99', 'get_shared_memories'))['episodes']
        self.assertEqual([record['uuid'] for record in records], [identity])

    async def test_agent_and_unconfirmed_owner_requests_cannot_publish_or_replace_text(self):
        proposed = await self.mcp('gh-12', 'propose_shared_memory', name='Review', episode_body='Only this text.')
        identity = proposed['uuid']
        _, _, page = await self.http('/memory', cookie=self.cookie)
        fields = self.proposal_form(page, identity)
        for tool, arguments in (
            ('approve_proposal', {'uuid': identity, 'revision': fields['revision']}),
            ('propose_shared_memory', {'name': 'Forged', 'episode_body': 'Replace it', 'project': 'gh-99'}),
            ('propose_shared_memory', {'name': 'Forged', 'episode_body': 'Replace it', 'approved': True}),
            ('get_shared_memories', {'group_ids': ['gh-99']}),
        ):
            self.assertTrue((await self.mcp('gh-12', tool, **arguments))['isError'])
        self.assertEqual((await self.http('/memory'))[0], 404)
        self.assertEqual((await self.http('/memory/approve', form=fields))[0], 404)
        self.assertEqual((await self.http('/memory/approve', cookie=self.cookie))[0], 404)
        for form, expected in (
            ({**fields, 'csrf': 'wrong'}, 403),
            ({k: v for k, v in fields.items() if k != 'confirm'}, 400),
            ({**fields, 'episode_body': 'Replacement'}, 403),
            ({**fields, 'revision': '0' * 64}, 503),
        ):
            self.assertEqual((await self.http('/memory/approve', cookie=self.cookie, form=form))[0], expected)
            self.assertEqual((await self.mcp('gh-99', 'get_shared_memories'))['episodes'], [])
        # Even a trusted project-service caller cannot approve over its socket.
        reader, writer = await asyncio.open_unix_connection(str(self.agent_socket))
        try:
            writer.write(json.dumps({'action': 'approve_proposal', 'uuid': identity,
                'revision': fields['revision'], 'project': 'gh-12'}).encode() + b'\n')
            await writer.drain()
            self.assertIn('error', json.loads(await reader.readline()))
        finally:
            writer.close()
            await writer.wait_closed()
        self.assertEqual((await self.mcp('gh-99', 'get_shared_memories'))['episodes'], [])

    async def test_maximum_unicode_text_can_be_proposed_and_published_exactly(self):
        name, text = '🙂' * 160, '🙂' * 4000
        proposed = await self.mcp('gh-12', 'propose_shared_memory', name=name, episode_body=text)
        self.assertEqual(proposed.get('status'), 'pending', proposed)
        _, _, page = await self.http('/memory', cookie=self.cookie)
        self.assertIn(text, page)
        fields = self.proposal_form(page, proposed['uuid'])
        self.assertEqual((await self.http('/memory/approve', cookie=self.cookie, form=fields))[0], 200)
        self.assertEqual((await self.mcp('gh-99', 'get_shared_memory', uuid=proposed['uuid']))['content'], text)

    async def test_pending_and_published_records_survive_service_restart(self):
        pending = await self.mcp('gh-12', 'propose_shared_memory', name='Pending', episode_body='Not public yet.')
        published = await self.mcp('gh-12', 'propose_shared_memory', name='Published', episode_body='Keep this exact text.')
        _, _, page = await self.http('/memory', cookie=self.cookie)
        fields = self.proposal_form(page, published['uuid'])
        self.assertEqual((await self.http('/memory/approve', cookie=self.cookie, form=fields))[0], 200)
        await self.stop_shared_servers()
        await self.shared.close()
        self.shared = shared_memory.SharedMemory(self.root / 'shared')
        await self.shared.start()
        await self.start_shared_servers()
        self.assertEqual((await self.mcp('gh-99', 'get_shared_memory', uuid=published['uuid']))['content'],
                         'Keep this exact text.')
        self.assertTrue((await self.mcp('gh-99', 'get_shared_memory', uuid=pending['uuid']))['isError'])
        _, _, page = await self.http('/memory', cookie=self.cookie)
        self.assertEqual(self.proposal_form(page, pending['uuid'])['uuid'], pending['uuid'])
        self.assertEqual((await self.http('/memory/approve', cookie=self.cookie, form=fields))[0], 200)
        self.assertEqual(len((await self.mcp('gh-99', 'get_shared_memories'))['episodes']), 1)

    async def test_publishing_a_proposal_does_not_expose_other_project_memories(self):
        private = await self.mcp('gh-12', 'add_memory', name='Private', episode_body='Secret purple decision.')
        proposed = await self.mcp('gh-12', 'propose_shared_memory', name='Public', episode_body='Public decision.')
        _, _, page = await self.http('/memory', cookie=self.cookie)
        self.assertEqual((await self.http('/memory/approve', cookie=self.cookie,
            form=self.proposal_form(page, proposed['uuid'])))[0], 200)
        self.assertEqual((await self.mcp('gh-12', 'get_episode', uuid=private['uuid']))['content'],
                         'Secret purple decision.')
        for tool in ('get_episode', 'delete_episode', 'get_shared_memory'):
            self.assertTrue((await self.mcp('gh-99', tool, uuid=private['uuid']))['isError'])
        self.assertTrue((await self.mcp('gh-12', 'get_episode', uuid=proposed['uuid']))['isError'])
        self.assertEqual((await self.mcp('gh-99', 'get_episodes'))['episodes'], [])
        self.assertEqual([item['content'] for item in (await self.mcp('gh-99', 'get_shared_memories'))['episodes']],
                         ['Public decision.'])

    async def test_only_foreground_visits_and_successful_approvals_renew_owner_session(self):
        proposed = await self.mcp('gh-12', 'propose_shared_memory', name='Decision', episode_body='Review before publish.')
        self.clock += 20 * 86400
        _, headers, _ = await self.http('/memory', cookie=self.cookie)
        self.assertNotIn('Set-Cookie', headers)
        _, headers, page = await self.http('/memory', cookie=self.cookie,
            headers={'HTTP_SEC_FETCH_MODE': 'navigate', 'HTTP_SEC_FETCH_DEST': 'document'})
        self.assertIn('Set-Cookie', headers)
        fields = self.proposal_form(page, proposed['uuid'])
        self.clock += 29 * 86400
        self.assertEqual((await self.http('/memory', cookie=self.cookie))[0], 200)
        status, headers, _ = await self.http('/memory/approve', cookie=self.cookie, form=fields)
        self.assertEqual(status, 200)
        self.assertIn('Set-Cookie', headers)
        self.clock += 29 * 86400
        self.assertEqual((await self.http('/memory', cookie=self.cookie))[0], 200)
        self.assertEqual((await self.http('/memory/approve', cookie=self.cookie,
            form={**fields, 'csrf': 'wrong'}))[0], 403)
        self.clock += 2 * 86400
        self.assertEqual((await self.http('/memory', cookie=self.cookie))[0], 404)

    async def test_both_shared_sockets_reject_an_untrusted_unix_user(self):
        for owner in (False, True):
            path = self.root / ('wrong-owner.sock' if owner else 'wrong-agent.sock')
            server = await shared_memory.serve(self.listener(path), self.shared, os.getuid() + 1, owner=owner)
            try:
                reader, writer = await asyncio.open_unix_connection(str(path))
                self.assertEqual(await asyncio.wait_for(reader.read(), 3), b'')
                writer.close()
                await writer.wait_closed()
            finally:
                server.close()
                await server.wait_closed()

    async def test_owner_and_agents_can_page_through_proposals_and_published_records(self):
        proposals = [await self.mcp('gh-12', 'propose_shared_memory',
            name=f'Proposal {index}', episode_body=f'Exact text {index}.') for index in range(21)]
        _, _, page = await self.http('/memory', cookie=self.cookie)
        self.assertEqual(page.count('<article '), 20)
        next_page = unescape(re.search(r'href="([^"]+)">Следующие предложения', page).group(1))
        _, _, last_page = await self.http(next_page, cookie=self.cookie)
        self.assertEqual(last_page.count('<article '), 1)
        self.assertEqual(self.proposal_form(last_page, proposals[-1]['uuid'])['uuid'], proposals[-1]['uuid'])
        for item in proposals[:3]:
            self.assertEqual((await self.http('/memory/approve', cookie=self.cookie,
                form=self.proposal_form(page, item['uuid'])))[0], 200)
        identities, arguments = [], {'num_results': 1}
        for _ in range(3):
            result = await self.mcp('gh-99', 'get_shared_memories', **arguments)
            identities.extend(item['uuid'] for item in result['episodes'])
            arguments = {'num_results': 1, 'cursor': result['next_cursor']}
        self.assertIsNone(result['next_cursor'])
        self.assertCountEqual(identities, [item['uuid'] for item in proposals[:3]])


if __name__ == '__main__':
    unittest.main()
