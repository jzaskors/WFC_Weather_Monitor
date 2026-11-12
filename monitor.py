#!/usr/bin/env python3
"""
WFC West Harbor weather monitor.

Checks live conditions every ~10 minutes and emails (and texts, if Twilio is
set up) when paddle rentals or sailing programs should HALT or use CAUTION,
and again when they clear.

How it decides
  The rules live in wxcore/ and the WFC settings live in sites/wfc.json:
  limits, offshore arc, hours, stations, and NWS routing. Edit limits there,
  never in this file. This file only fetches data, sends messages, and
  remembers state between runs.

What it does when things go wrong
  - A source is down: falls back to the next source and says so in the alert.
  - No wind data at all: holds the last call. It never sends an all-clear
    because data went missing.
  - An alert fails to send: doesn't record it as sent, so the next run
    tries again.
  - A source stays down for several runs: one "monitor degraded" email, then
    one "back to normal" email when it recovers.
  - The monitor stops running entirely: the optional dead-man's switch
    (HEALTHCHECK_URL) emails you when the pings stop.

State is kept in state.json between runs (committed back by the workflow).
"""

from __future__ import annotations

import json
import os
import smtplib
import sys
import time
import traceback
from datetime import datetime, timezone
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from wxcore import (Conditions, Observation, LEVEL_WORD, ORDER, advance, assess,
                    in_operating_hours, load_site)

ROOT = Path(__file__).resolve().parent
SITE = load_site(os.environ.get("SITE_FILE") or ROOT / "sites" / "wfc.json")
TZ = ZoneInfo(SITE.timezone)
STATE_FILE = ROOT / "state.json"
STATUS_FILE = ROOT / "status.json"
STATE_VERSION = 2

USER_AGENT = "WFC-weather-monitor (ops@thewaterfrontcenter.org)"
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": USER_AGENT})
HTTP_ATTEMPTS = 2          # one retry per request
RETRY_DELAY_S = 3
# Whole-run budget for fetching. Once spent, no retries and short timeouts, so
# a total outage finishes well inside the workflow's time limit and state is
# always saved.
FETCH_BUDGET_S = 180
_run_started = time.monotonic()


def budget_left() -> float:
    return FETCH_BUDGET_S - (time.monotonic() - _run_started)
SEND_ATTEMPTS = 2
# DRY_RUN=1: run every real check and print what WOULD be sent, but send
# nothing, ping nothing, and leave state.json untouched. Safe to run any time.
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")

# Sagamore YC WeatherLink fetcher (sagamore.py, same folder, lowercase name).
try:
    from sagamore import fetch_sagamore, SagamoreUnavailable
except Exception as _e:
    print(f"!!! sagamore module unavailable ({_e}); Sagamore will be skipped. "
          f"The file must be named lowercase 'sagamore.py' — the runner is case-sensitive.")
    fetch_sagamore, SagamoreUnavailable = None, Exception

# WeatherFlow Tempest fetcher (tempest.py, same folder, lowercase name).
try:
    from tempest import fetch_tempest, TempestUnavailable
except Exception as _e:
    print(f"!!! tempest module unavailable ({_e}); Tempest stations will be skipped. "
          f"The file must be named lowercase 'tempest.py'.")
    fetch_tempest, TempestUnavailable = None, Exception

# Weather Underground station fetcher (wunderground.py, same folder, lowercase name).
try:
    from wunderground import fetch_wu, WUUnavailable
except Exception as _e:
    print(f"!!! wunderground module unavailable ({_e}); Weather Underground stations will be "
          f"skipped. The file must be named lowercase 'wunderground.py'.")
    fetch_wu, WUUnavailable = None, Exception

# Conditions graphic (render_conditions.py + Pillow). Optional: alerts still
# go out as HTML + text without it.
try:
    from render_conditions import render_conditions_png
except Exception as _e:
    print(f"render_conditions unavailable ({_e}); alerts will have no graphic")
    render_conditions_png = None


def now_local() -> datetime:
    return datetime.now(TZ)


def log(msg: str) -> None:
    print(msg, flush=True)


# ============================================================================
# Fetching
# ============================================================================
class SourceError(Exception):
    """A source could not supply usable data; the message says why."""


def http_get(url, *, params=None, timeout=20, headers=None):
    """GET with one retry. Raises SourceError with a readable reason."""
    last = None
    for attempt in range(1, HTTP_ATTEMPTS + 1):
        left = budget_left()
        if left <= 0 and attempt > 1:
            break
        try:
            r = HTTP.get(url, params=params, timeout=max(3, min(timeout, left)), headers=headers)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = e
            if attempt < HTTP_ATTEMPTS and budget_left() > RETRY_DELAY_S + 3:
                time.sleep(RETRY_DELAY_S)
            else:
                break
    raise SourceError(f"{type(last).__name__}: {last}")


