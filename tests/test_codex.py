import asyncio
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from codex_service import serve


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
    if method=='account/read':
        result={'account': None if scenario['status']=='login_required' else {'type':'chatgpt','email':'private@example.test'}}
    elif method=='account/rateLimits/read':
        result={'ordinaryUsageAllowed':scenario['status']!='limit_reached','rateLimits':{}}
    elif method=='account/login/start':
        result={'type':'chatgptDeviceCode','loginId':'private-login','userCode':'TEST-CODE','verificationUrl':'https://auth.openai.com/codex/device'}
    print(json.dumps({'id':req['id'],'result':result}),flush=True)
'''


class CodexControlTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
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
                                         binary=str(self.binary), api_key=self.api_key))
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


if __name__ == '__main__':
    unittest.main()
