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

# Also alert when status enters CAUTION (not just HALT)? True = yes, you'll get
# a heads-up at CAUTION and a second alert if it escalates to HALT.
ALERT_ON_CAUTION = True

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
# Fetch  (multi-model: HRRR primary, ICON + ECMWF for agreement / fallback)
# ----------------------------------------------------------------------------
# Open-Meteo model IDs. HRRR (ncep_hrrr_conus) is the high-res US short-range
# model — best for the harbor inside ~2 days. ICON + ECMWF are the cross-check
# and the fallback if HRRR has a coverage/run gap.
MODELS = {"HRRR": "ncep_hrrr_conus", "ICON": "icon_seamless", "ECMWF": "ecmwf_ifs025"}
CUR_FIELDS = ("temperature_2m,precipitation,weather_code,wind_speed_10m,"
              "wind_direction_10m,wind_gusts_10m,cape,visibility")
SRC_LABEL = {"HRRR": "HRRR 3 km", "ICON": "ICON", "ECMWF": "ECMWF 9 km",
             "best-match": "global best-match (HRRR unavailable)"}


def _pick(d, base):
    """Return d[base], tolerating model-suffixed keys
    (e.g. wind_speed_10m_ncep_hrrr_conus) that appear when models= is used."""
    if d.get(base) is not None:
        return d[base]
    for k, v in d.items():
        if (k == base or k.startswith(base + "_")) and v is not None:
            return v
    return None


def _forecast_url(model_id=None):
    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={LAT}&longitude={LON}&current={CUR_FIELDS}"
           "&wind_speed_unit=kn&temperature_unit=fahrenheit&precipitation_unit=inch"
           "&timezone=America%2FNew_York")
    return url + (f"&models={model_id}" if model_id else "")


def _parse_current(c):
    vis = _pick(c, "visibility")
    return {
        "temp": _pick(c, "temperature_2m"),
        "precip": _pick(c, "precipitation"),
        "weatherCode": _pick(c, "weather_code"),
        "wind": _pick(c, "wind_speed_10m"),
        "dir": _pick(c, "wind_direction_10m"),
        "gust": _pick(c, "wind_gusts_10m"),
        "cape": _pick(c, "cape"),
        "visMi": vis / 1609.34 if vis is not None else None,
    }


def _fetch_model(model_id):
    try:
        c = requests.get(_forecast_url(model_id), timeout=20).json().get("current", {}) or {}
        return _parse_current(c)
    except Exception as e:
        print(f"model {model_id} fetch failed: {e}")
        return None


def assess_agreement(model_winds):
    """Confidence based on how closely the models agree on sustained wind."""
    if len(model_winds) < 2:
        return {"level": "SINGLE-SOURCE", "spread": None, "winds": model_winds}
    spread = max(model_winds.values()) - min(model_winds.values())
    level = "HIGH" if spread <= 3 else "MODERATE" if spread <= 6 else "LOW"
    return {"level": level, "spread": spread, "winds": model_winds}


def agreement_line(ag):
    if ag["level"] == "SINGLE-SOURCE":
        return "Model confidence: single-source (cross-check unavailable)"
    parts = ", ".join(f"{n} {w}" for n, w in ag["winds"].items())
    return f"Model agreement: {ag['level']} (spread {ag['spread']} kn — {parts})"


# ----------------------------------------------------------------------------
# Real-time observations (NDBC). These are MEASURED conditions, not forecasts.
# 44040 (WLIS buoy, ~5 nm N in the Sound) is closest but intermittent;
# KPTN6 (Kings Point NOAA station, ~11 mi WSW) is the reliable fallback.
# ----------------------------------------------------------------------------
OBS_STATIONS = [("44040", "WLIS buoy (5 nm N)"), ("KPTN6", "Kings Point (11 mi WSW)")]
OBS_MAX_AGE_MIN = 90          # ignore observations older than this
MS_TO_KN = 1.94384


def fetch_observations():
    """Return newest usable observation: {wind, gust, dir, station, age_min} in kn, or None."""
    from datetime import timezone
    for sid, label in OBS_STATIONS:
        try:
            txt = requests.get(f"https://www.ndbc.noaa.gov/data/realtime2/{sid}.txt",
                               timeout=20).text
            lines = [l for l in txt.splitlines() if l.strip() and not l.startswith("#")]
            if not lines:
                continue
            # Standard met format: YY MM DD hh mm WDIR WSPD GST ... (UTC, m/s)
            p = lines[0].split()
            obs_time = datetime(int(p[0]), int(p[1]), int(p[2]), int(p[3]), int(p[4]),
                                tzinfo=timezone.utc)
            age_min = (datetime.now(timezone.utc) - obs_time).total_seconds() / 60
            if age_min > OBS_MAX_AGE_MIN:
                print(f"obs {sid}: stale ({age_min:.0f} min old), skipping")
                continue

            def num(v):
                return None if v in ("MM", "") else float(v)

            wdir, wspd, gust = num(p[5]), num(p[6]), num(p[7])
            if wspd is None:
                print(f"obs {sid}: wind missing, skipping")
                continue
            return {
                "wind": wspd * MS_TO_KN,
                "gust": gust * MS_TO_KN if gust is not None else None,
                "dir": wdir,
                "station": label,
                "age_min": round(age_min),
            }
        except Exception as e:
            print(f"obs {sid} fetch failed: {e}")
    return None


