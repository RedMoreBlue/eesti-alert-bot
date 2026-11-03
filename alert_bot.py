#!/usr/bin/env python3
"""
Relays Estonian emergency alerts (the ones the eesti.ee app shows) to a Discord
channel via a webhook, pinging a role per affected county plus an "all" role.

Uses only the Python standard library, so there is nothing to pip install.

Usage:
    python3 alert_bot.py                      # run forever (what systemd does)
    python3 alert_bot.py --dry-run            # list current alerts and their counties, post nothing
    python3 alert_bot.py --roles              # show which county roles are configured
    python3 alert_bot.py --test               # post a short "connected" message to the webhook
    python3 alert_bot.py --test-alert         # re-post the newest Kaitsevägi/Päästeamet alert as a test,
                                              # pinging the roles a real alert would
    python3 alert_bot.py --test-alert harju   # same, but ping only one county's role
    python3 alert_bot.py --test-alert all     # same, but ping only the "all" role

County names for --test-alert: harju, hiiu, ida-viru, jogeva, jarva, laane,
laane-viru, polva, parnu, rapla, saare, tartu, valga, viljandi, voru
"""

import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime


def load_env_file(path="/etc/eesti-alert-bot.env"):
    """Read KEY=value lines from the config file, so manual runs don't need
    the variables exported first. Variables already set (e.g. by systemd) win."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())
    except OSError:
        pass  # missing or not readable: rely on the environment


load_env_file()

API_URL = "https://api.app.eesti.ee/api/sitrep/v1/full-events"
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "20"))
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
USER_AGENT = "eesti-alert-discord-relay/2.0"

# The same message is often sent both as an app push and as an SMS a few
# seconds apart. We skip a message if the identical text was posted recently.
DUPLICATE_WINDOW_SECONDS = 6 * 3600


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


# ---------------------------------------------------------------- counties

# key: (official name as the API writes it, towns well inside the county as (lat, lon))
#
# App notifications list counties by name. SMS / cell-broadcast alerts only
# carry a map polygon, so for those we check which counties' towns fall inside it.
COUNTIES = {
    "harju": ("Harju maakond", [(59.437, 24.745), (59.304, 24.413), (59.357, 24.053),
                                (59.186, 25.166), (59.336, 25.336), (59.578, 25.716)]),
    "hiiu": ("Hiiu maakond", [(58.998, 22.749), (58.829, 22.778), (59.000, 22.430)]),
    "ida-viru": ("Ida-Viru maakond", [(59.359, 27.421), (59.377, 28.190), (59.398, 27.273),
                                      (59.399, 27.763), (59.098, 27.316)]),
    "jogeva": ("Jõgeva maakond", [(58.746, 26.394), (58.652, 25.971), (58.848, 26.943)]),
    "jarva": ("Järva maakond", [(58.886, 25.557), (58.808, 25.432), (58.963, 26.032), (59.040, 25.880)]),
    "laane": ("Lääne maakond", [(58.943, 23.541), (58.682, 23.845), (58.990, 24.060), (58.980, 23.890)]),
    "laane-viru": ("Lääne-Viru maakond", [(59.346, 26.355), (59.261, 25.959), (59.495, 26.533),
                                          (59.127, 26.250), (59.158, 26.115)]),
    "polva": ("Põlva maakond", [(58.061, 27.069), (58.098, 27.464), (57.984, 26.761)]),
    "parnu": ("Pärnu maakond", [(58.385, 24.497), (58.150, 24.963), (58.648, 25.036),
                                (58.074, 24.495), (58.330, 23.980)]),
    "rapla": ("Rapla maakond", [(58.999, 24.793), (58.904, 24.426), (59.168, 24.751), (58.778, 24.823)]),
    "saare": ("Saare maakond", [(58.253, 22.485), (58.560, 23.084), (58.360, 22.080), (58.570, 23.390)]),
    "tartu": ("Tartu maakond", [(58.378, 26.729), (58.223, 26.421), (58.600, 27.130), (58.240, 26.700)]),
    "valga": ("Valga maakond", [(57.777, 26.047), (58.002, 25.934), (58.058, 26.497)]),
    "viljandi": ("Viljandi maakond", [(58.364, 25.590), (58.103, 25.567), (58.537, 25.471), (58.093, 25.188)]),
    "voru": ("Võru maakond", [(57.834, 27.019), (57.826, 26.540), (57.729, 26.928), (57.730, 27.280)]),
}


def lest97(lat, lon):
    """Convert GPS lat/lon to the Estonian national grid (L-EST97, EPSG:3301),
    which is what the alert polygons use. Lambert conformal conic projection."""
    a, f = 6378137.0, 1 / 298.257222101  # GRS80 ellipsoid
    e = math.sqrt(2 * f - f * f)

    def m(p):
        return math.cos(p) / math.sqrt(1 - (e * math.sin(p)) ** 2)

    def t(p):
        s = math.sin(p)
        return math.tan(math.pi / 4 - p / 2) / ((1 - e * s) / (1 + e * s)) ** (e / 2)

    p1, p2, p0, l0 = map(math.radians, (59 + 20 / 60, 58, 57.51755393055556, 24))
    n = (math.log(m(p1)) - math.log(m(p2))) / (math.log(t(p1)) - math.log(t(p2)))
    F = m(p1) / (n * t(p1) ** n)
    r0 = a * F * t(p0) ** n
    r = a * F * t(math.radians(lat)) ** n
    theta = n * (math.radians(lon) - l0)
    return 500000 + r * math.sin(theta), 6375000 + r0 - r * math.cos(theta)


COUNTY_POINTS = {key: [lest97(lat, lon) for lat, lon in towns] for key, (_, towns) in COUNTIES.items()}
COUNTY_BY_NAME = {name: key for key, (name, _) in COUNTIES.items()}

# Discord role IDs from the config file: ROLE_HARJU, ROLE_IDA_VIRU, ... and ROLE_ALL.
def role_env_name(key):
    return "ROLE_" + key.upper().replace("-", "_")


ROLE_IDS = {key: os.environ.get(role_env_name(key), "").strip() for key in COUNTIES}
ROLE_ALL = os.environ.get("ROLE_ALL", "").strip()


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


def affected_counties(alert):
    """County keys this alert covers, in COUNTIES order. Empty if unknown."""
    # 1) App notification with no area at all: sent to everyone in Estonia.
    if alert.get("type") == "NOTIFICATION" and not alert.get("ehakLocations") and not alert.get("geoJsons"):
        return list(COUNTIES)

    # 2) Explicit county list: trust it over the polygon.
    named = {COUNTY_BY_NAME.get(loc.get("settlementUnit")) for loc in alert.get("ehakLocations") or []}
    named.discard(None)
    if named:
        return [key for key in COUNTIES if key in named]

    # 3) Only a polygon: which counties' towns are inside it.
    rings = []
    for layer in alert.get("geoJsons") or []:
        try:
            collection = json.loads(layer.get("geoJson") or "{}")
        except json.JSONDecodeError:
            continue
        for feature in collection.get("features") or []:
            rings.extend(outer_rings(feature.get("geometry") or {}))
    found = [key for key in COUNTIES
             if any(point_in_ring(x, y, ring) for ring in rings for x, y in COUNTY_POINTS[key])]
    if found or rings:
        return found

    # 4) No area data at all (happens with some "threat over" SMS): read county
    #    names from the Estonian text instead.
    return counties_in_text(text_in(alert, "ET"))


# Patterns for county names in Estonian alert text, checked in order. "ö" is
# normalised to "õ" first, because some SMS texts are typed with the wrong letter.
TEXT_PATTERNS = [
    (r"kogu eesti", list(COUNTIES)),
    (r"(ida- ja lääne-|lääne- ja ida-)\s*viru", ["ida-viru", "laane-viru"]),
    (r"ida-\s*viru", ["ida-viru"]),
    (r"lääne-\s*viru", ["laane-viru"]),
    (r"\blääne(maa|\s+maakon)", ["laane"]),
    (r"\bsaare(maa|\s+maakon)", ["saare"]),
    (r"\bharju", ["harju"]), (r"\bhiiu", ["hiiu"]), (r"\bjõgeva", ["jogeva"]),
    (r"\bjärva", ["jarva"]), (r"\bpõlva", ["polva"]), (r"\bpärnu", ["parnu"]),
    (r"\brapla", ["rapla"]), (r"\btartu", ["tartu"]), (r"\bvalga", ["valga"]),
    (r"\bviljandi", ["viljandi"]), (r"\bvõru", ["voru"]),
]


def counties_in_text(text):
    text = text.lower().replace("ö", "õ")
    found = set()
    for pattern, keys in TEXT_PATTERNS:
        if re.search(pattern, text):
            found.update(keys)
    return [key for key in COUNTIES if key in found]


def area_text(county_keys):
    if len(county_keys) == len(COUNTIES):
        return "Kogu Eesti / All of Estonia"
    if not county_keys:
        return "Teadmata / Unknown"
    return ", ".join(COUNTIES[key][0] for key in county_keys)


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


def mentions(role_ids):
    """The ping text, plus the allowed_mentions that tells Discord to actually
    ping those roles. Without allowed_mentions the text shows up but nobody is notified."""
    role_ids = [r for r in role_ids if r]
    return " ".join(f"<@&{r}>" for r in role_ids), {"parse": [], "roles": role_ids}


def roles_for(county_keys):
    """The "all" role plus each affected county's role."""
    return [ROLE_ALL] + [ROLE_IDS[key] for key in county_keys]


