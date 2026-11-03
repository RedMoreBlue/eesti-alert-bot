# Eesti alert → Discord relay

Polls the eesti.ee alert API every 20 seconds and posts any new alert covering
**Harju maakond** to a Discord channel with an `@everyone` ping.

## How it works

1. **Poll.** `GET https://api.app.eesti.ee/api/sitrep/v1/full-events` returns a
   list of *events* (e.g. "Droonioht"), each with a list of *alerts* that were
   actually sent out. The API has no push mechanism, so we just ask regularly.
2. **Two kinds of alerts:**
   - `NOTIFICATION` is the push notification in the app. It lists the counties it
     targets in `ehakLocations` (Harju = `MK00000037`). One with no area at all
     went to everyone in Estonia.
   - `MASS_SMS` is the SMS / cell broadcast to phones. It has no county list,
     only a map polygon (`geoJsons`) in the Estonian grid (L-EST97). We check
     whether the polygon contains Tallinn, Keila, Paldiski, Kose, Kehra or Loksa.
3. **Remember what was posted.** Every alert has a numeric `id`. Handled ids go
   into `state.json`, so a restart doesn't repost anything. On the very first
   start, everything already in the API is marked as seen without posting.
4. **No double posts.** The app push and the SMS usually carry the same text, so
   if identical text was posted in the last 6 hours, the second copy is skipped.
5. **Post.** A webhook is just a secret URL; an HTTP POST with JSON to it makes a
   message appear in the channel. No bot account or login needed.

## 1. Create the webhook

Discord → your server → channel settings (gear) → **Integrations** → **Webhooks**
→ **New Webhook** → name it, set a picture, pick the channel → **Copy Webhook URL**.

## 2. Install in a Debian LXC (Proxmox)

Debian 12, unprivileged, 1 core / 512–1024 MB RAM / 4 GB disk. Inside the container, as root:

```bash
apt update && apt install -y python3 git
git clone https://github.com/RedMoreBlue/eesti-alert-bot.git /opt/eesti-alert-bot
useradd --system --no-create-home --shell /usr/sbin/nologin alertbot
cp /opt/eesti-alert-bot/alert-bot.env.example /etc/eesti-alert-bot.env
nano /etc/eesti-alert-bot.env      # paste your webhook URL
chmod 600 /etc/eesti-alert-bot.env
```

The code in `/opt/eesti-alert-bot` stays owned by root; the bot only needs to read it.
Its memory (`state.json`) lives in `/var/lib/eesti-alert-bot`, which systemd creates.

## 3. Try it by hand

```bash
cd /opt/eesti-alert-bot
python3 alert_bot.py --dry-run     # lists current alerts that match Harju; posts nothing
python3 alert_bot.py --test        # sends a "connected" message to Discord
```

The script reads `/etc/eesti-alert-bot.env` itself, so run these as root (the file is root-only).

## Manual test with a real alert

```bash
python3 /opt/eesti-alert-bot/alert_bot.py --test-alert
python3 /opt/eesti-alert-bot/alert_bot.py --test-alert --ping   # also pings @everyone
```

Fetches the API, takes the newest alert sent by Kaitsevägi or Päästeamet (any region),
and posts it exactly like a real alert, but titled **🧪 MANUAL TEST**. It doesn't touch
the running service or its memory, so the real alert can still be posted when it arrives.

## 4. Run it as a service

```bash
cp /opt/eesti-alert-bot/eesti-alert-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now eesti-alert-bot
systemctl status eesti-alert-bot
journalctl -u eesti-alert-bot -f   # live log
```

`Restart=always` brings it back if it crashes; `enable` starts it on boot.

## Updating

```bash
cd /opt/eesti-alert-bot && git pull
cp eesti-alert-bot.service /etc/systemd/system/ && systemctl daemon-reload
systemctl restart eesti-alert-bot
```

To change the county, edit `COUNTY_CODE`, `COUNTY_NAME` and `HARJU_POINTS` at the top of `alert_bot.py`.
