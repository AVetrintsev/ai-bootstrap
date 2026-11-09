#!/usr/bin/env bash
set -Eeuo pipefail

[[ $EUID == 0 ]] || { echo 'Run on the target VM as root' >&2; exit 1; }
first=accept-a
second=accept-b
marker=/root/ai-bootstrap-acceptance
read -r enabled min_uid max_uid < /etc/ai-bootstrap/sandbox-settings
[[ $enabled == yes ]]
vm_ipv4=$(ip -4 route get 1.1.1.1 | awk '{for (i=1; i<NF; i++) if ($i=="src") print $(i+1)}')

deny() {
    if "$@"; then
        printf 'Expected failure: %s\n' "$*" >&2
        exit 1
    fi
}

connect() {
    ai-sandbox shell "$1" timeout 5 bash -c 'exec 3<>"/dev/tcp/$1/$2"' _ "$2" "$3"
}

check_internal_tcp() (
    namespace=ai-sandbox-acceptance
    host_if=aisbx-test0
    peer_if=aisbx-test1
    address=10.252.6.2
    port=38006
    element="{ $address . $port }"
    ip netns add "$namespace"
    cleanup() {
        if [[ ${allowed:-} ]]; then
            nft delete element inet ai_bootstrap sandbox_internal_tcp "$element"
        fi
        if [[ ${server:-} ]]; then
            kill "$server" 2>/dev/null || true
        fi
        if [[ ${created_link:-} ]]; then
            ip link delete "$host_if" 2>/dev/null || true
        fi
        ip netns delete "$namespace"
    }
    trap cleanup EXIT
    ip link add "$host_if" type veth peer name "$peer_if" netns "$namespace"
    created_link=yes
    ip addr add 10.252.6.1/30 dev "$host_if"
    ip link set "$host_if" up
    ip -n "$namespace" addr add "$address/30" dev "$peer_if"
    ip -n "$namespace" link set "$peer_if" up
    ip netns exec "$namespace" python3 -m http.server "$port" --bind "$address" --directory /var/empty &
    server=$!
    ready=no
    for attempt in {1..10}; do
        if ip netns exec "$namespace" bash -c 'exec 3<>"/dev/tcp/$1/$2"' _ "$address" "$port" 2>/dev/null; then
            ready=yes
            break
        fi
        sleep 1
    done
    [[ $ready == yes ]]
    deny connect "$first" "$address" "$port"
    nft add element inet ai_bootstrap sandbox_internal_tcp "$element"
    allowed=yes
    connect "$first" "$address" "$port"
)

check_isolation() {
    for project in "$first" "$second"; do
        [[ $(ai-sandbox shell "$project" id -u) == 0 ]]
        uid=$(id -u "aisbx-$project")
        [[ $uid -ge $min_uid && $uid -le $max_uid ]]
        user=aisbx-$project
        pid=$(
            cd /
            runuser -u "$user" -- env -i PATH=/usr/bin:/bin \
                HOME="/var/lib/ai-sandboxes/$project/disk/home" XDG_RUNTIME_DIR="/run/user/$uid" \
                podman inspect --format '{{.State.Pid}}' project
        )
        [[ $(awk '/^Uid:/ {print $2}' "/proc/$pid/status") == "$uid" ]]
        grep -q "^0::/user.slice/user-$uid.slice/" "/proc/$pid/cgroup"
        slice=/sys/fs/cgroup/user.slice/user-$uid.slice
        read -r quota period < "$slice/cpu.max"
        [[ $quota == "$period" ]]
        [[ $(<"$slice/memory.max") == 402653184 ]]
        [[ $(<"$slice/pids.max") == 256 ]]
        for path in /var/lib/ai-sandboxes /home/owner/.ssh /root/.ssh /etc/ai-bootstrap \
            /run/podman/podman.sock "/run/user/$uid/podman/podman.sock"; do
            deny ai-sandbox shell "$project" test -e "$path"
        done
        deny ai-sandbox shell "$project" fallocate -l 5G /tmp/ai-bootstrap-oversized
        ai-sandbox shell "$project" rm -f /tmp/ai-bootstrap-oversized
    done
    [[ $(id -u "aisbx-$first") != "$(id -u "aisbx-$second")" ]]
    deny ai-sandbox shell "$second" test -e "$marker"
    deny connect "$first" 10.0.2.2 8080
    deny connect "$first" "$vm_ipv4" 22
    deny connect "$first" "$vm_ipv4" 9090
    deny connect "$first" 169.254.169.254 80
    deny connect "$first" 10.0.0.1 443
}

case ${1:-} in
    before)
        ai-sandbox create "$first"
        ai-sandbox create "$second"
        ai-sandbox shell "$first" touch "$marker"
        check_isolation
        check_internal_tcp
        ai-sandbox shell "$first" bash -c '
            set -eu
            if [[ -n $1 ]]; then
                printf "deb %s resolute main universe\n" "$1" >/tmp/ai-bootstrap-acceptance.list
                options=(-o Dir::Etc::sourcelist=/tmp/ai-bootstrap-acceptance.list -o Dir::Etc::sourceparts=-)
            else
                options=()
            fi
            apt-get "${options[@]}" update
            apt-get "${options[@]}" install -y --no-install-recommends hello
        ' _ "${2:-}"
        public_ip=$(ai-sandbox shell "$first" getent ahostsv4 example.com | awk 'NR==1 {print $1}')
        [[ -n $public_ip ]]
        connect "$first" "$public_ip" 443
        trap 'systemctl start ai-bootstrap-mihomo.service' EXIT
        systemctl stop ai-bootstrap-mihomo.service
        deny connect "$first" "$public_ip" 443
        deny ai-sandbox shell "$first" timeout 8 getent ahostsv4 example.com
        ;;
    after)
        ai-sandbox shell "$first" test -f "$marker"
        [[ $(ai-sandbox shell "$first" hello) == 'Hello, world!' ]]
        check_isolation
        ai-sandbox create "$first"
        ai-sandbox shell "$first" test -f "$marker"
        ai-sandbox shell "$first" hello
        ;;
    *) echo 'Usage: accept_sandbox.sh before [APT_MIRROR] | after' >&2; exit 1 ;;
esac
echo "OK sandbox acceptance ($1)"
