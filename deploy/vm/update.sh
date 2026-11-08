#!/bin/sh
# Install a new build on the VM:  sudo sh update.sh   (expects /tmp/lifeline-server.zip, optional /tmp/lifeline.env)
set -eu
cd /opt/lifeline/server
find . -mindepth 1 -maxdepth 1 ! -name .env -exec rm -rf {} +
unzip -q -o /tmp/lifeline-server.zip -d /opt/lifeline/server
if [ -f /tmp/lifeline.env ]; then
  install -m 600 -o lifeline -g lifeline /tmp/lifeline.env /opt/lifeline/server/.env
  shred -u /tmp/lifeline.env 2>/dev/null || rm -f /tmp/lifeline.env
fi
chown -R lifeline:lifeline /opt/lifeline
rm -f /tmp/lifeline-server.zip
systemctl restart lifeline
sleep 3
systemctl --no-pager --lines=0 status lifeline | head -3