def http_json(url, **kw):
    r = http_get(url, **kw)
    try:
        return r.json()
    except ValueError:
        raise SourceError(f"not JSON (HTTP {r.status_code}): {r.text[:100]!r}")


# --- Open-Meteo forecast models ---------------------------------------------
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
CUR_FIELDS = ("temperature_2m,precipitation,weather_code,wind_speed_10m,"
              "wind_direction_10m,wind_gusts_10m,cape,visibility")


def _pick(d, base):
    """d[base], tolerating model-suffixed keys (wind_speed_10m_ncep_hrrr_conus)."""
    if d.get(base) is not None:
        return d[base]
    for k, v in d.items():
        if k.startswith(base + "_") and v is not None:
            return v
    return None


def _forecast_params(model_id=None):
    p = {"latitude": SITE.lat, "longitude": SITE.lon, "current": CUR_FIELDS,
         "wind_speed_unit": "kn", "temperature_unit": "fahrenheit",
         "precipitation_unit": "inch", "timezone": SITE.timezone}
    if model_id:
        p["models"] = model_id
    return p


def parse_current(c: dict) -> dict:
    """Open-Meteo 'current' block -> plain values.

    Open-Meteo's current precipitation is the TOTAL over the preceding
    interval (15 min, given in seconds as `interval`), not an hourly rate.
    The rain limits are inches per HOUR, so convert. Before Nov 2026 this was
    compared raw, which made rain read about 4x lighter than it was.
    """
    vis = _pick(c, "visibility")
    precip = _pick(c, "precipitation")
    interval_s = c.get("interval") or 900
    return {
        "temp_f": _pick(c, "temperature_2m"),
        "precip_in_hr": None if precip is None else precip * 3600 / interval_s,
        "weather_code": _pick(c, "weather_code"),
        "wind": _pick(c, "wind_speed_10m"),
        "dir": _pick(c, "wind_direction_10m"),
        "gust": _pick(c, "wind_gusts_10m"),
        "cape": _pick(c, "cape"),
        "vis_mi": None if vis is None else vis / 1609.34,
    }


def fetch_forecast() -> dict:
    """Primary model plus cross-check. Never raises; 'primary' is None if
    every model failed."""
    runs, errors = {}, []
    for m in SITE.forecast_models:
        try:
            runs[m.name] = parse_current(http_json(OPEN_METEO, params=_forecast_params(m.open_meteo_id))
                                         .get("current") or {})
        except SourceError as e:
            errors.append(f"{m.name}: {e}")
            log(f"forecast model {m.name} failed: {e}")

    primary, source = None, None
    for m in SITE.forecast_models:                 # site file order = priority
        r = runs.get(m.name)
        if r and r["wind"] is not None:
            primary, source = r, m.label
            break
    if primary is None:
        try:
            r = parse_current(http_json(OPEN_METEO, params=_forecast_params()).get("current") or {})
            if r["wind"] is not None:
                primary, source = r, "global best-match (preferred models unavailable)"
        except SourceError as e:
            errors.append(f"best-match: {e}")

    winds = {n: round(r["wind"]) for n, r in runs.items() if r and r["wind"] is not None}
    return {"primary": primary, "source": source, "agreement": assess_agreement(winds),
            "errors": errors}


def assess_agreement(model_winds: dict) -> dict:
    if len(model_winds) < 2:
        return {"level": "SINGLE-SOURCE", "spread": None, "winds": model_winds}
    spread = max(model_winds.values()) - min(model_winds.values())
    level = "HIGH" if spread <= 3 else "MODERATE" if spread <= 6 else "LOW"
    return {"level": level, "spread": spread, "winds": model_winds}


def fetch_marine() -> dict:
    """Waves / water temp — display only, never decides a call."""
    try:
        m = http_json("https://marine-api.open-meteo.com/v1/marine",
                      params={"latitude": SITE.lat, "longitude": SITE.lon,
                              "current": "wave_height,sea_surface_temperature",
                              "timezone": SITE.timezone}).get("current") or {}
    except SourceError as e:
        log(f"marine fetch failed (display only): {e}")
        return {}
    wave, sst = m.get("wave_height"), m.get("sea_surface_temperature")
    return {"wave_ft": None if wave is None else wave * 3.281,
            "sst_f": None if sst is None else sst * 9 / 5 + 32}


