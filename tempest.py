"""
tempest.py — WeatherFlow Tempest live observation source for the WFC weather monitor.

Any Tempest station can be a source: WFC's own station on the dock (61040)
and any public neighbour (e.g. the Centre Island Rd station). Each one is an
entry in sites/wfc.json -> observation_sources with "type": "tempest".

    GET https://swd.weatherflow.com/swd/rest/observations/station/<STATION_ID>

AUTH — two ways, set per source with "auth" in the site file:
    "token"   WFC's own station. Uses the TEMPEST_TOKEN repo secret, a
              Personal Access Token made at tempestwx.com -> Settings ->
              Data Authorizations. A token only works for stations on the
              account that made it.
    "public"  Someone else's public station. Uses TEMPEST_API_KEY if set,
              otherwise the key WeatherFlow's own public web map uses. That
              key is not an officially supported third-party key: if
              WeatherFlow ever rotates it, the source fails with a clear
              reason in the Actions log and the monitor falls through to the
              next station. Nothing breaks silently.

FILENAME MATTERS: lowercase `tempest.py` (GitHub's runners are case-sensitive,
same lesson as sagamore.py).

Interface contract with monitor.py:
    fetch_tempest(station_id, auth="public", session=None) -> dict
        wind_kn         float  1-min average wind, knots
        gust_kn         float  1-min peak 3-second gust, knots (LIVE, unlike Sagamore)
        wind_dir_deg    int    direction wind is FROM, degrees true
        observed_epoch  float  UTC epoch seconds of the observation
    plus display-only extras (air_temp_f, lightning counts, ...).
    Raises TempestUnavailable on any failure.

Payload notes: REST observations are always SI regardless of the station's
display units — wind in m/s, temperature in °C, pressure in mb. `obs` is a
list holding the latest 1-minute record; an offline station returns an empty
list, and status.status_code is 0 on success.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Optional

import requests

BASE = "https://swd.weatherflow.com/swd/rest/observations/station/"
# The key WeatherFlow's public web map (tempestwx.com) sends. See AUTH above.
PUBLIC_WEB_KEY = "20c70eae-e62f-4d3b-b3a4-8586e90f3ac8"

MS_TO_KN = 1.94384
MAX_FUTURE_SECONDS = 300
REQUEST_TIMEOUT_SECONDS = 15


class TempestUnavailable(Exception):
    """A Tempest station cannot supply a usable current observation."""


def _num(raw: Any) -> Optional[float]:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _auth_params(auth: str) -> dict:
    if auth == "token":
        token = os.environ.get("TEMPEST_TOKEN", "").strip()
        if not token:
            raise TempestUnavailable("auth is 'token' but the TEMPEST_TOKEN secret is not set")
        return {"token": token}
    if auth == "public":
        return {"api_key": os.environ.get("TEMPEST_API_KEY", "").strip() or PUBLIC_WEB_KEY}
    raise TempestUnavailable(f"unknown auth mode {auth!r} (use 'token' or 'public')")


def parse_tempest(payload: dict, now: Optional[float] = None) -> dict:
    """Parse a raw observations/station payload into the monitor's dict shape.
    Staleness is NOT judged here — monitor.py applies the site file's max_age_min."""
    now = time.time() if now is None else now
    if not isinstance(payload, dict):
        raise TempestUnavailable(f"payload was {type(payload).__name__}, not an object")

    status = payload.get("status") or {}
    if status.get("status_code", 0) != 0:
        raise TempestUnavailable(f"API status {status.get('status_code')}: "
                                 f"{status.get('status_message')}")
    obs_list = payload.get("obs") or []
    if not obs_list:
        raise TempestUnavailable("station returned no observations (offline?)")
    o = obs_list[0]

    ts = _num(o.get("timestamp"))
    if ts is None:
        raise TempestUnavailable("observation has no timestamp")
    if ts - now > MAX_FUTURE_SECONDS:
        raise TempestUnavailable(f"timestamp {(ts - now) / 60:.1f} min in the future — clock skew?")

    wind_ms = _num(o.get("wind_avg"))
    if wind_ms is None:
        raise TempestUnavailable("wind_avg missing (wind sensor fault?)")
    wind_kn = wind_ms * MS_TO_KN
    if not (0 <= wind_kn <= 100):
        raise TempestUnavailable(f"implausible wind speed {wind_kn:.1f} kn")

    gust_ms = _num(o.get("wind_gust"))
    gust_kn = None if gust_ms is None else gust_ms * MS_TO_KN
    if gust_kn is not None and gust_kn < wind_kn:
        gust_kn = wind_kn          # guard against a glitched record
    d = _num(o.get("wind_direction"))
    temp_c = _num(o.get("air_temperature"))

    return {
        # --- monitor contract ---
        "wind_kn": wind_kn,
        "gust_kn": gust_kn,
        "wind_dir_deg": None if d is None else int(d) % 360,
        "observed_epoch": ts,
        # --- display only ---
        "station_name": payload.get("public_name") or payload.get("station_name"),
        "gust_monitored": gust_kn is not None,
        "lull_kn": None if _num(o.get("wind_lull")) is None else _num(o.get("wind_lull")) * MS_TO_KN,
        "air_temp_f": None if temp_c is None else temp_c * 9 / 5 + 32,
        "humidity_pct": _num(o.get("relative_humidity")),
        "sea_level_pressure_mb": _num(o.get("sea_level_pressure")),
        "rain_last_hr_mm": _num(o.get("precip_accum_last_1hr")),
        "lightning_last_3hr": _num(o.get("lightning_strike_count_last_3hr")),
        "lightning_last_km": _num(o.get("lightning_strike_last_distance")),
    }


