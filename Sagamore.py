"""
sagamore.py — Sagamore Yacht Club live observation source for the WFC weather monitor.

PRIMARY live-observation source. The station is a Davis Vantage Vue at SYC,
~250 m from the WFC dock, publishing through WeatherLink. No API key needed:
the embeddable-page data endpoint is public.

    https://www.weatherlink.com/embeddablePage/getData/<EMBED_ID>

FILENAME MATTERS: this file must be lowercase `sagamore.py`. monitor.py does
`from sagamore import fetch_sagamore`, and GitHub's Linux runners are
case-sensitive — `Sagamore.py` silently fails to import and the monitor falls
back to Kings Point.

Interface contract with monitor.py:
    fetch_sagamore() -> dict with keys
        wind_kn         float  sustained wind, knots
        gust_kn         None   ALWAYS None — see THE GUST NOTE
        wind_dir_deg    int    direction wind is FROM, degrees true
        observed_epoch  float  UTC epoch seconds of the observation
    plus display-only extras (gust_monitored, peak_gust_kn, air_temp_f, ...).
    Raises SagamoreUnavailable on any failure so the reason shows in the
    Actions log and monitor.py falls through to the buoy tier.

Payload shape (verified against the real feed, July 2026). Flat JSON; every
numeric except windDirection arrives as a STRING:
    wind "2"  windDirection 46  windUnits "knots"  gust "15"  gustAt 1785300955568
    lastReceived 1785354342000 (epoch ms)  temperature "78.4"  tempUnits "&deg;F"
    humidity, barometer, barometerTrend, rain, hiTemp/hiTempDate, loTemp/loTempDate

=============================================================================
THE GUST NOTE — read before changing gust_kn
=============================================================================
`gust` / `gustAt` are the DAILY PEAK gust and when it happened, not a current
reading (July test: gustAt was ~15 hours older than lastReceived). A daily
maximum only climbs, so feeding it to the threshold engine produces a HALT
that cannot clear until the station resets at midnight. There is no
current-gust field in this feed, so gust_kn is None by design and
gust_monitored is False. monitor.py surfaces that in the email so a blank
gust never reads as "no gust hazard".
=============================================================================
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

import requests

EMBED_ID = "e9aef99860fc4aecb73f08d0d9cb3e37"
ENDPOINT = f"https://www.weatherlink.com/embeddablePage/getData/{EMBED_ID}"

STATION_LABEL = "Sagamore YC (in harbor)"

# Demote to the buoy tier if the last report is older than this. The station
# reports every few minutes, so 20 minutes of silence means something is wrong.
MAX_AGE_SECONDS = 20 * 60
# Reject timestamps this far in the future (clock skew would read as fresh).
MAX_FUTURE_SECONDS = 300
REQUEST_TIMEOUT_SECONDS = 15

# Unit conversion by the station's declared display units. If SYC changes
# their WeatherLink display preference, the feed changes with it — convert
# rather than assume.
_TO_KNOTS = {"knots": 1.0, "kt": 1.0, "kts": 1.0, "mph": 0.868976,
             "m/s": 1.94384, "km/h": 0.539957, "kph": 0.539957}


class SagamoreUnavailable(Exception):
    """Sagamore cannot supply a usable current observation — fall back a tier."""


def _to_float(raw: Any, field: str) -> float:
    if raw is None or raw == "":
        raise SagamoreUnavailable(f"field '{field}' was empty")
    try:
        return float(str(raw).replace(",", ""))
    except (TypeError, ValueError) as exc:
        raise SagamoreUnavailable(f"field '{field}' not numeric: {raw!r}") from exc


def _opt_float(raw: Any) -> Optional[float]:
    if raw is None or raw in ("", "--"):
        return None
    try:
        return float(str(raw).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _decode_units(raw: Optional[str]) -> str:
    """WeatherLink returns temperature units as the HTML entity '&deg;F'."""
    return "" if raw is None else raw.replace("&deg;", "°").strip()


def _wind_factor(payload: dict) -> float:
    units = (payload.get("windUnits") or "").strip().lower()
    if units not in _TO_KNOTS:
        raise SagamoreUnavailable(
            f"unrecognized station wind units {units!r} — refusing to guess")
    return _TO_KNOTS[units]


def parse_sagamore(payload: dict, now: Optional[float] = None) -> dict:
    """Parse a raw WeatherLink getData payload into the monitor's dict shape."""
    now = time.time() if now is None else now

    if not isinstance(payload, dict):
        raise SagamoreUnavailable(f"payload was {type(payload).__name__}, not an object")
    if payload.get("noAccess"):
        raise SagamoreUnavailable("station embed reports noAccess (embed disabled?)")

    last_received_ms = payload.get("lastReceived")
    if not last_received_ms:
        raise SagamoreUnavailable(
            f"payload has no lastReceived timestamp (keys: {sorted(payload)[:12]})")
    observed_epoch = float(last_received_ms) / 1000.0
    age = now - observed_epoch
    if age > MAX_AGE_SECONDS:
        raise SagamoreUnavailable(
            f"last report {age / 60:.1f} min old (limit {MAX_AGE_SECONDS / 60:.0f} min)")
    if age < -MAX_FUTURE_SECONDS:
        raise SagamoreUnavailable(
            f"lastReceived is {abs(age) / 60:.1f} min in the future — clock skew?")

    factor = _wind_factor(payload)
    wind_kn = _to_float(payload.get("wind"), "wind") * factor
    if not (0 <= wind_kn <= 100):
        raise SagamoreUnavailable(f"implausible wind speed {wind_kn:.1f} kn")

    direction = payload.get("windDirection")
    wind_dir = int(float(direction)) % 360 if direction not in (None, "") else None

    peak_gust = _opt_float(payload.get("gust"))
    gust_at_ms = payload.get("gustAt")
    temp_units = _decode_units(payload.get("tempUnits"))
    temp = _opt_float(payload.get("temperature"))
    if temp is not None and temp_units.endswith("C"):
        temp = temp * 9 / 5 + 32

    return {
        # --- monitor contract ---
        "wind_kn": wind_kn,
        "gust_kn": None,                 # see THE GUST NOTE
        "wind_dir_deg": wind_dir,
        "observed_epoch": observed_epoch,
        # --- display only ---
        "station": STATION_LABEL,
        "gust_monitored": False,
        "peak_gust_kn": peak_gust * factor if peak_gust is not None else None,
        "peak_gust_epoch": float(gust_at_ms) / 1000.0 if gust_at_ms else None,
        "air_temp_f": temp,
        "humidity_pct": _opt_float(payload.get("humidity")),
        "barometer_in_hg": _opt_float(payload.get("barometer")),
        "barometer_trend": (payload.get("barometerTrend") or "").strip() or None,
        "rain_today_in": _opt_float(payload.get("rain")),
    }


