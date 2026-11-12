"""Provision trusted project configuration; run as ai-codex by the installer."""
import json
from pathlib import Path
import re
import sys

project = sys.argv[1]
if not re.fullmatch(r'gh-[1-9][0-9]{0,18}', project):
    sys.exit('Invalid project')
root = Path('/var/lib/ai-codex')
for profile in ('chatgpt', 'api'):
    home = root / 'projects' / project / profile
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    (home / 'environments.toml').write_text(
        'default = "project"\ninclude_local = false\n\n[[environments]]\nid = "project"\n'
        'program = "/usr/bin/socat"\nargs = ["STDIO", "UNIX-CONNECT:/run/ai-codex-exec/' + project + '.sock"]\n'
        'initialize_timeout_sec = 5\n')
    (home / 'config.toml').write_text(
        'cli_auth_credentials_store = "file"\napproval_policy = "never"\nsandbox_mode = "danger-full-access"\n'
        '[features]\nmulti_agent = false\n')
    if profile == 'chatgpt':
        auth = home / 'auth.json'
        if not auth.is_symlink():
            if auth.exists():
                sys.exit('Unexpected project auth file; refusing to replace it')
            auth.symlink_to(root / 'auth' / 'auth.json')