def fetch_raw(station_id, auth: str = "public",
              session: Optional[requests.Session] = None) -> dict:
    http = session or requests
    params = _auth_params(auth)
    try:
        r = http.get(f"{BASE}{int(station_id)}", params=params,
                     headers={"User-Agent": "WFC-weather-monitor/1.0 (waterfront safety monitoring)",
                              "Accept": "application/json"},
                     timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise TempestUnavailable(f"request failed: {exc}") from exc
    if r.status_code in (401, 403):
        raise TempestUnavailable(
            f"HTTP {r.status_code}: not authorized for station {station_id} "
            f"({'check TEMPEST_TOKEN belongs to the account that owns it' if auth == 'token' else 'station may be private, or the public key changed'})")
    if r.status_code == 404:
        raise TempestUnavailable(f"HTTP 404: station {station_id} not found")
    try:
        r.raise_for_status()
        return r.json()
    except requests.RequestException as exc:
        raise TempestUnavailable(f"request failed: {exc}") from exc
    except ValueError as exc:
        raise TempestUnavailable(f"response was not JSON (status {r.status_code})") from exc


# --- Public-station route -------------------------------------------------
# For a station on someone else's account the observations endpoint answers
# 404 even when the station is public. tempestwx.com's own station page reads
# current conditions from better_forecast instead, so public stations fall back
# to that. Guard: only trusted when the station reports itself online and the
# reading is fresh (monitor.py applies max_age_min).
FORECAST = "https://swd.weatherflow.com/swd/rest/better_forecast"
STATIONS = "https://swd.weatherflow.com/swd/rest/stations"


def fetch_public_raw(station_id, session: Optional[requests.Session] = None) -> dict:
    http = session or requests
    params = dict(_auth_params("public"), station_id=int(station_id),
                  units_wind="mps", units_temp="c", units_pressure="mb",
                  units_precip="mm", units_distance="km")
    try:
        r = http.get(FORECAST, params=params, timeout=REQUEST_TIMEOUT_SECONDS,
                     headers={"User-Agent": "WFC-weather-monitor/1.0 (waterfront safety monitoring)",
                              "Accept": "application/json"})
    except requests.RequestException as exc:
        raise TempestUnavailable(f"public route request failed: {exc}") from exc
    if r.status_code == 404:
        raise TempestUnavailable(f"HTTP 404 on both routes: station {station_id} not found "
                                 f"(check the number in its tempestwx.com link)")
    if r.status_code in (401, 403):
        raise TempestUnavailable(f"HTTP {r.status_code} on public route: station {station_id} "
                                 f"is not public, or the public key changed")
    try:
        r.raise_for_status()
        return r.json()
    except requests.RequestException as exc:
        raise TempestUnavailable(f"public route request failed: {exc}") from exc
    except ValueError as exc:
        raise TempestUnavailable(f"public route response was not JSON (status {r.status_code})") from exc


def parse_public(payload: dict, now: Optional[float] = None) -> dict:
    now = time.time() if now is None else now
    if not isinstance(payload, dict):
        raise TempestUnavailable("public route payload was not an object")
    status = payload.get("status") or {}
    if status.get("status_code", 0) != 0:
        raise TempestUnavailable(f"public route status {status.get('status_code')}: "
                                 f"{status.get('status_message')}")
    st = payload.get("station") or {}
    if st.get("is_station_online") is False:
        raise TempestUnavailable("station reports itself offline")
    cc = payload.get("current_conditions") or {}
    ts = _num(cc.get("time"))
    if ts is None:
        raise TempestUnavailable("public route has no current-conditions time")
    if ts - now > MAX_FUTURE_SECONDS:
        raise TempestUnavailable("public route timestamp is in the future — clock skew?")
    wind_ms = _num(cc.get("wind_avg"))
    if wind_ms is None:
        raise TempestUnavailable("public route has no wind_avg (station may lack a wind sensor)")
    wind_kn = wind_ms * MS_TO_KN
    if not (0 <= wind_kn <= 100):
        raise TempestUnavailable(f"implausible wind speed {wind_kn:.1f} kn")
    gust_ms = _num(cc.get("wind_gust"))
    gust_kn = None if gust_ms is None else max(gust_ms * MS_TO_KN, wind_kn)
    d = _num(cc.get("wind_direction"))
    temp_c = _num(cc.get("air_temperature"))
    return {
        "wind_kn": wind_kn, "gust_kn": gust_kn,
        "wind_dir_deg": None if d is None else int(d) % 360,
        "observed_epoch": ts,
        "station_name": payload.get("location_name"),
        "gust_monitored": gust_kn is not None,
        "lull_kn": None if _num(cc.get("wind_lull")) is None else _num(cc.get("wind_lull")) * MS_TO_KN,
        "air_temp_f": None if temp_c is None else temp_c * 9 / 5 + 32,
        "humidity_pct": _num(cc.get("relative_humidity")),
        "sea_level_pressure_mb": _num(cc.get("sea_level_pressure")),
        "rain_last_hr_mm": None,
        "lightning_last_3hr": _num(cc.get("lightning_strike_count_last_3hr")),
        "lightning_last_km": _num(cc.get("lightning_strike_last_distance")),
        "route": "public",
    }


def fetch_tempest(station_id, auth: str = "public",
                  session: Optional[requests.Session] = None) -> dict:
    """Fetch + parse one Tempest station (monitor.py entry point).
    Public stations try the observations endpoint, then the public route."""
    try:
        return parse_tempest(fetch_raw(station_id, auth, session))
    except TempestUnavailable as first:
        if auth != "public":
            raise
        try:
            return parse_public(fetch_public_raw(station_id, session))
        except TempestUnavailable as second:
            raise TempestUnavailable(f"{first}; then {second}") from second


# --- Lightning ------------------------------------------------------------
# The Tempest has a lightning sensor. Two readings are combined so a close
# strike can't be hidden by a later, more distant one:
#   1. the station's "last strike" (distance + time), and
#   2. the sensor's minute-by-minute record over the window (needs device_id):
#      each 1-minute row carries a strike count and the average distance.
# Distances are the sensor's own estimates, in km, and are approximate.
KM_TO_MI = 0.621371
DEVICE_OBS = "https://swd.weatherflow.com/swd/rest/observations/device/"
SENSOR_SILENT_SECONDS = 20 * 60     # no report for this long = sensor offline
_I_EPOCH, _I_STRIKE_KM, _I_STRIKE_COUNT = 0, 14, 15     # obs_st row layout


def fetch_lightning(station_id, auth: str = "token", device_id=None, window_min: float = 30,
                    session: Optional[requests.Session] = None,
                    now: Optional[float] = None) -> dict:
    """Closest strike inside the last window_min minutes.

    Returns {"closest_mi": float|None, "closest_epoch": float|None,
             "strikes": int, "minute_history": bool}. closest_mi None = no
    strike in the window. Raises TempestUnavailable when the sensor can't be
    read or has gone quiet, so the caller never mistakes silence for clear sky.
    """
    now = time.time() if now is None else now
    since = now - window_min * 60
    raw = fetch_raw(station_id, auth, session)
    status = raw.get("status") or {}
    if status.get("status_code", 0) != 0:
        raise TempestUnavailable(f"API status {status.get('status_code')}: {status.get('status_message')}")
    obs_list = raw.get("obs") or []
    if not obs_list:
        raise TempestUnavailable("station returned no observations (offline?)")
    o = obs_list[0]
    ts = _num(o.get("timestamp"))
    if ts is None or now - ts > SENSOR_SILENT_SECONDS:
        age = "unknown" if ts is None else f"{(now - ts) / 60:.0f} min"
        raise TempestUnavailable(f"sensor has not reported recently (last report {age} ago)")

    hits = []                                   # (km, epoch)
    last_epoch, last_km = _num(o.get("lightning_strike_last_epoch")), _num(o.get("lightning_strike_last_distance"))
    if last_epoch is not None and last_km is not None and since <= last_epoch <= now + MAX_FUTURE_SECONDS:
        hits.append((last_km, last_epoch))

    minute_history = False
    if device_id:
        http = session or requests
        try:
            r = http.get(f"{DEVICE_OBS}{int(device_id)}",
                         params=dict(_auth_params(auth), time_start=int(since), time_end=int(now)),
                         timeout=REQUEST_TIMEOUT_SECONDS)
            r.raise_for_status()
            for row in (r.json().get("obs") or []):
                if not isinstance(row, list) or len(row) <= _I_STRIKE_COUNT:
                    continue
                count, km, ep = _num(row[_I_STRIKE_COUNT]), _num(row[_I_STRIKE_KM]), _num(row[_I_EPOCH])
                if count and km is not None and ep is not None and ep >= since:
                    hits.append((km, ep))
            minute_history = True
        except Exception:
            minute_history = False              # the last-strike reading above still stands

    if not hits:
        return {"closest_mi": None, "closest_epoch": None, "strikes": 0,
                "minute_history": minute_history}
    km, ep = min(hits, key=lambda h: (h[0], -h[1]))
    return {"closest_mi": km * KM_TO_MI, "closest_epoch": ep, "strikes": len(hits),
            "minute_history": minute_history}


def account_report(token: str) -> None:
    """Diagnostic: every station and device on the token's account, with each
    device's latest reading. Shows whether the sensor moved to a new station ID."""
    try:
        r = requests.get(STATIONS, params={"token": token}, timeout=REQUEST_TIMEOUT_SECONDS)
        data = r.json()
    except Exception as e:
        print(f"  could not list account stations: {e}")
        return
    stations = data.get("stations") or []
    print(f"  account has {len(stations)} station(s):")
    for st in stations:
        print(f"  - station {st.get('station_id')} \"{st.get('name')}\"")
        for dv in st.get("devices") or []:
            line = (f"      device {dv.get('device_id')} type {dv.get('device_type')} "
                    f"serial {dv.get('serial_number')}")
            if dv.get("device_type") in ("ST", "SK", "AR"):
                try:
                    o = requests.get(f"https://swd.weatherflow.com/swd/rest/observations/device/{dv['device_id']}",
                                     params={"token": token}, timeout=REQUEST_TIMEOUT_SECONDS).json()
                    obs = o.get("obs") or []
                    if obs:
                        age = (time.time() - obs[-1][0]) / 60
                        line += f" — last reading {age:.0f} min ago"
                    else:
                        line += " — no readings"
                except Exception as e:
                    line += f" — reading check failed ({e})"
            print(line)


if __name__ == "__main__":
    # Diagnostic: python tempest.py 61040:token 184785:public
    # (the "Tempest check" workflow runs every Tempest source in the site file).
    targets = sys.argv[1:]
    if not targets:
        print("usage: python tempest.py STATION_ID[:token|:public] ...")
        raise SystemExit(2)
    bad = 0
    for t in targets:
        sid, _, auth = t.partition(":")
        auth = auth or "public"
        print(f"\n=== Tempest {sid} ({auth}) ===")
        try:
            raw = fetch_raw(sid, auth)
            print(json.dumps({k: raw.get(k) for k in ("station_name", "public_name", "status")}, indent=2))
            print("latest obs:", json.dumps((raw.get("obs") or [None])[0])[:1500])
        except TempestUnavailable as e:
            print(f"observations route: {e}")
        if auth == "public":
            try:
                praw = fetch_public_raw(sid)
                print("public route station:", json.dumps(praw.get("station"))[:500])
                print("public route current:", json.dumps(praw.get("current_conditions"))[:1500])
            except TempestUnavailable as e:
                print(f"public route: {e}")
        if auth == "token":
            account_report(os.environ.get("TEMPEST_TOKEN", "").strip())
        try:
            obs = fetch_tempest(sid, auth)
        except TempestUnavailable as e:
            print(f"FAILED — {e}")
            bad += 1
            continue
        age = (time.time() - obs["observed_epoch"]) / 60
        g = "--" if obs["gust_kn"] is None else f"{obs['gust_kn']:.1f}"
        print(f"PARSED OK: {obs['wind_kn']:.1f} kn g{g} from {obs['wind_dir_deg']}° "
              f"({age:.0f} min ago) — {obs['station_name']}"
              f"{' [public route]' if obs.get('route') == 'public' else ''}")
    raise SystemExit(1 if bad else 0)
