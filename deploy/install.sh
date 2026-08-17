#!/usr/bin/env bash
# Example Linux installation. Configure credentials in this application's own .env.
set -euo pipefail
APP_DIR=${APP_DIR:-/opt/acadtrack}
APP_USER=${APP_USER:-acadtrack}
PORT=${PORT:-8000}
SRC_ENV=${SRC_ENV:-}
if ss -tlnp 2>/dev/null | grep -q "127.0.0.1:${PORT} "; then
  systemctl is-active --quiet acadtrack || { echo "Application port already in use" >&2; exit 1; }
fi
need=()
dpkg -s python3-venv >/dev/null 2>&1 || need+=(python3-venv)
dpkg -s python3-pip >/dev/null 2>&1 || need+=(python3-pip)
if [ ${#need[@]} -gt 0 ]; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${need[@]}"
fi
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR/data"
[ -x "$APP_DIR/.venv/bin/python" ] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
if [ ! -f "$APP_DIR/.env" ]; then
  umask 077
  if [ -n "$SRC_ENV" ]; then
    [ -r "$SRC_ENV" ] || { echo "Cannot read supplied environment file" >&2; exit 1; }
    cp "$SRC_ENV" "$APP_DIR/.env"
  else
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    key=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
    sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$key|;s|^ACADTRACK_DB=.*|ACADTRACK_DB=$APP_DIR/data/acadtrack.db|" "$APP_DIR/.env"
    # A new deployment denies login until its operator configures an allowlist.
    sed -i 's|^ALLOWED_EMAILS=.*|ALLOWED_EMAILS=|;s|^ACADTRACK_HTTPS=.*|ACADTRACK_HTTPS=1|' "$APP_DIR/.env"
  fi
fi
if [ -n "${DOMAIN:-}" ]; then
  sed -i "s|^GOOGLE_REDIRECT_URI=.*|GOOGLE_REDIRECT_URI=https://$DOMAIN/auth/callback|" "$APP_DIR/.env"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"
chmod 750 "$APP_DIR/data"
find "$APP_DIR/data" -maxdepth 1 -type f -name 'acadtrack.db*' -exec chmod 640 {} +
sed -e "s|__APP_DIR__|$APP_DIR|g" -e "s|__USER__|$APP_USER|g" -e "s|__PORT__|$PORT|g" \
  "$APP_DIR/deploy/acadtrack.service" > /etc/systemd/system/acadtrack.service
systemctl daemon-reload
systemctl enable --quiet acadtrack
systemctl restart acadtrack
for i in $(seq 1 20); do
  if curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
    echo "Application healthy"; exit 0
  fi
  sleep 1
done
echo "Application did not become healthy; inspect its service log" >&2
exit 1
