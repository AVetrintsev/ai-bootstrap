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
github_marker=$project_dir/github-managed
unit=ai-bootstrap-sandbox.service
github_socket=ai-bootstrap-git@$project.socket
github_mounts=()
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
    local image=$project_dir/disk.ext4 mount_unit socket_requires=
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
    if [[ ${#github_mounts[@]} != 0 ]]; then
        socket_requires=$'Requires='"$github_socket"$'\nAfter='"$github_socket"
    fi
    publish_unit "/etc/systemd/system/user@$uid.service.d/90-ai-bootstrap.conf" <<EOF
[Unit]
Requires=ai-bootstrap-firewall.service
After=ai-bootstrap-firewall.service
RequiresMountsFor=$disk
$socket_requires
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

configure_qwen() {
    [[ -f /etc/ai-bootstrap/model-gateway.json ]] || return 0
    local gateway_host client
    gateway_host=$(python3 -I -c '
import ipaddress,json,subprocess
routes=json.loads(subprocess.check_output(["ip","-4","-j","route","show","default"]))
for route in routes:
 for interface in json.loads(subprocess.check_output(["ip","-4","-j","address","show","dev",route["dev"]])):
  for address in interface["addr_info"]:
   if address["scope"] == "global":
    print(ipaddress.IPv4Address(address["local"])); raise SystemExit
raise SystemExit("No host IPv4 address for the model gateway")
') || die 'could not determine the model gateway address'
    install -d -m 0755 "/etc/systemd/system/user-$uid.slice.d"
    publish_unit "/etc/systemd/system/user-$uid.slice.d/95-qwen.conf" <<'EOF'
[Slice]
MemoryMax=768M
EOF
    systemctl daemon-reload
    systemctl set-property --runtime "user-$uid.slice" MemoryMax=768M
    # The installed release pins Qwen; all package changes remain inside this container.
    source /etc/ai-bootstrap/sandbox-release.env
    as_project podman exec --interactive project /bin/bash -s -- "$QWEN_CODE_VERSION" \
        < /usr/local/libexec/ai-bootstrap/qwen-setup.sh
    as_project podman cp /usr/local/libexec/ai-bootstrap/qwen-launcher project:/usr/local/bin/qwen
    as_project podman exec project chmod 0755 /usr/local/bin/qwen
    client=$(runuser -u ai-models -- python3 -I /usr/local/libexec/ai-bootstrap/model_gateway.py provision "$project")
    printf '%s' "$client" | as_project podman exec --interactive --env "AI_GATEWAY_HOST=$gateway_host" project python3 -I -c '
import json,os,pathlib,sys
settings=json.load(sys.stdin)
settings["base_url"]="http://"+os.environ["AI_GATEWAY_HOST"]+":18081/v1"
directory=pathlib.Path("/root/.config/ai-bootstrap")
directory.mkdir(mode=0o700,parents=True,exist_ok=True)
path=directory/"model-gateway.json"
path.write_text(json.dumps(settings))
path.chmod(0o600)
'
}

create_project() {
    [[ $# == 0 ]] || die 'create expects one project name'
    [[ $enabled == yes ]] || die 'enable sandbox in config.yaml and rerun the installer'
    nft list chain inet ai_bootstrap sandbox_route >/dev/null || die 'sandbox firewall is not loaded'
    exec 9>/run/lock/ai-bootstrap-sandbox.lock
    flock 9
    if [[ ${#github_mounts[@]} != 0 && ! -f $github_marker ]]; then
        if id "$user" >/dev/null 2>&1 || [[ -e $disk || -e $project_dir/disk.ext4 ]]; then
            die 'project name is already used by a standalone sandbox'
        fi
        install -d -m 0711 "$project_dir"
        : > "$github_marker"
    fi
    if ! id "$user" >/dev/null 2>&1; then
        useradd --user-group --no-create-home --home-dir "$home" --shell /usr/sbin/nologin \
            -K "UID_MIN=$min_uid" -K "UID_MAX=$max_uid" "$user"
    fi
    project_account
    setup_disk
    if [[ ${#github_mounts[@]} != 0 ]]; then
        systemctl start "$github_socket"
    fi
    systemctl start "user@$uid.service"
    if ! as_project podman container exists project; then
        base_archive
        local image=localhost/ai-bootstrap/base:$SANDBOX_BASE_SHA256
        if ! as_project podman image exists "$image"; then
            as_project podman import - "$image" < "$archive" >/dev/null
        fi
        as_project podman --cgroup-manager=systemd create --name project --hostname "$project" \
            --user 0 --userns=keep-id:uid=0,gid=0 \
            --network slirp4netns:allow_host_loopback=false,enable_ipv6=false --dns 1.1.1.1 \
            --no-hosts --http-proxy=false --image-volume=ignore --log-driver=none \
            --security-opt=no-new-privileges --cap-drop=NET_RAW --cap-drop=MKNOD \
            "${github_mounts[@]}" --stop-timeout=5 --pull=never "$image" /usr/bin/sleep infinity >/dev/null
    fi
    as_project systemctl --user daemon-reload
    as_project systemctl --user enable --now "$unit"
    for attempt in {1..20}; do
        if as_project podman exec project /usr/bin/true >/dev/null 2>&1; then
            loginctl enable-linger "$user"
            configure_qwen
            return
        fi
        sleep 1
    done
    die 'container did not start; check the project user service journal'
}

github_create() {
    [[ $project =~ ^gh-[1-9][0-9]{0,18}$ && $# == 1 &&
       $1 =~ ^[a-zA-Z0-9][a-zA-Z0-9-]{0,38}/[a-zA-Z0-9_.-]{1,100}$ &&
       ${1#*/} != . && ${1#*/} != .. ]] || die 'invalid GitHub project'
    local repository=$1
    github_mounts=(
        --volume "/run/ai-bootstrap-git/$project:/run/ai-bootstrap-git:ro"
        --volume /usr/local/libexec/ai-bootstrap/github-sandbox:/usr/local/lib/ai-bootstrap:ro
    )
    create_project
    as_project podman exec --env DEBIAN_FRONTEND=noninteractive project /bin/bash -eu -c '
        url=https://github.com/$1.git
        install -d /workspace
        cd /workspace
        exec 8>.ai-bootstrap-setup.lock
        flock 8
        dpkg --configure -a
        if ! command -v git >/dev/null || ! command -v python3 >/dev/null || [[ ! -s /etc/ssl/certs/ca-certificates.crt ]]; then
            apt-get -q update
            apt-get -qy --no-install-recommends install git ca-certificates python3-minimal
        fi
        git config --system credential.https://github.com.useHttpPath true
        git config --system credential.https://github.com.helper /usr/local/lib/ai-bootstrap/github-credential
        if [[ -d repo/.git ]]; then
            git -C repo remote set-url origin "$url"
            exit
        fi
        if [[ -e repo || -L repo ]]; then
            echo "The project checkout path is occupied" >&2
            exit 1
        fi
        rm -rf -- .ai-bootstrap-clone
        GIT_TERMINAL_PROMPT=0 git clone -- "$url" .ai-bootstrap-clone
        mv -T -- .ai-bootstrap-clone repo
    ' _ "$repository"
}

github_delete() {
    [[ $project =~ ^gh-[1-9][0-9]{0,18}$ && $# == 0 ]] || die 'invalid GitHub project'
    local mount_unit
    exec 9>/run/lock/ai-bootstrap-sandbox.lock
    flock 9
    if [[ ! -f $github_marker ]]; then
        if id "$user" >/dev/null 2>&1 || [[ -e $disk || -e $project_dir/disk.ext4 ]]; then
            die 'refusing to delete a standalone sandbox'
        fi
        return 0
    fi
    if [[ -f /var/lib/ai-models/gateway.sqlite3 ]]; then
        runuser -u ai-models -- python3 -I /usr/local/libexec/ai-bootstrap/model_gateway.py revoke "$project"
    fi
    if ! id "$user" >/dev/null 2>&1; then
        [[ ! -e $project_dir/disk.ext4 ]] && ! mountpoint -q -- "$disk" || die 'project account is missing'
        return 0
    fi
    project_account
    loginctl disable-linger "$user"
    systemctl stop "user@$uid.service" "user-$uid.slice" "$github_socket"
    mount_unit=$(systemd-escape --path --suffix=mount "$disk")
    if [[ -e /etc/systemd/system/$mount_unit ]]; then
        systemctl stop "$mount_unit"
    fi
    ! mountpoint -q -- "$disk" || die 'project disk is still mounted'
    rm -f -- "$project_dir/disk.ext4" "$project_dir/disk.ext4.new" \
        "/etc/systemd/system/$mount_unit" \
        "/etc/systemd/system/user@$uid.service.d/90-ai-bootstrap.conf" \
        "/etc/systemd/system/user-$uid.slice.d/90-ai-bootstrap.conf"
    rm -f -- "/etc/systemd/system/user-$uid.slice.d/95-qwen.conf"
    systemctl daemon-reload
    userdel "$user"
}

case $action in
    create)
        [[ $project != gh-* ]] || die 'gh- is reserved for GitHub projects'
        create_project "$@"
        ;;
    github-create) github_create "$@" ;;
    github-delete) github_delete "$@" ;;
    shell)
        project_account
        configure_qwen
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
