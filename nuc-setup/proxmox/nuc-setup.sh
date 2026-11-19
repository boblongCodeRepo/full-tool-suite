#!/usr/bin/env bash
# Sets up the whole NUC. Runs ON THE PROXMOX HOST as root (Setup-NUC.ps1 starts it).
# Safe to re-run: anything that already exists is skipped.
#
#   CT 100  pihole   Pi-hole + Unbound + Network Map     (Linux container)
#   CT 101  ledger   Home Ledger finance server          (Linux container)
#   VM 200  windows  Windows 11 for the Market Terminal  (virtual machine)
set -euo pipefail
cd "$(dirname "$0")"
B=/root/nuc-setup
ISO_DIR=/var/lib/vz/template/iso

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!!  %s\033[0m\n' "$*"; }
ask_yes() { local a; read -r -p "$1 [Y/n] (Enter = yes) " a; [[ ! "$a" =~ ^[Nn] ]]; }
ask_no()  { local a; read -r -p "$1 [y/N] (Enter = no) " a; [[ "$a" =~ ^[Yy] ]]; }

[[ $EUID -eq 0 ]] || { echo "Run as root on the Proxmox host."; exit 1; }
command -v pct >/dev/null || { echo "This doesn't look like a Proxmox host (no 'pct')."; exit 1; }

# ------------------------------------------------------------------ 1. Updates
say "Step 1/5: Proxmox updates (free 'no-subscription' channel)"
CODENAME=$(. /etc/os-release; echo "$VERSION_CODENAME")
for f in /etc/apt/sources.list.d/pve-enterprise.list /etc/apt/sources.list.d/ceph.list; do
  [[ -f $f ]] && sed -i 's/^deb/#deb/' "$f"
done
for f in /etc/apt/sources.list.d/pve-enterprise.sources /etc/apt/sources.list.d/ceph.sources; do
  if [[ -f $f ]]; then
    if grep -q '^Enabled:' "$f"; then sed -i 's/^Enabled:.*/Enabled: no/' "$f"; else echo 'Enabled: no' >> "$f"; fi
  fi
done
if [[ $CODENAME == bookworm ]]; then
  echo "deb http://download.proxmox.com/debian/pve bookworm pve-no-subscription" > /etc/apt/sources.list.d/pve-no-subscription.list
else
  cat > /etc/apt/sources.list.d/pve-no-subscription.sources <<EOF
Types: deb
URIs: http://download.proxmox.com/debian/pve
Suites: $CODENAME
Components: pve-no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
EOF
fi
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get -y dist-upgrade
DEBIAN_FRONTEND=noninteractive apt-get -y install usbutils pciutils >/dev/null

CT_STORE=$(pvesm status --content rootdir 2>/dev/null | awk 'NR>1 && $3=="active" {print $1; exit}')
VM_STORE=$(pvesm status --content images 2>/dev/null | awk 'NR>1 && $3=="active" {print $1; exit}')
[[ -n $CT_STORE && -n $VM_STORE ]] || { warn "Couldn't find Proxmox storage for containers/VMs."; exit 1; }
echo "Storage: containers on '$CT_STORE', VM disks on '$VM_STORE'"

# Debian container template
pveam update >/dev/null || true
TEMPLATE=$(pveam available --section system | awk '{print $2}' | grep -E '^debian-13-standard' | sort -V | tail -1)
[[ -n $TEMPLATE ]] || TEMPLATE=$(pveam available --section system | awk '{print $2}' | grep -E '^debian-12-standard' | sort -V | tail -1)
[[ -f /var/lib/vz/template/cache/$TEMPLATE ]] || pveam download local "$TEMPLATE"

make_ct() {  # id name memMB diskGB
  local id=$1 name=$2 mem=$3 disk=$4 ip=""
  if pct status "$id" >/dev/null 2>&1; then
    echo "Container $id ($name) already exists."
  else
    pct create "$id" "local:vztmpl/$TEMPLATE" --hostname "$name" --cores 1 --memory "$mem" --swap 256 \
      --rootfs "$CT_STORE:$disk" --net0 name=eth0,bridge=vmbr0,ip=dhcp --unprivileged 1 \
      --features nesting=1 --onboot 1 --timezone host --ostype debian
  fi
  pct status "$id" | grep -q running || pct start "$id"
  for _ in $(seq 1 60); do
    ip=$(pct exec "$id" -- hostname -I 2>/dev/null | awk '{print $1}') || true
    [[ -n $ip ]] && break; sleep 2
  done
  [[ -n $ip ]] || { warn "Container $id didn't get a network address."; return 1; }
  echo "Container $id ($name) is at $ip"
}
in_ct() { local id=$1; shift; pct exec "$id" -- env HOME=/root USER=root TERM="${TERM:-xterm}" LANG=C.UTF-8 "$@"; }
avahi_in_ct() {  # makes <name>.local work on the home network
  in_ct "$1" bash -c 'DEBIAN_FRONTEND=noninteractive apt-get -y install avahi-daemon >/dev/null &&
    sed -i "s/^rlimit-nproc=/#rlimit-nproc=/" /etc/avahi/avahi-daemon.conf && systemctl restart avahi-daemon' \
    || warn "Couldn't set up the .local name for container $1 - use its IP address instead."
}

# ------------------------------------------------------------------ 2. Pi-hole
say "Step 2/5: Pi-hole + Network Map container (CT 100 'pihole')"
make_ct 100 pihole 1024 8
in_ct 100 bash -c 'apt-get update >/dev/null'
avahi_in_ct 100
in_ct 100 mkdir -p /root/netmap
pct push 100 "$B/pihole/setup-pihole.sh" /root/setup-pihole.sh
for f in netmap.py netmap.html install-netmap.sh; do pct push 100 "$B/pihole/netmap/$f" "/root/netmap/$f"; done
echo "Starting the Pi-hole setup inside the container..."
in_ct 100 bash /root/setup-pihole.sh || warn "Pi-hole setup stopped early. Re-run this script, or: pct enter 100  then  bash /root/setup-pihole.sh"
PIHOLE_IP=$(pct exec 100 -- hostname -I | awk '{print $1}')

# ------------------------------------------------------------------ 3. Home Ledger
say "Step 3/5: Home Ledger container (CT 101 'ledger')"
make_ct 101 ledger 512 4
in_ct 101 bash -c 'apt-get update >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get -y install python3 >/dev/null'
avahi_in_ct 101
in_ct 101 bash -c 'id ledger >/dev/null 2>&1 || useradd --system --home /var/lib/homeledger --shell /usr/sbin/nologin ledger
  install -d -m 755 /opt/homeledger /opt/homeledger/static && install -d -m 700 -o ledger -g ledger /var/lib/homeledger'
pct push 101 "$B/ledger/server.py" /opt/homeledger/server.py
pct push 101 "$B/ledger/static/index.html" /opt/homeledger/static/index.html
pct push 101 "$B/ledger/static/app.js" /opt/homeledger/static/app.js
in_ct 101 bash -c 'cat > /etc/systemd/system/homeledger.service <<UNIT
[Unit]
Description=Home Ledger (household finance)
After=network-online.target
Wants=network-online.target

[Service]
User=ledger
Environment=HOME_LEDGER_DATA=/var/lib/homeledger
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 /opt/homeledger/server.py --host 0.0.0.0 --port 8780
Restart=always
RestartSec=5
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/homeledger
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload && systemctl enable homeledger >/dev/null && systemctl restart homeledger'
sleep 4
LEDGER_IP=$(pct exec 101 -- hostname -I | awk '{print $1}')
SETUP_CODE=$(pct exec 101 -- journalctl -u homeledger --no-pager -n 50 2>/dev/null | grep -o 'setup code: *[0-9a-f]*' | tail -1 | awk '{print $3}') || true
if pct exec 101 -- systemctl is-active --quiet homeledger; then echo "Home Ledger is running."; else warn "Home Ledger didn't start: pct exec 101 -- journalctl -u homeledger"; fi

# ------------------------------------------------------------------ 4. Windows VM
say "Step 4/5: Windows 11 virtual machine (VM 200 'windows') for the Market Terminal"
WIN_ISO=$(ls "$ISO_DIR" 2>/dev/null | grep -iE '^win.*\.iso$' | head -1 || true)
if qm status 200 >/dev/null 2>&1; then
  echo "VM 200 already exists."
elif [[ -z $WIN_ISO ]]; then
  warn "No Windows 11 installer found, so the Windows VM was skipped."
  echo "    Download the Windows 11 ISO from microsoft.com/software-download/windows11 into your"
  echo "    Downloads folder, then run Setup-NUC again - it uploads it and finishes this step."
else
  if [[ ! -f $ISO_DIR/virtio-win.iso ]]; then
    echo "Downloading VirtIO drivers for Windows..."
    wget -q --show-progress -O "$ISO_DIR/virtio-win.iso.part" \
      https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso \
      && mv "$ISO_DIR/virtio-win.iso.part" "$ISO_DIR/virtio-win.iso" || warn "VirtIO download failed (optional)."
  fi
  TOTAL_MB=$(free -m | awk '/^Mem:/ {print $2}')
  MEM=$(( TOTAL_MB / 2 )); (( MEM > 8192 )) && MEM=8192; (( MEM < 4096 )) && MEM=4096
  CORES=$(( $(nproc) / 2 )); (( CORES < 2 )) && CORES=2
  echo "Creating VM: $CORES CPU cores, $((MEM/1024)) GB RAM, 96 GB disk"
  # SATA disk + Intel e1000e network card: Windows has drivers for both built in, so setup needs no extra driver steps.
  qm create 200 --name windows --ostype win11 --machine q35 --bios ovmf --cpu host \
    --cores "$CORES" --memory "$MEM" --balloon 0 \
    --efidisk0 "$VM_STORE:1,efitype=4m,pre-enrolled-keys=1" --tpmstate0 "$VM_STORE:1,version=v2.0" \
    --sata0 "$VM_STORE:96,ssd=1,discard=on" --net0 e1000e,bridge=vmbr0 \
    --ide2 "local:iso/$WIN_ISO,media=cdrom" --boot 'order=ide2;sata0' --agent 1 --vga std --onboot 1
  [[ -f $ISO_DIR/virtio-win.iso ]] && qm set 200 --ide0 local:iso/virtio-win.iso,media=cdrom >/dev/null

  # Bluetooth adapter -> Windows (the Market Terminal's Bluetooth features need it)
  BT=""
  for d in /sys/bus/usb/devices/*; do
    [[ -f $d/idVendor ]] || continue
    if grep -qsx 'e0' "$d"/*:*/bInterfaceClass; then BT="$(cat "$d/idVendor"):$(cat "$d/idProduct")"; break; fi
  done
  if [[ -n $BT ]]; then
    if ask_yes "Give the NUC's Bluetooth adapter ($BT) to Windows?"; then qm set 200 --usb0 "host=$BT" >/dev/null; echo "Bluetooth passed to Windows."; fi
  else
    echo "No Bluetooth adapter found (fine - a USB Bluetooth dongle can be added later)."
  fi

  # Wi-Fi card -> Windows (only needed for the Market Terminal's hotspot features)
  WIFI=$(lspci -Dnn | awk '/Network controller/ {print $1; exit}')
  if [[ -n $WIFI ]] && compgen -G '/sys/kernel/iommu_groups/*' >/dev/null; then
    echo "Wi-Fi card: $(lspci -s "$WIFI" | cut -d' ' -f2-)"
    if ask_no "Give the Wi-Fi card to Windows? (only for the hotspot features; experimental)"; then
      qm set 200 --hostpci0 "$WIFI,pcie=1" >/dev/null && echo "Wi-Fi passed to Windows."
    fi
  fi
fi
if qm status 200 >/dev/null 2>&1 && [[ -f $ISO_DIR/market-apps.iso ]]; then
  qm set 200 --ide3 local:iso/market-apps.iso,media=cdrom >/dev/null
  echo "Apps disc (Market Terminal + your data) is in the VM's CD drive."
fi

# ------------------------------------------------------------------ 5. Backups
say "Step 5/5: Weekly backups of the containers"
if pvesh get /cluster/backup --output-format json 2>/dev/null | grep -q '"comment":"nuc-setup"'; then
  echo "Backup job already set."
else
  pvesh create /cluster/backup --schedule 'sun 03:00' --storage local --vmid 100,101 --mode snapshot \
    --compress zstd --prune-backups keep-last=4 --comment nuc-setup --enabled 1 >/dev/null \
    && echo "Pi-hole and Home Ledger are backed up every Sunday at 3am (last 4 kept)." \
    || warn "Couldn't create the backup job - add one in the web page under Datacenter > Backup."
fi

HOST_IP=$(hostname -I | awk '{print $1}')
say "All done!"
cat <<EOF

  Proxmox (manage everything):  https://$HOST_IP:8006    user: root
  Pi-hole:                      http://${PIHOLE_IP:-?}/admin       (http://pihole.local/admin)
  Network Map:                  http://${PIHOLE_IP:-?}:8081        (http://pihole.local:8081)
  Home Ledger:                  http://${LEDGER_IP:-?}:8780        (http://ledger.local:8780)
EOF
[[ -n ${SETUP_CODE:-} ]] && echo "  Home Ledger first-account setup code: $SETUP_CODE"
cat <<EOF

  NEXT, in your router:
    1. Reserve the addresses above so they never change (DHCP reservation).
    2. Set the DNS server to the Pi-hole address: ${PIHOLE_IP:-?}

EOF
if qm status 200 >/dev/null 2>&1; then
  echo "  Windows: open the Proxmox page > VM 200 'windows' > Start > Console, and follow"
  echo "  'STEP 5' in READ ME FIRST.txt."
fi
