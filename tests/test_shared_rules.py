"""Owner HTTP forms and read-only startup interfaces for shared rules."""
import asyncio
from contextlib import closing
from html import unescape
import http.client
import json
from pathlib import Path
import re
import socket
import tempfile
import threading
import unittest

import test_portal
from portal import Portal, SESSION_LIFETIME
from shared_rules import Rules
from codex_service import serve
import model_gateway


def forms(page):
    result = []
    for action, body in re.findall(r'<form method="post" action="([^"]+)">(.*?)</form>', page, re.S):
        fields = dict((name, unescape(value)) for name, value in re.findall(r'name="([^"]+)" value="([^"]*)"', body))
        content = re.search(r'<textarea[^>]*name="content"[^>]*>(.*?)</textarea>', body, re.S)
        if content:
            fields['content'] = unescape(content.group(1))
        result.append((action, fields))
    return result


class OwnerRulesTest(unittest.TestCase):
    setUp = test_portal.PortalTest.setUp
    activation = test_portal.PortalTest.activation
    request = test_portal.PortalTest.request
    code = test_portal.PortalTest.code
    enroll = test_portal.PortalTest.enroll

    def new_portal(self, *, secure=False):
        rules_path = self.state / 'rules.sqlite3'
        Rules(rules_path).initialize()
        return Portal(self.state, self.key, clock=lambda: self.time, secure=secure, rules_path=rules_path)

    def page(self, cookie):
        status, headers, body = self.request('/rules', cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        return body.decode()

    def submit(self, cookie, action, *, content=None, fields=None):
        fields = fields or next(fields for path, fields in forms(self.page(cookie)) if path == '/rules/' + action)
        if content is not None:
            fields = {**fields, 'content': content}
        return self.request('/rules/' + action, cookie=cookie, form=fields)

    def test_owner_can_add_edit_and_delete_rules_and_reinitialization_preserves_them(self):
        cookie, _, _ = self.enroll()
        text = 'First rule: <script> & "\nСохраняйте контекст проекта. 🧭'
        self.assertEqual(self.submit(cookie, 'add', content=text)[0], 303)
        page = self.page(cookie)
        self.assertNotIn('<script>', page)
        saved = next(fields for path, fields in forms(page) if path == '/rules/save')
        self.assertEqual(saved['content'], text)
        self.assertEqual(self.submit(cookie, 'save', content='Updated rule', fields=saved)[0], 303)
        self.portal = self.new_portal()
        saved = next(fields for path, fields in forms(self.page(cookie)) if path == '/rules/save')
        self.assertEqual(saved['content'], 'Updated rule')
        self.assertEqual(self.submit(cookie, 'delete')[0], 303)
        self.portal = self.new_portal()
        self.assertNotIn('/rules/save', self.page(cookie))

    def test_anonymous_csrf_and_stale_forms_cannot_change_rules(self):
        cookie, _, _ = self.enroll()
        for path in ('/rules', '/rules/add', '/rules/save', '/rules/delete'):
            self.assertEqual(self.request(path)[0], 404)
            self.assertEqual(self.request(path, form={'csrf': 'forged', 'content': 'forged'})[0], 404)
        addition = next(fields for path, fields in forms(self.page(cookie)) if path == '/rules/add')
        for csrf in ('wrong', 'я' * 64):
            self.assertEqual(self.submit(cookie, 'add', content='forged', fields={**addition, 'csrf': csrf})[0], 403)
        self.assertEqual(self.submit(cookie, 'add', content='Original')[0], 303)
        old = next(fields for path, fields in forms(self.page(cookie)) if path == '/rules/save')
        self.assertEqual(self.submit(cookie, 'save', content='New version', fields=old)[0], 303)
        status, _, body = self.submit(cookie, 'save', content='Unsaved text', fields=old)
        self.assertEqual(status, 409)
        self.assertIn(b'Unsaved text', body)
        stale_delete = {key: value for key, value in old.items() if key != 'content'}
        self.assertEqual(self.submit(cookie, 'delete', fields={**stale_delete, 'confirm': 'yes'})[0], 409)
        self.assertEqual(self.submit(cookie, 'delete', fields=stale_delete)[0], 400)
        self.assertIn('New version', self.page(cookie))

    def test_unicode_limits_validation_and_session_renewal(self):
        cookie, _, _ = self.enroll()
        maximum = '🧭' * 4000
        self.assertEqual(self.submit(cookie, 'add', content=maximum)[0], 303)
        self.assertEqual(next(fields for path, fields in forms(self.page(cookie)) if path == '/rules/save')['content'], maximum)
        for invalid in (' ' * 5, 'x' * 4001, 'prefix\x00suffix'):
            self.assertEqual(self.submit(cookie, 'add', content=invalid)[0], 400)
        for _ in range(3):
            self.assertEqual(self.submit(cookie, 'add', content=maximum)[0], 303)
        self.assertEqual(self.submit(cookie, 'add', content='x')[0], 400)
        self.time += SESSION_LIFETIME - 10
        status, headers, _ = self.request('/rules', cookie=cookie,
            headers={'HTTP_SEC_FETCH_MODE': 'navigate', 'HTTP_SEC_FETCH_DEST': 'document'})
        self.assertEqual(status, 200)
        self.assertIn('Set-Cookie', headers)
        self.time += 20
        self.assertEqual(self.request('/rules', cookie=cookie)[0], 200)

    def test_gateway_startups_read_current_rules_for_two_projects_but_cannot_write(self):
        config = self.state / 'gateway.json'
        config.write_text(json.dumps({
            'providers': {'fixture': {'base_url': 'https://example.invalid/v1', 'key_file': '/unused/provider.key'}},
            'models': {'model': {'provider': 'fixture', 'model': 'fixture'}},
            'defaults': {'models': ['model'], 'requests_per_day': 1, 'max_output_tokens': 256}, 'projects': {}}))
        gateway_state = self.state / 'gateway'
        clients = [model_gateway.provision(config, gateway_state, project) for project in ('alpha', 'beta')]
        server = model_gateway.make_server(config, gateway_state, ('127.0.0.1', 0), rules_path=self.state / 'rules.sqlite3')
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(worker.join)
        self.addCleanup(server.shutdown)

        def read(client, *, method='GET', token=None):
            with closing(http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)) as connection:
                connection.request(method, '/v1/platform/rules', headers={'Authorization': 'Bearer ' + (token or client['api_key'])})
                response = connection.getresponse()
                return response.status, json.loads(response.read())

        cookie, _, _ = self.enroll()
        self.assertEqual(read(clients[0], token='forged')[0], 401)
        self.assertEqual(read(clients[0])[1]['text'], '')
        for content, action in [('Version one 🧭', 'add'), ('Version two', 'save')]:
            self.assertEqual(self.submit(cookie, action, content=content)[0], 303)
            for client in clients:
                status, payload = read(client)
                self.assertEqual(status, 200)
                self.assertEqual(payload['text'], 'Общие правила владельца для всех проектов:\n\n' + content)
                self.assertEqual(read(client, method='POST')[0], 404)
        self.assertEqual(self.submit(cookie, 'delete')[0], 303)
        self.assertEqual(read(clients[1])[1]['text'], '')
        (self.state / 'rules.sqlite3').unlink()
        status, payload = read(clients[0])
        self.assertEqual(status, 503)
        self.assertNotIn(str(self.state), str(payload))