# --- NWS alerts ----------------------------------------------------------------
def fetch_nws() -> dict:
    """Active NWS alerts for the site's point AND its marine zone(s).

    Marine products (Small Craft Advisory, Gale Warning, Special Marine
    Warning) are issued for water zones, so a point query at a dock on land
    can miss them. The zone query makes sure they're seen. ok is False if
    ANY query failed — the alert says what couldn't be checked, and no
    all-clear goes out until every query answers.
    """
    queries = [("point", {"point": f"{SITE.lat},{SITE.lon}"})]
    queries += [(f"zone {z}", {"zone": z}) for z in SITE.nws_marine_zones]
    names, failed = set(), []
    for label, params in queries:
        try:
            data = http_json("https://api.weather.gov/alerts/active", params=params,
                             headers={"Accept": "application/geo+json"})
            for feat in data.get("features", []):
                ev = (feat.get("properties") or {}).get("event")
                if ev:
                    names.add(ev)
        except SourceError as e:
            failed.append(label)
            log(f"NWS {label} query failed: {e}")
    # ok only if EVERY query answered: a missing zone query means marine
    # warnings went unchecked, which must block any all-clear.
    return {"names": sorted(names), "ok": not failed, "failed": failed}


# --- Live observations ---------------------------------------------------------
MS_TO_KN = 1.94384


def _obs_weatherlink_embed(src) -> Observation:
    if fetch_sagamore is None:
        raise SourceError("sagamore.py not importable")
    try:
        s = fetch_sagamore(session=HTTP)
    except SagamoreUnavailable as e:
        raise SourceError(str(e))
    except Exception as e:
        raise SourceError(f"{type(e).__name__}: {e}")
    if not isinstance(s, dict) or s.get("wind_kn") is None:
        raise SourceError(f"unusable data: {s!r}"[:200])
    age = (time.time() - s["observed_epoch"]) / 60 if s.get("observed_epoch") else 0
    if age > src.max_age_min:
        raise SourceError(f"stale ({age:.0f} min old)")
    return Observation(wind_kn=s["wind_kn"], gust_kn=s.get("gust_kn"), dir_deg=s.get("wind_dir_deg"),
                       station=src.label, age_min=max(age, 0),
                       exposure_offset_kn=src.exposure_offset_kn,
                       gust_monitored=s.get("gust_monitored", s.get("gust_kn") is not None),
                       peak_gust_kn=s.get("peak_gust_kn"))


def _obs_ndbc(src) -> Observation:
    sid = src.params["station"]
    txt = http_get(f"https://www.ndbc.noaa.gov/data/realtime2/{sid}.txt").text
    rows = [l.split() for l in txt.splitlines() if l.strip() and not l.startswith("#")]
    if not rows or len(rows[0]) < 8:
        raise SourceError("no data rows")
    p = rows[0]                                    # newest first
    try:
        t = datetime(int(p[0]), int(p[1]), int(p[2]), int(p[3]), int(p[4]), tzinfo=timezone.utc)
    except ValueError:
        raise SourceError(f"bad timestamp {p[:5]}")
    age = (datetime.now(timezone.utc) - t).total_seconds() / 60
    if age > src.max_age_min:
        raise SourceError(f"stale ({age:.0f} min old)")

    def num(v):
        return None if v in ("MM", "") else float(v)

    wdir, wspd, gst = num(p[5]), num(p[6]), num(p[7])
    if wspd is None:
        raise SourceError("wind missing")
    return Observation(wind_kn=wspd * MS_TO_KN, gust_kn=None if gst is None else gst * MS_TO_KN,
                       dir_deg=wdir, station=src.label, age_min=max(age, 0),
                       exposure_offset_kn=src.exposure_offset_kn)


def _obs_tempest(src) -> Observation:
    if fetch_tempest is None:
        raise SourceError("tempest.py not importable")
    sid = src.params.get("station_id")
    if not sid:
        raise SourceError("no station_id set in the site file")
    try:
        t = fetch_tempest(sid, auth=src.params.get("auth", "public"), session=HTTP)
    except TempestUnavailable as e:
        raise SourceError(str(e))
    except Exception as e:
        raise SourceError(f"{type(e).__name__}: {e}")
    age = (time.time() - t["observed_epoch"]) / 60
    if age > src.max_age_min:
        raise SourceError(f"stale ({age:.0f} min old)")
    return Observation(wind_kn=t["wind_kn"], gust_kn=t["gust_kn"], dir_deg=t["wind_dir_deg"],
                       station=src.label, age_min=max(age, 0),
                       exposure_offset_kn=src.exposure_offset_kn,
                       gust_monitored=t["gust_monitored"])


def _obs_wunderground(src) -> Observation:
    if fetch_wu is None:
        raise SourceError("wunderground.py not importable")
    sid = src.params.get("station")
    if not sid:
        raise SourceError("no station set in the site file")
    try:
        w = fetch_wu(sid, session=HTTP)
    except WUUnavailable as e:
        raise SourceError(str(e))
    except Exception as e:
        raise SourceError(type(e).__name__)      # no message: it could hold the request URL
    age = (time.time() - w["observed_epoch"]) / 60
    if age > src.max_age_min:
        raise SourceError(f"stale ({age:.0f} min old)")
    return Observation(wind_kn=w["wind_kn"], gust_kn=w["gust_kn"], dir_deg=w["wind_dir_deg"],
                       station=src.label, age_min=max(age, 0),
                       exposure_offset_kn=src.exposure_offset_kn,
                       gust_monitored=w["gust_monitored"])