def fetch_raw(session: Optional[requests.Session] = None) -> dict:
    """GET the embed payload. Raises SagamoreUnavailable with a readable reason."""
    http = session or requests
    headers = {"User-Agent": "WFC-weather-monitor/1.0 (waterfront safety monitoring)",
               "Accept": "application/json"}
    # Cache-buster: the embed page itself sends one; without it a CDN copy can go stale.
    params = {"_": int(time.time() * 1000)}
    try:
        r = http.get(ENDPOINT, params=params, headers=headers,
                     timeout=REQUEST_TIMEOUT_SECONDS)
        r.raise_for_status()
    except requests.RequestException as exc:
        raise SagamoreUnavailable(f"request failed: {exc}") from exc
    try:
        return r.json()
    except ValueError as exc:
        snippet = r.text[:120].replace("\n", " ")
        raise SagamoreUnavailable(
            f"response was not JSON (status {r.status_code}): {snippet!r}") from exc


def fetch_sagamore(session: Optional[requests.Session] = None) -> dict:
    """Fetch + parse the current Sagamore observation (monitor.py entry point)."""
    return parse_sagamore(fetch_raw(session))


if __name__ == "__main__":
    # Run directly (or via the "Sagamore check" workflow) to see exactly what
    # the feed returns and how it parses. Exit code 1 if unusable.
    try:
        raw = fetch_raw()
    except SagamoreUnavailable as e:
        print(f"FETCH FAILED — {e}")
        raise SystemExit(1)
    print("RAW PAYLOAD:")
    print(json.dumps(raw, indent=2)[:4000])
    try:
        obs = parse_sagamore(raw)
    except SagamoreUnavailable as e:
        print(f"\nPARSE FAILED — {e}")
        raise SystemExit(1)
    age = (time.time() - obs["observed_epoch"]) / 60
    print(f"\nPARSED OK: {obs['wind_kn']:.1f} kn from {obs['wind_dir_deg']}° "
          f"({age:.0f} min ago); peak gust today "
          f"{obs['peak_gust_kn'] if obs['peak_gust_kn'] is not None else '--'} kn")
