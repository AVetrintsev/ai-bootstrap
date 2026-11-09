#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

die() {
    printf 'ERROR sandbox: %s\n' "$1" >&2
    exit 1
}

[[ $EUID == 0 ]] || die 'run as root'
[[ $# -ge 2 ]] || die 'usage: ai-sandbox create PROJECT | shell PROJECT [COMMAND [ARG...]]'
action=$1
project=$2
shift 2
[[ $project =~ ^[a-z][a-z0-9-]{0,23}$ ]] || die 'invalid project name'
cd /

user=aisbx-$project
project_dir=/var/lib/ai-sandboxes/$project
disk=$project_dir/disk
home=$disk/home
unit=ai-bootstrap-sandbox.service
read -r enabled min_uid max_uid < /etc/ai-bootstrap/sandbox-settings

project_account() {
    uid=$(id -u "$user") || die 'unknown project'
    gid=$(id -g "$user")
    [[ $uid -ge $min_uid && $uid -le $max_uid && $(id -gn "$user") == "$user" &&
       $(getent passwd "$user") == *:"$home":/usr/sbin/nologin ]] || die 'incompatible project account'
}

as_project() {
    runuser -u "$user" -- env -i PATH=/usr/bin:/bin HOME="$home" USER="$user" \
        TERM="${TERM:-dumb}" XDG_RUNTIME_DIR="/run/user/$uid" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" "$@"
}

publish_unit() {
    local target=$1 staged
    staged=$(mktemp "${target}.XXXXXX")
    cat > "$staged"
    if cmp -s -- "$staged" "$target"; then
        rm -- "$staged"
    else
        chmod 0644 "$staged"
        mv -fT -- "$staged" "$target"
    fi
}

setup_disk() {
    local image=$project_dir/disk.ext4 mount_unit
    install -d -m 0711 "$project_dir"
    if [[ ! -d $disk ]]; then
        install -d -m 0700 "$disk"
    fi
    if [[ ! -e $image ]]; then
        # The final name is only published after the filesystem has been made.
        fallocate -l 4G "$image.new"
        mkfs.ext4 -q -F -m 0 -E nodiscard "$image.new"
        mv -T -- "$image.new" "$image"
    fi
    mount_unit=$(systemd-escape --path --suffix=mount "$disk")
    publish_unit "/etc/systemd/system/$mount_unit" <<EOF
[Mount]
What=$image
Where=$disk
Type=ext4
Options=loop,nodev,nosuid
EOF
    install -d -m 0755 "/etc/systemd/system/user@$uid.service.d" "/etc/systemd/system/user-$uid.slice.d"
    publish_unit "/etc/systemd/system/user@$uid.service.d/90-ai-bootstrap.conf" <<EOF
[Unit]
Requires=ai-bootstrap-firewall.service
After=ai-bootstrap-firewall.service
RequiresMountsFor=$disk
EOF
    publish_unit "/etc/systemd/system/user-$uid.slice.d/90-ai-bootstrap.conf" <<'EOF'
[Slice]
CPUQuota=100%
MemoryMax=384M
MemorySwapMax=0
TasksMax=256
EOF
    systemctl daemon-reload
    systemctl start "$mount_unit"
    if [[ ! -e $home ]]; then
        install -d -o "$uid" -g "$gid" -m 0700 "$home"
    fi
    [[ -d $home && ! -L $home && $(stat -c '%u:%g:%a' "$home") == "$uid:$gid:700" ]] ||
        die 'incompatible project home'
}

base_archive() {
    # shellcheck source=../release.env
    source /etc/ai-bootstrap/sandbox-release.env
    archive=/var/lib/ai-bootstrap/$SANDBOX_BASE
    if [[ ! -f $archive ]]; then
        curl -q --noproxy '*' --fail --silent --show-error --location --proto '=https' \
            --proto-redir '=https' --connect-timeout 10 --max-time 180 --output "$archive.download" \
            "https://cdimage.ubuntu.com/ubuntu-base/releases/26.04/release/$SANDBOX_BASE" ||
            die "could not download $SANDBOX_BASE"
        printf '%s  %s\n' "$SANDBOX_BASE_SHA256" "$archive.download" | sha256sum -c --status ||
            die 'base archive failed the release SHA-256 check'
        mv -T -- "$archive.download" "$archive"
    fi
    printf '%s  %s\n' "$SANDBOX_BASE_SHA256" "$archive" | sha256sum -c --status ||
        die 'cached base archive failed the release SHA-256 check'
}

create_project() {
    [[ $# == 0 ]] || die 'create expects one project name'
    [[ $enabled == yes ]] || die 'enable sandbox in config.yaml and rerun the installer'
    nft list chain inet ai_bootstrap sandbox_route >/dev/null || die 'sandbox firewall is not loaded'
    exec 9>/run/lock/ai-bootstrap-sandbox.lock
    flock 9
    if ! id "$user" >/dev/null 2>&1; then
        useradd --user-group --no-create-home --home-dir "$home" --shell /usr/sbin/nologin \
            -K "UID_MIN=$min_uid" -K "UID_MAX=$max_uid" "$user"
    fi
    project_account
    setup_disk
    systemctl start "user@$uid.service"
    if ! as_project podman container exists project; then
        base_archive
        local image=localhost/ai-bootstrap/base:$SANDBOX_BASE_SHA256
        if ! as_project podman image exists "$image"; then
            as_project podman import - "$image" < "$archive" >/dev/null
        fi
        as_project podman create --name project --hostname "$project" --user 0 --userns=private \
            --network slirp4netns:allow_host_loopback=false,enable_ipv6=false --dns 1.1.1.1 \
            --no-hosts --http-proxy=false --image-volume=ignore --log-driver=none \
            --security-opt=no-new-privileges --cap-drop=NET_RAW --cap-drop=MKNOD \
            --stop-timeout=5 --pull=never "$image" /usr/bin/sleep infinity >/dev/null
    fi
    as_project systemctl --user daemon-reload
    as_project systemctl --user enable --now "$unit"
    for attempt in {1..20}; do
        if as_project podman exec project /usr/bin/true >/dev/null 2>&1; then
            loginctl enable-linger "$user"
            return
        fi
        sleep 1
    done
    die 'container did not start; check the project user service journal'
}

case $action in
    create) create_project "$@" ;;
    shell)
        project_account
        args=(--interactive)
        if [[ -t 0 && -t 1 ]]; then
            args+=(--tty)
        fi
        if [[ $# == 0 ]]; then
            set -- /bin/bash -l
        fi
        as_project podman exec "${args[@]}" project "$@"
        ;;
    *) die 'expected create or shell' ;;
esac
