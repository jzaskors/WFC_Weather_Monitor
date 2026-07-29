"""
Sagamore Yacht Club (WeatherLink) observation source for the WFC weather monitor.

PRIMARY live-observation source. No API key or secret required -- the
embeddable-page data endpoint is public.

Interface contract with monitor.py:
    fetch_sagamore() -> dict with keys
        wind_kn         float   sustained wind, knots
        gust_kn         None    ALWAYS None -- see THE GUST NOTE below
        wind_dir_deg    int     direction wind is FROM, degrees true
        observed_epoch  float   UTC epoch seconds of the observation
    ...plus display-only extras (peak_gust_kn, air_temp_f, etc.)
    Raises SagamoreUnavailable on any failure, so the reason lands in the
    Actions log instead of vanishing.

=============================================================================
THE GUST NOTE -- READ BEFORE CHANGING gust_kn
=============================================================================
The WeatherLink payload's `gust` field is the DAILY PEAK gust, with `gustAt`
giving the time it occurred. It is not a current reading. A daily maximum only
ever climbs, so feeding it to the threshold engine produces a HALT that cannot
clear until the station resets at local midnight -- an alert that fires at
dawn and stays stuck through a dead-calm afternoon.

There is NO current-gust field in this feed. gust_kn is therefore None by
design. Do not map `gust` onto it.

To restore observed gust coverage, pull it from the WU PWS (KNYOYSTE13), which
reports a live gust for the same physical station. Until then the alert email
must state that gust is unmonitored -- a blank gust field reads to staff as
"no gust hazard," which is the opposite of true.
=============================================================================
"""

from __future__ import annotations

import time
from typing import Any, Optional

import requests

EMBED_ID = "e9aef99860fc4aecb73f08d0d9cb3e37"
ENDPOINT = f"https://www.weatherlink.com/embeddablePage/getData/{EMBED_ID}"

STATION_LABEL = "Sagamore YC (in harbor)"

# Demote to the buoy tier if the last report is older than this.
# Kept tighter than monitor.OBS_MAX_AGE_MIN (90) because this station reports
# every few minutes -- 20 minutes of silence means something is wrong.
MAX_AGE_SECONDS = 20 * 60

# Reject timestamps this far in the future; clock skew would otherwise read as
# a perfectly fresh observation.
MAX_FUTURE_SECONDS = 300

REQUEST_TIMEOUT_SECONDS = 10

EXPECTED_WIND_UNITS = "knots"
EXPECTED_TEMP_UNITS = "°F"


class SagamoreUnavailable(Exception):
    """Sagamore cannot supply a usable current observation -- fall back a tier."""


def _decode_units(raw: Optional[str]) -> str:
    """WeatherLink returns temperature units as the HTML entity '&deg;F'."""
    return "" if raw is None else raw.replace("&deg;", "°").strip()


def _to_float(raw: Any, field: str) -> float:
    """Coerce a WeatherLink string field to float, loudly.

    Every numeric in this payload except windDirection arrives as a string.
    """
    if raw is None:
        raise SagamoreUnavailable(f"field '{field}' was null")
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise SagamoreUnavailable(f"field '{field}' not numeric: {raw!r}") from exc


def _opt_float(raw: Any) -> Optional[float]:
    """Coerce a display-only field; None rather than raising."""
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _validate_units(payload: dict) -> None:
    """Refuse to report if the station's display units have changed.

    WeatherLink serves whatever units the station owner has selected. If anyone
    at SYC flips their preference to mph, this feed follows -- and every
    threshold in the monitor, all calibrated in knots, silently tightens by
    about 15%. Failing loudly here drops us to the buoy tier instead, which is
    the correct outcome.
    """
    wind_units = (payload.get("windUnits") or "").strip().lower()
    if wind_units != EXPECTED_WIND_UNITS:
        raise SagamoreUnavailable(
            f"station wind units changed: expected {EXPECTED_WIND_UNITS!r}, "
            f"got {wind_units!r} -- thresholds are in knots, refusing to report"
        )

    temp_units = _decode_units(payload.get("tempUnits"))
    if temp_units != EXPECTED_TEMP_UNITS:
        raise SagamoreUnavailable(
            f"station temperature units changed: expected "
            f"{EXPECTED_TEMP_UNITS!r}, got {temp_units!r}"
        )


