# Eesti alert → Discord relay (MADE WITH AI!!!! BECAUSE IM LAZY ASFFFFFFFFFFF)

Polls the eesti.ee alert API every 5 minutes and posts every new alert to a
Discord channel, pinging a role for each affected county plus an "all counties" role.

> [!NOTE]
> **Unofficial hobby project.** Not made by, affiliated with or endorsed by RIA,
> the Eesti äpp or any government agency. It has no special access: it reads
> the same public, login-free alert list that the Eesti äpp downloads on every
> phone, and only reads it, nothing more.

> [!WARNING]
> **Be gentle with the API.** That address is meant for the Eesti äpp, not
> published for third parties. It has no documentation and no published rate
> limits, and it sits behind Cloudflare. A Discord relay is a backup channel:
> people already get the official SMS and app push.
>
> - The bot checks **every 5 minutes** by default (`POLL_SECONDS=300`).
> - You can make it check more often, but it **never goes below 60 seconds**,
>   whatever you set. Please don't patch that out.
> - After a failed check it waits longer each time (up to 1 hour), and systemd
>   waits a minute before restarting after a crash, so a block or outage
>   doesn't turn into a flood of retries.
> - Running several copies of this bot multiplies the load. Run one.

## How it works

1. **Poll.** `GET https://api.app.eesti.ee/api/sitrep/v1/full-events` returns a
   list of *events* (e.g. "Droonioht"), each with a list of *alerts* that were
   actually sent out. The API has no push mechanism, so we just ask regularly.
2. **Work out which counties an alert covers.** There are two kinds of alerts:
   - `NOTIFICATION` is the push notification in the app. It lists its counties
     by name in `ehakLocations`. One with no area at all went to all of Estonia.
   - `MASS_SMS` is the SMS / cell broadcast to phones. It only has a map polygon
     (`geoJsons`) in the Estonian grid (L-EST97). The bot has 3–6 towns per county
     and checks which of them fall inside the polygon.
   - If an alert has no area data at all (some "threat over" SMS), the bot reads
     county names from the text instead ("Tartu, Jõgeva … maakonnas").
3. **Ping.** The `ROLE_ALL` role plus the role of each affected county.
   All-clear messages ("õhuoht … lõppenud", "Häire lõpp") are posted as a green
   **✅ Ohu lõpp** instead of the red **⚠️ Ohuteade**, with the same pings.
4. **Remember what was posted.** Every alert has a numeric `id`. Handled ids go
   into `state.json`, so a restart doesn't repost anything. On the very first
   start, everything already in the API is marked as seen without posting.
5. **No double posts.** The app push and the SMS usually carry the same text, so
   if identical text was posted in the last 6 hours, the second copy is skipped.

## 1. Discord setup

The trick: Discord can't ping "people with role A **and** role B", but it can
hide a channel. People who can't see a channel aren't notified by pings in it.

1. **Roles.** Create `@ohuteavitus`, one role per county you want (e.g.
   `@Harjumaa`, `@Tartumaa`) and one for all counties (e.g. `@Kõik maakonnad`).
   For each county role and the all role, turn on **Allow anyone to @mention this role**.
2. **Channel.** Create e.g. `#ohuteavitus`. In its permissions, deny
   **View Channel** for `@everyone` and allow it for `@ohuteavitus`.
3. **Webhook.** In that channel: settings → **Integrations** → **Webhooks** →
   **New Webhook** → name and picture → **Copy Webhook URL**.
4. **Role IDs.** User Settings → **Advanced** → **Developer Mode** on. Then
   Server Settings → **Roles** → right-click a role → **Copy Role ID**.
5. **Letting people pick roles.** Server Settings → **Onboarding** can ask new
   members questions and hand out roles from the answers, e.g. "Do you want
   alerts?" → `@ohuteavitus`, "Which counties?" (multiple choice) → county roles.
   Onboarding needs the server to be a Community server. Existing members can
   change their answers under **Channels & Roles**.

## 2. Install in a Debian LXC (Proxmox)

Debian 12, unprivileged, 1 core / 512–1024 MB RAM / 4 GB disk. Inside the container, as root:

```bash
apt update && apt install -y python3 git
git clone https://github.com/RedMoreBlue/eesti-alert-bot.git /opt/eesti-alert-bot
useradd --system --no-create-home --shell /usr/sbin/nologin alertbot
cp /opt/eesti-alert-bot/alert-bot.env.example /etc/eesti-alert-bot.env
nano /etc/eesti-alert-bot.env      # webhook URL and role IDs
chmod 600 /etc/eesti-alert-bot.env
```

The code in `/opt/eesti-alert-bot` stays owned by root; the bot only needs to read it.
Its memory (`state.json`) lives in `/var/lib/eesti-alert-bot`, which systemd creates.

## 3. Try it by hand

Run these as root (the config file is root-only; the script reads it itself).

```bash
cd /opt/eesti-alert-bot
python3 alert_bot.py --dry-run     # lists current alerts and their counties; posts nothing
python3 alert_bot.py --roles       # shows which role IDs are filled in
python3 alert_bot.py --test        # sends a "connected" message to Discord
```

## Manual test with a real alert

```bash
python3 /opt/eesti-alert-bot/alert_bot.py --test-alert          # pings what a real alert would
python3 /opt/eesti-alert-bot/alert_bot.py --test-alert harju    # pings only the Harju role
python3 /opt/eesti-alert-bot/alert_bot.py --test-alert all      # pings only the all-counties role
```

Fetches the API, takes the newest alert sent by Kaitsevägi or Päästeamet, and
posts it exactly like a real alert, but titled **🧪 MANUAL TEST**. It doesn't
touch the running service or its memory. County names: `harju hiiu ida-viru
jogeva jarva laane laane-viru polva parnu rapla saare tartu valga viljandi voru`.

## 4. Run it as a service

```bash
cp /opt/eesti-alert-bot/eesti-alert-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now eesti-alert-bot
systemctl status eesti-alert-bot
journalctl -u eesti-alert-bot -f   # live log; Ctrl+C to stop watching
```

`Restart=always` brings it back if it crashes; `enable` starts it on boot.

## Updating

```bash
cd /opt/eesti-alert-bot && git pull
cp eesti-alert-bot.service /etc/systemd/system/ && systemctl daemon-reload
systemctl restart eesti-alert-bot
```

`git pull` never touches `/etc/eesti-alert-bot.env`. If an update adds new
settings, compare with `alert-bot.env.example` and add them by hand.
