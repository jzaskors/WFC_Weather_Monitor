#!/usr/bin/env python3
"""
WFC West Harbor weather monitor.
Checks live conditions and sends SMS (Twilio) + email when paddle rentals or
sailing programs should HALT, and an all-clear when they reopen.

Mirrors the decision logic in the dashboard. Edit THRESHOLDS to keep them in sync.
Designed to run every ~10 min on a scheduler (e.g. GitHub Actions cron).
State is persisted in state.json so you only get alerted on a *change*, not every run.
"""

import json
import os
import smtplib
import sys
from datetime import datetime
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import requests

# ----------------------------------------------------------------------------
# SITE + THRESHOLDS  (keep in sync with the dashboard)
# ----------------------------------------------------------------------------
LAT, LON = 40.876, -73.530
TZ = ZoneInfo("America/New_York")

# Only alert during operating hours (24h clock, local time)
OPEN_HOUR, CLOSE_HOUR = 7, 20

# Also alert when status enters CAUTION (not just HALT)? Usually noisy -> False
ALERT_ON_CAUTION = true

THRESHOLDS = {
    "paddle": {"windCaution": 12, "windStop": 16, "gustCaution": 16, "gustStop": 20,
               "offWindCaution": 8, "offWindStop": 13},
    "sail":   {"windCaution": 16, "windStop": 22, "gustCaution": 22, "gustStop": 28},
    "shared": {"rainCaution": 0.10, "rainStop": 0.30, "capeWatch": 1000, "visStopMi": 0.5},
    # Offshore wind arc (degrees the wind blows FROM). North-facing launch -> S/SW = offshore.
    "offshoreArc": {"from": 150, "to": 240},
}

LABELS = {"paddle": "Kayak / SUP rentals", "sail": "Sailing programs"}
ORDER = {"GO": 0, "CAUTION": 1, "STOP": 2}
COMPASS = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"]
STATE_FILE = "state.json"


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def compass(d):
    return "—" if d is None else COMPASS[round((d % 360) / 22.5) % 16]


def in_arc(d, a, b):
    if d is None:
        return False
    d = (d % 360 + 360) % 360
    return (a <= d <= b) if a <= b else (d >= a or d <= b)


def is_storm(code):
    return code in (95, 96, 99)


def wx_text(c):
    if c in (95, 96, 99): return "Thunderstorm"
    if c in (80, 81, 82): return "Rain showers"
    if c in (61, 63, 65): return "Rain"
    if c in (51, 53, 55, 56, 57): return "Drizzle"
    if c in (45, 48): return "Fog"
    if c == 0: return "Clear"
    if c in (1, 2): return "Partly cloudy"
    if c == 3: return "Overcast"
    return "—"


# ----------------------------------------------------------------------------
# Fetch
# ----------------------------------------------------------------------------
def fetch_conditions():
    fc = ("https://api.open-meteo.com/v1/forecast"
          f"?latitude={LAT}&longitude={LON}"
          "&current=temperature_2m,precipitation,weather_code,wind_speed_10m,"
          "wind_direction_10m,wind_gusts_10m,cape,visibility"
          "&wind_speed_unit=kn&temperature_unit=fahrenheit&precipitation_unit=inch"
          "&timezone=America%2FNew_York")
    mar = ("https://marine-api.open-meteo.com/v1/marine"
           f"?latitude={LAT}&longitude={LON}"
           "&current=wave_height,sea_surface_temperature&timezone=America%2FNew_York")
    nws = f"https://api.weather.gov/alerts/active?point={LAT},{LON}"

    f = requests.get(fc, timeout=20).json()
    c = f["current"]

    try:
        m = requests.get(mar, timeout=20).json().get("current", {})
    except Exception:
        m = {}

    alerts = []
    try:
        a = requests.get(nws, timeout=20,
                         headers={"User-Agent": "WFC-weather-monitor (ops@thewaterfrontcenter.org)"}).json()
        alerts = [feat["properties"]["event"] for feat in a.get("features", [])
                  if feat.get("properties", {}).get("event")]
    except Exception:
        pass

    vis = c.get("visibility")
    wave = m.get("wave_height")
    sst = m.get("sea_surface_temperature")
    return {
        "temp": c.get("temperature_2m"),
        "precip": c.get("precipitation"),
        "weatherCode": c.get("weather_code"),
        "storm": is_storm(c.get("weather_code")),
        "wind": c.get("wind_speed_10m"),
        "dir": c.get("wind_direction_10m"),
        "gust": c.get("wind_gusts_10m"),
        "cape": c.get("cape"),
        "visMi": vis / 1609.34 if vis is not None else None,
        "waveFt": wave * 3.281 if wave is not None else None,
        "sst": sst * 9 / 5 + 32 if sst is not None else None,
    }, alerts


