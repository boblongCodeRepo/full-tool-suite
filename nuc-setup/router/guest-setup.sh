#!/bin/sh
# Guest Wi-Fi with a terms page and activity logging, for a GL.iNet Flint 2 (GL-MT6000) or another OpenWrt router.
# See README.md in this folder. Run it ON THE ROUTER, after switching Guest Wi-Fi on in the router's web page:
#
#   sh /tmp/guest-router/guest-setup.sh <PC address> "<your name or business>" "<contact email or phone>"
#
# It is safe to run again (for example after changing the terms): every step replaces what it set up before.
set -e

PC_IP="$1"
OWNER="$2"
CONTACT="$3"
PORT=5514                      # Device Watch listens here (UDP)
KEEP_DAYS=90                   # must match the GUESTS tab's setting; it is written into the terms
GUEST_IF="${GUEST_IF:-br-guest}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ND=/usr/lib/opennds

die() { echo; echo "STOPPED: $*" >&2; exit 1; }
# HTML-escape a value, then escape it for use as a sed replacement
sed_esc() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' | sed -e 's/[&|\\]/\\&/g'; }

[ -n "$PC_IP" ] && [ -n "$OWNER" ] && [ -n "$CONTACT" ] || die 'usage: sh guest-setup.sh <PC address> "<your name>" "<contact>"'
echo "$PC_IP" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' || die "\"$PC_IP\" is not an address like 192.168.8.100"
[ -f "$HERE/guest_terms.html" ] || die "guest_terms.html is missing: copy the whole guest-router folder to the router"
ip link show "$GUEST_IF" >/dev/null 2>&1 || die "there is no $GUEST_IF network yet. In the router's web page open
  Wireless > Guest Wi-Fi, switch it on and apply, then run this again."

echo "1/6 Sending the router's log to Device Watch at $PC_IP (UDP port $PORT)"
uci set system.@system[0].log_ip="$PC_IP"
uci set system.@system[0].log_port="$PORT"
uci set system.@system[0].log_proto='udp'
uci set system.@system[0].log_size='1024'
uci commit system
/etc/init.d/log restart

echo "2/6 Logging the names devices look up (DNS) and the addresses they are given"
uci set dhcp.@dnsmasq[0].logqueries='1'
uci -q delete dhcp.@dnsmasq[0].quietdhcp || true
# Firefox's built-in encrypted DNS would hide lookups from the log; this name tells Firefox to use the network's DNS
uci -q del_list dhcp.@dnsmasq[0].address='/use-application-dns.net/' || true
uci add_list dhcp.@dnsmasq[0].address='/use-application-dns.net/'
uci commit dhcp
/etc/init.d/dnsmasq restart

echo "3/6 Logging every new connection a guest device opens"
if [ -x /sbin/fw4 ]; then
	cat > /etc/guestlog.nft <<EOF
table inet guestlog
delete table inet guestlog
table inet guestlog {
	chain fwd {
		type filter hook forward priority -5; policy accept;
		iifname "$GUEST_IF" ct state new limit rate 30/second log prefix "GUESTNEW " level info
	}
}
EOF
	nft -f /etc/guestlog.nft
	grep -q guestlog.nft /etc/rc.local 2>/dev/null || sed -i '/^exit 0/i nft -f /etc/guestlog.nft' /etc/rc.local
else
	touch /etc/firewall.user
	sed -i '/GUESTNEW/d' /etc/firewall.user
	echo "iptables -I FORWARD -i $GUEST_IF -m conntrack --ctstate NEW -m limit --limit 30/second -j LOG --log-prefix \"GUESTNEW \" --log-level 6" >> /etc/firewall.user
	/etc/init.d/firewall restart
fi

echo "4/6 Installing the terms page (openNDS)"
if ! [ -x /usr/bin/opennds ]; then
	opkg update >/dev/null || die "could not reach the package list: is the router online?"
	opkg install opennds || die "could not install openNDS (see the message above)"
fi
BASE="$ND/theme_click-to-continue-basic.sh"
[ -f "$BASE" ] || BASE="$(ls $ND/theme_click-to-continue*.sh 2>/dev/null | grep -v guest | head -n 1)"
[ -n "$BASE" ] && [ -f "$BASE" ] || die "openNDS is installed but its click-to-continue page was not found in $ND"

mkdir -p /etc/opennds
sed -e "s|{{OWNER}}|$(sed_esc "$OWNER")|g" -e "s|{{CONTACT}}|$(sed_esc "$CONTACT")|g" -e "s|{{KEEP_DAYS}}|$KEEP_DAYS|g" \
	"$HERE/guest_terms.html" > /etc/opennds/guest_terms.html

# Our page = openNDS's own click-to-continue page, with the terms printed in full on it. Later shell functions replace
# earlier ones of the same name, so these two override the stock versions without editing them.
THEME="$ND/theme_guest-terms.sh"
cp "$BASE" "$THEME"
cat >> "$THEME" <<'EOF'

# ---- added by guest-setup.sh: show the full terms on the first page, not behind a button
read_terms() {
	echo "<div style=\"text-align:left;max-height:55vh;overflow:auto;border:1px solid #bbb;padding:10px;margin:10px 0;font-size:14px\">"
	cat /etc/opennds/guest_terms.html
	echo "</div>"
}
display_terms() {
	read_terms
	echo "<form action=\"/opennds_preauth/\" method=\"get\"><input type=\"hidden\" name=\"fas\" value=\"$fas\"><input type=\"submit\" value=\"Back\"></form>"
	footer
}
EOF

# One log line each time a guest accepts the terms, and each time a session ends
cat > "$ND/guest_binauth.sh" <<'EOF'
#!/bin/sh
logger -t guestwifi "binauth $*"
exit 0
EOF
chmod +x "$ND/guest_binauth.sh" "$THEME"

echo "5/6 Switching the terms page on for the guest network"
uci set opennds.@opennds[0].enabled='1'
uci set opennds.@opennds[0].gatewayinterface="$GUEST_IF"
uci set opennds.@opennds[0].gatewayname='Guest Wi-Fi - please read and accept the terms'
uci set opennds.@opennds[0].login_option_enabled='3'
uci set opennds.@opennds[0].themespec_path="$THEME"
uci set opennds.@opennds[0].binauth="$ND/guest_binauth.sh"
uci set opennds.@opennds[0].sessiontimeout='1440'
uci commit opennds
/etc/init.d/opennds enable
/etc/init.d/opennds restart

echo "6/6 Test line to Device Watch"
logger -t guestwifi "setup complete: owner=$OWNER keep_days=$KEEP_DAYS"

echo
echo "Done. Now:"
echo "  * Device Watch > GUESTS on the PC should say it is receiving the router's log."
echo "  * Join the guest Wi-Fi on a phone: the terms page should pop up. Tap Accept and browse a little;"
echo "    the phone appears on the GUESTS tab with the time it accepted the terms."
