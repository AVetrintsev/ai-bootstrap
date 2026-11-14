"""Provision trusted project configuration; run as ai-codex by the installer."""
from pathlib import Path
import re
import sys

project = sys.argv[1]
if not re.fullmatch(r'gh-[1-9][0-9]{0,18}', project):
    sys.exit('Invalid project')
root = Path('/var/lib/ai-codex')
(root / 'auth' / 'auth.lock').touch(mode=0o600, exist_ok=True)
for profile in ('chatgpt', 'api'):
    home = root / 'projects' / project / profile
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    (home / 'environments.toml').write_text(
        'default = "project"\ninclude_local = false\n\n[[environments]]\nid = "project"\n'
        'program = "/usr/bin/socat"\nargs = ["STDIO", "UNIX-CONNECT:/run/ai-codex-exec/' + project + '.sock"]\n'
        'initialize_timeout_sec = 5\n')
    (home / 'config.toml').write_text(
        'cli_auth_credentials_store = "file"\napproval_policy = "never"\nsandbox_mode = "danger-full-access"\n'
        '[features]\nmulti_agent = false\n'
        '[mcp_servers.serena]\ncommand = "/opt/ai-serena/bin/serena"\n'
        'args = ["start-mcp-server", "--project", "/workspace/repo", "--context", "ide", "--agent-interface", "tools", '
        '"--open-web-dashboard", "false", "--enable-web-dashboard", "false", "--enable-gui-log-window", "false"]\n'
        'cwd = "/workspace/repo"\nenvironment_id = "project"\nrequired = true\nstartup_timeout_sec = 120\ntool_timeout_sec = 180\n'
        '[mcp_servers.codegraphcontext]\ncommand = "/usr/bin/python3"\n'
        'args = ["-I", "/opt/ai-project-tools/graph.py", "client"]\n'
        'cwd = "/workspace/repo"\nenvironment_id = "project"\nrequired = true\nstartup_timeout_sec = 120\ntool_timeout_sec = 180\n')
    if Path('/etc/ai-bootstrap/project-memory.json').is_file():
        with (home / 'config.toml').open('a') as config:
            config.write('[mcp_servers.graphiti]\ncommand = "/usr/bin/python3"\n'
                         'args = ["-I", "/usr/local/lib/ai-bootstrap/memory-client"]\n'
                         'cwd = "/workspace/repo"\nenvironment_id = "project"\n'
                         'required = true\nstartup_timeout_sec = 120\ntool_timeout_sec = 300\n')
    if profile == 'chatgpt':
        auth = home / 'auth.json'
        if not auth.is_symlink():
            if auth.exists():
                sys.exit('Unexpected project auth file; refusing to replace it')
            auth.symlink_to(root / 'auth' / 'auth.json')
        lock = home / 'auth.lock'
        if not lock.is_symlink():
            # The caller stops this project's app-server before migrating a lock.
            if lock.exists():
                lock.unlink()
            lock.symlink_to(root / 'auth' / 'auth.lock')
