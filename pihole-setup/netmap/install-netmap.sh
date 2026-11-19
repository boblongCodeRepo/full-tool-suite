#!/usr/bin/env bash
# Installs (or updates) the Network Map service on the Pi.
# Run ON THE PI:  bash ~/netmap/install-netmap.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
if [[ $EUID -eq 0 ]] && ! command -v sudo >/dev/null 2>&1; then sudo() { env "$@"; }; fi

echo "Installing arp-scan..."
sudo DEBIAN_FRONTEND=noninteractive apt-get -y install arp-scan python3 >/dev/null

sudo install -d -m 755 /opt/netmap /var/lib/netmap
sudo install -m 644 "$HERE/netmap.py" "$HERE/netmap.html" /opt/netmap/

sudo tee /etc/systemd/system/netmap.service >/dev/null <<'UNIT'
[Unit]
Description=Network Map (web page listing every device on the network)
After=network-online.target pihole-FTL.service
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /opt/netmap/netmap.py --db /var/lib/netmap/netmap.db --port 8081
Restart=always
RestartSec=10
ProtectHome=yes
PrivateTmp=yes
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable netmap.service >/dev/null
sudo systemctl restart netmap.service
sleep 4
if systemctl is-active --quiet netmap.service; then
  IP=$(hostname -I | awk '{print $1}')
  echo "Network Map is running:  http://$IP:8081   (or http://$(hostname).local:8081)"
  echo "The first scan takes about a minute."
else
  echo "Network Map failed to start. Last log lines:"
  sudo journalctl -u netmap.service -n 30 --no-pager
  exit 1
fi
