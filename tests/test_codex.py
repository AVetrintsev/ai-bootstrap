import asyncio
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from codex_service import serve
from shared_rules import Rules


FAKE_CODEX = '''#!/usr/bin/python3
import json,os,sys
from pathlib import Path
home=Path(os.environ['CODEX_HOME'])
for line in sys.stdin:
    req=json.loads(line)
    if 'id' not in req: continue
    scenario=json.loads((home/'scenario.json').read_text())
    method=req['method']
    result={}
    if method=='environment/info' and not scenario.get('executor',True):
        print(json.dumps({'id':req['id'],'error':{'code':-32603,'message':'Transport closed'}}),flush=True)
        continue
    if method=='account/read':
        result={'account': None if scenario['status']=='login_required' else {'type':'chatgpt','email':'private@example.test'}}
    elif method=='account/rateLimits/read':
        result={'ordinaryUsageAllowed':scenario['status']!='limit_reached','rateLimits':{}}
    elif method=='account/login/start':
        result={'type':'chatgptDeviceCode','loginId':'private-login','userCode':'TEST-CODE','verificationUrl':'https://auth.openai.com/codex/device'}
    elif method in ('thread/start','thread/resume'):
        settings=home/'settings.json'
        result={'thread':{'id':'fixture-thread'},'model':'fixture-model','reasoningEffort':'medium'}
        if method=='thread/resume' and settings.exists():
            result.update(json.loads(settings.read_text()))
    elif method=='model/list':
        result={'data':[
            {'model':'fixture-model','displayName':'Fixture','description':'Base','hidden':False,
             'defaultReasoningEffort':'medium','supportedReasoningEfforts':[{'reasoningEffort':'medium','description':'Balanced'}]},
            {'model':'fixture-other','displayName':'Other','description':'Other','hidden':False,
             'defaultReasoningEffort':'medium','supportedReasoningEfforts':[
                 {'reasoningEffort':'medium','description':'Balanced'},{'reasoningEffort':'high','description':'Deep'}]},
            {'model':'hidden-model','hidden':True},
        ]}
    elif method=='thread/settings/update':
        assert set(req['params']) <= {'threadId','model','effort'}
        settings=home/'settings.json'
        current=json.loads(settings.read_text()) if settings.exists() else {}
        if 'model' in req['params']: current['model']=req['params']['model']
        if 'effort' in req['params']: current['reasoningEffort']=req['params']['effort']
        settings.write_text(json.dumps(current))
    elif method=='turn/start':
        (home/'input.json').write_text(json.dumps(req['params']['input']))
        result={'turn':{'id':'fixture-turn'}}
    print(json.dumps({'id':req['id'],'result':result}),flush=True)
    if method=='turn/start':
        for event, params in (
            ('turn/plan/updated', {'plan':[{'step':'Read input','status':'inProgress'}]}),
            ('item/reasoning/summaryTextDelta', {'delta':'Reasoning summary'}),
            ('turn/completed', {'turn':{'id':'fixture-turn','status':'completed'}}),
        ):
            print(json.dumps({'method':event,'params':{'threadId':'fixture-thread',**params}}),flush=True)
'''


class CodexControlTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.rules = Rules(self.state / 'rules.sqlite3')
        self.rules.initialize()
        (self.state / 'auth').mkdir()
        self.scenario('login_required')
        self.binary = self.state / 'codex'
        self.binary.write_text(FAKE_CODEX)
        self.binary.chmod(0o700)
        self.api_key = self.state / 'api.key'
        self.path = self.state / 'control.sock'
        self.task = await self.start(self.path)

    def scenario(self, status):
        (self.state / 'auth' / 'scenario.json').write_text(json.dumps({'status': status}))

    async def start(self, path, project=None):
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(path))
        task = asyncio.create_task(serve(listener, project, state=self.state,
                                         binary=str(self.binary), api_key=self.api_key, rules_path=self.rules.path))
        async def cleanup():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            listener.close()
        self.addAsyncCleanup(cleanup)
        await asyncio.sleep(0.02)
        return task

    async def request(self, action, **fields):
        reader, writer = await asyncio.open_unix_connection(str(self.path))
        try:
            writer.write(json.dumps({'action': action, **fields}).encode() + b'\n')
            await writer.drain()
            return json.loads(await asyncio.wait_for(reader.readline(), 3))
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_login_and_limit_are_distinct_without_automatic_paid_fallback(self):
        self.api_key.write_text('private-api-credential')
        signed_out = await self.request('status')
        self.assertEqual(signed_out['status'], 'login_required')
        self.assertEqual(signed_out['profile'], 'chatgpt')
        login = await self.request('login')
        self.assertEqual(login['login']['userCode'], 'TEST-CODE')
        self.scenario('limit_reached')
        limited = await self.request('status')
        self.assertEqual(limited['status'], 'limit_reached')
        self.assertEqual(limited['profile'], 'chatgpt')
        self.assertNotIn('private', json.dumps(limited))
        switched = await self.request('profile', profile='api')
        self.assertEqual(switched['profile'], 'api')
        self.scenario('ready')
        self.assertEqual((await self.request('status'))['profile'], 'api')

    async def test_unconfigured_api_is_rejected_and_subscription_remains_default(self):
        self.assertIn('error', await self.request('profile', profile='api'))
        self.assertEqual((await self.request('status'))['profile'], 'chatgpt')
        self.assertIn('error', await self.request('account/login/start', apiKey='untrusted'))

    async def test_control_recovers_when_codex_becomes_available_again(self):
        self.binary.unlink()
        self.assertIn('error', await self.request('status'))
        self.assertIn('error', await self.request('status'))
        self.binary.write_text(FAKE_CODEX)
        self.binary.chmod(0o700)
        self.assertEqual((await self.request('status'))['status'], 'login_required')

    async def test_executor_failure_and_profile_change_complete_the_turn_instead_of_leaving_it_pending(self):
        home = self.state / 'projects/gh-123/chatgpt'
        home.mkdir(parents=True)
        scenario = home / 'scenario.json'
        scenario.write_text('{"status":"ready","executor":true}')
        path = self.state / 'project.sock'
        await self.start(path, 'gh-123')
        reader, writer = await asyncio.open_unix_connection(str(path))
        try:
            writer.write(b'{"id":1,"method":"session/new","params":{"cwd":"/workspace/repo","mcpServers":[]}}\n')
            await writer.drain()
            created = json.loads(await asyncio.wait_for(reader.readline(), 3))
            self.assertEqual(created['result']['sessionId'], 'fixture-thread')
            scenario.write_text('{"status":"ready","executor":false}')
            writer.write(b'{"id":2,"method":"session/prompt","params":{"sessionId":"fixture-thread","prompt":[{"type":"text","text":"write a file"}]}}\n')
            await writer.drain()
            message = json.loads(await asyncio.wait_for(reader.readline(), 3))
            self.assertIn('Исполнитель проекта недоступен', message['params']['update']['content']['text'])
            response = json.loads(await asyncio.wait_for(reader.readline(), 3))
            self.assertEqual(response['result']['stopReason'], 'refusal')
            self.assertEqual(response['result']['_meta']['ai-bootstrap/error'], 'executor_unavailable')
            scenario.write_text('{"status":"ready","executor":true}')
            (self.state / 'profile').write_text('api')
            writer.write(b'{"id":3,"method":"session/prompt","params":{"sessionId":"fixture-thread","prompt":[{"type":"text","text":"write a file"}]}}\n')
            await writer.drain()
            message = json.loads(await asyncio.wait_for(reader.readline(), 3))
            self.assertIn('Способ оплаты изменён', message['params']['update']['content']['text'])
            response = json.loads(await asyncio.wait_for(reader.readline(), 3))
            self.assertEqual(response['result']['stopReason'], 'refusal')
            self.assertEqual(response['result']['_meta']['ai-bootstrap/error'], 'profile_changed')
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_sandbox_protocol_cannot_change_auth_environment_or_host_configuration(self):
        path = self.state / 'project.sock'
        await self.start(path, 'gh-123')
        reader, writer = await asyncio.open_unix_connection(str(path))
        try:
            requests = [
                ('account/read', {}), ('account/login/start', {'type': 'apiKey', 'apiKey': 'untrusted'}),
                ('environment/add', {'id': 'local'}), ('fs/readFile', {'path': '/var/lib/ai-codex/auth/auth.json'}),
                ('session/new', {'cwd': '/var/lib/ai-codex', 'mcpServers': []}),
                ('session/new', {'cwd': '/workspace/repo', 'mcpServers': [{'command': 'sh'}]}),
                ('session/new', ['invalid']), ('session/prompt', {'prompt': [None]}),
                (['invalid-method'], {}),
                ('session/load', {'cwd': '/workspace/repo', 'sessionId': None}),
                ('session/load', {'cwd': '/workspace/repo', 'sessionId': {}}),
            ]
            for key, (method, params) in enumerate(requests):
                writer.write(json.dumps({'id': key, 'method': method, 'params': params}).encode() + b'\n')
                await writer.drain()
                result = json.loads(await asyncio.wait_for(reader.readline(), 3))
                self.assertEqual(result['id'], key)
                self.assertIn('error', result, method)
                self.assertNotIn('private', json.dumps(result))
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_project_switches_model_and_reasoning_in_place_and_resumes_them(self):
        home = self.state / 'projects/gh-123/chatgpt'
        home.mkdir(parents=True)
        (home / 'scenario.json').write_text('{"status":"ready"}')
        path = self.state / 'project.sock'
        await self.start(path, 'gh-123')
        project = {'cwd': '/workspace/repo', 'mcpServers': []}
        session = {'sessionId': 'fixture-thread'}

        async def request(method, params):
            writer.write(json.dumps({'id': 1, 'method': method, 'params': params}).encode() + b'\n')
            await writer.drain()
            updates = []
            while True:
                response = json.loads(await asyncio.wait_for(reader.readline(), 3))
                if 'id' in response:
                    return response, updates
                updates.append(response['params']['update'])

        def options(message):
            return {option['id']: option for option in message['result']['configOptions']}

        reader, writer = await asyncio.open_unix_connection(str(path))
        try:
            created, _ = await request('session/new', project)
            self.assertEqual(options(created)['model']['currentValue'], 'fixture-model')
            self.assertEqual([item['value'] for item in options(created)['model']['options']],
                             ['fixture-model', 'fixture-other'])
            for key, value in [('model', 'hidden-model'), ('reasoning_effort', 'high'), ('provider', 'api')]:
                forbidden, _ = await request('session/set_config_option', {**session, 'configId': key, 'value': value})
                self.assertIn('error', forbidden)
            switched, _ = await request('session/set_config_option',
                                        {**session, 'configId': 'model', 'value': 'fixture-other'})
            self.assertEqual(options(switched)['model']['currentValue'], 'fixture-other')
            reasoning, _ = await request('session/set_config_option',
                                         {**session, 'configId': 'reasoning_effort', 'value': 'high'})
            self.assertEqual(options(reasoning)['reasoning_effort']['currentValue'], 'high')
            turn, updates = await request('session/prompt', {**session, 'prompt': [
                {'type': 'image', 'mimeType': 'image/png', 'data': 'UE5H'},
            ]})
            self.assertEqual(turn['result']['stopReason'], 'end_turn')
            self.assertEqual([update['sessionUpdate'] for update in updates], ['plan', 'agent_thought_chunk'])
            self.assertEqual(json.loads((home / 'input.json').read_text()),
                             [{'type': 'image', 'url': 'data:image/png;base64,UE5H'}])
        finally:
            writer.close()
            await writer.wait_closed()
        reader, writer = await asyncio.open_unix_connection(str(path))
        try:
            restored, _ = await request('session/load', {**project, **session})
            self.assertEqual(options(restored)['model']['currentValue'], 'fixture-other')
            self.assertEqual(options(restored)['reasoning_effort']['currentValue'], 'high')
        finally:
            writer.close()
            await writer.wait_closed()


if __name__ == '__main__':
    unittest.main()
