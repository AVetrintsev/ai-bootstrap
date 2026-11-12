"""Root-only target VM acceptance; uses native RPC, MCP and the sandbox client."""
import asyncio
import base64
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import tempfile

if os.geteuid() != 0 or len(sys.argv) != 2 or not re.fullmatch(r'gh-[1-9][0-9]{0,18}', sys.argv[1]):
    sys.exit('Usage: sudo python3 tests/accept_codex_isolation.py gh-ID')
project = sys.argv[1]
root = Path('/var/lib/ai-codex')
marker = root / 'auth' / 'isolation-marker'
marker.write_text('host-only-canary')
marker.chmod(0o600)
probe_path = '/tmp/ai-codex-accept-probe.py'
probe = '''import json,os,socket,sys
for line in sys.stdin:
    m=json.loads(line)
    if 'id' not in m: continue
    if m['method']=='initialize':
        result={'protocolVersion':m['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'isolation-probe','version':'1'}}
    elif m['method']=='tools/list':
        result={'tools':[{'name':'probe','inputSchema':{'type':'object'}}]}
    elif m['method']=='tools/call':
        assert not os.path.exists('/var/lib/ai-codex/auth/isolation-marker')
        assert not any(k in os.environ for k in ('OPENAI_API_KEY','CODEX_ACCESS_TOKEN'))
        with open('/workspace/repo/.task10-mcp','w') as f:f.write(socket.gethostname())
        result={'content':[{'type':'text','text':socket.gethostname()+':'+os.getcwd()}]}
    else: result={}
    print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':result}),flush=True)
'''
child_code = '''import asyncio,json,sys
from pathlib import Path
sys.path.insert(0,'/usr/local/libexec/ai-bootstrap')
from codex_service import AppServer,CodexError
async def main():
    server=AppServer(Path(sys.argv[1]))
    try:
        await server.start()
        await server.call('environment/info',{'environmentId':'project'})
        thread=(await server.call('thread/start',{'cwd':'/workspace/repo','approvalPolicy':'never','sandbox':'danger-full-access'}))['thread']['id']
        result=await server.call('mcpServer/tool/call',{'threadId':thread,'server':'probe','tool':'probe','arguments':{}})
        print(json.dumps(result),flush=True)
        await asyncio.to_thread(sys.stdin.readline)
        try:
            await server.call('mcpServer/tool/call',{'threadId':thread,'server':'probe','tool':'probe','arguments':{}},timeout=35)
        except CodexError:
            print('EXECUTOR_FAILURE',flush=True)
        else:
            raise AssertionError('MCP unexpectedly succeeded without the executor')
    finally: await server.close()
asyncio.run(main())
'''


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


