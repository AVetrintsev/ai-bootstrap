#!/usr/bin/env bash
set -Eeuo pipefail
version=$1
exec 6>/run/ai-canvas-setup.lock
flock 6
if [[ ! -x /opt/ai-agent/bin/python ]] || ! command -v socat >/dev/null; then
    export DEBIAN_FRONTEND=noninteractive
    dpkg --configure -a
    apt-get -q update
    apt-get -qy --no-install-recommends install python3-venv socat
    python3 -m venv /opt/ai-agent
fi
installed=$(/opt/ai-agent/bin/python -c 'import importlib.metadata as m; print(m.version("openhands-agent-server"))' 2>/dev/null || true)
if [[ $installed != "$version" ]]; then
    /opt/ai-agent/bin/pip install --no-cache-dir --disable-pip-version-check \
        "openhands-agent-server==$version" "openhands-sdk==$version" "openhands-tools==$version"
fi