OBS_FETCHERS = {"weatherlink_embed": _obs_weatherlink_embed, "ndbc": _obs_ndbc,
                "tempest": _obs_tempest, "wunderground": _obs_wunderground}


def fetch_observation() -> tuple:
    """(best Observation or None, [(label, why it was skipped), ...])
    in the site file's priority order."""
    skipped = []
    for src in SITE.observation_sources:
        fn = OBS_FETCHERS.get(src.type)
        if fn is None:
            skipped.append((src.label, f"unsupported source type '{src.type}'"))
            continue
        try:
            obs = fn(src)
            if skipped:
                log(f"observation from backup {src.label}; skipped: {skipped}")
            return obs, skipped
        except (SourceError, KeyError, ValueError) as e:
            skipped.append((src.label, str(e)))
            log(f"obs {src.label} unavailable: {e}")
    return None, skipped


# ============================================================================
# Text formatting (every value may be missing)
# ============================================================================
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def compass(d):
    return "—" if d is None else COMPASS[round((d % 360) / 22.5) % 16]


def kn(v):
    return "—" if v is None else f"{round(v)}"


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


def conditions_line(fc, marine):
    if not fc:
        return "Forecast unavailable"
    rain = "—" if fc["precip_in_hr"] is None else f"{fc['precip_in_hr']:.2f} in/hr"
    s = (f"Wind {kn(fc['wind'])} kn {compass(fc['dir'])}, gusts {kn(fc['gust'])} kn, "
         f"{wx_text(fc['weather_code'])}, rain {rain}")
    if marine.get("wave_ft") is not None:
        s += f", waves {marine['wave_ft']:.1f} ft"
    return s


def obs_line(obs):
    if obs is None:
        return "Observed: unavailable (no station reporting)"
    if obs.gust_kn is not None:
        g = f" g{round(obs.gust_kn)}"
    elif not obs.gust_monitored:
        g = " (gust not reported by station"
        g += f"; peak today {round(obs.peak_gust_kn)} kn)" if obs.peak_gust_kn is not None else ")"
    else:
        g = ""
    return (f"Observed at {obs.station}: {kn(obs.wind_kn)} kn{g} {compass(obs.dir_deg)} "
            f"({round(obs.age_min)} min ago)")


def agreement_line(ag):
    if ag["level"] == "SINGLE-SOURCE":
        return "Model confidence: single-source (cross-check unavailable)"
    parts = ", ".join(f"{n} {w}" for n, w in ag["winds"].items())
    return f"Model agreement: {ag['level']} (spread {ag['spread']} kn — {parts})"


def data_gap_notes(fc, obs, skipped, nws) -> list:
    """Plain-language list of anything missing, so a quiet email never reads
    as 'checked and fine' when it wasn't checked."""
    notes = []
    if fc["primary"] is None:
        notes.append("Forecast models unavailable — storm, rain, visibility and instability "
                     "were NOT checked this run")
    if obs is None:
        notes.append("No live station reporting — call is forecast-only")
    elif skipped:
        notes.append(f"Using backup station {obs.station} "
                     f"({skipped[0][0]} unavailable: {skipped[0][1]})")
    if not nws["ok"]:
        notes.append(f"NWS alerts could not be checked this run ({', '.join(nws['failed'])})")
    return notes


# ============================================================================
# Sending
# ============================================================================
def _recipients(var):
    return [x.strip() for x in os.environ.get(var, "").split(",") if x.strip()]


def send_sms(body):
    """True sent, False failed, None not configured."""
    sid, token, frm = (os.environ.get(k) for k in ("TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM"))
    to = _recipients("ALERT_SMS_TO")
    if not (sid and token and frm and to):
        return None
    try:
        from twilio.rest import Client
        client = Client(sid, token)
    except Exception as e:
        log(f"SMS setup failed: {e}")
        return False
    ok = 0
    for num in to:
        try:
            client.messages.create(body=body[:1500], from_=frm, to=num)
            ok += 1
        except Exception as e:
            log(f"SMS to {num} failed: {e}")
    log(f"SMS sent to {ok} of {len(to)} number(s)")
    # Any number reached counts as delivered, so a retry never re-texts the
    # people who already got it.
    return ok > 0


DEFAULT_FROM_NAME = "WFC Weather Monitor Alert System"


