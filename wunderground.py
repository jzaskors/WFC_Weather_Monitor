"""
wunderground.py — Weather Underground personal-weather-station source for the
WFC weather monitor.

Reads the current observation of ANY station on Weather Underground by its
station ID (e.g. KNYOYSTE13 = Sagamore YC), whatever brand it is. Each one is
an entry in sites/wfc.json -> observation_sources with "type": "wunderground".

    GET https://api.weather.com/v2/pws/observations/current
        ?stationId=<ID>&format=json&units=e&numericPrecision=decimal&apiKey=<KEY>

AUTH: the WU_API_KEY repo secret. The key is free to anyone whose own station
uploads to Weather Underground (WFC's Tempest does): wunderground.com ->
Member Settings -> API Keys. It is for personal, non-commercial use and is
rate-limited per day, so the monitor only asks for the stations it needs.

FILENAME MATTERS: lowercase `wunderground.py`.

Interface contract with monitor.py:
    fetch_wu(station, session=None) -> dict
        wind_kn         float  sustained wind, knots
        gust_kn         float or None  current gust as uploaded by the station
        wind_dir_deg    int or None    direction wind is FROM, degrees true
        observed_epoch  float  UTC epoch seconds of the observation
    Raises WUUnavailable on any failure so the reason shows in the log.

Payload notes: with units=e the numbers sit under "imperial" and wind is mph.
A station with nothing in the last hour answers HTTP 204 with an empty body.
qcStatus: 1 passed Weather Underground's quality check, 0 failed, -1 unchecked.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Optional

import requests

ENDPOINT = "https://api.weather.com/v2/pws/observations/current"
MPH_TO_KN = 0.868976
MAX_FUTURE_SECONDS = 300
REQUEST_TIMEOUT_SECONDS = 15


class WUUnavailable(Exception):
    """A Weather Underground station cannot supply a usable current observation."""


def _num(raw: Any) -> Optional[float]:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _key() -> str:
    key = os.environ.get("WU_API_KEY", "").strip()
    if not key:
        raise WUUnavailable("the WU_API_KEY secret is not set")
    return key


def fetch_raw(station: str, session: Optional[requests.Session] = None) -> dict:
    http = session or requests
    params = {"stationId": str(station).strip().upper(), "format": "json", "units": "e",
              "numericPrecision": "decimal", "apiKey": _key()}
    try:
        r = http.get(ENDPOINT, params=params, timeout=REQUEST_TIMEOUT_SECONDS,
                     headers={"Accept": "application/json"})
    except requests.RequestException as exc:
        # never echo the exception text: it can contain the URL with the key in it
        raise WUUnavailable(f"request failed ({type(exc).__name__})") from None
    if r.status_code == 204 or not r.content:
        raise WUUnavailable("station has not reported in the last hour")
    if r.status_code == 401:
        raise WUUnavailable("HTTP 401: Weather Underground rejected the API key")
    if r.status_code == 404:
        raise WUUnavailable(f"HTTP 404: station {station} not found")
    if r.status_code == 429:
        raise WUUnavailable("HTTP 429: daily API call limit reached")
    if r.status_code >= 400:
        raise WUUnavailable(f"HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError:
        raise WUUnavailable(f"response was not JSON (status {r.status_code})") from None


def parse_wu(payload: dict, now: Optional[float] = None) -> dict:
    """Parse a raw current-conditions payload into the monitor's dict shape.
    Staleness is NOT judged here — monitor.py applies the site file's max_age_min."""
    now = time.time() if now is None else now
    if not isinstance(payload, dict):
        raise WUUnavailable(f"payload was {type(payload).__name__}, not an object")
    obs_list = payload.get("observations") or []
    if not obs_list:
        raise WUUnavailable("no observations in the response")
    o = obs_list[0]
    if o.get("qcStatus") == 0:
        raise WUUnavailable("reading failed Weather Underground's quality check")

    ts = _num(o.get("epoch"))
    if ts is None:
        raise WUUnavailable("observation has no timestamp")
    if ts - now > MAX_FUTURE_SECONDS:
        raise WUUnavailable("timestamp is in the future — clock skew?")

    imp = o.get("imperial") or {}
    wind_mph = _num(imp.get("windSpeed"))
    if wind_mph is None:
        raise WUUnavailable("windSpeed missing (station has no working wind sensor?)")
    wind_kn = wind_mph * MPH_TO_KN
    if not (0 <= wind_kn <= 100):
        raise WUUnavailable(f"implausible wind speed {wind_kn:.1f} kn")
    gust_mph = _num(imp.get("windGust"))
    gust_kn = None if gust_mph is None else max(gust_mph * MPH_TO_KN, wind_kn)
    d = _num(o.get("winddir"))

    return {
        # --- monitor contract ---
        "wind_kn": wind_kn,
        "gust_kn": gust_kn,
        "wind_dir_deg": None if d is None else int(d) % 360,
        "observed_epoch": ts,
        # --- display only ---
        "station_id": o.get("stationID"),
        "neighborhood": o.get("neighborhood"),
        "lat": _num(o.get("lat")), "lon": _num(o.get("lon")),
        "elev_ft": _num(imp.get("elev")),
        "software": o.get("softwareType"),
        "qc_status": o.get("qcStatus"),
        "gust_monitored": gust_kn is not None,
        "air_temp_f": _num(imp.get("temp")),
    }


