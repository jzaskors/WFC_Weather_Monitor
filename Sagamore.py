"""
Sagamore Yacht Club (WeatherLink) observation source for the WFC weather monitor.

This is the PRIMARY live-observation source. It requires no API key or secret --
the embeddable-page data endpoint is public.

Key gotchas this module handles, all learned from the raw payload:

  * `gust` / `gustAt` are the DAILY PEAK gust and the time it occurred, NOT a
    current gust reading. Same for hiTemp/loTemp. Never use `gust` as a live
    value or as an alert trigger. There is no current-gust field in this feed;
    pull that from the WU PWS (KNYOYSTE13) if you need gust-based thresholds.
  * Units are declared in the payload and must be validated, not assumed. If
    the SYC WeatherLink display preference is changed to mph, this feed changes
    with it, silently, and every threshold in the system becomes wrong.
  * Every numeric field except `windDirection` is a STRING.
  * `lastReceived` is epoch MILLISECONDS and is the authoritative staleness
    signal for tier demotion.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, asdict
from typing import Any, Optional

import requests

EMBED_ID = "e9aef99860fc4aecb73f08d0d9cb3e37"
ENDPOINT = f"https://www.weatherlink.com/embeddablePage/getData/{EMBED_ID}"

SOURCE_NAME = "Sagamore Yacht Club"

# Demote to the backup tier if the station's last report is older than this.
MAX_AGE_SECONDS = 20 * 60

REQUEST_TIMEOUT_SECONDS = 10

EXPECTED_WIND_UNITS = "knots"
EXPECTED_TEMP_UNITS = "°F"

_COMPASS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]


class SagamoreUnavailable(Exception):
    """Sagamore cannot supply a usable current observation -- fall back a tier."""


@dataclass
class Observation:
    """A normalized observation. All wind values in knots, temps in degrees F."""

    source: str
    wind_kt: float
    wind_dir_deg: int
    wind_dir_cardinal: str
    air_temp_f: float
    feels_like_f: float
    humidity_pct: float
    barometer_in_hg: float
    barometer_trend: str
    rain_today_in: float
    observed_at: float          # epoch seconds
    age_seconds: float

    # Daily peak gust -- historical, NOT current. Present for display only.
    # Do not threshold against this.
    peak_gust_kt: float
    peak_gust_at: float         # epoch seconds

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def attribution(self) -> str:
        """Line for the alert email so the reader always knows the source."""
        observed = time.strftime("%-I:%M %p", time.localtime(self.observed_at))
        return f"Obs: {self.source}, {observed}"


def cardinal(degrees: float) -> str:
    """Convert wind direction in degrees to a 16-point compass label."""
    return _COMPASS[int((degrees % 360) / 22.5 + 0.5) % 16]


def _decode_units(raw: Optional[str]) -> str:
    """WeatherLink returns temp units as the HTML entity '&deg;F'."""
    if raw is None:
        return ""
    return raw.replace("&deg;", "°").strip()


def _to_float(raw: Any, field: str) -> float:
    """Coerce a WeatherLink string field to float, loudly."""
    if raw is None:
        raise SagamoreUnavailable(f"field '{field}' was null")
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise SagamoreUnavailable(
            f"field '{field}' was not numeric: {raw!r}"
        ) from exc


def _validate_units(payload: dict) -> None:
    """Refuse to return an observation if the station's units have changed.

    Silently accepting mph where knots are expected would corrupt every
    threshold in the alert system with no visible error.
    """
    wind_units = (payload.get("windUnits") or "").strip().lower()
    if wind_units != EXPECTED_WIND_UNITS:
        raise SagamoreUnavailable(
            f"wind units changed at the station: expected "
            f"{EXPECTED_WIND_UNITS!r}, got {wind_units!r}. "
            f"Thresholds are calibrated in knots -- refusing to report."
        )

    temp_units = _decode_units(payload.get("tempUnits"))
    if temp_units != EXPECTED_TEMP_UNITS:
        raise SagamoreUnavailable(
            f"temperature units changed at the station: expected "
            f"{EXPECTED_TEMP_UNITS!r}, got {temp_units!r}"
        )


def parse(payload: dict, now: Optional[float] = None) -> Observation:
    """Parse a raw WeatherLink embeddable-page payload into an Observation.

    Raises SagamoreUnavailable if the payload is inaccessible, malformed,
    has changed units, or is too stale to be treated as current.
    """
    now = time.time() if now is None else now

    if payload.get("noAccess"):
        raise SagamoreUnavailable("station embed reports noAccess")

    last_received_ms = payload.get("lastReceived")
    if not last_received_ms:
        raise SagamoreUnavailable("payload has no lastReceived timestamp")

    observed_at = float(last_received_ms) / 1000.0
    age = now - observed_at

    if age > MAX_AGE_SECONDS:
        raise SagamoreUnavailable(
            f"last report is {age / 60:.1f} min old "
            f"(limit {MAX_AGE_SECONDS / 60:.0f} min)"
        )

    # A clock skew or a bad timestamp should not read as a fresh observation.
    if age < -300:
        raise SagamoreUnavailable(
            f"lastReceived is {abs(age) / 60:.1f} min in the future"
        )

    _validate_units(payload)

    direction = int(payload.get("windDirection") or 0)

    gust_at_ms = payload.get("gustAt")
    peak_gust_at = float(gust_at_ms) / 1000.0 if gust_at_ms else observed_at

    return Observation(
        source=SOURCE_NAME,
        wind_kt=_to_float(payload.get("wind"), "wind"),
        wind_dir_deg=direction,
        wind_dir_cardinal=cardinal(direction),
        air_temp_f=_to_float(payload.get("temperature"), "temperature"),
        feels_like_f=_to_float(
            payload.get("temperatureFeelLike"), "temperatureFeelLike"
        ),
        humidity_pct=_to_float(payload.get("humidity"), "humidity"),
        barometer_in_hg=_to_float(payload.get("barometer"), "barometer"),
        barometer_trend=(payload.get("barometerTrend") or "Unknown").strip(),
        rain_today_in=_to_float(payload.get("rain"), "rain"),
        observed_at=observed_at,
        age_seconds=age,
        peak_gust_kt=_to_float(payload.get("gust"), "gust"),
        peak_gust_at=peak_gust_at,
    )


def fetch(session: Optional[requests.Session] = None) -> Observation:
    """Fetch and parse the current Sagamore observation.

    Raises SagamoreUnavailable on any failure. The caller is responsible for
    falling back to the backup tier and LABELING the email accordingly --
    never let a fallback reading present itself as an on-site observation.
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
            "response was not JSON (embed may have been disabled)"
        ) from exc

    return parse(payload)


if __name__ == "__main__":
    try:
        obs = fetch()
    except SagamoreUnavailable as err:
        print(f"UNAVAILABLE: {err}")
    else:
        peak_time = time.strftime("%-I:%M %p", time.localtime(obs.peak_gust_at))
        print(obs.attribution)
        print(
            f"Wind {obs.wind_kt:.0f} kt from {obs.wind_dir_cardinal} "
            f"({obs.wind_dir_deg:03d}°)"
        )
        print(f"Peak gust today {obs.peak_gust_kt:.0f} kt at {peak_time}")
        print(
            f"Air {obs.air_temp_f:.0f}°F (feels {obs.feels_like_f:.0f}°F), "
            f"RH {obs.humidity_pct:.0f}%"
        )
        print(
            f"Baro {obs.barometer_in_hg:.2f} in Hg {obs.barometer_trend}, "
            f"rain today {obs.rain_today_in:.2f} in"
        )
        print(f"Age {obs.age_seconds / 60:.1f} min")
