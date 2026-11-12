#!/usr/bin/env bash
set -Eeuo pipefail
[[ $EUID == 0 && $# -ge 2 ]] || { echo 'Usage: sudo bash tests/accept_resources.sh PROJECT PROJECT [...]' >&2; exit 1; }
python3 - "$@" <<'PY'
from pathlib import Path
import os, pwd, re, subprocess, sys
memory_percent,cpu_percent=map(int,Path('/etc/ai-bootstrap/sandbox-resources').read_text().split())
pool=Path('/sys/fs/cgroup/ai.slice/ai-sandboxes.slice')
physical=os.sysconf('SC_PHYS_PAGES')*os.sysconf('SC_PAGE_SIZE')
memory=int((pool/'memory.max').read_text())
assert abs(memory-physical*memory_percent//100)<os.sysconf('SC_PAGE_SIZE')
quota,period=map(int,(pool/'cpu.max').read_text().split())
assert quota==period*os.cpu_count()*cpu_percent//100
assert (pool/'memory.swap.max').read_text().strip()=='0'
for project in sys.argv[1:]:
 assert re.fullmatch(r'[a-z][a-z0-9-]{0,23}',project)
 user='aisbx-'+project
 account=pwd.getpwnam(user)
 uid=account.pw_uid
 pid=subprocess.check_output(['runuser','-u',user,'--','env','-i','PATH=/usr/bin:/bin',
  'HOME='+account.pw_dir,f'XDG_RUNTIME_DIR=/run/user/{uid}',
  'podman','inspect','--format','{{.State.Pid}}','project'],cwd='/',text=True).strip()
 assert f'/ai.slice/ai-sandboxes.slice/ai-sandboxes-{uid}.slice/' in Path(f'/proc/{pid}/cgroup').read_text()
 child=pool/f'ai-sandboxes-{uid}.slice'
 assert (child/'memory.max').read_text().strip()=='max'
 assert (child/'cpu.max').read_text().split()[0]=='max'
 assert (child/'pids.max').read_text().strip()=='512'
 print('OK shared resource membership:',project)
print(f'OK shared pool: {memory_percent}% VM RAM, {cpu_percent}% of all {os.cpu_count()} vCPUs')
PY