def obs_line(obs):
    if not obs:
        return "Observed: unavailable (no reporting station)"
    g = f" g{round(obs['gust'])}" if obs["gust"] is not None else ""
    return (f"Observed at {obs['station']}: {round(obs['wind'])} kn"
            f"{g} {compass(obs['dir'])} ({obs['age_min']} min ago)")


# Observation stations sit in more exposed water than the sheltered harbor, so
# observed winds are judged against thresholds raised by this many knots.
OBS_EXPOSURE_OFFSET_KN = 2

_WIND_KEYS = ("windCaution", "windStop", "gustCaution", "gustStop",
              "offWindCaution", "offWindStop")


def _offset_thresholds(offset):
    th = {k: (dict(v) if isinstance(v, dict) else v) for k, v in THRESHOLDS.items()}
    for act in ("paddle", "sail"):
        for k in _WIND_KEYS:
            if k in th[act]:
                th[act][k] = th[act][k] + offset
    return th


def evaluate_observed(activity, obs):
    """Run MEASURED wind through thresholds raised by the exposure offset;
    reasons tagged with station. Reported values stay the true measured ones."""
    if not obs:
        return "GO", []
    cur = {"storm": False, "precip": None, "visMi": None, "cape": None,
           "wind": obs["wind"], "dir": obs["dir"], "gust": obs["gust"]}
    level, reasons = evaluate(activity, cur, False,
                              thresholds=_offset_thresholds(OBS_EXPOSURE_OFFSET_KN))
    tagged = [(l, f"{r} — MEASURED at {obs['station']} "
                  f"(judged vs +{OBS_EXPOSURE_OFFSET_KN} kn exposure-adjusted limits)")
              for l, r in reasons]
    return level, tagged


def fetch_conditions():
    # Pull each model independently so one failing can't break the others
    runs = {name: _fetch_model(mid) for name, mid in MODELS.items()}

    # Primary = HRRR; fall back through ICON, ECMWF, then global best-match
    primary, source = None, None
    for name in ("HRRR", "ICON", "ECMWF"):
        r = runs.get(name)
        if r and r["wind"] is not None:
            primary, source = r, name
            break
    if primary is None:
        try:
            c = requests.get(_forecast_url(), timeout=20).json().get("current", {})
            primary, source = _parse_current(c), "best-match"
        except Exception as e:
            raise RuntimeError(f"All weather-model fetches failed: {e}")

    # Marine (waves / SST) and NWS alerts
    try:
        mar = ("https://marine-api.open-meteo.com/v1/marine"
               f"?latitude={LAT}&longitude={LON}"
               "&current=wave_height,sea_surface_temperature&timezone=America%2FNew_York")
        m = requests.get(mar, timeout=20).json().get("current", {}) or {}
    except Exception:
        m = {}

    alerts = []
    try:
        nws = f"https://api.weather.gov/alerts/active?point={LAT},{LON}"
        a = requests.get(nws, timeout=20,
                         headers={"User-Agent": "WFC-weather-monitor (ops@thewaterfrontcenter.org)"}).json()
        alerts = [feat["properties"]["event"] for feat in a.get("features", [])
                  if feat.get("properties", {}).get("event")]
    except Exception:
        pass

    wave = m.get("wave_height")
    sst = m.get("sea_surface_temperature")
    cur = {
        **primary,
        "storm": is_storm(primary["weatherCode"]),
        "waveFt": wave * 3.281 if wave is not None else None,
        "sst": sst * 9 / 5 + 32 if sst is not None else None,
        "source": source,
    }

    model_winds = {n: round(r["wind"]) for n, r in runs.items() if r and r["wind"] is not None}
    return cur, alerts, assess_agreement(model_winds)


