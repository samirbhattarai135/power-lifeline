#!/bin/sh
# One-time VM setup, run as root on the VM:  sudo sh setup.sh <public hostname>
# Installs Caddy (automatic Let's Encrypt HTTPS), creates the lifeline user and service.
# Idempotent: safe to run again.
set -eu
HOST="$1"

apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3 unzip unattended-upgrades debian-keyring debian-archive-keyring apt-transport-https curl gnupg
if [ ! -f /usr/share/keyrings/caddy-stable-archive-keyring.gpg ]; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q
fi
DEBIAN_FRONTEND=noninteractive apt-get install -y -q caddy

# Security updates every day, reboot not automatic (a reboot is a short outage; do it by hand outside demo hours).
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF
systemctl enable --now unattended-upgrades

id lifeline >/dev/null 2>&1 || useradd --system --home-dir /opt/lifeline --shell /usr/sbin/nologin lifeline
mkdir -p /opt/lifeline/server
chown -R lifeline:lifeline /opt/lifeline

# X-Real-IP is set (overwriting anything the caller sent) so the app throttles real callers
# and never mistakes an outside caller for this machine; app.py reads it via LIFELINE_CLIENT_IP_HEADER.
cat > /etc/caddy/Caddyfile <<CONF
$HOST {
	reverse_proxy 127.0.0.1:8080 {
		header_up X-Real-IP {remote_host}
	}
}
CONF
systemctl reload caddy || systemctl restart caddy

install -m 644 /tmp/lifeline.service /etc/systemd/system/lifeline.service
systemctl daemon-reload
systemctl enable lifeline
echo "setup done for $HOST"
