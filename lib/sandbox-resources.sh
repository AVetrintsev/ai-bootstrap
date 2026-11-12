#!/usr/bin/env bash
set -Eeuo pipefail
read -r memory_percent cpu_percent < /etc/ai-bootstrap/sandbox-resources
for percent in "$memory_percent" "$cpu_percent"; do
    [[ $percent =~ ^[0-9]+$ && $percent -ge 1 && $percent -le 100 ]] || exit 1
done
cpus=$(getconf _NPROCESSORS_ONLN)
# CPUQuota is relative to one CPU, while MemoryMax percentages use total VM RAM.
systemctl set-property --runtime ai-sandboxes.slice \
    "CPUQuota=$((cpus * cpu_percent))%" "MemoryMax=$memory_percent%" MemorySwapMax=0
