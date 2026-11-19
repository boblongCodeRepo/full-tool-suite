#!/bin/sh
# Undo guest-setup.sh: no terms page, no logging, nothing sent to the PC. The guest Wi-Fi itself is left as it is
# (switch it off in the router's web page if you want it gone too). Run on the router:  sh /tmp/guest-router/guest-undo.sh
ND=/usr/lib/opennds

echo "Terms page off"
/etc/init.d/opennds stop 2>/dev/null
/etc/init.d/opennds disable 2>/dev/null
rm -f "$ND/theme_guest-terms.sh" "$ND/guest_binauth.sh" /etc/opennds/guest_terms.html

echo "Connection log off"
nft delete table inet guestlog 2>/dev/null
rm -f /etc/guestlog.nft
sed -i '/guestlog.nft/d' /etc/rc.local 2>/dev/null
if [ -f /etc/firewall.user ] && grep -q GUESTNEW /etc/firewall.user; then
	sed -i '/GUESTNEW/d' /etc/firewall.user
	/etc/init.d/firewall restart
fi

echo "DNS log off"
uci set dhcp.@dnsmasq[0].logqueries='0'
uci -q del_list dhcp.@dnsmasq[0].address='/use-application-dns.net/'
uci commit dhcp
/etc/init.d/dnsmasq restart

echo "No longer sending the log to the PC"
uci -q delete system.@system[0].log_ip
uci -q delete system.@system[0].log_port
uci -q delete system.@system[0].log_proto
uci commit system
/etc/init.d/log restart

echo "Done."
