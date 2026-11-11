#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
[[ $EUID == 0 && $# == 2 ]] || { echo 'Usage: sudo bash tests/accept_model_gateway.sh PROJECT MODEL' >&2; exit 1; }
project=$1
model=$2
[[ $project =~ ^[a-z][a-z0-9-]{0,23}$ ]] || exit 1
config=/etc/ai-bootstrap/model-gateway.json
work=$(mktemp -d /run/ai-model-accept.XXXXXX)
marker=ai-qwen-accept-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')
changed=no
cleanup() {
    if [[ $changed == yes ]]; then
        install -m 0640 -o root -g ai-models "$work/config.json" "$config.restore"
        mv -fT "$config.restore" "$config"
    fi
    ai-sandbox shell "$project" rm -f -- "/tmp/$marker" >/dev/null 2>&1 || true
    rm -rf -- "$work"
}
trap cleanup EXIT
cp -- "$config" "$work/config.json"
ai-sandbox shell "$project" python3 - "$model" <<'PY'
import json, os, pathlib, socket, sys, urllib.error, urllib.request
c=json.loads(pathlib.Path('/root/.config/ai-bootstrap/model-gateway.json').read_text())
base='http://'+os.environ['AI_GATEWAY_HOST']+':18081/v1'
def call(path, body=None):
 r=urllib.request.Request(base+path, data=json.dumps(body).encode() if body is not None else None,
                          headers={'Authorization':'Bearer '+c['api_key'],'Content-Type':'application/json'})
 try:
  with urllib.request.urlopen(r,timeout=10) as response: return response.status,json.load(response)
 except urllib.error.HTTPError as error: return error.code,json.load(error)
status,body=call('/models')
assert status==200 and sys.argv[1] in [m['id'] for m in body['data']]
status,body=call('/chat/completions',{'model':'ai-bootstrap-forbidden-model','messages':[]})
assert status==403 and body['error']['code']=='model_not_allowed'
for path in ['/etc/ai-bootstrap','/var/lib/ai-models','/var/lib/ai-sandboxes','/home/owner/.bailian','/root/.ssh']:
 assert not pathlib.Path(path).exists(), 'Host path exposed: '+path
for port in [22,8080]:
 try:
  connection=socket.create_connection((os.environ['AI_GATEWAY_HOST'],port),timeout=2)
 except OSError: continue
 connection.close()
 raise AssertionError('Unexpected access to host port '+str(port))
print('OK gateway access, denied model and host isolation')
PY
prompt="Use the shell tool to run: printf '%s' '$marker' > /tmp/$marker . Execute that command, then reply DONE. Do not read other files."
timeout 180 ai-sandbox shell "$project" bash -c \
    'cd /tmp; exec qwen --model "$1" -p "$2" --approval-mode yolo --output-format stream-json' \
    _ "$model" "$prompt" < /dev/null > "$work/qwen.jsonl" 2>&1
ai-sandbox shell "$project" test -f "/tmp/$marker"
[[ $(ai-sandbox shell "$project" cat "/tmp/$marker") == "$marker" ]]
python3 - "$work/qwen.jsonl" "$config" <<'PY'
import json,pathlib,sys
raw=pathlib.Path(sys.argv[1]).read_text()
events=[]
for line in raw.splitlines():
 try: events.append(json.loads(line))
 except ValueError: pass
def used_shell(value):
 if isinstance(value,list): return any(map(used_shell,value))
 if not isinstance(value,dict): return False
 return (value.get('type')=='tool_use' and value.get('name',value.get('tool_name'))=='run_shell_command') or any(map(used_shell,value.values()))
assert used_shell(events), 'Qwen did not report a shell tool call'
for provider in json.loads(pathlib.Path(sys.argv[2]).read_text())['providers'].values():
 assert pathlib.Path(provider['key_file']).read_text().strip() not in raw, 'Provider key leaked into Qwen output'
print('OK Qwen called the shell tool inside the sandbox')
PY
changed=yes
python3 - "$config" "$project" <<'PY'
import json,os,pathlib,sys
p=pathlib.Path(sys.argv[1]); c=json.loads(p.read_text()); project=sys.argv[2]
policy=dict(c.get('projects',{}).get(project,c['defaults'])); policy['requests_per_day']=0
c.setdefault('projects',{})[project]=policy
staged=p.with_suffix('.accept'); staged.write_text(json.dumps(c)); staged.chmod(0o640)
os.chown(staged,p.stat().st_uid,p.stat().st_gid); os.replace(staged,p)
PY
ai-sandbox shell "$project" python3 - "$model" <<'PY'
import json,os,pathlib,sys,urllib.error,urllib.request
c=json.loads(pathlib.Path('/root/.config/ai-bootstrap/model-gateway.json').read_text())
r=urllib.request.Request('http://'+os.environ['AI_GATEWAY_HOST']+':18081/v1/chat/completions',
 data=json.dumps({'model':sys.argv[1],'messages':[{'role':'user','content':'quota check'}]}).encode(),
 headers={'Authorization':'Bearer '+c['api_key'],'Content-Type':'application/json'})
try: urllib.request.urlopen(r,timeout=10)
except urllib.error.HTTPError as error:
 assert error.code==429 and json.load(error)['error']['code']=='project_quota_exceeded'
else: raise AssertionError('Exhausted project quota was accepted')
print('OK exhausted project quota')
PY
printf 'OK model gateway acceptance: %s\n' "$project"
