"""Windows pop-up notifications and the optional Sunday email digest."""
from __future__ import annotations

import datetime as dt
import os
import smtplib
import subprocess
from email.message import EmailMessage

TOAST_PS = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$x = New-Object Windows.Data.Xml.Dom.XmlDocument
$x.LoadXml('<toast><visual><binding template="ToastGeneric"><text></text><text></text></binding></visual></toast>')
$t = $x.GetElementsByTagName('text')
$t.Item(0).AppendChild($x.CreateTextNode($env:TDH_TITLE)) | Out-Null
$t.Item(1).AppendChild($x.CreateTextNode($env:TDH_BODY)) | Out-Null
$n = [Windows.UI.Notifications.ToastNotification]::new($x)
$id = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Show($n)
"""

PASSWORD_ENV = "TRAVELHUB_SMTP_PASSWORD"


def toast(title: str, body: str) -> bool:
    """Starts the notification in the background; PowerShell can take a minute to start on a busy PC."""
    env = dict(os.environ, TDH_TITLE=title[:120], TDH_BODY=body[:240])
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", TOAST_PS],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except OSError:
        return False


def _fmt(item: dict) -> str:
    price = f"${item['price_usd']:,} - " if item.get("price_usd") else ""
    return f"  - {price}{item['title']}\n    {item['link']}"


def build_digest(snap: dict, user: dict, new_matches: list) -> str:
    import alerts

    items = snap["items"]
    home = [i for i in items if i.get("home") and "flight" in i["tags"] and i.get("price_usd")]
    home.sort(key=lambda i: i["price_usd"])
    today = dt.date.today()
    soon = [i for i in items if i.get("deadline") and today <= dt.date.fromisoformat(i["deadline"])
            <= today + dt.timedelta(days=7)]
    soon.sort(key=lambda i: i["deadline"])
    watch = alerts.watch_matches(items, user.get("watchlist", []))
    parts = [f"Travel Deals Hub - week of {snap['week_of']}\n"]
    if snap.get("ai_summary"):
        parts.append("TOP PICKS\n" + snap["ai_summary"] + "\n")
    if new_matches:
        parts.append("FARE ALERTS\n" + "\n".join(f"  [{r['name']}]\n" + _fmt(i) for r, i in new_matches) + "\n")
    if home:
        parts.append("CHEAPEST FROM DC\n" + "\n".join(_fmt(i) for i in home[:5]) + "\n")
    if watch:
        parts.append("YOUR WATCHLIST\n" + "\n".join(f"  [{p}]\n" + _fmt(i) for p, i in watch[:8]) + "\n")
    if soon:
        parts.append("ENDING THIS WEEK\n" + "\n".join(f"  (ends {i['deadline']})\n" + _fmt(i) for i in soon[:8]))
    return "\n".join(parts)


def send_weekly_digest(snap: dict, user: dict, new_matches: list, force: bool = False) -> bool:
    """Sends once per week (or when forced from the Settings tab). Password comes from an environment variable."""
    import userdata

    cfg = user.get("email", {})
    password = os.environ.get(PASSWORD_ENV, "")
    if not (cfg.get("enabled") and cfg.get("username") and cfg.get("to") and password):
        return False
    if not force and user.get("digest_sent_week") == snap["week_of"]:
        return False
    msg = EmailMessage()
    msg["Subject"] = f"Travel deals for the week of {snap['week_of']}"
    msg["From"] = cfg["username"]
    msg["To"] = cfg["to"]
    msg.set_content(build_digest(snap, user, new_matches))
    with smtplib.SMTP(cfg.get("smtp_host", "smtp.mail.yahoo.com"), int(cfg.get("smtp_port", 587)), timeout=30) as s:
        s.starttls()
        s.login(cfg["username"], password)
        s.send_message(msg)
    userdata.update(lambda d: d.__setitem__("digest_sent_week", snap["week_of"]))
    return True
