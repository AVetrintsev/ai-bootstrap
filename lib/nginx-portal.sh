#!/usr/bin/env bash

local_servers() {
    cat <<'EOF'
    server {
        listen 127.0.0.1:8080 default_server;
        return 404;
    }
    server {
        listen 127.0.0.1:8080;
        server_name localhost 127.0.0.1;
EOF
    cat "$release/lib/nginx-portal-routes.conf"
    printf '    }\n'
}

public_http_servers() {
    cat <<EOF
    server {
        listen 80 default_server;
        return 404;
    }
    server {
        listen 80;
        server_name $portal_domain;

        location ^~ /.well-known/acme-challenge/ {
            root $acme_webroot;
            default_type text/plain;
            try_files \$uri =404;
        }

        location / {
            return 308 https://$portal_domain\$request_uri;
        }
    }
EOF
}

public_tls_servers() {
    local serve_portal=$1
    cat <<EOF
    server {
        listen 443 ssl default_server;
        ssl_reject_handshake on;
        return 404;
    }
    server {
        listen 443 ssl;
        server_name $portal_domain;
        ssl_certificate $certificate/fullchain.pem;
        ssl_certificate_key $certificate/privkey.pem;
EOF
    if [[ -n ${probe_path:-} ]]; then
        cat <<EOF
        location = $probe_path {
            default_type text/plain;
            add_header Cache-Control "no-store" always;
            alias $probe_dir/body;
        }
EOF
    fi
    if [[ $serve_portal == yes ]]; then
        cat "$release/lib/nginx-portal-routes.conf"
    else
        printf '        location / { return 404; }\n'
    fi
    printf '    }\n'
}

render_nginx() {
    local serve_local=$1 phase=$2
    if [[ $phase == public ]]; then
        printf '# ai-bootstrap public portal %s\n' "$portal_domain"
    fi
    cat "$release/lib/nginx-portal-base.conf"
    if [[ $serve_local == yes ]]; then local_servers; fi
    case "$phase" in
        acme) public_http_servers ;;
        probe) public_http_servers; public_tls_servers "$keep_public" ;;
        public) public_http_servers; public_tls_servers yes ;;
    esac
    printf '}\n'
}
