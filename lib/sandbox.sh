#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

die() {
    printf 'ERROR sandbox: %s\n' "$1" >&2
    exit 1
}

[[ $EUID == 0 ]] || die 'run as root'
[[ $# -ge 2 ]] || die 'usage: ai-sandbox create PROJECT | qwen-setup PROJECT | shell PROJECT [COMMAND [ARG...]]'
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

configure_resources() {
    local slice=ai-sandboxes-$uid.slice group restart=no manager_active=no container_active=no
    group=$(systemctl show "user@$uid.service" --property=ControlGroup --value)
    if systemctl is-active --quiet "user@$uid.service"; then
        manager_active=yes
        if as_project systemctl --user is-active --quiet "$unit"; then
            container_active=yes
        fi
    fi
    if [[ $manager_active == yes && $group != /ai.slice/ai-sandboxes.slice/$slice/* ]]; then
        # Stop the container while its original user manager and cgroup still exist.
        if [[ $container_active == yes ]]; then
            as_project systemctl --user stop "$unit"
        fi
        systemctl stop "user@$uid.service"
        restart=yes
    fi
    install -d -m 0755 "/etc/systemd/system/user@$uid.service.d"
    publish_unit "/etc/systemd/system/$slice" <<'EOF'
[Slice]
TasksMax=256
EOF
    publish_unit "/etc/systemd/system/user@$uid.service.d/95-sandbox-resources.conf" <<EOF
[Unit]
Requires=ai-bootstrap-sandbox-resources.service
After=ai-bootstrap-sandbox-resources.service
[Service]
Slice=$slice
EOF
    rm -f -- "/etc/systemd/system/user-$uid.slice.d/90-ai-bootstrap.conf" \
        "/etc/systemd/system/user-$uid.slice.d/95-qwen.conf"
    systemctl daemon-reload
    systemctl start ai-bootstrap-sandbox-resources.service
    if [[ $restart == yes || -f /var/lib/systemd/linger/$user ]]; then
        # Reparent the user manager once; persistent container storage stays in place.
        systemctl start "user@$uid.service"
        if [[ $container_active == yes && $restart == yes ]]; then
            as_project systemctl --user start "$unit"
        elif [[ $container_active != yes ]]; then
            # A failed initial create may have a user manager but no container yet.
            if [[ $restart == no && $manager_active == yes ]] ||
                ! as_project systemctl --user is-enabled --quiet "$unit" 2>/dev/null; then
                return
            fi
        fi
        for attempt in {1..30}; do
            if as_project podman exec project /usr/bin/true >/dev/null 2>&1; then
                return
            fi
            sleep 1
        done
        die 'sandbox did not restart after applying the shared resource pool'
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
        fallocate -l 8G "$image.new"
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
    install -d -m 0755 "/etc/systemd/system/user@$uid.service.d"
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
    systemctl daemon-reload
    systemctl start "$mount_unit"
    if [[ ! -e $home ]]; then
        install -d -o "$uid" -g "$gid" -m 0700 "$home"
    fi
    [[ -d $home && ! -L $home && $(stat -c '%u:%g:%a' "$home") == "$uid:$gid:700" ]] ||
        die 'incompatible project home'
    configure_resources
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

gateway_address() {
    python3 -I /usr/local/libexec/ai-bootstrap/gateway-address.py
}

configure_qwen() {
    [[ -f /etc/ai-bootstrap/model-gateway.json ]] || return 0
    local gateway_host client
    gateway_host=$(gateway_address) || die 'could not determine the model gateway address'
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

configure_tools() {
    local image=$project_dir/disk.ext4 loop generation previous
    # Grow in place: preserve the rootless container, packages and all project files.
    if [[ $(stat -c %s "$image") -lt 8589934592 ]]; then
        fallocate -l 8G "$image"
    fi
    loop=$(findmnt -n -o SOURCE --target "$disk")
    [[ $loop == /dev/loop* ]] || die 'project disk is not mounted on a loop device'
    losetup -c "$loop"
    resize2fs "$loop" >/dev/null
    source /etc/ai-bootstrap/sandbox-release.env
    as_project podman exec --interactive project bash -s -- "$RIPGREP_VERSION" "$AST_GREP_VERSION" \
        "$SERENA_VERSION" "$CODEGRAPHCONTEXT_VERSION" < /usr/local/libexec/ai-bootstrap/project-tools-setup.sh
    generation=$(sha256sum /usr/local/libexec/ai-bootstrap/project-graph.py \
        /etc/ai-bootstrap/sandbox-release.env | sha256sum | cut -d ' ' -f 1)
    previous=$(as_project podman exec project cat /opt/ai-project-tools/generation 2>/dev/null || true)
    as_project podman cp /usr/local/libexec/ai-bootstrap/project-graph.py project:/opt/ai-project-tools/graph.py
    as_project systemctl --user daemon-reload
    as_project systemctl --user enable ai-bootstrap-project-tools.service
    if [[ $previous != "$generation" ]]; then
        as_project systemctl --user restart ai-bootstrap-project-tools.service
    else
        as_project systemctl --user start ai-bootstrap-project-tools.service
    fi
    for attempt in {1..180}; do
        if as_project podman exec project python3 -I /opt/ai-project-tools/graph.py ready >/dev/null 2>&1; then
            printf '%s' "$generation" | as_project podman exec --interactive project sh -c 'cat > /opt/ai-project-tools/generation'
            return
        fi
        sleep 2
    done
    die 'project index is not ready; retry setup in the same sandbox'
}

configure_codex() {
    [[ -x /opt/ai-codex/bin/codex ]] || die 'install Codex on the platform before connecting Canvas'
    source /etc/ai-bootstrap/sandbox-release.env
    local installed generation previous
    installed=$(as_project podman exec project cat /opt/ai-codex/ai-bootstrap-version 2>/dev/null || true)
    if [[ $installed != "$CODEX_VERSION:$CODEX_SHA256" ]]; then
        as_project podman exec project sh -eu -c 'rm -rf /opt/ai-codex.next; install -d -m 0755 /opt/ai-codex.next'
        as_project podman cp /opt/ai-codex/. project:/opt/ai-codex.next/
        as_project podman exec project sh -eu -c '
            rm -rf /opt/ai-codex.previous
            if [ -d /opt/ai-codex ]; then mv /opt/ai-codex /opt/ai-codex.previous; fi
            mv /opt/ai-codex.next /opt/ai-codex
            printf "%s" "$1" > /opt/ai-codex/ai-bootstrap-version
        ' _ "$CODEX_VERSION:$CODEX_SHA256"
    fi
    as_project podman cp /usr/local/libexec/ai-bootstrap/codex-client project:/usr/local/bin/codex
    as_project podman exec project chmod 0755 /usr/local/bin/codex
    if [[ -f /var/lib/ai-codex/projects/$project/chatgpt/auth.lock &&
          ! -L /var/lib/ai-codex/projects/$project/chatgpt/auth.lock ]]; then
        systemctl stop "ai-bootstrap-codex-project@$project.socket" "ai-bootstrap-codex-project@$project.service"
    fi
    runuser -u ai-codex -- python3 -I /usr/local/libexec/ai-bootstrap/codex-project.py "$project"
    generation=$(sha256sum /usr/local/libexec/ai-bootstrap/codex_service.py \
        /usr/local/libexec/ai-bootstrap/codex-executor.py /usr/local/libexec/ai-bootstrap/codex-project.py \
        /etc/systemd/system/ai-bootstrap-codex-project@.service \
        /etc/systemd/system/ai-bootstrap-codex-executor@.service /etc/ai-bootstrap/sandbox-release.env | sha256sum | cut -d ' ' -f 1)
    previous=$(cat "$project_dir/codex-generation" 2>/dev/null || true)
    systemctl enable --now "ai-bootstrap-codex-executor@$project.socket" "ai-bootstrap-codex-project@$project.socket"
    if [[ $previous != "$generation" ]]; then
        systemctl try-restart "ai-bootstrap-codex-project@$project.service" "ai-bootstrap-codex-executor@$project.service"
        printf '%s' "$generation" > "$project_dir/codex-generation"
    fi
}

configure_canvas() {
    [[ $project =~ ^gh-[1-9][0-9]{0,18}$ && $# == 0 && -f $github_marker ]] || die 'invalid GitHub project'
    [[ -f /etc/ai-bootstrap/model-gateway.json ]] || die 'configure the model gateway before Canvas'
    exec 9>/run/lock/ai-bootstrap-sandbox.lock
    flock 9
    project_account
    configure_qwen
    configure_tools
    configure_codex
    source /etc/ai-bootstrap/sandbox-release.env
    local generation previous
    generation="$AGENT_SERVER_VERSION:$(sha256sum /usr/local/libexec/ai-bootstrap/canvas-agent.py | cut -d ' ' -f 1)"
    previous=$(as_project podman exec project cat /opt/ai-canvas-control/generation 2>/dev/null || true)
    as_project podman exec project test -d /workspace/repo/.git
    as_project podman exec --interactive project bash -s -- "$AGENT_SERVER_VERSION" \
        < /usr/local/libexec/ai-bootstrap/canvas-setup.sh
    python3 -I - "$project" <<'PY'
import grp,json,os,pathlib,secrets,sys,uuid
path=pathlib.Path('/var/lib/ai-canvas/connections')/(sys.argv[1]+'.json')
settings=json.loads(path.read_text()) if path.exists() else {'key':secrets.token_urlsafe(32)}
if 'conversation_id' not in settings:
    settings['conversation_id']=str(uuid.uuid4())
if 'codex_conversation_id' not in settings:
    settings['codex_conversation_id']=str(uuid.uuid4())
path.write_text(json.dumps(settings))
path.chmod(0o640)
os.chown(path,0,grp.getgrnam('ai-portal').gr_gid)
PY
    as_project podman exec project install -d -m 0700 /opt/ai-canvas-control /root/.config/ai-bootstrap
    as_project podman cp /usr/local/libexec/ai-bootstrap/canvas-agent.py project:/opt/ai-canvas-control/agent.py
    # The host's root-owned record is not readable by the sandbox user. Send it on stdin.
    as_project podman exec --interactive project sh -c 'umask 077; cat > /root/.config/ai-bootstrap/canvas.json' \
        < "/var/lib/ai-canvas/connections/$project.json"
    as_project systemctl --user daemon-reload
    as_project systemctl --user enable ai-bootstrap-agent.service
    if [[ $previous != "$generation" ]]; then
        as_project systemctl --user restart ai-bootstrap-agent.service
    else
        as_project systemctl --user start ai-bootstrap-agent.service
    fi
    if ! as_project podman exec project python3 -I /opt/ai-canvas-control/agent.py configure; then
        # A retry must also repair a running but unresponsive server.
        as_project systemctl --user restart ai-bootstrap-agent.service
        as_project podman exec project python3 -I /opt/ai-canvas-control/agent.py configure
    fi
    printf '%s' "$generation" | as_project podman exec --interactive project sh -c 'cat > /opt/ai-canvas-control/generation'
    systemctl enable --now "ai-bootstrap-canvas-relay@$project.socket"
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
    if [[ -f /etc/systemd/system/ai-bootstrap-canvas-relay@.socket ]]; then
        systemctl disable --now "ai-bootstrap-canvas-relay@$project.socket"
        systemctl stop "ai-bootstrap-canvas-relay@$project.service"
        rm -f -- "/var/lib/ai-canvas/connections/$project.json"
    fi
    if [[ -f /etc/systemd/system/ai-bootstrap-codex-project@.socket ]]; then
        systemctl disable --now "ai-bootstrap-codex-project@$project.socket" "ai-bootstrap-codex-executor@$project.socket"
        systemctl stop "ai-bootstrap-codex-project@$project.service" "ai-bootstrap-codex-executor@$project.service"
        rm -rf -- "/var/lib/ai-codex/projects/$project"
    fi
    if ! id "$user" >/dev/null 2>&1; then
        [[ ! -e $project_dir/disk.ext4 ]] && ! mountpoint -q -- "$disk" || die 'project account is missing'
        return 0
    fi
    project_account
    loginctl disable-linger "$user"
    systemctl stop "user@$uid.service" "ai-sandboxes-$uid.slice" "user-$uid.slice" "$github_socket"
    mount_unit=$(systemd-escape --path --suffix=mount "$disk")
    if [[ -e /etc/systemd/system/$mount_unit ]]; then
        systemctl stop "$mount_unit"
    fi
    ! mountpoint -q -- "$disk" || die 'project disk is still mounted'
    rm -f -- "$project_dir/disk.ext4" "$project_dir/disk.ext4.new" \
        "/etc/systemd/system/$mount_unit" \
        "/etc/systemd/system/user@$uid.service.d/90-ai-bootstrap.conf" \
        "/etc/systemd/system/user-$uid.slice.d/90-ai-bootstrap.conf"
    rm -f -- "/etc/systemd/system/user-$uid.slice.d/95-qwen.conf" \
        "/etc/systemd/system/user@$uid.service.d/95-sandbox-resources.conf" \
        "/etc/systemd/system/ai-sandboxes-$uid.slice"
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
    canvas-setup) configure_canvas "$@" ;;
    tools-setup)
        [[ $project =~ ^gh-[1-9][0-9]{0,18}$ && $# == 0 && -f $github_marker ]] || die 'invalid GitHub project'
        exec 9>/run/lock/ai-bootstrap-sandbox.lock
        flock 9
        project_account
        configure_tools
        ;;
    codex-setup)
        [[ $project =~ ^gh-[1-9][0-9]{0,18}$ && $# == 0 && -f $github_marker ]] || die 'invalid GitHub project'
        exec 9>/run/lock/ai-bootstrap-sandbox.lock
        flock 9
        project_account
        configure_tools
        configure_codex
        ;;
    resources)
        [[ $# == 0 ]] || die 'resources expects one project name'
        project_account
        configure_resources
        ;;
    qwen-setup)
        [[ $# == 0 ]] || die 'qwen-setup expects one project name'
        project_account
        configure_qwen
        ;;
    shell)
        project_account
        gateway_env=()
        if [[ -f /etc/ai-bootstrap/model-gateway.json ]] && gateway_host=$(gateway_address 2>/dev/null); then
            gateway_env=(--env "AI_GATEWAY_HOST=$gateway_host")
        fi
        args=(--interactive)
        if [[ -t 0 && -t 1 ]]; then
            args+=(--tty)
        fi
        if [[ $# == 0 ]]; then
            set -- /bin/bash -l
        fi
        as_project podman exec "${args[@]}" "${gateway_env[@]}" project "$@"
        ;;
    *) die 'expected create, qwen-setup or shell' ;;
esac
