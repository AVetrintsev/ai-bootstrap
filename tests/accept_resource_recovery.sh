#!/usr/bin/env bash
set -Eeuo pipefail
[[ $EUID == 0 ]] || { echo 'Run as root on the target VM' >&2; exit 1; }
project=accept-resources
user=aisbx-$project
project_dir=/var/lib/ai-sandboxes/$project
home=$project_dir/disk/home
if id "$user" >/dev/null 2>&1 || [[ -e $project_dir ]]; then
    echo 'Resource recovery fixture already exists; refusing to change it' >&2
    exit 1
fi
uid=
cleanup() {
    if [[ -n $uid ]]; then
        systemctl stop "user@$uid.service"
        userdel "$user"
        rm -rf -- "/etc/systemd/system/user@$uid.service.d"
        rm -f -- "/etc/systemd/system/ai-sandboxes-$uid.slice"
        systemctl daemon-reload
    fi
    rm -rf -- "$project_dir"
}
trap cleanup EXIT
useradd --user-group --no-create-home --home-dir "$home" --shell /usr/sbin/nologin \
    -K UID_MIN=60001 -K UID_MAX=60999 "$user"
uid=$(id -u "$user")
install -d -o "$user" -g "$user" -m 0700 "$home"
systemctl start "user@$uid.service"
# Emulate an interrupted old create: user manager running, no container or enabled unit.
ai-sandbox resources "$project"
group=$(systemctl show "user@$uid.service" --property=ControlGroup --value)
[[ $group == /ai.slice/ai-sandboxes.slice/ai-sandboxes-$uid.slice/* ]]
pid=$(systemctl show "user@$uid.service" --property=MainPID --value)
systemctl reload ai-bootstrap-sandbox-resources.service
ai-sandbox resources "$project"
[[ $(systemctl show "user@$uid.service" --property=MainPID --value) == "$pid" ]]
echo 'OK incomplete sandbox migrated; repeated resource setup preserved its user manager'
