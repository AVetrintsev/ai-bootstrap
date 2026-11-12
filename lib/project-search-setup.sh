#!/usr/bin/env bash
set -Eeuo pipefail
rg_version=$1 ast_version=$2
exec 4>/run/ai-project-search-setup.lock
flock 4
export DEBIAN_FRONTEND=noninteractive
if ! command -v rg >/dev/null || ! command -v npm >/dev/null; then
    dpkg --configure -a
    apt-get -q update
    apt-get -qy --no-install-recommends install "ripgrep=$rg_version" nodejs npm ca-certificates
    apt-get clean
fi
[[ $(dpkg-query -W -f='${Version}' ripgrep) == "$rg_version" ]] || apt-get -qy install "ripgrep=$rg_version"
if [[ $(/opt/ai-search/bin/ast-grep --version 2>/dev/null || true) != "ast-grep $ast_version" ]]; then
    npm install --global --prefix /opt/ai-search --no-audit --no-fund "@ast-grep/cli@$ast_version"
fi
ln -sfn /opt/ai-search/bin/ast-grep /usr/local/bin/ast-grep
