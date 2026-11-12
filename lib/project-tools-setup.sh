#!/usr/bin/env bash
set -Eeuo pipefail
serena_version=$1 cgc_version=$2
exec 5>/run/ai-project-tools-setup.lock
flock 5
export DEBIAN_FRONTEND=noninteractive
if [[ ! -x /opt/ai-serena/bin/python || ! -x /opt/ai-codegraph/bin/python ]]; then
    dpkg --configure -a
    apt-get -q update
    apt-get -qy --no-install-recommends install python3-venv git ca-certificates
    python3 -m venv /opt/ai-serena
    python3 -m venv /opt/ai-codegraph
    apt-get clean
fi
for package in "serena:serena-agent:$serena_version" "codegraph:codegraphcontext:$cgc_version"; do
    IFS=: read -r name distribution version <<< "$package"
    installed=$("/opt/ai-$name/bin/python" -c 'import importlib.metadata as m,sys; print(m.version(sys.argv[1]))' "$distribution" 2>/dev/null || true)
    if [[ $installed != "$version" ]]; then
        "/opt/ai-$name/bin/pip" install --no-cache-dir --disable-pip-version-check \
            --timeout 60 --retries 5 --resume-retries 5 "$distribution==$version"
    fi
done
install -d -m 0700 /root/.qwen /root/.local/share/ai-codegraph /opt/ai-project-tools
python3 -I - <<'PY'
import json
from pathlib import Path
path = Path('/root/.qwen/settings.json')
settings = json.loads(path.read_text()) if path.exists() else {}
servers = settings.setdefault('mcpServers', {})
servers['serena'] = {'command': '/opt/ai-serena/bin/serena', 'args': [
    'start-mcp-server', '--project', '/workspace/repo', '--context', 'ide',
    '--open-web-dashboard', 'false', '--enable-web-dashboard', 'false',
    '--enable-gui-log-window', 'false'], 'timeout': 180000}
servers['codegraphcontext'] = {'command': '/usr/bin/python3',
    'args': ['-I', '/opt/ai-project-tools/graph.py', 'client'], 'timeout': 180000}
staged = path.with_suffix('.json.tmp')
staged.write_text(json.dumps(settings, indent=2) + '\n')
staged.chmod(0o600)
staged.replace(path)
PY