def send_email(subject, text_body, html_body=None, image_png=None):
    """True sent, False failed after retry, None not configured."""
    host, user, pw = (os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS"))
    try:
        port = int(os.environ.get("SMTP_PORT") or 587)
    except ValueError:
        log(f"SMTP_PORT secret is not a number ({os.environ.get('SMTP_PORT')!r}); using 587")
        port = 587
    # EMAIL_FROM may be a bare address or "Name <address>". EMAIL_FROM_NAME,
    # if set, is the display name inboxes show. The bare address is what the
    # mail server sends from, so it must be a sender verified in Brevo.
    name_in_from, frm = parseaddr(os.environ.get("EMAIL_FROM") or user)
    frm = frm or user
    display = (os.environ.get("EMAIL_FROM_NAME") or name_in_from or DEFAULT_FROM_NAME).strip()
    reply_to = (os.environ.get("EMAIL_REPLY_TO") or "").strip()
    to = _recipients("EMAIL_TO")
    if not (host and user and pw and to):
        log("Email not configured, skipping.")
        return None

    if html_body:
        msg = MIMEMultipart("related")
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(text_body, "plain", "utf-8"))
        alt.attach(MIMEText(html_body, "html", "utf-8"))
        msg.attach(alt)
        if image_png:
            img = MIMEImage(image_png, _subtype="png")
            img.add_header("Content-ID", "<conditions>")
            img.add_header("Content-Disposition", "inline", filename="conditions.png")
            msg.attach(img)
    else:
        msg = MIMEText(text_body, "plain", "utf-8")
    msg["Subject"], msg["To"] = subject, ", ".join(to)
    msg["From"] = formataddr((display, frm))
    if reply_to:
        msg["Reply-To"] = reply_to

    for attempt in range(1, SEND_ATTEMPTS + 1):
        try:
            if port == 465:
                server = smtplib.SMTP_SSL(host, port, timeout=30)
            else:
                server = smtplib.SMTP(host, port, timeout=30)
                server.starttls()
            with server:
                server.login(user, pw)
                server.sendmail(frm, to, msg.as_string())
            log(f"Email sent: {subject}")
            return True
        except Exception as e:
            log(f"Email attempt {attempt} failed: {e}")
            if attempt < SEND_ATTEMPTS:
                time.sleep(RETRY_DELAY_S)
    return False


def deliver(subject, sms_body, text_body, html_body=None, image_png=None):
    """Send on every configured channel. True if ANY channel delivered,
    False if channels are configured but all failed, None if none configured."""
    if DRY_RUN:
        log(f"[DRY RUN] would send: {subject}\n{text_body}\n[DRY RUN] end of message")
        return True
    results = [send_sms(sms_body), send_email(subject, text_body, html_body, image_png)]
    if any(r is True for r in results):
        return True
    if any(r is False for r in results):
        return False
    log("!!! No alert channel configured — nothing was sent.")
    return None


LEVEL_HTML = {"GO": "#2fa36b", "CAUTION": "#b87400", "STOP": "#c62828"}


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fleet_table_html(rows):
    """Fleet status as a real table — readable even when images are blocked."""
    out = ['<table cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;'
           'width:100%;max-width:460px;font-family:Arial,Helvetica,sans-serif;font-size:14px;'
           'margin:8px 0 4px"><tr>'
           '<th align="left" style="padding:6px 8px;border-bottom:2px solid #ccc;font-size:11px;'
           'letter-spacing:.08em;color:#666">FLEET</th>'
           '<th align="right" style="padding:6px 8px;border-bottom:2px solid #ccc;font-size:11px;'
           'letter-spacing:.08em;color:#666">STATUS</th></tr>']
    for label, lvl, why in rows:
        color = LEVEL_HTML[lvl]
        note = f'<span style="color:#777;font-size:12px"> — {esc(why)}</span>' if why else ""
        out.append(f'<tr><td style="padding:7px 8px;border-bottom:1px solid #eee">'
                   f'<span style="display:inline-block;width:4px;height:13px;background:{color};'
                   f'vertical-align:-2px;margin-right:8px"></span>{esc(label)}{note}</td>'
                   f'<td align="right" style="padding:7px 8px;border-bottom:1px solid #eee;'
                   f'color:{color};font-weight:bold">{LEVEL_WORD[lvl]}</td></tr>')
    out.append("</table>")
    return "".join(out)


def fleet_table_text(rows):
    return "\n".join(f"  {label:<16} {LEVEL_WORD[lvl]}" + (f"  ({why})" if why else "")
                     for label, lvl, why in rows)


DISCLAIMER = ("Forecast and observation based. Confirm lightning by eye and ear and apply "
              "the 30-30 rule. This assists the call; it does not make it.")


