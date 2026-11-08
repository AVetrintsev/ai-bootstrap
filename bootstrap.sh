#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

step=arguments
fail() {
    printf 'ERROR bootstrap/%s: %s\n' "$step" "$1" >&2
    exit 1
}
trap 'fail "unexpected failure"' ERR

[[ $# == 2 || $# == 4 ]] || fail 'usage: bootstrap.sh SSH_REPOSITORY COMMIT [--config FILE]'
repo=$1
revision=$2
shift 2
[[ $# == 0 || ($1 == --config && -n $2) ]] || fail 'expected --config FILE'
[[ $repo =~ ^git@[a-zA-Z0-9.-]+:[a-zA-Z0-9_./-]+\.git$ ]] ||
    fail 'expected an SSH repository URL in git@host:owner/repository.git format'
[[ $revision =~ ^[0-9a-f]{40}$ ]] || fail 'expected a full Git commit SHA'
[[ $EUID == 0 ]] || fail 'run as root'

step=release
release_dir=/opt/ai-bootstrap/releases
release=$release_dir/$revision
install -d -m 0700 "$release_dir"
exec 9>/run/lock/ai-bootstrap-release.lock
flock -n 9 || fail 'another bootstrap is running'
if [[ -d $release ]]; then
    [[ $(git -C "$release" rev-parse HEAD 2>/dev/null) == "$revision" &&
       $(git -C "$release" remote get-url origin 2>/dev/null) == "$repo" &&
       -z $(git -C "$release" status --porcelain --untracked-files=all 2>/dev/null) ]] ||
        fail 'cached release does not match the requested repository and commit'
    printf 'SKIP bootstrap/release: cached commit %s\n' "$revision"
else
    key=/root/.ssh/ai-bootstrap-deploy
    hosts=/root/.ssh/ai-bootstrap-known_hosts
    [[ -f $key && ! -L $key && $(stat -c '%u:%a' "$key") == 0:600 && -s $hosts ]] ||
        fail 'install the root-owned 0600 deploy key and pinned SSH host keys'
    checkout=$(mktemp -d "$release_dir/.checkout.XXXXXX")
    trap 'rm -rf -- "$checkout"' EXIT
    ssh_command="ssh -F /dev/null -i $key -o IdentitiesOnly=yes -o BatchMode=yes"
    ssh_command+=" -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$hosts -o GlobalKnownHostsFile=/dev/null"
    export GIT_SSH_COMMAND=$ssh_command GIT_TERMINAL_PROMPT=0
    {
        git -C "$checkout" init &&
        git -C "$checkout" remote add origin "$repo" &&
        git -C "$checkout" fetch --depth=1 origin "$revision" &&
        git -C "$checkout" checkout --detach FETCH_HEAD
    } >/dev/null 2>&1 || fail 'could not fetch the pinned release with the deploy key'
    [[ $(git -C "$checkout" rev-parse HEAD) == "$revision" ]] || fail 'commit mismatch'
    mv -T -- "$checkout" "$release"
    trap - EXIT
    printf 'OK bootstrap/release: %s\n' "$revision"
fi

exec "$release/install.sh" "$@"