def parse_sagamore(payload: dict, now: Optional[float] = None) -> dict:
    """Parse a raw WeatherLink embeddable-page payload into the monitor's shape.

    Raises SagamoreUnavailable if the feed is inaccessible, malformed, has
    changed units, or is too stale to treat as current.
    """
    now = time.time() if now is None else now

    if payload.get("noAccess"):
        raise SagamoreUnavailable("station embed reports noAccess")

    last_received_ms = payload.get("lastReceived")
    if not last_received_ms:
        raise SagamoreUnavailable("payload has no lastReceived timestamp")

    observed_epoch = float(last_received_ms) / 1000.0
    age = now - observed_epoch

    if age > MAX_AGE_SECONDS:
        raise SagamoreUnavailable(
            f"last report {age / 60:.1f} min old "
            f"(limit {MAX_AGE_SECONDS / 60:.0f} min)"
        )
    if age < -MAX_FUTURE_SECONDS:
        raise SagamoreUnavailable(
            f"lastReceived is {abs(age) / 60:.1f} min in the future -- clock skew?"
        )

    _validate_units(payload)

    gust_at_ms = payload.get("gustAt")
    peak_gust_epoch = float(gust_at_ms) / 1000.0 if gust_at_ms else None

    return {
        # --- monitor contract ---
        "wind_kn": _to_float(payload.get("wind"), "wind"),
        "gust_kn": None,          # see THE GUST NOTE at the top of this file
        "wind_dir_deg": int(payload.get("windDirection") or 0),
        "observed_epoch": observed_epoch,

        # --- display only ---
        "station": STATION_LABEL,
        "gust_monitored": False,  # monitor should surface this in the email
        "peak_gust_kn": _opt_float(payload.get("gust")),
        "peak_gust_epoch": peak_gust_epoch,
        "air_temp_f": _opt_float(payload.get("temperature")),
        "feels_like_f": _opt_float(payload.get("temperatureFeelLike")),
        "humidity_pct": _opt_float(payload.get("humidity")),
        "barometer_in_hg": _opt_float(payload.get("barometer")),
        "barometer_trend": (payload.get("barometerTrend") or "Unknown").strip(),
        "rain_today_in": _opt_float(payload.get("rain")),
    }


def fetch_sagamore(session: Optional[requests.Session] = None) -> dict:
    """Fetch and parse the current Sagamore observation.

    Raises SagamoreUnavailable on any failure. monitor.py catches this, logs
    the reason, and falls through to the NDBC tier.
    """
    http = session or requests

    try:
        response = http.get(ENDPOINT, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise SagamoreUnavailable(f"request failed: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise SagamoreUnavailable(
            "response was not JSON -- embed may have been disabled"
        ) from exc

    return parse_sagamore(payload)


# Back-compat alias.
fetch = fetch_sagamore


if __name__ == "__main__":
    try:
        obs = fetch_sagamore()
    except SagamoreUnavailable as err:
        print(f"UNAVAILABLE: {err}")
        raise SystemExit(1)

    age_min = (time.time() - obs["observed_epoch"]) / 60
    print(f"{obs['station']} — {age_min:.1f} min old")
    print(f"Sustained wind : {obs['wind_kn']:.0f} kn from {obs['wind_dir_deg']:03d}°")
    print("Current gust   : not reported by this station")
    if obs["peak_gust_kn"] is not None and obs["peak_gust_epoch"]:
        when = time.strftime("%-I:%M %p", time.localtime(obs["peak_gust_epoch"]))
        print(f"Peak gust today: {obs['peak_gust_kn']:.0f} kn at {when} "
              f"(historical — NOT a trigger)")
    print(f"Air            : {obs['air_temp_f']:.0f}°F "
          f"(feels {obs['feels_like_f']:.0f}°F), RH {obs['humidity_pct']:.0f}%")
    print(f"Barometer      : {obs['barometer_in_hg']:.2f} in Hg "
          f"{obs['barometer_trend']}")
    print(f"Rain today     : {obs['rain_today_in']:.2f} in")
