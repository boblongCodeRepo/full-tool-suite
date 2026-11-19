# Guest Wi-Fi with terms page and logging

Steps for the day the **GL.iNet Flint 2 (GL-MT6000)** arrives. These steps take about 30 minutes.

When you're done:
- Guests join a separate Wi-Fi network. They can't reach this PC or your other devices.
- Before they get internet access, guests see your terms ([guest_terms.html](guest_terms.html)) and must tap **Accept**.
- The router sends its log to this PC. **Device Watch > GUESTS** keeps a record of:
  - who accepted the terms, and when;
  - every site name each guest device looked up;
  - every connection each guest device opened.
- Logs are kept for 90 days. Device Watch alerts you if a guest seems to be using BitTorrent or piracy sites.

---

## 1. Plug it in

1. Connect your internet modem to the Flint 2's **WAN** port, and this PC to one of its **LAN** ports, or to its main (not guest) Wi-Fi.
2. In a browser, go to **http://192.168.8.1**. Choose a language and set an **admin password**, and write it down.
3. Follow the setup wizard until the router is online, then update the firmware: **System > Upgrade**.
4. **Wireless:** give your own Wi-Fi a name and a strong password. This network is for you only.

## 2. Turn on the guest network

1. Go to **Wireless > Guest Wi-Fi** and switch it **on**, for both 2.4 GHz and 5 GHz.
2. Name it something like `Guest WiFi`.
3. For **Security**, choose **None (open)**. The terms page is what controls access. If you'd rather have a password as well, you can set one; the terms page still appears.
4. Click **Apply**. GL.iNet keeps guest devices away from your own network by default. Keep it that way.

## 3. Give this PC a fixed address

The router must always know where to send its log.

1. Go to **Clients**, find this PC (MELE or similar) and choose **Reserve IP**. Note the address, for example `192.168.8.100`.
2. Under **Network > DNS**, switch on **Override DNS Settings of All Clients**. This makes guest phones use the router's DNS, so their lookups get logged.

## 4. Allow the log through the PC's firewall

Open **Device Watch > GUESTS** on the PC. It shows whether it is listening.

The first time it starts listening, Windows may ask whether MarketTerminal may use the network. Choose **Private networks** and click **Allow**.

## 5. Run the setup script on the router

On this PC, open PowerShell in the project folder and run these commands. Replace the address, name and contact with your own:

```powershell
scp -O -r tools\guest-router root@192.168.8.1:/tmp/
ssh root@192.168.8.1
```

The password is the router admin password from step 1. Then, on the router:

```sh
sh /tmp/guest-router/guest-setup.sh 192.168.8.100 "Your Name" "you@example.com"
```

The name and contact are printed in the terms ("provided by …", "Questions: …"). You can use a business name and a separate email address.

The script:
- sends the router log to the PC;
- turns on DNS and connection logging for the guest network;
- installs the terms page (openNDS) on the guest network.

It finishes with **Done**. If it stops, it says why. Usually the guest Wi-Fi isn't switched on yet, or the router isn't online.

## 6. Test with your phone

1. On the phone, join `Guest WiFi`. The terms page should open by itself. If it doesn't, open any `http://` site, such as http://neverssl.com.
2. **Check that the full terms are shown on the page**, then tap **Accept** and browse for a minute.
3. On the PC, check **Device Watch > GUESTS**. Your phone should appear with **Accepted terms** and the time, plus the sites it looked up.
4. Click **MINE** next to your phone, so your own tests aren't counted as a guest. They're still logged like any other guest.

## Optional: send guest traffic through a VPN

This gives the most protection. Guest traffic then reaches the internet from the VPN's address, not yours, so copyright notices about guests don't come to you.

1. You need a VPN subscription that supports WireGuard and allows routers, such as Mullvad, Proton VPN or IVPN.
2. On the router: **VPN > WireGuard Client**. Add the VPN's configuration.
3. Under **VPN Policy**, choose **"Based on the VPN client's interface"** and send **only the guest network** through it.

## When something happens

When you get an ISP copyright notice, a police request or any other complaint, it will give a **date and time**, and usually a remote address and port.

1. Go to **GUESTS > Who was on at…**. Enter the time, and the address and port if you have them. The tab shows which guest devices were active and when each accepted the terms.
2. Use **Export evidence** for that day. This saves a folder in your Downloads containing:
   - spreadsheets of acceptances, devices, lookups and connections;
   - the raw router log, with a tamper-evident fingerprint chain.
3. Answer the notice through a lawyer, or by stating that the activity came from a guest on your public network who agreed to your terms. Don't send logs about other guests.

## Turning it off

```sh
sh /tmp/guest-router/guest-undo.sh
```

If `/tmp` was cleared by a reboot, copy the folder over again first, as in step 5. This removes the terms page and all logging. The guest Wi-Fi itself stays on until you switch it off in the web page.

## Changing the terms

1. Edit [guest_terms.html](guest_terms.html).
2. Run steps 5's `scp` and the `guest-setup.sh` command again.

If this is for a business open to the public, have a DC lawyer read the terms once.
