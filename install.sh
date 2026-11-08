#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

step=arguments
fail() {
    printf 'ERROR install/%s: %s\n' "$step" "$1" >&2
    exit 1
}
trap 'fail "unexpected failure"' ERR

quiet() {
    local reason=$1
    shift
    "$@" >/dev/null 2>&1 || fail "$reason"
}

run_step() {
    step=$1
    skip_reason=
    "$1"
    if [[ -n $skip_reason ]]; then
        printf 'SKIP install/%s: %s\n' "$step" "$skip_reason"
    else
        printf 'OK install/%s\n' "$step"
    fi
}

config=./config.yaml
if (( $# )); then
    [[ $# == 2 && $1 == --config && -n $2 ]] || fail 'usage: install.sh [--config FILE]'
    config=$2
fi

release=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
etc=/etc/ai-bootstrap
libexec=/usr/local/libexec/ai-bootstrap
cache=/var/lib/ai-bootstrap
state=/var/lib/ai-mihomo
firewall_unit=ai-bootstrap-firewall.service
mihomo_unit=ai-bootstrap-mihomo.service

preflight() {
    [[ $EUID == 0 ]] || fail 'run as root'
    [[ -r /etc/os-release ]] || fail 'cannot identify the operating system'
    # shellcheck source=/dev/null
    source /etc/os-release
    [[ ${ID:-} == ubuntu && ${VERSION_ID:-} == 26.04 ]] || fail 'Ubuntu Server 26.04 LTS is required'
    [[ $(dpkg --print-architecture) == amd64 && $(uname -m) == x86_64 ]] || fail 'amd64 is required'
    if ! dpkg-query -W -f='${db:Status-Status}\n' ubuntu-server ubuntu-server-minimal 2>/dev/null |
        grep -x installed >/dev/null; then
        # Cloud images can omit the server metapackage.
        [[ $(systemctl get-default 2>/dev/null) == multi-user.target ]] ||
            fail 'an Ubuntu Server installation is required'
    fi
    [[ -d /run/systemd/system && -c /dev/net/tun ]] || fail 'systemd and /dev/net/tun are required'
    exec 8>/run/lock/ai-bootstrap-install.lock
    flock -n 8 || fail 'another installer is running'
    work=$(mktemp -d /run/ai-bootstrap.XXXXXX)
    trap 'rm -rf -- "$work"' EXIT

    # The YAML reader is a prerequisite and installs no service of its own.
    export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l
    if ! python3 -I -c 'import yaml' >/dev/null 2>&1; then
        quiet 'could not refresh package indexes for the YAML reader' apt-get -q update
        quiet 'could not install the YAML reader' apt-get -qy --no-install-recommends install python3-yaml
    fi
    local reason
    reason=$(python3 -I "$release/lib/config.py" "$config" "$work" 2>&1) ||
        fail "${reason%%$'\n'*}"
    [[ $(readlink -f /etc/resolv.conf) == /run/systemd/resolve/stub-resolv.conf ]] ||
        fail 'expected /etc/resolv.conf to use the systemd-resolved stub'
    quiet 'systemd-resolved must be running' systemctl is-active --quiet systemd-resolved.service
    [[ -n $(ss -Hltn 'sport = :22') ]] || fail 'SSH must already be listening on TCP port 22'
    reason=$(ss -Htn state established 'sport = :22' |
        python3 -I "$release/lib/config.py" --check-ssh "$work" 2>&1) ||
        fail "${reason%%$'\n'*}"
}

packages() {
    local package
    local -a missing=()
    for package in ca-certificates curl gzip iproute2 nftables openssh-server procps; do
        if [[ $(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null) != installed ]]; then
            missing+=("$package")
        fi
    done
    if (( ${#missing[@]} == 0 )); then
        skip_reason='base packages are installed'
        return
    fi
    quiet 'could not refresh package indexes' apt-get -q update
    quiet 'could not install base packages' apt-get -qy --no-install-recommends install "${missing[@]}"
}

directories() {
    if ! id ai-mihomo >/dev/null 2>&1; then
        quiet 'could not create the service account' useradd --system --user-group --no-create-home \
            --home-dir /var/lib/ai-mihomo --shell /usr/sbin/nologin ai-mihomo
    fi
    local account
    account=$(getent passwd ai-mihomo)
    [[ $(id -u ai-mihomo) -gt 0 && $(id -u ai-mihomo) -lt 1000 &&
       $(id -gn ai-mihomo) == ai-mihomo && $account == *:/var/lib/ai-mihomo:/usr/sbin/nologin ]] ||
        fail 'existing ai-mihomo account is incompatible'
    install -d -m 0700 "$cache"
    install -d -m 0755 "$etc" "$libexec" /etc/systemd/resolved.conf.d
    chgrp ai-mihomo "$work"
    chmod 0710 "$work"
    install -d -o ai-mihomo -g ai-mihomo -m 0700 "$state"
}

verify_hash() {
    printf '%s  %s\n' "$MIHOMO_SHA256" "$1" | sha256sum --check --status
}

validate_mihomo() {
    # shellcheck source=release.env
    source "$release/release.env"
    local asset=mihomo-linux-amd64-v1-$MIHOMO_VERSION.gz
    local archive=$cache/$asset
    if [[ ! -f $archive ]]; then
        local partial=$cache/.$asset.download
        quiet 'could not download the pinned mihomo archive' curl -q --fail --silent --location \
            --proto '=https' --proto-redir '=https' --connect-timeout 10 --max-time 180 \
            --output "$partial" "https://github.com/MetaCubeX/mihomo/releases/download/$MIHOMO_VERSION/$asset"
        quiet 'mihomo archive failed the release SHA-256 check' verify_hash "$partial"
        mv -T -- "$partial" "$archive"
    fi
    quiet 'cached mihomo archive failed the release SHA-256 check' verify_hash "$archive"
    gzip -dc -- "$archive" > "$work/mihomo" || fail 'could not unpack mihomo'
    chgrp ai-mihomo "$work/mihomo" "$work/profile.yaml"
    chmod 0750 "$work/mihomo"
    chmod 0640 "$work/profile.yaml"
    # Reuse geodata and providers so retries do not depend on a running DNS listener.
    quiet 'mihomo rejected the profile or could not initialize its data' \
        timeout 120 runuser -u ai-mihomo -- env -i PATH=/usr/sbin:/usr/bin \
        "$work/mihomo" -t -d "$state" -f "$work/profile.yaml"
}

# Keep the previous file intact until its replacement is ready.
publish() {
    local source=$1 target=$2 mode=$3 group=${4:-root} staged
    if cmp -s -- "$source" "$target" && [[ $(stat -c '%a:%U:%G' "$target") == "$mode:root:$group" ]]; then
        return
    fi
    staged=$(mktemp "${target}.XXXXXX")
    install -o root -g "$group" -m "$mode" -- "$source" "$staged"
    mv -fT -- "$staged" "$target"
}

validate_network() {
    local ssh_config=/etc/ssh/sshd_config
    cp -p -- "$ssh_config" "$work/ssh-original"
    {
        cat <<'EOF'
# BEGIN AI BOOTSTRAP SSH
PubkeyAuthentication yes
AuthenticationMethods publickey
PasswordAuthentication no
KbdInteractiveAuthentication no
DenyUsers ai-mihomo
# END AI BOOTSTRAP SSH
EOF
        sed '/^# BEGIN AI BOOTSTRAP SSH$/,/^# END AI BOOTSTRAP SSH$/d' "$work/ssh-original"
    } > "$work/sshd_config"
    quiet 'sshd rejected the candidate configuration' /usr/sbin/sshd -t -f "$work/sshd_config"
    /usr/sbin/sshd -T -ddd -f "$work/sshd_config" > "$work/ssh-effective" 2>"$work/ssh-debug" ||
        fail 'could not read effective sshd configuration'
    if grep -q "checking syntax for 'Match " "$work/ssh-debug"; then
        fail 'SSH Match blocks can override key-only authentication; remove them before bootstrap'
    fi
    [[ $(grep '^port ' "$work/ssh-effective") == 'port 22' ]] ||
        fail 'expected the SSH configuration to use only TCP port 22'
    quiet 'nftables rejected the candidate firewall' nft --check --file "$work/firewall.nft"
}

configure_ssh() {
    local mode
    mode=$(stat -c '%a' "$work/ssh-original")
    publish "$work/sshd_config" /etc/ssh/sshd_config "$mode"
    if systemctl is-active --quiet ssh.service && ! systemctl reload ssh.service >/dev/null 2>&1; then
        publish "$work/ssh-original" /etc/ssh/sshd_config "$mode"
        fail 'could not reload sshd; restored the previous configuration'
    fi
}

configure_firewall() {
    publish "$work/firewall.nft" "$etc/firewall.nft" 600
    publish "$release/lib/$firewall_unit" "/etc/systemd/system/$firewall_unit" 644
    quiet 'could not load firewall unit' systemctl daemon-reload
    # Always reload the table in case another tool removed it from the kernel.
    quiet 'could not apply the firewall' systemctl reload-or-restart "$firewall_unit"
    quiet 'could not enable the firewall at boot' systemctl enable "$firewall_unit"
    local renderer
    for renderer in systemd-networkd NetworkManager; do
        local dropin=/etc/systemd/system/$renderer.service.d
        install -d -m 0755 "$dropin"
        publish "$release/lib/require-firewall.conf" "$dropin/90-ai-bootstrap-firewall.conf" 644
    done
    quiet 'could not configure boot dependency on firewall' systemctl daemon-reload
}

configure_mihomo() {
    publish "$work/mihomo" "$libexec/mihomo" 755
    publish "$work/profile.yaml" "$etc/mihomo.yaml" 640 ai-mihomo
    publish "$release/lib/mihomo-route.sh" "$libexec/mihomo-route.sh" 755
    publish "$release/lib/$mihomo_unit" "/etc/systemd/system/$mihomo_unit" 644
    quiet 'could not load mihomo unit' systemctl daemon-reload
    # Reapply even if files match: the previous run might have ended before restart.
    quiet 'mihomo could not start or configure the TUN route' systemctl restart "$mihomo_unit"
    quiet 'could not enable mihomo at boot' systemctl enable "$mihomo_unit"
}

verify_egress() {
    publish "$release/lib/resolved.conf" /etc/systemd/resolved.conf.d/90-ai-bootstrap.conf 644
    quiet 'could not apply DNS configuration' systemctl restart systemd-resolved.service
    quiet 'DNS query through mihomo failed' timeout 20 resolvectl query -4 example.com
    quiet 'HTTPS check through mihomo failed' curl -q --noproxy '*' -4 --fail --silent \
        --connect-timeout 10 --max-time 20 https://example.com/
}

run_step preflight
run_step packages
run_step directories
run_step validate_mihomo
run_step validate_network
run_step configure_ssh
run_step configure_firewall
run_step configure_mihomo
run_step verify_egress