# ----------------------------------------------------------------------------
# Decision engine (mirrors dashboard evalActivity)
# ----------------------------------------------------------------------------
def evaluate(activity, cur, alert_stop):
    reasons, level = [], "GO"

    def bump(lvl, reason):
        nonlocal level
        reasons.append((lvl, reason))
        if ORDER[lvl] > ORDER[level]:
            level = lvl

    s = THRESHOLDS["shared"]
    if cur["storm"]:
        bump("STOP", "Thunderstorm overhead — clear the water")
    if alert_stop:
        bump("STOP", "Active NWS marine/storm warning")
    if cur["precip"] is not None and cur["precip"] >= s["rainStop"]:
        bump("STOP", f"Heavy rain ({cur['precip']:.2f} in/hr)")
    elif cur["precip"] is not None and cur["precip"] >= s["rainCaution"]:
        bump("CAUTION", f"Rain ({cur['precip']:.2f} in/hr)")
    if cur["visMi"] is not None and cur["visMi"] < s["visStopMi"]:
        bump("STOP", f"Low visibility ({cur['visMi']:.1f} mi)")
    if cur["cape"] is not None and cur["cape"] >= s["capeWatch"]:
        bump("CAUTION", f"Unstable air (CAPE {round(cur['cape'])}) — storms possible")

    t = THRESHOLDS[activity]
    if activity == "paddle" and in_arc(cur["dir"], THRESHOLDS["offshoreArc"]["from"], THRESHOLDS["offshoreArc"]["to"]):
        if cur["wind"] is not None and cur["wind"] >= t["offWindStop"]:
            bump("STOP", f"Offshore wind {round(cur['wind'])} kn — pushes paddlers out")
        elif cur["wind"] is not None and cur["wind"] >= t["offWindCaution"]:
            bump("CAUTION", f"Offshore wind {round(cur['wind'])} kn")
    if cur["wind"] is not None and cur["wind"] >= t["windStop"]:
        bump("STOP", f"Sustained wind {round(cur['wind'])} kn")
    elif cur["wind"] is not None and cur["wind"] >= t["windCaution"]:
        bump("CAUTION", f"Sustained wind {round(cur['wind'])} kn")
    if cur["gust"] is not None and cur["gust"] >= t["gustStop"]:
        bump("STOP", f"Gusts {round(cur['gust'])} kn")
    elif cur["gust"] is not None and cur["gust"] >= t["gustCaution"]:
        bump("CAUTION", f"Gusts {round(cur['gust'])} kn")

    return level, reasons


# ----------------------------------------------------------------------------
# Notifications
# ----------------------------------------------------------------------------
def send_sms(body):
    sid = os.environ.get("TWILIO_SID")
    token = os.environ.get("TWILIO_TOKEN")
    frm = os.environ.get("TWILIO_FROM")
    to = [x.strip() for x in os.environ.get("ALERT_SMS_TO", "").split(",") if x.strip()]
    if not (sid and token and frm and to):
        print("SMS not configured, skipping.")
        return
    try:
        from twilio.rest import Client
        client = Client(sid, token)
        for num in to:
            client.messages.create(body=body, from_=frm, to=num)
            print(f"SMS sent to {num}")
    except Exception as e:
        print(f"SMS send failed (continuing): {e}")


def send_email(subject, body):
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    frm = os.environ.get("EMAIL_FROM", user)
    to = [x.strip() for x in os.environ.get("EMAIL_TO", "").split(",") if x.strip()]
    if not (host and user and pw and to):
        print("Email not configured, skipping.")
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = frm
    msg["To"] = ", ".join(to)
    try:
        with smtplib.SMTP(host, port) as server:
            server.starttls()
            server.login(user, pw)
            server.sendmail(frm, to, msg.as_string())
        print(f"Email sent to {', '.join(to)}")
    except Exception as e:
        print(f"Email send failed (continuing): {e}")


def conditions_line(cur):
    return (f"Wind {round(cur['wind'])} kn {compass(cur['dir'])}, "
            f"gusts {round(cur['gust'])} kn, "
            f"{wx_text(cur['weatherCode'])}, "
            f"rain {cur['precip']:.2f} in/hr"
            + (f", waves {cur['waveFt']:.1f} ft" if cur['waveFt'] is not None else ""))


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"paddle": "GO", "sail": "GO"}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def main():
    now = datetime.now(TZ)
    # Outside operating hours: reset to baseline so a bad morning triggers a fresh alert.
    if not (OPEN_HOUR <= now.hour < CLOSE_HOUR):
        save_state({"paddle": "GO", "sail": "GO"})
        print(f"Outside operating hours ({now:%H:%M}); state reset, no alerts.")
        return

    cur, alerts = fetch_conditions()
    stop_events = ("Thunderstorm", "Tornado", "Special Marine", "Marine Warning",
                   "Hurricane", "Tropical Storm", "Gale", "Storm Warning")
    alert_stop = any(any(k in a for k in stop_events) for a in alerts)

    prev = load_state()
    new_state = {}
    ts = now.strftime("%-I:%M %p")

    def alert_level(lvl):
        return lvl == "STOP" or (ALERT_ON_CAUTION and lvl == "CAUTION")

    for act in ("paddle", "sail"):
        level, reasons = evaluate(act, cur, alert_stop)
        new_state[act] = level
        was, name = prev.get(act, "GO"), LABELS[act]
        rlist = [r for _, r in reasons if ORDER[_] >= ORDER["STOP"]] or [r for _, r in reasons]

        # transition INTO an alert level
        if alert_level(level) and not alert_level(was):
            why = "; ".join(rlist[:3])
            send_sms(f"⚠️ WFC HALT — {name}. {why}. ({ts}) {conditions_line(cur)}")
            send_email(
                f"⚠️ WFC HALT — {name}",
                f"Halt {name} at West Harbor as of {ts}.\n\n"
                f"Reasons:\n" + "\n".join(f"  • {r}" for r in rlist) +
                f"\n\nConditions: {conditions_line(cur)}\n"
                + (f"NWS alerts: {', '.join(set(alerts))}\n" if alerts else "")
                + "\nThis is forecast/observation-based. Confirm lightning by eye/ear (30-30 rule)."
            )
            print(f"ALERT: {name} -> {level}")
        # transition back to all-clear
        elif not alert_level(level) and alert_level(was):
            send_sms(f"✅ WFC CLEAR — {name} OK. ({ts}) {conditions_line(cur)}")
            send_email(f"✅ WFC CLEAR — {name}",
                       f"{name} cleared to operate as of {ts}.\n\nConditions: {conditions_line(cur)}")
            print(f"ALL CLEAR: {name} -> {level}")
        else:
            print(f"{name}: {was} -> {level} (no change in alert state)")

    save_state(new_state)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