def build_message(lead, reasons, ctx, has_image):
    """(text_body, html_body) for an alert or all-clear."""
    details = [f"Forecast: {ctx['cond_line']}", ctx["obs_line"],
               f"Primary model: {ctx['source'] or 'none'}", ctx["agreement_line"]]
    if ctx["nws_names"]:
        details.append(f"NWS alerts: {', '.join(ctx['nws_names'])}")

    text = lead
    if reasons:
        text += "\n\nReasons:\n" + "\n".join(f"  • {r}" for r in reasons)
    if ctx["fleet_rows"]:
        text += "\n\nFleet:\n" + fleet_table_text(ctx["fleet_rows"])
    if ctx["gaps"]:
        text += "\n\nData gaps:\n" + "\n".join(f"  ! {g}" for g in ctx["gaps"])
    text += "\n\n" + "\n".join(details) + "\n\n" + DISCLAIMER

    h = ['<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;color:#222;'
         'line-height:1.5;max-width:620px">', f'<p style="margin:0 0 10px"><b>{esc(lead)}</b></p>']
    if reasons:
        h.append('<ul style="margin:0 0 12px;padding-left:20px">' +
                 "".join(f"<li>{esc(r)}</li>" for r in reasons) + "</ul>")
    if has_image:
        h.append('<img src="cid:conditions" width="720" alt="Wind compass and fleet status" '
                 'style="width:100%;max-width:720px;height:auto;border:1px solid #ddd;'
                 'border-radius:6px;margin:6px 0 2px">')
    if ctx["fleet_rows"]:
        h.append(fleet_table_html(ctx["fleet_rows"]))
    if ctx["gaps"]:
        h.append('<p style="margin:12px 0 4px;padding:8px 10px;background:#fff4e5;'
                 'border-left:4px solid #b87400;font-size:13px"><b>Data gaps:</b><br>' +
                 "<br>".join(esc(g) for g in ctx["gaps"]) + "</p>")
    h.append('<p style="margin:14px 0 4px;font-size:13px;color:#555">' +
             "<br>".join(esc(d) for d in details) + "</p>")
    h.append('<p style="margin:12px 0 0;font-size:12px;color:#777;border-top:1px solid #eee;'
             f'padding-top:8px">{esc(DISCLAIMER)}</p></div>')
    return text, "".join(h)


# ============================================================================
# State
# ============================================================================
def blank_state():
    return {"version": STATE_VERSION,
            "activities": {a.id: {"level": "GO", "below_runs": 0, "below_peak": "GO"}
                           for a in SITE.activities},
            "health": {"forecast": 0, "observation": 0, "nws": 0, "degraded_notified": False}}


def _count(v) -> int:
    """A non-negative whole number from state.json, or 0 if it's anything else."""
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def load_state():
    """Read state.json, upgrading the original {"paddle": "GO", ...} format.
    A missing or corrupt file starts from all-GO rather than crashing."""
    st = blank_state()
    try:
        raw = json.loads(STATE_FILE.read_text())
    except FileNotFoundError:
        return st
    except Exception as e:
        log(f"state.json unreadable ({e}); starting fresh")
        return st
    if not isinstance(raw, dict):
        return st
    if "activities" in raw:                                   # v2
        acts = raw.get("activities")
        for aid, rec in (acts.items() if isinstance(acts, dict) else ()):
            if aid in st["activities"] and isinstance(rec, dict) and rec.get("level") in ORDER:
                st["activities"][aid] = {
                    "level": rec["level"],
                    "below_runs": _count(rec.get("below_runs")),
                    "below_peak": rec["below_peak"] if rec.get("below_peak") in ORDER else "GO"}
        health = raw.get("health")
        for k, v in (health.items() if isinstance(health, dict) else ()):
            if k == "degraded_notified":
                st["health"][k] = v is True
            elif k in st["health"]:
                st["health"][k] = _count(v)
    else:                                                     # v1: {"paddle": "GO"}
        for aid, lvl in raw.items():
            if aid in st["activities"] and lvl in ORDER:
                st["activities"][aid]["level"] = lvl
    return st


def save_state(st):
    """Write atomically so a cancelled run can't leave half a file."""
    if DRY_RUN:
        log("[DRY RUN] state.json not changed")
        return
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2) + "\n")
    os.replace(tmp, STATE_FILE)


# ============================================================================
# Health: tell a human when the monitor itself can't be trusted
# ============================================================================
HEALTH_PARTS = {"forecast": "forecast models", "observation": "every live station",
                "nws": "NWS alerts"}


def update_health(st, ok: dict, ts: str):
    h = st["health"]
    for part, good in ok.items():
        h[part] = 0 if good else h.get(part, 0) + 1
    n = SITE.health_alert_after_runs
    down = [HEALTH_PARTS[p] for p in HEALTH_PARTS if h.get(p, 0) >= n]
    tag = SITE.short_name
    if down and not h["degraded_notified"]:
        what = ", ".join(down)
        body = (f"The {tag} weather monitor has been unable to reach {what} for {n}+ "
                f"consecutive runs (as of {ts}).\n\nAlerts are still running on whatever data "
                f"remains, but treat the monitor as unreliable until you get the all-clear "
                f"email. Make the call by eye.")
        if deliver(f"⚠️ {tag} monitor degraded — {what} down",
                   f"⚠️ {tag} monitor degraded: {what} down. Make the call by eye. ({ts})",
                   body):
            h["degraded_notified"] = True
    elif not down and h["degraded_notified"]:
        if deliver(f"✅ {tag} monitor back to normal",
                   f"✅ {tag} monitor back to normal ({ts})",
                   f"All weather data sources are reporting again as of {ts}."):
            h["degraded_notified"] = False
    return down