FAKE_RULE_MODEL = '''#!/usr/bin/python3
import json,sys
instructions = None
for line in sys.stdin:
    request=json.loads(line)
    if 'id' not in request: continue
    method=request['method']
    result={}
    if method=='account/read': result={'account': {'type':'chatgpt'}}
    elif method in ('thread/start','thread/resume'):
        instructions=request['params'].get('developerInstructions')
        result={'thread':{'id':'fixture-thread'},'model':'fixture-model'}
    elif method=='turn/start': result={'turn':{'id':'fixture-turn'}}
    print(json.dumps({'id':request['id'],'result':result}),flush=True)
    if method=='turn/start':
        print(json.dumps({'method':'item/agentMessage/delta','params':{
            'threadId':'fixture-thread','delta':instructions}}),flush=True)
        print(json.dumps({'method':'turn/completed','params':{
            'threadId':'fixture-thread','turn':{'id':'fixture-turn','status':'completed'}}}),flush=True)
'''


class CodexRulesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.owner = OwnerRulesTest()
        self.owner.setUp()
        self.addCleanup(self.owner.doCleanups)
        self.cookie, _, _ = self.owner.enroll()
        self.root = self.owner.state
        self.binary = self.root / 'fake-model'
        self.binary.write_text(FAKE_RULE_MODEL)
        self.binary.chmod(0o700)
        self.sockets = []
        for project in ('gh-12', 'gh-99'):
            (self.root / 'projects' / project / 'chatgpt').mkdir(parents=True)
            listener = socket.socket(socket.AF_UNIX)
            path = self.root / (project + '.sock')
            listener.bind(str(path))
            task = asyncio.create_task(serve(listener, project, state=self.root, binary=str(self.binary),
                rules_path=self.root / 'rules.sqlite3'))
            async def close(task=task, listener=listener):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                listener.close()
            self.addAsyncCleanup(close)
            self.sockets.append(path)
        await asyncio.sleep(0.02)

    async def start_and_prompt(self, path, *, resume=False):
        reader, writer = await asyncio.open_unix_connection(str(path))
        try:
            params = {'cwd': '/workspace/repo', 'mcpServers': [], 'developerInstructions': 'FORGED'}
            if resume:
                params['sessionId'] = 'fixture-thread'
            writer.write(json.dumps({'id': 1, 'method': 'session/load' if resume else 'session/new', 'params': params}).encode() + b'\n')
            await writer.drain()
            result = json.loads(await asyncio.wait_for(reader.readline(), 5))
            if 'error' in result:
                return result
            writer.write(b'{"id":2,"method":"session/prompt","params":{"sessionId":"fixture-thread","prompt":[{"type":"text","text":"startup instructions"}]}}\n')
            await writer.drain()
            answer = json.loads(await asyncio.wait_for(reader.readline(), 5))
            done = json.loads(await asyncio.wait_for(reader.readline(), 5))
            self.assertEqual(done['result']['stopReason'], 'end_turn')
            return answer['params']['update']['content']['text']
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_new_and_loaded_sessions_receive_latest_owner_rules_not_client_overrides(self):
        self.assertEqual(self.owner.submit(self.cookie, 'add', content='Use Russian. 🧭')[0], 303)
        for path in self.sockets:
            self.assertEqual(await self.start_and_prompt(path),
                'Общие правила владельца для всех проектов:\n\nUse Russian. 🧭')
        self.assertEqual(self.owner.submit(self.cookie, 'save', content='Use concise Russian.')[0], 303)
        for path in self.sockets:
            self.assertEqual(await self.start_and_prompt(path, resume=True),
                'Общие правила владельца для всех проектов:\n\nUse concise Russian.')
        self.assertEqual(self.owner.submit(self.cookie, 'delete')[0], 303)
        for path in self.sockets:
            self.assertEqual(await self.start_and_prompt(path), '')

    async def test_missing_rules_stop_codex_instead_of_silently_starting_without_them(self):
        (self.root / 'rules.sqlite3').unlink()
        result = await self.start_and_prompt(self.sockets[0])
        self.assertIn('error', result)
        self.assertNotIn(str(self.root), str(result))


if __name__ == '__main__':
    unittest.main()
