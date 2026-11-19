#!/usr/bin/env bash
# Pi-hole setup for Raspberry Pi OS Lite (64-bit).
# Run ON THE PI:  bash setup-pihole.sh
# Safe to re-run: each step checks whether it's already done.
set -euo pipefail

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!!  %s\033[0m\n' "$*"; }
ask()  { read -r -p "$1 [y/N] " a; [[ "$a" =~ ^[Yy] ]]; }
ask_yes() { read -r -p "$1 [Y/n] (Enter = yes) " a; [[ ! "$a" =~ ^[Nn] ]]; }

# On a Pi this runs as a normal user with sudo; in a Proxmox container it runs as root without sudo.
if [[ $EUID -eq 0 ]] && ! command -v sudo >/dev/null 2>&1; then sudo() { env "$@"; }; export -f sudo; fi
ME=$(id -un)

# ---------------------------------------------------------------- 1. Updates
say "Step 1/6: Updating the system (can take 5-15 min the first time)"
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get -y full-upgrade
sudo DEBIAN_FRONTEND=noninteractive apt-get -y install unattended-upgrades sqlite3 curl
echo 'unattended-upgrades unattended-upgrades/enable_auto_updates boolean true' | sudo debconf-set-selections
sudo DEBIAN_FRONTEND=noninteractive dpkg-reconfigure -f noninteractive unattended-upgrades
echo "Automatic security updates: ON"

# ---------------------------------------------------------------- 2. Pi-hole
if command -v pihole >/dev/null 2>&1; then
  say "Step 2/6: Pi-hole already installed - skipping"
else
  say "Step 2/6: Installing Pi-hole (an installer with menus will appear)"
  cat <<'TIP'
  In the installer:
   - Upstream DNS: pick anything (Cloudflare is fine) - this script switches it to Unbound next.
   - Accept the default blocklist.
   - Say YES to the web admin interface and query logging.
   - Privacy level: 0 (show everything) - needed to see what each device contacts.
TIP
  read -r -p "Press Enter to start the Pi-hole installer..."
  curl -sSL https://install.pi-hole.net | bash
fi

# ---------------------------------------------------------------- 3. Unbound
say "Step 3/6: Installing Unbound (private DNS lookups, no Google/Cloudflare)"
sudo DEBIAN_FRONTEND=noninteractive apt-get -y install unbound
sudo tee /etc/unbound/unbound.conf.d/pi-hole.conf >/dev/null <<'CONF'
server:
    verbosity: 0
    interface: 127.0.0.1
    port: 5335
    do-ip4: yes
    do-udp: yes
    do-tcp: yes
    do-ip6: no
    prefer-ip6: no
    harden-glue: yes
    harden-dnssec-stripped: yes
    use-caps-for-id: no
    edns-buffer-size: 1232
    prefetch: yes
    num-threads: 1
    so-rcvbuf: 1m
    private-address: 192.168.0.0/16
    private-address: 169.254.0.0/16
    private-address: 172.16.0.0/12
    private-address: 10.0.0.0/8
    private-address: fd00::/8
    private-address: fe80::/10
CONF
# Debian quirk: stop resolvconf from overriding Unbound's config
if systemctl list-unit-files | grep -q '^unbound-resolvconf.service'; then
  sudo systemctl disable --now unbound-resolvconf.service || true
fi
[[ -f /etc/resolvconf.conf ]] && sudo sed -i 's/^unbound_conf=/#unbound_conf=/' /etc/resolvconf.conf
sudo rm -f /etc/unbound/unbound.conf.d/resolvconf_resolvers.conf
sudo systemctl restart unbound
sleep 2
if dig +short pi-hole.net @127.0.0.1 -p 5335 >/dev/null 2>&1 || \
   host -p 5335 pi-hole.net 127.0.0.1 >/dev/null 2>&1; then
  echo "Unbound is answering."
else
  # dig may not be installed; try installing it once and test again
  sudo apt-get -y install dnsutils >/dev/null 2>&1 || true
  if dig +short pi-hole.net @127.0.0.1 -p 5335 | grep -q .; then
    echo "Unbound is answering."
  else
    warn "Unbound test failed - leaving Pi-hole on its original upstream DNS."
    UNBOUND_FAILED=1
  fi
fi
if [[ -z "${UNBOUND_FAILED:-}" ]]; then
  sudo pihole-FTL --config dns.upstreams '["127.0.0.1#5335"]'
  echo "Pi-hole now uses Unbound (127.0.0.1#5335)."
fi

# ---------------------------------------------------------------- 4. Blocklists
say "Step 4/6: Adding extra blocklists"
DB=/etc/pihole/gravity.db
add_list() {
  local url="$1" comment="$2"
  if sudo sqlite3 "$DB" "SELECT 1 FROM adlist WHERE address='$url';" | grep -q 1; then
    echo "  already have: $comment"
  else
    sudo sqlite3 "$DB" "INSERT INTO adlist (address, enabled, comment) VALUES ('$url', 1, '$comment');"
    echo "  added: $comment"
  fi
}
add_list "https://cdn.jsdelivr.net/gh/hagezi/dns-blocklists@latest/adblock/pro.txt"        "HaGeZi Pro (ads, tracking, malware)"
add_list "https://cdn.jsdelivr.net/gh/hagezi/dns-blocklists@latest/adblock/tif.medium.txt" "HaGeZi Threat Intelligence medium (malware, phishing)"
add_list "https://cdn.jsdelivr.net/gh/hagezi/dns-blocklists@latest/adblock/native.winoffice.txt" "HaGeZi Windows/Office telemetry"
add_list "https://cdn.jsdelivr.net/gh/hagezi/dns-blocklists@latest/adblock/native.lgwebos.txt"   "HaGeZi LG TV (webOS) tracking and ads"
sudo pihole -g
echo "If any list shows an error above, it can be removed in the web admin under Lists."

# ---------------------------------------------------------------- 5. Password + backup
say "Step 5/6: Web admin password and first backup"
if ask_yes "Set the web admin password now?"; then
  sudo pihole setpassword
fi
mkdir -p ~/pihole-backups
( cd ~/pihole-backups && sudo pihole-FTL --teleporter && sudo chown "$ME": ./*.zip ) \
  && echo "Backup saved in ~/pihole-backups (copy it to your PC - see README)." \
  || warn "Backup failed - you can make one from the web admin: Settings > Teleporter."

# ---------------------------------------------------------------- 6. Tailscale (optional)
say "Step 6/6: Tailscale (ad blocking on your phone/laptop when away from home)"
if command -v tailscale >/dev/null 2>&1; then
  echo "Tailscale already installed."
elif ask "Install Tailscale now? (needs a free Tailscale account)"; then
  curl -fsSL https://tailscale.com/install.sh | sh
  sudo tailscale up
else
  echo "Skipped. Re-run this script any time to add it."
fi

# ---------------------------------------------------------------- Extra: Network Map
if [[ -f ~/netmap/install-netmap.sh ]]; then
  say "Extra: Network Map (web page listing every device on your network)"
  if systemctl is-active --quiet netmap.service 2>/dev/null; then
    echo "Already installed - updating it."
    bash ~/netmap/install-netmap.sh || warn "Network Map update failed (Pi-hole is unaffected)."
  elif ask_yes "Install the Network Map now?"; then
    bash ~/netmap/install-netmap.sh || warn "Network Map install failed (Pi-hole is unaffected)."
  fi
fi

IP=$(hostname -I | awk '{print $1}')
say "Done!"
cat <<EOF
  Web admin:   http://$IP/admin   (or http://$(hostname).local/admin)
  Network Map: http://$IP:8081    (if installed)
  NEXT (on your router): set DNS server to  $IP
  Then on your PC run:  ipconfig /flushdns   and browse a bit - the dashboard should start counting.
EOF
