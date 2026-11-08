#!/usr/bin/env bash
set -Eeuo pipefail

for attempt in {1..50}; do
    if ip -4 addr show dev ai-mihomo up 2>/dev/null | grep -q 'inet '; then
        break
    fi
    sleep 0.2
done

# The physical interface remains the reverse path for packets injected by TUN.
sysctl -qw net.ipv4.conf.ai-mihomo.rp_filter=2
expected=$(ip -4 rule show priority 100 fwmark 0xa1b0 table 41110)
existing=$(ip -4 rule show priority 100)
if [[ -z $existing ]]; then
    ip -4 rule add priority 100 fwmark 0xa1b0 lookup 41110
elif [[ $existing != "$expected" ]]; then
    exit 1
fi
ip -4 route replace table 41110 default dev ai-mihomo
