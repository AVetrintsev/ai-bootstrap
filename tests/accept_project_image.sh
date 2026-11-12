#!/usr/bin/env bash
set -Eeuo pipefail
[[ $EUID == 0 && $# == 1 && $1 =~ ^[a-z][a-z0-9-]{0,23}$ ]] || exit 1
account=aisbx-$1
uid=$(id -u "$account")
[[ $uid -ge 60001 && $uid -le 60999 ]] || exit 1
cd /
source /etc/ai-bootstrap/sandbox-release.env
as_project() { runuser -u "$account" -- env "XDG_RUNTIME_DIR=/run/user/$uid" "$@"; }
image=localhost/ai-bootstrap/image-acceptance:$RANDOM-$RANDOM
! as_project podman image exists "$image"
trap 'as_project podman image rm "$image" >/dev/null 2>&1 || true' EXIT
as_project podman build --http-proxy=false \
    --network slirp4netns:allow_host_loopback=false,enable_ipv6=false --dns 1.1.1.1 \
    --build-arg "BASE_IMAGE=localhost/ai-bootstrap/base:$SANDBOX_BASE_SHA256" \
    --build-arg "RIPGREP_VERSION=$RIPGREP_VERSION" --build-arg "AST_GREP_VERSION=$AST_GREP_VERSION" \
    --tag "$image" --file /usr/local/libexec/ai-bootstrap/project-image/Containerfile \
    /usr/local/libexec/ai-bootstrap/project-image
as_project podman run --rm --network none --userns keep-id:uid=0,gid=0 --user 0 "$image" \
    bash -ec 'rg --version; ast-grep --version'
echo 'OK both search tools work directly from the sandbox image without network'