def ping_healthcheck(fail: bool, summary: str):
    """Dead-man's switch: ping HEALTHCHECK_URL (e.g. healthchecks.io) every run.
    If the pings stop — cron-job.org down, token expired, Actions broken —
    the service emails you. Never lets a ping problem break the run."""
    url = os.environ.get("HEALTHCHECK_URL")
    if not url or DRY_RUN:
        return
    try:
        HTTP.post(url.rstrip("/") + ("/fail" if fail else ""), data=summary[:9000].encode(),
                  timeout=10)
    except Exception as e:
        log(f"healthcheck ping failed (ignored): {e}")


# ============================================================================
# Main
# ============================================================================
def run() -> dict:
    """One monitor cycle. Returns a summary dict (used by tests and the log)."""
    now = now_local()
    ts = now.strftime("%-I:%M %p")
    st = load_state()

    if not in_operating_hours(SITE, now.hour) and not DRY_RUN:   # dry runs check any time
        # Closed: reset to GO so a bad morning triggers a fresh alert at opening.
        for rec in st["activities"].values():
            rec.update(level="GO", below_runs=0, below_peak="GO")
        save_state(st)
        log(f"Outside operating hours ({now:%H:%M}); state reset, no checks.")
        return {"open": False, "sent": [], "degraded": []}

    fc = fetch_forecast()
    obs, skipped = fetch_observation()
    nws = fetch_nws()
    marine = fetch_marine() if fc["primary"] else {}

    p = fc["primary"] or {}
    cond = Conditions(wind_kn=p.get("wind"), gust_kn=p.get("gust"), dir_deg=p.get("dir"),
                      precip_in_hr=p.get("precip_in_hr"), vis_mi=p.get("vis_mi"),
                      cape=p.get("cape"), storm=p.get("weather_code") in (95, 96, 99))
    snap = assess(SITE, cond, obs, nws["names"], now, forecast_source=fc["source"],
                  agreement=fc["agreement"])
    gaps = data_gap_notes(fc, obs, skipped, nws)
    snap["data_gaps"] = gaps

    ctx = {"cond_line": conditions_line(fc["primary"], marine), "obs_line": obs_line(obs),
           "source": fc["source"], "agreement_line": agreement_line(fc["agreement"]),
           "nws_names": nws["names"], "gaps": gaps,
           "fleet_rows": [(f["label"], f["level"], f["reason"]) for f in snap["fleet"]]}
    log(f"Primary model: {fc['source']} | {ctx['agreement_line']}")
    log(ctx["obs_line"])
    for g in gaps:
        log(f"DATA GAP: {g}")

    graphic = None
    if render_conditions_png and (obs is not None or cond.wind_kn is not None):
        try:
            if obs is not None:
                gw, gg, gd, gs = obs.wind_kn, obs.gust_kn, obs.dir_deg, obs.station
            else:
                gw, gg, gd = cond.wind_kn, cond.gust_kn, cond.dir_deg
                gs = f"{fc['source']} forecast (no station reporting)"
            graphic = render_conditions_png(
                gw, gg, gd, gs, ctx["fleet_rows"],
                arcs=[(a.start, a.end) for a in SITE.offshore_arcs],
                headline=f"{SITE.area_name} conditions")
        except Exception as e:
            log(f"graphic render failed (continuing without it): {e}")

    sent = []
    have_wind = obs is not None or cond.wind_kn is not None
    if not have_wind:
        log("!!! No wind data from any source — holding the last call, no changes sent.")
    else:
        # Stepping DOWN needs a complete picture: if the forecast (storm, rain,
        # visibility) or any NWS query failed, a HALT for one of those reasons
        # can't be confirmed gone, so hold instead of counting toward a clear.
        checks_complete = fc["primary"] is not None and nws["ok"]
        if not checks_complete:
            log("Checks incomplete this run — no step-down or all-clear allowed.")
        for act in snap["activities"]:
            rec = st["activities"][act["id"]]
            new_rec, action = advance(rec, act["level"], SITE, can_step_down=checks_complete)
            if action is None:
                log(f"{act['label']}: {rec['level']} -> {act['level']} "
                    f"(stored {new_rec['level']}, no notice)")
                st["activities"][act["id"]] = new_rec
                continue
            result = notify(action, act, rec["level"], new_rec["level"], ctx, ts, graphic)
            if result is False:
                log(f"!!! {act['label']}: notice failed to send — will retry next run")
                continue                                # keep old record => retry
            st["activities"][act["id"]] = new_rec
            sent.append((act["id"], action, new_rec["level"]))
            save_state(st)          # record it now, so a later crash can't re-send it

    down = update_health(st, {"forecast": fc["primary"] is not None,
                              "observation": obs is not None, "nws": nws["ok"]}, ts)
    save_state(st)
    try:
        STATUS_FILE.write_text(json.dumps(snap, indent=2) + "\n")
    except Exception as e:
        log(f"status.json write failed (ignored): {e}")
    if DRY_RUN:
        log("[DRY RUN] calls: " + ", ".join(f"{a['label']} {a['word']}" for a in snap["activities"])
            + " | fleet: " + ", ".join(f"{f['label']} {f['word']}" for f in snap["fleet"]))
    return {"open": True, "sent": sent, "degraded": down, "gaps": gaps, "snapshot": snap}


