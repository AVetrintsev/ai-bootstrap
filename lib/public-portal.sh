#!/usr/bin/env bash

acme_webroot=/var/lib/ai-bootstrap-acme

public_dns() {
    local reason
    reason=$(python3 -I "$release/lib/public-portal.py" dns "$portal_domain" "$portal_ipv4" 2>&1) ||
        fail "$reason"
}

certbot_packages() {
    ensure_packages certbot
}

begin_public_https() {
    certificate=/etc/letsencrypt/live/ai-bootstrap-$portal_domain
    keep_local=no
    keep_public=no
    nginx_was_active=no
    if installed_local_portal; then
        keep_local=yes
    fi
    if installed_public_portal "$portal_domain"; then
        keep_public=yes
    fi
    if systemctl is-active --quiet nginx.service; then
        nginx_was_active=yes
    fi

    cp -p -- "$nginx_config" "$work/nginx-before-https.conf"
    cp -p -- "$etc/firewall.nft" "$work/firewall-before-https.nft"
    restore_public_portal=yes

    # Only the HTTP challenge is served publicly until there is a certificate.
    install -d -m 0755 "$acme_webroot"
    if [[ $keep_public == no ]]; then
        render_nginx "$keep_local" acme > "$work/nginx-acme.conf"
        apply_nginx "$work/nginx-acme.conf"
    fi
    publish "$work/firewall-public.nft" "$etc/firewall.nft" 600
    quiet 'could not open inbound TCP 80 and 443' systemctl reload "$firewall_unit"
}

restore_portal() {
    local restored=yes
    if ! publish "$work/firewall-before-https.nft" "$etc/firewall.nft" 600 ||
        ! systemctl reload "$firewall_unit" >/dev/null 2>&1; then
        printf 'ERROR install/rollback: could not restore the previous firewall\n' >&2
        restored=no
    fi
    if ! publish "$work/nginx-before-https.conf" "$nginx_config" 644; then
        printf 'ERROR install/rollback: could not restore the previous nginx configuration\n' >&2
        restored=no
    elif [[ $nginx_was_active == yes ]]; then
        if ! systemctl restart nginx.service >/dev/null 2>&1; then
            printf 'ERROR install/rollback: could not restart nginx with its previous configuration\n' >&2
            restored=no
        fi
    else
        if ! systemctl stop nginx.service >/dev/null 2>&1; then
            printf 'ERROR install/rollback: could not stop nginx\n' >&2
            restored=no
        fi
    fi
    [[ $restored == yes ]]
}

acme() {
    local log=$work/certbot.log
    if ! timeout 300 /usr/bin/certbot certonly --non-interactive --agree-tos \
        --register-unsafely-without-email --server https://acme-v02.api.letsencrypt.org/directory \
        --webroot --webroot-path "$acme_webroot" --preferred-challenges http-01 \
        --cert-name "ai-bootstrap-$portal_domain" -d "$portal_domain" >"$log" 2>&1; then
        if grep -Eqi 'timeout during connect|connection refused|invalid response from|fetching http://' "$log"; then
            fail 'ACME HTTP-01 could not reach the challenge on inbound TCP 80; see /var/log/letsencrypt/letsencrypt.log'
        fi
        fail 'ACME certificate issuance failed; see /var/log/letsencrypt/letsencrypt.log'
    fi
}

globalping() {
    local body reason
    probe_dir=$(mktemp -d /run/ai-bootstrap-https-probe.XXXXXXXX)
    chmod 0755 "$probe_dir"
    probe_path=/_ai-bootstrap-probe/$(openssl rand -hex 24)
    body=$(openssl rand -hex 24)
    printf '%s' "$body" > "$probe_dir/body"
    chmod 0644 "$probe_dir/body"
    render_nginx "$keep_local" probe > "$work/nginx-probe.conf"
    apply_nginx "$work/nginx-probe.conf"
    reason=$(python3 -I "$release/lib/public-portal.py" probe \
        "$portal_domain" "$portal_ipv4" "$probe_path" "$body" 2>&1) || fail "$reason"
    rm -f -- "$probe_dir/body"
    old_probe_path=$probe_path
    unset probe_path
    render_nginx "$keep_local" probe > "$work/nginx-verified.conf"
    apply_nginx "$work/nginx-verified.conf"
}

configure_public_https() {
    local status path listeners
    render_nginx no public > "$work/nginx-public.conf"
    apply_nginx "$work/nginx-public.conf"

    for path in / /api/status /api/test-stream /projects /canvas "$old_probe_path"; do
        status=$(curl -q --noproxy '*' -4 --silent --max-time 10 --output /dev/null \
            --resolve "$portal_domain:443:127.0.0.1" --write-out '%{http_code}' "https://$portal_domain$path") ||
            fail 'could not verify the installed HTTPS listener and certificate'
        [[ $status == 404 ]] || fail 'expected a 404 for anonymous access and the expired HTTPS probe'
    done
    listeners=$(ss -Hltnp 'sport = :8080' | awk '/"nginx"/ {print $4}') ||
        fail 'could not check the local HTTP port 8080'
    [[ -z $listeners ]] ||
        fail 'nginx still listens on the local HTTP port 8080'

    install -d -m 0755 /etc/letsencrypt/renewal-hooks/deploy
    publish "$release/lib/certbot-reload-nginx.sh" \
        /etc/letsencrypt/renewal-hooks/deploy/90-ai-bootstrap-nginx 755
    quiet 'could not enable automatic certificate renewal' systemctl enable --now certbot.timer
    restore_public_portal=no
}