def build_message(event, alert):
    et = text_in(alert, "ET")
    en = text_in(alert, "EN")
    description = et
    if en:
        description += f"\n\n🇬🇧 {en}"

    counties = affected_counties(alert)
    kind = "App notification" if alert.get("type") == "NOTIFICATION" else "SMS / cell broadcast"
    pings, allowed = mentions(roles_for(counties))
    return {
        "content": pings,
        "allowed_mentions": allowed,
        "embeds": [{
            "title": "⚠️ Ohuteade",
            "description": description[:4000],
            "color": 0xE02424,
            "fields": [
                {"name": "Piirkond / Area", "value": area_text(counties)[:1000]},
                {"name": "Sündmus / Event", "value": (event.get("title") or "–")[:1000]},
            ],
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

        if alert.get("state") == "CANCELLED":
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

        log(f"Posting alert {alert_id} [{area_text(affected_counties(alert))}]: {text[:80]}")
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

    configured = sum(1 for r in ROLE_IDS.values() if r)
    log(f"Watching for alerts every {POLL_SECONDS}s "
        f"({configured}/{len(COUNTIES)} county roles, all role {'set' if ROLE_ALL else 'not set'})")
    while True:
        try:
            check_once(seen_ids, recent_texts)
            save_state(seen_ids, recent_texts)
        except Exception as e:  # network blips etc. - log and try again next round
            log(f"Error: {e}")
        time.sleep(POLL_SECONDS)


def dry_run():
    for event, alert in fetch_alerts():
        text = text_in(alert, "ET") or text_in(alert, "EN") or "(no text)"
        print(f"#{alert['id']} {alert.get('type')} {alert.get('state')} {alert.get('startDate')}\n"
              f"    area: {area_text(affected_counties(alert))}\n    {text[:150]}")


def show_roles():
    print(f"{'ROLE_ALL':18} {ROLE_ALL or '(not set)':22} every alert")
    for key, (name, _) in COUNTIES.items():
        print(f"{role_env_name(key):18} {ROLE_IDS[key] or '(not set)':22} {name}  (--test-alert {key})")


def test_post():
    if not WEBHOOK_URL:
        sys.exit("DISCORD_WEBHOOK_URL is not set")
    post_to_discord({"content": "✅ Alert relay is connected."})
    print("Sent.")


# Messages from these senders count as "real" alerts for --test-alert.
TEST_ALERT_SENDERS = ("Kaitsevägi", "Päästeamet", "Estonian Defence Forces", "Rescue Board")


def test_alert():
    """Re-post the newest Kaitsevägi/Päästeamet alert from the API, marked as a
    manual test. Goes through the same message building and posting as a real
    alert, but ignores the seen/duplicate state. An optional county key or
    "all" pings only that one role instead of the roles a real alert would."""
    if not WEBHOOK_URL:
        sys.exit("DISCORD_WEBHOOK_URL is not set")

    target = sys.argv[2].lower() if len(sys.argv) > 2 else None
    if target == "all":
        if not ROLE_ALL:
            sys.exit("ROLE_ALL is not set in /etc/eesti-alert-bot.env")
        roles, label = [ROLE_ALL], "all counties"
    elif target:
        if target not in COUNTIES:
            sys.exit(f"Unknown county '{target}'. Use one of: all, {', '.join(COUNTIES)}")
        if not ROLE_IDS[target]:
            sys.exit(f"{role_env_name(target)} is not set in /etc/eesti-alert-bot.env")
        roles, label = [ROLE_IDS[target]], COUNTIES[target][0]
    else:
        roles, label = None, None

    newest = None
    for event, alert in fetch_alerts():
        text = text_in(alert, "ET") or text_in(alert, "EN")
        if text.startswith(TEST_ALERT_SENDERS):
            if newest is None or (alert.get("startDate") or "") > (newest[1].get("startDate") or ""):
                newest = (event, alert)
    if newest is None:
        sys.exit("No Kaitsevägi or Päästeamet alert found in the API right now")

    event, alert = newest
    message = build_message(event, alert)
    message["embeds"][0]["title"] = "🧪 MANUAL TEST – " + message["embeds"][0]["title"]
    if roles is not None:
        message["content"], message["allowed_mentions"] = mentions(roles)
    heading = f"**MANUAL TEST{f' ({label})' if label else ''}** – not a real alert"
    message["content"] = f"{message['content']} {heading}".strip()

    post_to_discord(message)
    print(f"Sent alert #{alert.get('id')} from {alert.get('startDate')} as a manual test"
          f"{f' pinging {label}' if label else ''}.")


if __name__ == "__main__":
    commands = {"": run, "--dry-run": dry_run, "--roles": show_roles,
                "--test": test_post, "--test-alert": test_alert}
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg not in commands:
        sys.exit(__doc__)  # unknown option: show usage instead of starting the bot
    commands[arg]()