async def check():
    directory = Path(tempfile.mkdtemp(prefix='accept-', dir=root))
    owner = pwd.getpwnam('ai-codex')
    os.chown(directory, owner.pw_uid, owner.pw_gid)
    home = root / 'projects' / project / 'chatgpt'
    for name in ('auth.json', 'auth.lock'):
        assert (home / name).is_symlink()
        assert (home / name).resolve() == root / 'auth' / name
        (directory / name).symlink_to(root / 'auth' / name)
    for name in ('environments.toml', 'config.toml'):
        (directory / name).write_text((home / name).read_text())
        os.chown(directory / name, owner.pw_uid, owner.pw_gid)
    with (directory / 'config.toml').open('a') as config:
        config.write('\n[mcp_servers.probe]\ncommand="/usr/bin/python3"\n'
                     f'args=["{probe_path}"]\ncwd="/workspace/repo"\nenvironment_id="project"\n')
    process = None
    reader = writer = None
    socket_unit = 'ai-bootstrap-codex-executor@' + project + '.socket'
    service_unit = 'ai-bootstrap-codex-executor@' + project + '.service'
    try:
        fs_reader, fs_writer = await asyncio.open_unix_connection('/run/ai-codex-exec/' + project + '.sock')
        try:
            async def fs_call(key, method, params):
                fs_writer.write(json.dumps({'id': key, 'method': method, 'params': params}).encode() + b'\n')
                await fs_writer.drain()
                return json.loads(await asyncio.wait_for(fs_reader.readline(), 10))
            initialized = await fs_call(1, 'initialize', {'clientName': 'ai-bootstrap-acceptance'})
            assert 'result' in initialized, initialized
            fs_writer.write(b'{"method":"initialized"}\n')
            encoded = base64.b64encode(b'isolated-filesystem').decode()
            written = await fs_call(2, 'fs/writeFile', {'path': 'file:///workspace/repo/.task10-fs', 'dataBase64': encoded})
            assert 'result' in written, written
            read = await fs_call(3, 'fs/readFile', {'path': 'file:///workspace/repo/.task10-fs'})
            assert read['result']['dataBase64'] == encoded, read
            denied = await fs_call(4, 'fs/readFile', {'path': 'file:///var/lib/ai-codex/auth/isolation-marker'})
            assert 'error' in denied, denied
            print('OK native file RPC reads and writes in the sandbox and cannot read the host auth directory')
        finally:
            fs_writer.close()
            await fs_writer.wait_closed()
        run('ai-sandbox', 'shell', project, 'python3', '-c',
            'import sys;open(sys.argv[1],"w").write(sys.stdin.read())', probe_path, input=probe, text=True)
        process = await asyncio.create_subprocess_exec('runuser', '-u', 'ai-codex', '--',
            'python3', '-I', '-c', child_code, str(directory), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        result = await asyncio.wait_for(process.stdout.readline(), 90)
        assert (project + ':/workspace/repo').encode() in result, result
        print('OK native MCP executes in the project without host marker or authentication environment')
        run('ai-sandbox', 'shell', project, 'test', '!', '-e', str(marker))
        run('ai-sandbox', 'shell', project, 'test', '!', '-S', '/run/ai-codex-exec/' + project + '.sock')

        # Keep an already-open ACP session while stopping both executor activation and process.
        reader, writer = await asyncio.open_unix_connection('/run/ai-bootstrap-git/' + project + '/codex.sock')
        async def call(key, method, params):
            writer.write(json.dumps({'id': key, 'method': method, 'params': params}).encode() + b'\n')
            await writer.drain()
            while line := await asyncio.wait_for(reader.readline(), 45):
                response = json.loads(line)
                if response.get('id') == key:
                    return response
            raise AssertionError('ACP closed without a response')
        await call(1, 'initialize', {'protocolVersion': 1})
        current = await call(2, 'session/new', {'cwd': '/workspace/repo', 'mcpServers': []})
        session = current['result']['sessionId']
        run('systemctl', 'stop', socket_unit, service_unit)
        run('ai-sandbox', 'shell', project, 'rm', '-f', '/workspace/repo/.task10-mcp')
        process.stdin.write(b'continue\n')
        await process.stdin.drain()
        failed = await asyncio.wait_for(process.stdout.readline(), 45)
        assert failed.strip() == b'EXECUTOR_FAILURE', failed
        await asyncio.wait_for(process.wait(), 10)
        assert process.returncode == 0, (await process.stderr.read()).decode()
        response = await call(3, 'session/prompt', {'sessionId': session,
            'prompt': [{'type': 'text', 'text': 'Write /workspace/repo/.task10-down'}]})
        assert response['error']['data']['kind'] == 'executor_unavailable', response
        result = subprocess.run(['ai-sandbox', 'shell', project, 'codex', 'exec', 'Write /workspace/repo/.task10-down'],
                                capture_output=True, text=True, timeout=30)
        assert result.returncode != 0 and 'Исполнитель' in result.stderr, result
        run('ai-sandbox', 'shell', project, 'test', '!', '-e', '/workspace/repo/.task10-mcp')
        run('ai-sandbox', 'shell', project, 'test', '!', '-e', '/workspace/repo/.task10-down')
        assert not Path('/workspace/repo/.task10-down').exists()
        assert marker.read_text() == 'host-only-canary'
        print('OK stopped executor fails native MCP, existing ACP session and new terminal session without host fallback')
    finally:
        if writer:
            writer.close()
            await writer.wait_closed()
        if process and process.returncode is None:
            process.terminate()
            await process.wait()
        run('systemctl', 'start', socket_unit)
        run('ai-sandbox', 'shell', project, 'rm', '-f', probe_path, '/workspace/repo/.task10-mcp', '/workspace/repo/.task10-down', '/workspace/repo/.task10-fs')
        marker.unlink(missing_ok=True)
        shutil.rmtree(directory)


asyncio.run(check())
