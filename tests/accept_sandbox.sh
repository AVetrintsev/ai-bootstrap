#!/usr/bin/env bash
set -Eeuo pipefail

[[ $EUID == 0 ]] || { echo 'Run on the target VM as root' >&2; exit 1; }
first=accept-a
second=accept-b
marker=/root/ai-bootstrap-acceptance
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

check_isolation() {
    for project in "$first" "$second"; do
        [[ $(ai-sandbox shell "$project" id -u) == 0 ]]
        uid=$(id -u "aisbx-$project")
        [[ $uid -ge 61000 && $uid -le 61999 ]]
        user=aisbx-$project
        pid=$(runuser -u "$user" -- env -i PATH=/usr/bin:/bin \
            HOME="/var/lib/ai-sandboxes/$project/disk/home" XDG_RUNTIME_DIR="/run/user/$uid" \
            podman inspect --format '{{.State.Pid}}' project)
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
