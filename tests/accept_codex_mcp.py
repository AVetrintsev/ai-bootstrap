"""Temporarily configure a remote MCP probe and exercise both model entry points."""
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys

if os.geteuid() != 0 or len(sys.argv) != 4 or not re.fullmatch(r'gh-[1-9][0-9]{0,18}', sys.argv[1]):
    sys.exit('Usage: sudo BROWSER_PYTHON tests/accept_codex_mcp.py PROJECT CHROMIUM BROWSER_PYTHON')
project, chromium, browser_python = sys.argv[1:]
account = pwd.getpwnam('aisbx-' + project)
config = Path('/var/lib/ai-codex/projects') / project / 'chatgpt/config.toml'
original = config.read_bytes()
probe_path = '/tmp/ai-codex-model-probe.py'
probe = '''import json,os,socket,sys
from pathlib import Path
for line in sys.stdin:
 m=json.loads(line)
 if 'id' not in m: continue
 if m['method']=='initialize':
  result={'protocolVersion':m['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'acceptance','version':'1'}}
 elif m['method']=='tools/list':
  result={'tools':[{'name':'probe','description':'Write a test marker inside the current project sandbox.','inputSchema':{'type':'object','properties':{'file':{'type':'string'},'text':{'type':'string'}},'required':['file','text']}}]}
 elif m['method']=='tools/call':
  args=m['params']['arguments'];path=Path(args['file'])
  assert path.parent==Path('/workspace/repo') and (path.name.startswith('.ai-canvas-accept-') or path.name=='.task10-mcp-terminal')
  assert os.getcwd()=='/workspace/repo'
  assert not Path('/var/lib/ai-codex/auth/auth.json').exists()
  path.write_text(args['text'])
  result={'content':[{'type':'text','text':socket.gethostname()+':'+str(path)+':'+args['text']}]}
 else: result={}
 print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':result}),flush=True)
'''


def run(*args, **kwargs):
    kwargs.setdefault('cwd', '/')
    return subprocess.run(args, check=True, **kwargs)


def restart():
    run('systemctl', 'stop', 'ai-bootstrap-codex-project@' + project + '.service')
    run('runuser', '-u', account.pw_name, '--', 'env', f'XDG_RUNTIME_DIR=/run/user/{account.pw_uid}',
        'systemctl', '--user', 'restart', 'ai-bootstrap-agent.service')


try:
    run('ai-sandbox', 'shell', project, 'python3', '-c', 'import sys;open(sys.argv[1],"w").write(sys.stdin.read())',
        probe_path, input=probe, text=True)
    with config.open('a') as output:
        output.write('\n[mcp_servers.acceptance]\ncommand="/usr/bin/python3"\n'
                     f'args=["{probe_path}"]\ncwd="/workspace/repo"\nenvironment_id="project"\n')
    restart()
    result = run('ai-sandbox', 'shell', project, 'codex', 'exec',
        'Call only the MCP acceptance probe tool with file="/workspace/repo/.task10-mcp-terminal" and text="terminal-mcp-ok". Do not use shell. Reply DONE.',
        capture_output=True, text=True, timeout=180)
    assert '[Codex] probe' in result.stderr, result.stderr
    value = run('ai-sandbox', 'shell', project, 'cat', '/workspace/repo/.task10-mcp-terminal', capture_output=True, text=True)
    assert value.stdout == 'terminal-mcp-ok', value.stdout
    print('OK terminal model called the remote MCP probe')
    run(browser_python, str(Path(__file__).resolve().with_name('accept_canvas.py')), project, chromium, 'codex', 'mcp')
finally:
    config.write_bytes(original)
    restart()
    run('ai-sandbox', 'shell', project, 'rm', '-f', probe_path, '/workspace/repo/.task10-mcp-terminal')
