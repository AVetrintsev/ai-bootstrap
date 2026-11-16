#!/bin/sh
set -eu

case "${RENEWED_LINEAGE:-}" in
    /etc/letsencrypt/live/ai-bootstrap-*)
        systemctl reload nginx.service
        ;;
esac