def notify(action, act, was, level, ctx, ts, graphic):
    label = act["label"]
    tag = SITE.short_name
    if action == "ease":
        subject = f"🟡 {tag} CAUTION — {label} (eased from HALT)"
        lead = (f"{label} at {SITE.area_name} eased from HALT to CAUTION as of {ts}. "
                f"Conditions are still near limits — not an all-clear.")
        reasons = act["headline_reasons"]
        sms = f"{subject}. ({ts}) {ctx['cond_line']}"
    elif action == "alert":
        if level == "STOP":
            subject = f"⚠️ {tag} HALT — {label}"
            lead = f"Halt {label} at {SITE.area_name} as of {ts}."
        else:
            subject = f"🟡 {tag} CAUTION — {label}"
            lead = (f"Use caution for {label} at {SITE.area_name} as of {ts} — conditions are "
                    f"approaching limits.")
        reasons = act["headline_reasons"]
        sms = (f"{subject}. {'; '.join(reasons[:3])}. ({ts}) {ctx['cond_line']}"
               + (" [DATA GAPS]" if ctx["gaps"] else ""))
    else:
        subject = f"✅ {tag} CLEAR — {label}"
        lead = f"{label} cleared to operate as of {ts}."
        reasons = []
        sms = f"{subject} OK. ({ts}) {ctx['cond_line']}"
    text, html = build_message(lead, reasons, ctx, graphic is not None)
    log(f"{action.upper()}: {label} {was} -> {level}")
    return deliver(subject, sms, text, html, graphic)


def main():
    try:
        summary = run()
    except Exception:
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        ping_healthcheck(True, tb)
        sys.exit(1)
    ping_healthcheck(bool(summary.get("degraded")),
                     json.dumps({k: summary.get(k) for k in ("open", "sent", "degraded", "gaps")}))


def station_report() -> int:
    """`python monitor.py --stations`: every station in the site file, side by
    side, including ones switched off. Reads only: no alerts, no state change."""
    from wxcore.config import site_from_dict
    raw = json.loads(Path(os.environ.get("SITE_FILE") or ROOT / "sites" / "wfc.json").read_text())
    off = {s["id"] for s in raw.get("observation_sources", []) if s.get("enabled", True) is False}
    for s in raw.get("observation_sources", []):
        s["enabled"] = True
    site = site_from_dict(raw)
    print(f"Station check for {site.name} at {now_local():%a %b %d %I:%M %p %Z}\n")
    print(f"{'#':>2}  {'Station':<34} {'Wind':>7} {'Gust':>7} {'From':>5} {'Age':>7}  Notes")
    ok = 0
    for i, src in enumerate(site.observation_sources, 1):
        tag = "OFF in settings. " if src.id in off else ""
        fn = OBS_FETCHERS.get(src.type)
        try:
            if fn is None:
                raise SourceError(f"unsupported source type '{src.type}'")
            o = fn(src)
        except (SourceError, KeyError, ValueError) as e:
            print(f"{i:>2}  {src.label:<34} {'--':>7} {'--':>7} {'--':>5} {'--':>7}  {tag}FAILED: {e}")
            continue
        ok += 1
        gust = f"{o.gust_kn:.0f} kn" if o.gust_kn is not None else "none"
        note = tag + (f"judged +{src.exposure_offset_kn:g} kn (exposed)" if src.exposure_offset_kn else "")
        if o.gust_kn is None and o.peak_gust_kn is not None:
            note += f" day peak {o.peak_gust_kn:.0f} kn"
        print(f"{i:>2}  {src.label:<34} {o.wind_kn:>4.0f} kn {gust:>7} {compass(o.dir_deg):>5} "
              f"{o.age_min:>4.0f} min  {note}")
    print(f"\n{ok} of {len(site.observation_sources)} stations answered. "
          f"The monitor uses the first one in this list that answers and isn't switched off.")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--stations" in sys.argv[1:]:
        sys.exit(station_report())
    main()
