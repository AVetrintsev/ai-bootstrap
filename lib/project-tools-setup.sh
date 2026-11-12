#!/usr/bin/env bash
set -Eeuo pipefail
rg_version=$1 ast_version=$2 serena_version=$3 cgc_version=$4
exec 5>/run/ai-project-tools-setup.lock
flock 5
export DEBIAN_FRONTEND=noninteractive
if ! command -v rg >/dev/null || ! command -v npm >/dev/null || [[ ! -x /opt/ai-serena/bin/python || ! -x /opt/ai-codegraph/bin/python ]]; then
    dpkg --configure -a
    apt-get -q update
    apt-get -qy --no-install-recommends install "ripgrep=$rg_version" nodejs npm python3-venv git ca-certificates
    python3 -m venv /opt/ai-serena
    python3 -m venv /opt/ai-codegraph
    apt-get clean
fi
[[ $(dpkg-query -W -f='${Version}' ripgrep) == "$rg_version" ]] || apt-get -qy install "ripgrep=$rg_version"
if [[ $(/opt/ai-search/bin/ast-grep --version 2>/dev/null || true) != "ast-grep $ast_version" ]]; then
    npm install --global --prefix /opt/ai-search --no-audit --no-fund "@ast-grep/cli@$ast_version"
fi
ln -sfn /opt/ai-search/bin/ast-grep /usr/local/bin/ast-grep
for package in "serena:serena-agent:$serena_version" "codegraph:codegraphcontext:$cgc_version"; do
    IFS=: read -r name distribution version <<< "$package"
    installed=$("/opt/ai-$name/bin/python" -c 'import importlib.metadata as m,sys; print(m.version(sys.argv[1]))' "$distribution" 2>/dev/null || true)
    if [[ $installed != "$version" ]]; then
        "/opt/ai-$name/bin/pip" install --no-cache-dir --disable-pip-version-check "$distribution==$version"
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
    '--open-web-dashboard', 'false', '--enable-gui-log-window', 'false'], 'timeout': 180000}
servers['codegraphcontext'] = {'command': '/usr/bin/python3',
    'args': ['-I', '/opt/ai-project-tools/graph.py', 'client'], 'timeout': 180000}
staged = path.with_suffix('.json.tmp')
staged.write_text(json.dumps(settings, indent=2) + '\n')
staged.chmod(0o600)
staged.replace(path)
PY
