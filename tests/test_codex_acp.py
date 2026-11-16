"""Boundary tests; live upstream/remote execution has a separate acceptance check."""
import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from codex_acp import serve, workspace_path

FAKE_ADAPTER = '''#!/usr/bin/python3
import json,os,sys
from pathlib import Path
home=Path(os.environ['CODEX_HOME'])
(home/'environment.json').write_text(json.dumps(dict(os.environ)))
for line in sys.stdin:
    request=json.loads(line)
    with (home/'requests.jsonl').open('a') as record: record.write(line)
    method=request.get('method')
    if method=='initialize':
        result={'protocolVersion':1,'authMethods':[{'id':'login','name':'Login'}],
                'agentCapabilities':{'loadSession':True}}
    elif method in ('session/new','session/load','session/resume','session/fork'):
        result={'sessionId':'fixture-session','models':{'currentModelId':'gpt-fixture[high]',
                'availableModels':[{'modelId':'gpt-fixture[high]','name':'Native model (high)'}]},
                'configOptions':[{'id':'model','category':'model'},{'id':'reasoning_effort','category':'thought_level'},
                                 {'id':'mode','category':'mode'}]}
    elif method=='session/prompt':
        print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'fixture-session',
              'update':{'sessionUpdate':'plan','entries':[{'content':'Native plan','status':'pending','priority':'high'}]}}}),flush=True)
        continue
    elif method=='session/cancel':
        print(json.dumps({'jsonrpc':'2.0','id':3,'result':{'stopReason':'cancelled'}}),flush=True)
        continue
    else: result={}
    if 'id' in request:
        print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}),flush=True)
'''


class BoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / 'projects/gh-123/chatgpt'
        self.adapter = self.root / 'adapter'
        self.adapter.write_text(FAKE_ADAPTER)
        self.adapter.chmod(0o700)
        self.executor_alive = True
        self.workspaces = []

        async def workspace(reader, writer):
            line = await reader.readline()
            self.workspaces.append(json.loads(line.removeprefix(b'AI-WORKSPACE ')))
            writer.write(json.dumps({'cwd': self.workspaces[-1]} if self.executor_alive else {'error':'down'}).encode()+b'\n')
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        self.executor = await asyncio.start_unix_server(workspace, path=str(self.root / 'gh-123.sock'))
        self.addAsyncCleanup(self.close_executor)
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(self.root / 'client.sock'))
        self.task = asyncio.create_task(serve(listener, 'gh-123', state=self.root,
            adapter=str(self.adapter), binary='/trusted/pinned/codex', executors=self.root))
        await asyncio.sleep(.02)
        self.reader, self.writer = await asyncio.open_unix_connection(str(self.root / 'client.sock'))
        self.addAsyncCleanup(self.close_client)

    async def close_executor(self):
        self.executor.close()
        await self.executor.wait_closed()

    async def close_client(self):
        self.writer.close()
        await self.writer.wait_closed()
        await asyncio.sleep(.05)
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    async def send(self, identity, method, params):
        self.writer.write(json.dumps({'jsonrpc':'2.0','id':identity,'method':method,'params':params}).encode()+b'\n')
        await self.writer.drain()

    async def receive(self):
        return json.loads(await asyncio.wait_for(self.reader.readline(), 3))

    async def initialize(self):
        await self.send(1, 'initialize', {'protocolVersion':1,'clientCapabilities':{},'_meta':{'codex':{'hooks':'unsafe'}}})
        result = await self.receive()
        self.assertEqual(result['result']['authMethods'], [])

    async def new(self, cwd='/workspace/repo', **extra):
        await self.send(2, 'session/new', {'cwd':cwd,'mcpServers':[],**extra})
        return await self.receive()

    def requests(self):
        return [json.loads(line) for line in (self.home/'requests.jsonl').read_text().splitlines()]

    async def test_upstream_catalog_and_events_pass_through_without_translation(self):
        await self.initialize()
        result = (await self.new())['result']
        self.assertEqual(result['models']['currentModelId'], 'gpt-fixture[high]')
        self.assertEqual(result['configOptions'], [{'id':'mode','category':'mode'}])
        await self.send(3, 'session/prompt', {'sessionId':'fixture-session','prompt':[{'type':'text','text':'go'}]})
        self.assertEqual((await self.receive())['params']['update']['sessionUpdate'], 'plan')
        await self.send(4, 'session/cancel', {'sessionId':'fixture-session'})
        self.assertEqual((await self.receive())['result']['stopReason'], 'cancelled')
        await self.send(5, 'session/set_model', {'sessionId':'fixture-session','modelId':'gpt-fixture[high]'})
        await self.receive()
        self.assertEqual(self.requests()[-1]['params']['modelId'], 'gpt-fixture[high]')

    async def test_client_cannot_supply_host_settings_or_common_rules(self):
        await self.initialize()
        await self.new(_meta={'config':{'hooks':{'command':'unsafe'}}}, systemPrompt={'text':'old rules'})
        params = self.requests()[-1]['params']
        self.assertNotIn('_meta', params)
        self.assertEqual(params['systemPrompt']['content'], [{'type':'text','text':''}])
        env = json.loads((self.home/'environment.json').read_text())
        self.assertEqual(env['CODEX_PATH'], '/trusted/pinned/codex')
        self.assertEqual(json.loads(env['CODEX_CONFIG'])['developer_instructions'], '')
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertNotIn('DEFAULT_AUTH_REQUEST', env)

    async def test_host_rpc_mcp_hooks_and_foreign_sessions_are_rejected(self):
        await self.initialize()
        for method, params in [
            ('authenticate', {}), ('_codex/hooks/set', {}), ('account/read', {}),
            ('fs/readFile', {'path':'/var/lib/ai-codex/auth/auth.json'}),
            ('session/new', {'cwd':'/workspace/repo','mcpServers':[{'command':'sh'}]}),
            ('session/new', {'cwd':'/var/lib/ai-codex'}),
            ('session/load', {'cwd':'/workspace/repo','sessionId':'foreign-project'}),
            (['bad-method'], {}), ('session/new', ['bad-params']),
        ]:
            await self.send(10, method, params)
            self.assertIn('error', await self.receive(), method)
        self.assertEqual(len(self.requests()), 1)

    async def test_worktree_cwd_is_checked_and_preserved(self):
        await self.initialize()
        cwd='/workspace/worktrees/12345678-1234-1234-1234-123456789012/repo/src'
        self.assertIn('result', await self.new(cwd))
        self.assertEqual(self.workspaces[-1], cwd)
        self.assertEqual(self.requests()[-1]['params']['cwd'], cwd)
        self.executor_alive=False
        await self.send(3, 'session/prompt', {'sessionId':'fixture-session','prompt':[{'type':'text','text':'go'}]})
        await self.receive()
        self.assertEqual((await self.receive())['result']['stopReason'], 'refusal')
        self.assertEqual(len(self.requests()), 2)

    async def test_external_images_and_auth_slash_commands_cannot_reach_host(self):
        await self.initialize()
        await self.new()
        for block in [{'type':'image','uri':'http://127.0.0.1/private'},
                      {'type':'text','text':' / logout '}, {'type':'text','text':'/mcp reconnect'}]:
            await self.send(3, 'session/prompt', {'sessionId':'fixture-session','prompt':[block]})
            await self.receive()
            self.assertEqual((await self.receive())['result']['stopReason'], 'refusal')
        self.assertEqual(len(self.requests()), 2)

    async def test_billing_profile_change_requires_reconnect(self):
        await self.initialize()
        await self.new()
        (self.root/'profile').write_text('api')
        await self.send(3, 'session/prompt', {'sessionId':'fixture-session','prompt':[{'type':'text','text':'go'}]})
        self.assertIn('Способ оплаты изменён', (await self.receive())['params']['update']['content']['text'])
        self.assertEqual((await self.receive())['result']['stopReason'], 'refusal')
        self.assertEqual(len(self.requests()), 2)


class WorkspacePathTest(unittest.TestCase):
    def test_rejects_host_paths_and_ambiguous_names(self):
        for value in [None, '/', '/workspace', '/workspace/repo/../secrets', '/workspace/repo//src',
                      '/workspace/repo/', '/workspace/repo\n', '/workspace/worktrees/other/repo']:
            with self.assertRaises(ValueError, msg=str(value)):
                workspace_path(value)