# ----------------------------------------------------------------------------
# Decision engine (mirrors dashboard evalActivity)
# ----------------------------------------------------------------------------
def evaluate(activity, cur, alert_stop, thresholds=None, alert_caution=False):
    reasons, level = [], "GO"
    th = thresholds or THRESHOLDS

    def bump(lvl, reason):
        nonlocal level
        reasons.append((lvl, reason))
        if ORDER[lvl] > ORDER[level]:
            level = lvl

    s = th["shared"]
    if cur["storm"]:
        bump("STOP", "Thunderstorm overhead — clear the water")
    if alert_stop:
        bump("STOP", "Active NWS marine/storm warning")
    if alert_caution:
        bump("CAUTION", "Severe thunderstorm watch/warning in effect — monitor radar closely")
    if cur["precip"] is not None and cur["precip"] >= s["rainStop"]:
        bump("STOP", f"Heavy rain ({cur['precip']:.2f} in/hr)")
    elif cur["precip"] is not None and cur["precip"] >= s["rainCaution"]:
        bump("CAUTION", f"Rain ({cur['precip']:.2f} in/hr)")
    if cur["visMi"] is not None and cur["visMi"] < s["visStopMi"]:
        bump("STOP", f"Low visibility ({cur['visMi']:.1f} mi)")
    if cur["cape"] is not None and cur["cape"] >= s["capeWatch"]:
        bump("CAUTION", f"Unstable air (CAPE {round(cur['cape'])}) — storms possible")

    t = th[activity]
    if activity == "paddle" and in_arc(cur["dir"], th["offshoreArc"]["from"], th["offshoreArc"]["to"]):
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

    cur, alerts, agreement = fetch_conditions()
    obs = fetch_observations()
    # NWS alert routing: thunderstorm watches/warnings -> CAUTION (your eyes + radar
    # make the call); the rest remain hard HALTs. A storm actually overhead
    # (weather code / observation) still forces HALT separately.
    stop_events = ("Tornado", "Special Marine", "Marine Warning",
                   "Hurricane", "Tropical Storm", "Gale", "Storm Warning")
    caution_events = ("Thunderstorm",)
    alert_stop = any(any(k in a for k in stop_events) for a in alerts)
    alert_caution = (not alert_stop and
                     any(any(k in a for k in caution_events) for a in alerts))
    src = SRC_LABEL.get(cur.get("source"), cur.get("source"))
    print(f"Primary model: {src} | {agreement_line(agreement)}")
    print(obs_line(obs))

    prev = load_state()
    new_state = {}
    ts = now.strftime("%-I:%M %p")

    def is_alert(lvl):
        return lvl == "STOP" or (ALERT_ON_CAUTION and lvl == "CAUTION")

    for act in ("paddle", "sail"):
        f_level, f_reasons = evaluate(act, cur, alert_stop, alert_caution=alert_caution)
        o_level, o_reasons = evaluate_observed(act, obs)
        # Worst case wins: a measured exceedance forces the alert even if models say calm
        level = f_level if ORDER[f_level] >= ORDER[o_level] else o_level
        reasons = f_reasons + o_reasons
        new_state[act] = level
        was, name = prev.get(act, "GO"), LABELS[act]
        # reasons at the current (top) severity, so a CAUTION note lists caution reasons
        rlist = [r for l, r in reasons if ORDER[l] == ORDER[level]] or [r for _, r in reasons]

        # Notify when entering an alert level OR escalating to a higher one
        # (GO->CAUTION, GO->STOP, CAUTION->STOP). All-clear when fully back to GO.
        if is_alert(level) and (not is_alert(was) or ORDER[level] > ORDER[was]):
            if level == "STOP":
                tag, lead = "⚠️ WFC HALT", f"Halt {name} at West Harbor as of {ts}."
            else:
                tag, lead = "🟡 WFC CAUTION", (f"Use caution for {name} at West Harbor as of {ts} "
                                              "— conditions are approaching limits.")
            why = "; ".join(rlist[:3])
            conf = agreement["level"]
            send_sms(f"{tag} — {name}. {why}. ({ts}) {conditions_line(cur)} [{conf} conf]")
            send_email(
                f"{tag} — {name}",
                f"{lead}\n\nReasons:\n" + "\n".join(f"  • {r}" for r in rlist) +
                f"\n\nForecast: {conditions_line(cur)}\n"
                f"{obs_line(obs)}\n"
                f"Primary model: {src}\n"
                f"{agreement_line(agreement)}\n"
                + (f"NWS alerts: {', '.join(set(alerts))}\n" if alerts else "")
                + "\nThis is forecast/observation-based. Confirm lightning by eye/ear (30-30 rule)."
            )
            print(f"ALERT: {name} {was} -> {level}")
        # transition back to fully clear
        elif not is_alert(level) and is_alert(was):
            send_sms(f"✅ WFC CLEAR — {name} OK. ({ts}) {conditions_line(cur)}")
            send_email(f"✅ WFC CLEAR — {name}",
                       f"{name} cleared to operate as of {ts}.\n\nConditions: {conditions_line(cur)}")
            print(f"ALL CLEAR: {name} {was} -> {level}")
        else:
            print(f"{name}: {was} -> {level} (no change in alert state)")

    save_state(new_state)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
