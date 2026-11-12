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

skip_step() {
    printf 'SKIP install/%s: %s\n' "$1" "$2"
}

run_step() {
    step=$1
    skip_reason=
    "$1"
    if [[ -n $skip_reason ]]; then
        skip_step "$step" "$skip_reason"
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
portal_service=ai-bootstrap-portal.service
portal_socket=ai-bootstrap-portal.socket
portal_state=/var/lib/ai-portal
local_portal_url=http://localhost:8080

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
    read -r portal_transport < "$work/portal-transport"
    read -r sandbox_enabled _ < "$work/sandbox-settings"
    [[ $(readlink -f /etc/resolv.conf) == /run/systemd/resolve/stub-resolv.conf ]] ||
        fail 'expected /etc/resolv.conf to use the systemd-resolved stub'
    quiet 'systemd-resolved must be running' systemctl is-active --quiet systemd-resolved.service
    [[ -n $(ss -Hltn 'sport = :22') ]] || fail 'SSH must already be listening on TCP port 22'
    reason=$(ss -Htn state established 'sport = :22' |
        python3 -I "$release/lib/config.py" --check-ssh "$work" 2>&1) ||
        fail "${reason%%$'\n'*}"
}

ensure_packages() {
    local package
    local -a missing=()
    for package in "$@"; do
        if [[ $(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null) != installed ]]; then
            missing+=("$package")
        fi
    done
    if (( ${#missing[@]} == 0 )); then
        skip_reason='required packages are installed'
        return
    fi
    quiet 'could not refresh package indexes' apt-get -q update
    quiet 'could not install required packages' apt-get -qy --no-install-recommends install "${missing[@]}"
}

packages() {
    ensure_packages ca-certificates curl gzip iproute2 nftables openssh-server procps
}

service_account() {
    local user=$1 home=$2 account
    if ! id "$user" >/dev/null 2>&1; then
        quiet "could not create $user" useradd --system --user-group --no-create-home \
            --home-dir "$home" --shell /usr/sbin/nologin "$user"
    fi
    account=$(getent passwd "$user")
    [[ $(id -u "$user") -gt 0 && $(id -u "$user") -lt 1000 &&
       $(id -gn "$user") == "$user" && $account == *:"$home":/usr/sbin/nologin ]] ||
        fail "existing $user account is incompatible"
}

directories() {
    service_account ai-mihomo "$state"
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
DenyUsers ai-mihomo ai-portal ai-github aisbx-*
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
    if [[ $portal_transport == local-http ]]; then
        grep -Eq '^allowtcpforwarding (yes|all|local)$' "$work/ssh-effective" &&
            grep -qx 'disableforwarding no' "$work/ssh-effective" &&
            grep -Eq '^permitopen (.* )?(any|127\.0\.0\.1:8080|127\.0\.0\.1:\*|\*:8080|\*:\*)( |$)' "$work/ssh-effective" ||
            fail 'sshd must allow local TCP forwarding to 127.0.0.1:8080'
    fi
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

sandbox_packages() {
    ensure_packages dbus-user-session e2fsprogs fuse-overlayfs podman slirp4netns uidmap
}

configure_sandbox() {
    install -d -m 0711 /var/lib/ai-sandboxes
    install -d -m 0755 /etc/systemd/user
    publish "$release/release.env" "$etc/sandbox-release.env" 600
    publish "$release/lib/sandbox.sh" /usr/local/sbin/ai-sandbox 755
    publish "$release/lib/gateway-address.py" "$libexec/gateway-address.py" 644
    publish "$release/lib/ai-bootstrap-sandbox.service" /etc/systemd/user/ai-bootstrap-sandbox.service 644
    publish "$work/sandbox-settings" "$etc/sandbox-settings" 600
    publish "$work/sandbox-resources" "$etc/sandbox-resources" 600
    publish "$release/lib/sandbox-resources.sh" "$libexec/sandbox-resources.sh" 755
    publish "$release/lib/ai-sandboxes.slice" /etc/systemd/system/ai-sandboxes.slice 644
    publish "$release/lib/ai-bootstrap-sandbox-resources.service" /etc/systemd/system/ai-bootstrap-sandbox-resources.service 644
    systemctl daemon-reload
    systemctl enable ai-bootstrap-sandbox-resources.service
    systemctl start ai-bootstrap-sandbox-resources.service
    systemctl reload ai-bootstrap-sandbox-resources.service
    local account uid
    while IFS=: read -r account _ uid _; do
        [[ $account == aisbx-* && $uid -ge 60001 && $uid -le 60999 ]] || continue
        /usr/local/sbin/ai-sandbox resources "${account#aisbx-}"
    done < <(getent passwd)
}

configure_model_gateway() {
    service_account ai-models /var/lib/ai-models
    install -d -o ai-models -g ai-models -m 0700 /var/lib/ai-models
    install -d -o root -g ai-models -m 0750 "$etc/model-keys"
    publish "$release/lib/model_gateway.py" "$libexec/model_gateway.py" 644
    publish "$release/lib/model-key.py" /usr/local/sbin/ai-model-key 755
    publish "$release/lib/qwen-setup.sh" "$libexec/qwen-setup.sh" 644
    publish "$release/lib/qwen-launcher" "$libexec/qwen-launcher" 755
    publish "$release/lib/ai-bootstrap-models.service" /etc/systemd/system/ai-bootstrap-models.service 644
    quiet 'could not load model gateway unit' systemctl daemon-reload
    if [[ $(cat "$work/model-gateway.json") == null ]]; then
        quiet 'could not stop model gateway' systemctl disable --now ai-bootstrap-models.service
        rm -f -- "$etc/model-gateway.json"
        skip_reason='model_gateway is not configured'
        return
    fi
    publish "$work/model-gateway.json" "$etc/model-gateway.json" 640 ai-models
    quiet 'could not start model gateway' systemctl restart ai-bootstrap-models.service
    quiet 'could not enable model gateway' systemctl enable ai-bootstrap-models.service
    local status
    for attempt in {1..20}; do
        status=$(curl -q --noproxy '*' --silent --max-time 2 --output /dev/null \
            --write-out '%{http_code}' http://127.0.0.1:18081/v1/models) || true
        [[ $status == 401 ]] && return
        sleep 1
    done
    fail 'model gateway did not reject an unauthenticated readiness request'
}

portal_admin() {
    runuser -u ai-portal -- /usr/bin/python3 -I "$libexec/portal.py" "$@"
}

portal_packages() {
    ensure_packages gunicorn python3-cryptography python3-qrcode python3-aiohttp
}

configure_canvas() {
    install -d -m 0755 /opt/ai-canvas
    if [[ ! -f /opt/ai-canvas/version || $(cat /opt/ai-canvas/version) != "$AGENT_CANVAS_VERSION" ]]; then
        quiet 'could not download pinned Canvas' curl -q --fail --silent --show-error --location \
            --proto '=https' --proto-redir '=https' --output "$work/canvas.tgz" \
            "https://registry.npmjs.org/@openhands/agent-canvas/-/agent-canvas-$AGENT_CANVAS_VERSION.tgz"
        printf '%s  %s\n' "$AGENT_CANVAS_SHA256" "$work/canvas.tgz" | sha256sum --check --status ||
            fail 'Canvas archive checksum mismatch'
        install -d -m 0755 "$work/canvas"
        tar -xzf "$work/canvas.tgz" -C "$work/canvas" --no-same-owner package/build
        cp -a "$work/canvas/package/build/." /opt/ai-canvas/build.new/
        chmod -R a+rX /opt/ai-canvas/build.new
        # Keep the previous frontend until extraction and integrity checks have succeeded.
        rm -rf -- /opt/ai-canvas/build.previous
        if [[ -d /opt/ai-canvas/build ]]; then mv /opt/ai-canvas/build /opt/ai-canvas/build.previous; fi
        mv /opt/ai-canvas/build.new /opt/ai-canvas/build
        printf '%s\n' "$AGENT_CANVAS_VERSION" > /opt/ai-canvas/version
    fi
    install -d -m 0750 -o root -g ai-portal /var/lib/ai-canvas /var/lib/ai-canvas/connections
    local file unit
    for file in canvas.py canvas-agent.py canvas-relay.py canvas-setup.sh; do
        publish "$release/lib/$file" "$libexec/$file" 644
    done
    publish "$release/lib/canvas-exec" "$libexec/canvas-exec" 755
    publish "$release/lib/nginx-canvas.conf" "$etc/nginx-canvas.conf" 644
    publish "$release/lib/ai-bootstrap-agent.service" /etc/systemd/user/ai-bootstrap-agent.service 644
    for unit in ai-bootstrap-canvas.socket ai-bootstrap-canvas.service \
        ai-bootstrap-canvas-relay@.socket ai-bootstrap-canvas-relay@.service; do
        publish "$release/lib/$unit" "/etc/systemd/system/$unit" 644
    done
    systemctl daemon-reload
    systemctl enable --now ai-bootstrap-canvas.socket
    systemctl try-restart ai-bootstrap-canvas.service
}

configure_codex() {
    ensure_packages socat
    service_account ai-codex /var/lib/ai-codex
    install -d -m 0700 -o ai-codex -g ai-codex /var/lib/ai-codex /var/lib/ai-codex/auth
    if [[ ! -x /opt/ai-codex/bin/codex-code-mode-host || ! -x /opt/ai-codex/bin/codex || $(/opt/ai-codex/bin/codex --version) != "codex-cli $CODEX_VERSION" ]]; then
        quiet 'could not download pinned Codex' curl -q --fail --silent --show-error --location \
            --proto '=https' --proto-redir '=https' --output "$work/codex.tgz" \
            "https://github.com/openai/codex/releases/download/rust-v$CODEX_VERSION/codex-package-x86_64-unknown-linux-musl.tar.gz"
        printf '%s  %s\n' "$CODEX_SHA256" "$work/codex.tgz" | sha256sum --check --status || fail 'Codex checksum mismatch'
        install -d -m 0755 "$work/codex-package"
        tar -xzf "$work/codex.tgz" -C "$work/codex-package" --no-same-owner
        chmod -R a+rX "$work/codex-package"
        rm -rf -- /opt/ai-codex.previous
        if [[ -d /opt/ai-codex ]]; then mv /opt/ai-codex /opt/ai-codex.previous; fi
        mv "$work/codex-package" /opt/ai-codex
    fi
    local file unit
    for file in codex_service.py codex-project.py codex-executor.py codex-client; do
        publish "$release/lib/$file" "$libexec/$file" 644
    done
    publish "$release/lib/codex-api-key" /usr/local/sbin/ai-codex-api-key 755
    for unit in ai-bootstrap-codex.socket ai-bootstrap-codex.service \
        ai-bootstrap-codex-project@.socket ai-bootstrap-codex-project@.service \
        ai-bootstrap-codex-executor@.socket ai-bootstrap-codex-executor@.service; do
        publish "$release/lib/$unit" "/etc/systemd/system/$unit" 644
    done
    systemctl daemon-reload
    systemctl enable --now ai-bootstrap-codex.socket
    systemctl try-restart ai-bootstrap-codex.service
}

configure_github_projects() {
    service_account ai-github /var/lib/ai-github
    install -d -m 0755 "$libexec/github-sandbox"
    publish "$release/lib/github_projects.py" "$libexec/github_projects.py" 644
    publish "$release/lib/github_provision.py" "$libexec/github_provision.py" 644
    publish "$release/lib/github-credential" "$libexec/github-sandbox/github-credential" 755
    local unit
    for unit in ai-bootstrap-github.socket ai-bootstrap-github.service \
        ai-bootstrap-projects.socket ai-bootstrap-projects@.service \
        ai-bootstrap-git@.socket ai-bootstrap-git@.service; do
        publish "$release/lib/$unit" "/etc/systemd/system/$unit" 644
    done
    quiet 'could not load the GitHub project units' systemctl daemon-reload
    quiet 'could not enable the GitHub project socket' systemctl enable --now ai-bootstrap-github.socket
    quiet 'could not reload the GitHub project service' systemctl try-restart ai-bootstrap-github.service
}

configure_portal() {
    service_account ai-portal "$portal_state"
    install -d -o ai-portal -g ai-portal -m 0700 "$portal_state"
    configure_canvas
    configure_codex
    configure_github_projects
    if [[ ! -e $etc/portal.key ]]; then
        [[ ! -e $portal_state/portal.sqlite3 ]] || fail 'restore the portal encryption key'
        head -c 32 /dev/urandom > "$work/portal.key"
        publish "$work/portal.key" "$etc/portal.key" 640 ai-portal
    fi
    [[ -f $etc/portal.key && ! -L $etc/portal.key &&
       $(stat -c '%a:%U:%G:%s' "$etc/portal.key") == '640:root:ai-portal:32' ]] ||
        fail 'unexpected portal encryption key permissions or size'

    publish "$release/lib/portal.py" "$libexec/portal.py" 644
    quiet 'could not initialize portal state' portal_admin init
    publish "$release/lib/$portal_service" "/etc/systemd/system/$portal_service" 644
    publish "$release/lib/$portal_socket" "/etc/systemd/system/$portal_socket" 644
    quiet 'could not load the portal units' systemctl daemon-reload
    quiet 'could not enable the portal socket' systemctl enable --now "$portal_socket"
    quiet 'could not start the portal' systemctl restart "$portal_service"
}

configure_local_http() {
    local unit=nginx.service dropin=/etc/systemd/system/nginx.service.d
    local nginx_config=$release/lib/nginx-local-http.conf
    local unit_config=$release/lib/nginx-local-http.service.conf
    if [[ -x /usr/sbin/nginx ]]; then
        quiet 'nginx rejected the candidate configuration' /usr/sbin/nginx -t -q -c "$nginx_config"
    fi
    # Install the override before apt can start the vendor service with the default site.
    install -d -m 0755 "$dropin"
    publish "$nginx_config" "$etc/nginx.conf" 644
    publish "$unit_config" "$dropin/90-ai-bootstrap.conf" 644
    quiet 'could not load the nginx override' systemctl daemon-reload
    if [[ $(dpkg-query -W -f='${db:Status-Status}' nginx 2>/dev/null) != installed ]]; then
        quiet 'could not refresh package indexes for nginx' apt-get -q update
        quiet 'could not install nginx' apt-get -qy --no-install-recommends install nginx
    fi
    quiet 'nginx rejected the installed configuration' /usr/sbin/nginx -t -q -c "$etc/nginx.conf"
    quiet 'could not apply the local nginx configuration' systemctl restart "$unit"
    quiet 'could not enable nginx at boot' systemctl enable "$unit"
}

verify_local_http() {
    local listeners path status
    listeners=$(ss -Hltnp | awk '/"nginx"/ {print $4}')
    [[ $listeners == '127.0.0.1:8080' ]] ||
        fail 'expected nginx to listen only on 127.0.0.1:8080'
    for path in / /api/status /api/test-stream; do
        status=$(curl -q --noproxy '*' --silent --max-time 10 --output /dev/null \
            --write-out '%{http_code}' "http://127.0.0.1:8080$path") ||
            fail 'could not reach nginx on 127.0.0.1:8080'
        [[ $status == 404 ]] || fail "expected a 404 for anonymous access to $path"
    done
}

show_activation() {
    local base_url=$1
    if [[ -t 1 ]]; then
        portal_admin activation "$base_url" 2>/dev/null || fail 'could not prepare owner activation'
    elif { : > /dev/tty; } 2>/dev/null; then
        portal_admin activation "$base_url" > /dev/tty 2>/dev/null ||
            fail 'could not prepare owner activation'
    else
        quiet 'could not prepare owner activation' portal_admin activation "$base_url"
        printf '%s\n' 'To display any pending activation URL in a private terminal, run:' \
            "  sudo runuser -u ai-portal -- /usr/bin/python3 -I $libexec/portal.py activation $base_url"
    fi
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
if [[ $sandbox_enabled == yes ]]; then
    run_step sandbox_packages
fi
run_step configure_sandbox
run_step configure_model_gateway
if [[ $portal_transport == local-http ]]; then
    run_step portal_packages
    run_step configure_portal
    run_step configure_local_http
    run_step verify_local_http
    skip_step public_dns 'local-http needs no public DNS'
    skip_step acme 'local-http needs no TLS certificate'
    skip_step globalping 'local-http has no public web endpoint'
    printf '%s\n' 'From an allowed administrator address, run:' \
        '  ssh -4 -NT -o ExitOnForwardFailure=yes -L 127.0.0.1:8080:127.0.0.1:8080 USER@AI_HOST' \
        'Open the owner activation URL in your browser. If already activated, use http://localhost:8080.'
    step=owner_activation
    show_activation "$local_portal_url"
fi
