#!/usr/bin/env bash
set -Eeuo pipefail
version=$1
exec 7>/run/ai-qwen-setup.lock
flock 7
if ! command -v node >/dev/null || ! command -v npm >/dev/null ||
   ! command -v python3 >/dev/null || [[ ! -s /etc/ssl/certs/ca-certificates.crt ]]; then
    export DEBIAN_FRONTEND=noninteractive
    dpkg --configure -a
    apt-get -q update
    apt-get -qy --no-install-recommends install nodejs npm python3 ca-certificates
fi
[[ $(node -p 'Number(process.versions.node.split(".")[0])') -ge 22 ]] || {
    echo 'Qwen Code requires Node.js 22 or newer.' >&2
    exit 1
}
installed=$(python3 -I -c 'import json,pathlib; p=pathlib.Path("/opt/ai-qwen/lib/node_modules/@qwen-code/qwen-code/package.json"); print(json.loads(p.read_text())["version"] if p.exists() else "")')
if [[ $installed != "$version" ]]; then
    NODE_OPTIONS=--max-old-space-size=512 npm install --global --prefix /opt/ai-qwen \
        --omit=optional --no-audit --no-fund "@qwen-code/qwen-code@$version"
fi
