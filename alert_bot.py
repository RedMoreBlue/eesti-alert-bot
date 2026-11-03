#!/usr/bin/env python3
"""
Relays Estonian emergency alerts (the ones the eesti.ee app shows) that cover
Harju maakond to a Discord channel via a webhook.

Uses only the Python standard library, so there is nothing to pip install.

Usage:
    python3 alert_bot.py              # run forever (what systemd does)
    python3 alert_bot.py --dry-run    # print which current alerts match Harju, post nothing
    python3 alert_bot.py --test       # post a test message to the webhook
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

API_URL = "https://api.app.eesti.ee/api/sitrep/v1/full-events"
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "20"))
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
USER_AGENT = "eesti-alert-discord-relay/1.0"

# The county we care about. App notifications list counties by EHAK code/name.
COUNTY_CODE = "MK00000037"
COUNTY_NAME = "Harju maakond"

# SMS / cell-broadcast alerts only carry a map polygon, no county names.
# Coordinates are in the Estonian national grid (L-EST97, EPSG:3301) as
# [easting, northing] in metres. If any of these points falls inside the
# alert polygon, we treat the alert as covering Harju maakond.
HARJU_POINTS = {
    "Tallinn": (542275, 6589031),
    "Keila": (523527, 6574052),
    "Paldiski": (503015, 6579885),
    "Kose": (566649, 6561414),
    "Kehra": (576033, 6578302),
    "Loksa": (596965, 6605745),
}

# The same message is often sent both as an app push and as an SMS a few
# seconds apart. We skip a message if the identical text was posted recently.
DUPLICATE_WINDOW_SECONDS = 6 * 3600


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


# ---------------------------------------------------------------- fetching

def fetch_alerts():
    """Return a flat list of (event, alert) pairs from the API."""
    req = urllib.request.Request(API_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        events = json.load(resp)

    pairs = []
    for item in events:
        data = item.get("data") or {}
        event = data.get("event") or {}
        for alert in data.get("alerts") or []:
            pairs.append((event, alert))
    return pairs


# ---------------------------------------------------------------- location

def point_in_ring(x, y, ring):
    """Ray casting: count how many polygon edges a ray from (x, y) crosses."""
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def outer_rings(geometry):
    """Outer boundary of each polygon (holes are ignored; good enough here)."""
    if geometry.get("type") == "Polygon":
        return [geometry["coordinates"][0]]
    if geometry.get("type") == "MultiPolygon":
        return [poly[0] for poly in geometry["coordinates"]]
    return []


def covers_county(alert):
    # 0) App notification with no area at all: sent to everyone in Estonia.
    if alert.get("type") == "NOTIFICATION" and not alert.get("ehakLocations") and not alert.get("geoJsons"):
        return True

    # 1) App notifications: explicit list of counties.
    for loc in alert.get("ehakLocations") or []:
        if loc.get("identifier") == COUNTY_CODE or loc.get("settlementUnit") == COUNTY_NAME:
            return True

    # 2) SMS alerts: check the map polygon(s).
    for layer in alert.get("geoJsons") or []:
        try:
            collection = json.loads(layer.get("geoJson") or "{}")
        except json.JSONDecodeError:
            continue
        for feature in collection.get("features") or []:
            for ring in outer_rings(feature.get("geometry") or {}):
                if any(point_in_ring(x, y, ring) for x, y in HARJU_POINTS.values()):
                    return True
    return False


# ---------------------------------------------------------------- content

def text_in(alert, lang):
    """Message text in a language. The API puts the language in either
    countryCode (app notifications) or languageCode (SMS)."""
    for c in alert.get("content") or []:
        if lang in (c.get("languageCode"), c.get("countryCode")) and (c.get("text") or "").strip():
            return c["text"].strip()
    return ""


def fingerprint(text):
    return re.sub(r"[^0-9a-zõäöüšž]", "", text.lower())


def build_message(event, alert):
    et = text_in(alert, "ET")
    en = text_in(alert, "EN")
    description = et
    if en:
        description += f"\n\n🇬🇧 {en}"

    kind = "App notification" if alert.get("type") == "NOTIFICATION" else "SMS / cell broadcast"
    return {
        "content": "@everyone",
        # Without this, Discord would show "@everyone" as text but not ping anyone.
        "allowed_mentions": {"parse": ["everyone"]},
        "embeds": [{
            "title": f"⚠️ Ohuteade – {COUNTY_NAME}",
            "description": description[:4000],
            "color": 0xE02424,
            "fields": [{"name": "Sündmus / Event", "value": (event.get("title") or "–")[:1000]}],
            "footer": {"text": f"{kind} · alert #{alert.get('id')} · kriis.ee · 1247"},
            "timestamp": alert.get("startDate"),
        }],
    }


# ---------------------------------------------------------------- discord

def post_to_discord(payload):
    body = json.dumps(payload).encode()
    for attempt in range(3):
        req = urllib.request.Request(
            WEBHOOK_URL, data=body, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(req, timeout=30):
                return
        except urllib.error.HTTPError as e:
            if e.code == 429:  # rate limited: Discord says how long to wait
                wait = float(json.load(e).get("retry_after", 2))
                log(f"Discord rate limit, waiting {wait}s")
                time.sleep(wait)
                continue
            raise RuntimeError(f"Discord returned {e.code}: {e.read()[:300]!r}")
    raise RuntimeError("Discord kept rate limiting us")


# ---------------------------------------------------------------- state

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
        return set(state["seen_ids"]), state["recent_texts"]
    except FileNotFoundError:
        return None, {}


def save_state(seen_ids, recent_texts):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"seen_ids": sorted(seen_ids), "recent_texts": recent_texts}, f)
    os.replace(tmp, STATE_FILE)  # atomic, so a crash can't leave a half-written file


# ---------------------------------------------------------------- main loop

def check_once(seen_ids, recent_texts):
    now = time.time()
    for fp, ts in list(recent_texts.items()):
        if now - ts > DUPLICATE_WINDOW_SECONDS:
            del recent_texts[fp]

    for event, alert in fetch_alerts():
        alert_id = alert.get("id")
        if alert_id in seen_ids:
            continue

        if alert.get("state") == "CANCELLED" or not covers_county(alert):
            seen_ids.add(alert_id)
            continue
        text = text_in(alert, "ET") or text_in(alert, "EN")
        if not text:
            continue  # text not filled in yet; look again next round
        fp = fingerprint(text)
        if fp in recent_texts:
            log(f"Alert {alert_id} has the same text as one already posted, skipping")
            seen_ids.add(alert_id)
            continue

        log(f"Posting alert {alert_id}: {text[:80]}")
        post_to_discord(build_message(event, alert))
        # Only marked as seen after a successful post, so a failed post is retried.
        seen_ids.add(alert_id)
        recent_texts[fp] = now


def run():
    if not WEBHOOK_URL:
        sys.exit("DISCORD_WEBHOOK_URL is not set")

    seen_ids, recent_texts = load_state()
    if seen_ids is None:
        # First start: remember everything already in the API without posting,
        # otherwise we'd spam the channel with months-old alerts.
        seen_ids = {a.get("id") for _, a in fetch_alerts()}
        save_state(seen_ids, recent_texts)
        log(f"First run: marked {len(seen_ids)} existing alerts as already seen")

    log(f"Watching for {COUNTY_NAME} alerts every {POLL_SECONDS}s")
    while True:
        try:
            check_once(seen_ids, recent_texts)
            save_state(seen_ids, recent_texts)
        except Exception as e:  # network blips etc. - log and try again next round
            log(f"Error: {e}")
        time.sleep(POLL_SECONDS)


def dry_run():
    for event, alert in fetch_alerts():
        if covers_county(alert):
            text = text_in(alert, "ET") or text_in(alert, "EN") or "(no text)"
            print(f"#{alert['id']} {alert.get('type')} {alert.get('state')} {alert.get('startDate')}\n    {text[:150]}")


def test_post():
    if not WEBHOOK_URL:
        sys.exit("DISCORD_WEBHOOK_URL is not set")
    post_to_discord({"content": f"✅ Alert relay is connected. Watching {COUNTY_NAME}."})
    print("Sent.")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    {"--dry-run": dry_run, "--test": test_post}.get(arg, run)()
