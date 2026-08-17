#!/usr/bin/env bash
# Publish acadtrack: DuckDNS record, nginx site, Let's Encrypt certificate.
# Run only once the DuckDNS subdomain exists.
#
#   sudo DOMAIN=tracker.example.com DUCKDNS_TOKEN=... bash /opt/acadtrack/deploy/web.sh
#
# Written for a box that is already serving other sites, so:
#   - it only ever creates files named `acadtrack`; nothing else is edited
#   - it never claims `default_server` (00-default-redirect.conf owns that)
#   - certbot runs as `certonly --webroot`, so it never rewrites nginx config
#   - every reload is preceded by `nginx -t`, and it rolls back if that fails
set -euo pipefail

DOMAIN=${DOMAIN:?set DOMAIN, e.g. DOMAIN=tracker.example.com}
DUCKDNS_TOKEN=${DUCKDNS_TOKEN:-}
PORT=${PORT:-8000}
APP_DIR=${APP_DIR:-/opt/acadtrack}
SITE=/etc/nginx/sites-available/acadtrack
WEBROOT=/var/www/html

say() { printf '==> %s\n' "$*"; }

restore_and_die() {
  echo "ERROR: nginx config test failed — rolling back, nothing was reloaded" >&2
  rm -f /etc/nginx/sites-enabled/acadtrack "$SITE"
  nginx -t && echo "rolled back cleanly; other sites untouched" >&2
  exit 1
}

# --- DuckDNS ----------------------------------------------------------------
if [ -n "$DUCKDNS_TOKEN" ]; then
  sub=${DOMAIN%%.duckdns.org}
  say "duckdns: pointing $sub at this host"
  res=$(curl -fsS "https://www.duckdns.org/update?domains=${sub}&token=${DUCKDNS_TOKEN}&ip=")
  [ "$res" = "OK" ] || { echo "ERROR: DuckDNS returned '$res' for '$sub'." >&2
                         echo "The subdomain must already exist on that account." >&2
                         exit 1; }

  install -d -m 755 /opt/duckdns
  cat > /opt/duckdns/acadtrack.sh <<EOF
#!/usr/bin/env bash
# Blank ip= lets DuckDNS record the source address of the request.
curl -fsS "https://www.duckdns.org/update?domains=${sub}&token=${DUCKDNS_TOKEN}&ip=" \\
  -o /var/log/duckdns-acadtrack.log
EOF
  chmod 700 /opt/duckdns/acadtrack.sh
  # Named per-app so a second DuckDNS domain later cannot collide with this.
  cat > /etc/systemd/system/duckdns-acadtrack.service <<'EOF'
[Unit]
Description=DuckDNS update (acadtrack)
After=network-online.target
[Service]
Type=oneshot
ExecStart=/opt/duckdns/acadtrack.sh
EOF
  cat > /etc/systemd/system/duckdns-acadtrack.timer <<'EOF'
[Unit]
Description=DuckDNS update for acadtrack every 15 minutes
[Timer]
OnBootSec=60
OnUnitActiveSec=15min
[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  systemctl enable --now --quiet duckdns-acadtrack.timer
  say "duckdns timer installed"
fi

# --- HTTP first, so certbot can answer its challenge ------------------------
mkdir -p "$WEBROOT/.well-known/acme-challenge"
say "nginx: temporary HTTP site for $DOMAIN"
cat > "$SITE" <<EOF
server {
    listen 80;
    server_name $DOMAIN;
    location /.well-known/acme-challenge/ { root $WEBROOT; }
    location / { return 301 https://\$host\$request_uri; }
}
EOF
ln -sfn "$SITE" /etc/nginx/sites-enabled/acadtrack
nginx -t || restore_and_die
systemctl reload nginx

# --- certificate ------------------------------------------------------------
if [ ! -d "/etc/letsencrypt/live/$DOMAIN" ]; then
  say "requesting certificate for $DOMAIN"
  # certonly + webroot: certbot obtains the cert and writes nothing into nginx.
  certbot certonly --webroot -w "$WEBROOT" -d "$DOMAIN" \
      --non-interactive --agree-tos --register-unsafely-without-email
else
  say "certificate for $DOMAIN already exists"
fi

# --- real site --------------------------------------------------------------
say "nginx: HTTPS site"
cat > "$SITE" <<EOF
server {
    listen 80;
    server_name $DOMAIN;
    location /.well-known/acme-challenge/ { root $WEBROOT; }
    location / { return 301 https://\$host\$request_uri; }
}

server {
    # nginx 1.18 (Ubuntu 22.04) syntax. The separate "http2 on;" directive is
    # 1.25+ and is a hard config error here, which takes every site on the box
    # down with it if it ever reaches a reload.
    listen 443 ssl http2;
    server_name $DOMAIN;

    ssl_certificate     /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    add_header X-Frame-Options DENY always;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy no-referrer always;
    add_header Strict-Transport-Security "max-age=31536000" always;

    client_max_body_size 4m;

    location /static/ {
        alias $APP_DIR/static/;
        expires 7d;
        access_log off;
    }

    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 120s;
    }
}
EOF
nginx -t || restore_and_die
systemctl reload nginx

say "done"
echo "    https://$DOMAIN"
echo "    local health: $(curl -fsS "http://127.0.0.1:$PORT/healthz" || echo FAILED)"
echo
echo "Register this exact redirect URI in the Google Cloud console:"
echo "    https://$DOMAIN/auth/callback"