def fetch_wu(station: str, session: Optional[requests.Session] = None) -> dict:
    """Fetch + parse one Weather Underground station (monitor.py entry point)."""
    return parse_wu(fetch_raw(station, session))


NEAR_ENDPOINT = "https://api.weather.com/v3/location/near"


def nearby_stations(lat: float, lon: float, session: Optional[requests.Session] = None) -> list:
    """Weather Underground's own list of the stations nearest a point:
    [{"id", "name", "lat", "lon", "miles"}, ...] nearest first. Used by the
    station check to find candidates; the monitor itself never calls this."""
    http = session or requests
    try:
        r = http.get(NEAR_ENDPOINT, timeout=REQUEST_TIMEOUT_SECONDS,
                     params={"geocode": f"{lat:.4f},{lon:.4f}", "product": "pws",
                             "format": "json", "apiKey": _key()})
    except requests.RequestException as exc:
        raise WUUnavailable(f"request failed ({type(exc).__name__})") from None
    if r.status_code == 401:
        raise WUUnavailable("HTTP 401: this key is not allowed to list nearby stations")
    if r.status_code >= 400 or not r.content:
        raise WUUnavailable(f"HTTP {r.status_code}")
    try:
        loc = (r.json() or {}).get("location") or {}
    except ValueError:
        raise WUUnavailable("response was not JSON") from None
    ids = loc.get("stationId") or []
    col = lambda k: loc.get(k) or [None] * len(ids)
    out = [{"id": i, "name": n, "lat": _num(la), "lon": _num(lo), "miles": _num(mi)}
           for i, n, la, lo, mi in zip(ids, col("stationName"), col("latitude"),
                                       col("longitude"), col("distanceMi")) if i]
    return sorted(out, key=lambda s: (s["miles"] is None, s["miles"] or 0))


if __name__ == "__main__":
    # Diagnostic: python wunderground.py KNYOYSTE13 KNYOYSTE29
    # (for a side-by-side of every station, run: python monitor.py --stations)
    ids = sys.argv[1:]
    if not ids:
        print("usage: python wunderground.py STATION_ID ...")
        raise SystemExit(2)
    bad = 0
    for sid in ids:
        print(f"\n=== Weather Underground {sid} ===")
        try:
            raw = fetch_raw(sid)
            print(json.dumps((raw.get("observations") or [None])[0])[:1200])
            o = parse_wu(raw)
        except WUUnavailable as e:
            print(f"FAILED — {e}")
            bad += 1
            continue
        age = (time.time() - o["observed_epoch"]) / 60
        g = "--" if o["gust_kn"] is None else f"{o['gust_kn']:.1f}"
        print(f"PARSED OK: {o['wind_kn']:.1f} kn g{g} from {o['wind_dir_deg']}° "
              f"({age:.0f} min ago) — {o['neighborhood']} @ {o['lat']},{o['lon']} "
              f"elev {o['elev_ft']} ft, {o['software']}")
    raise SystemExit(1 if bad else 0)
