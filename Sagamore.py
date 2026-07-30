"""
sagamore.py — Sagamore Yacht Club live observation source for the WFC weather monitor.

Two backends, in priority order:

  1. WeatherLink v2 API  (documented, stable, signed — requires key+secret from SYC)
  2. WeatherLink embed JSON  (undocumented, no auth, works today, can break without notice)

Both return a normalized Observation. Failures raise SourceUnavailable with a reason
string, so the caller's tier logic can LABEL the fallback rather than silently
substituting Kings Point and presenting it as a local reading.
"""

from __future__ import annotations

import os
import time
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

import requests

log = logging.getLogger(__name__)

# --- Configuration -----------------------------------------------------------

SYC_EMBED_UUID = os.environ.get(
    "SYC_EMBED_UUID", "e9aef99860fc4aecb73f08d0d9cb3e37"
)
SYC_STATION_ID = os.environ.get("SYC_WL_STATION_ID")      # for v2 API
WL_API_KEY = os.environ.get("WEATHERLINK_API_KEY")
WL_API_SECRET = os.environ.get("WEATHERLINK_API_SECRET")

EMBED_URL = "https://www.weatherlink.com/embeddablePage/summaryData/{uuid}"
V2_URL = "https://api.weatherlink.com/v2/current/{station_id}"

# An observation older than this is not trustworthy for a live GO/CAUTION call.
MAX_AGE_MIN = int(os.environ.get("SYC_MAX_AGE_MIN", "25"))
TIMEOUT_S = 10


class SourceUnavailable(Exception):
    """Raised when Sagamore cannot supply a usable observation."""


@dataclass
class Observation:
    source: str               # "Sagamore YC" — what gets printed in the email
    backend: str              # "weatherlink-v2" | "weatherlink-embed"
    wind_kn: float | None
    gust_kn: float | None
    wind_dir_deg: int | None
    air_temp_f: float | None
    observed_at: datetime
    distance_mi: float = 0.2  # adjacent to WFC

    @property
    def age_min(self) -> float:
        return (datetime.now(timezone.utc) - self.observed_at).total_seconds() / 60

    def summary(self) -> str:
        w = f"{self.wind_kn:.0f}" if self.wind_kn is not None else "--"
        g = f" g{self.gust_kn:.0f}" if self.gust_kn is not None else ""
        d = f" {self.wind_dir_deg}" + "\u00b0" if self.wind_dir_deg is not None else ""
        return f"{w} kn{g}{d} ({self.age_min:.0f} min ago)"


# --- Unit handling -----------------------------------------------------------

_TO_KNOTS = {
    "mph": 0.868976,
    "kt": 1.0,
    "kts": 1.0,
    "knots": 1.0,
    "kn": 1.0,
    "m/s": 1.94384,
    "km/h": 0.539957,
    "kph": 0.539957,
}


def _to_knots(value: float, unit: str | None) -> float:
    """Davis stations in the US almost always report mph. Never assume — convert."""
    if unit is None:
        raise SourceUnavailable("wind speed returned with no unit label")
    key = unit.strip().lower().replace(" ", "")
    for candidate, factor in _TO_KNOTS.items():
        if key == candidate.replace(" ", ""):
            return value * factor
    raise SourceUnavailable(f"unrecognized wind unit {unit!r}")


# --- Embed backend -----------------------------------------------------------

# The embed payload identifies each reading by a human-readable name. The key
# holding that name has varied across WeatherLink releases, so check several.
_NAME_KEYS = ("sensorDataName", "displayName", "name", "label")


def _entry_name(entry: dict[str, Any]) -> str:
    for k in _NAME_KEYS:
        v = entry.get(k)
        if isinstance(v, str) and v:
            return v.strip().lower()
    return ""


def _find(entries: Iterable[dict[str, Any]], *needles: str) -> dict[str, Any] | None:
    """
    Match by NAME, not by array index. Index-based parsing (currConditionValues[0])
    is what breaks every time Davis reorders the tiles on the embed page.
    """
    for entry in entries:
        name = _entry_name(entry)
        if all(n in name for n in needles):
            return entry
    return None


def _numeric(entry: dict[str, Any] | None) -> float | None:
    if not entry:
        return None
    for key in ("value", "convertedValue"):
        raw = entry.get(key)
        if raw in (None, "", "--"):
            continue
        try:
            return float(str(raw).replace(",", ""))
        except ValueError:
            continue
    return None


def fetch_embed(uuid: str = SYC_EMBED_UUID) -> Observation:
    url = EMBED_URL.format(uuid=uuid)
    # The cache-buster is what the page itself sends; without it you can get a
    # stale CDN copy that never advances.
    params = {"ts": int(time.time() * 1000)}
    headers = {
        "User-Agent": "WFC-weather-monitor/1.0 (waterfront safety monitoring)",
        "Accept": "application/json",
    }

    try:
        r = requests.get(url, params=params, headers=headers, timeout=TIMEOUT_S)
        r.raise_for_status()
    except requests.RequestException as e:
        raise SourceUnavailable(f"embed request failed: {e}") from e

    try:
        payload = r.json()
    except ValueError as e:
        # Almost always means Davis served an HTML error/login page.
        raise SourceUnavailable("embed returned non-JSON (endpoint may have moved)") from e

    curr = payload.get("currConditionValues") or []
    highs = payload.get("highLowValues") or []
    if not curr:
        raise SourceUnavailable("embed payload had no currConditionValues")

    wind_entry = _find(curr, "wind", "speed")
    wind_raw = _numeric(wind_entry)
    if wind_raw is None:
        raise SourceUnavailable("no wind speed in embed payload")
    wind_unit = (wind_entry or {}).get("unitLabel")
    wind_kn = _to_knots(wind_raw, wind_unit)

    # Gust lives in current conditions on some stations, in the high/low block
    # on others. Try both before giving up.
    gust_entry = _find(curr, "gust") or _find(curr, "wind", "high") or _find(highs, "wind", "high")
    gust_raw = _numeric(gust_entry)
    gust_kn = _to_knots(gust_raw, (gust_entry or {}).get("unitLabel") or wind_unit) if gust_raw is not None else None

    dir_entry = _find(curr, "wind", "direction") or _find(curr, "wind", "dir")
    dir_raw = _numeric(dir_entry)
    wind_dir = int(dir_raw) % 360 if dir_raw is not None else None

    temp_raw = _numeric(_find(curr, "temp"))

    observed_at = _embed_timestamp(payload)

    obs = Observation(
        source="Sagamore YC",
        backend="weatherlink-embed",
        wind_kn=wind_kn,
        gust_kn=gust_kn,
        wind_dir_deg=wind_dir,
        air_temp_f=temp_raw,
        observed_at=observed_at,
    )
    _check_fresh(obs)
    return obs


def _embed_timestamp(payload: dict[str, Any]) -> datetime:
    for key in ("lastReceived", "generatedAt", "lastReading", "timestamp"):
        raw = payload.get(key)
        if raw is None:
            continue
        try:
            ts = float(raw)
        except (TypeError, ValueError):
            continue
        if ts > 1e11:      # epoch milliseconds
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    # No timestamp in the payload. Treating "now" as the observation time would
    # let a frozen station look permanently fresh, so refuse instead.
    raise SourceUnavailable("embed payload carried no observation timestamp")


# --- v2 API backend ----------------------------------------------------------

def fetch_v2() -> Observation:
    if not (WL_API_KEY and WL_API_SECRET and SYC_STATION_ID):
        raise SourceUnavailable("v2 API credentials not configured")

    url = V2_URL.format(station_id=SYC_STATION_ID)
    try:
        r = requests.get(
            url,
            params={"api-key": WL_API_KEY},
            headers={"X-Api-Secret": WL_API_SECRET},
            timeout=TIMEOUT_S,
        )
        r.raise_for_status()
        payload = r.json()
    except (requests.RequestException, ValueError) as e:
        raise SourceUnavailable(f"v2 API request failed: {e}") from e

    # Pick the sensor block that actually carries wind data; a station can expose
    # several (ISS, barometer, indoor console).
    block = None
    for sensor in payload.get("sensors", []):
        for record in sensor.get("data", []):
            if record.get("wind_speed_last") is not None or record.get("wind_speed_avg_last_1_min") is not None:
                block = record
                break
        if block:
            break
    if block is None:
        raise SourceUnavailable("v2 API response contained no wind sensor")

    mph = block.get("wind_speed_avg_last_1_min", block.get("wind_speed_last"))
    gust_mph = block.get("wind_speed_hi_last_2_min", block.get("wind_speed_hi_last_10_min"))
    direction = block.get("wind_dir_scalar_avg_last_1_min", block.get("wind_dir_last"))

    obs = Observation(
        source="Sagamore YC",
        backend="weatherlink-v2",
        wind_kn=_to_knots(float(mph), "mph") if mph is not None else None,
        gust_kn=_to_knots(float(gust_mph), "mph") if gust_mph is not None else None,
        wind_dir_deg=int(direction) % 360 if direction is not None else None,
        air_temp_f=block.get("temp"),
        observed_at=datetime.fromtimestamp(block["ts"], tz=timezone.utc),
    )
    _check_fresh(obs)
    return obs


# --- Shared ------------------------------------------------------------------

def _check_fresh(obs: Observation) -> None:
    if obs.age_min > MAX_AGE_MIN:
        raise SourceUnavailable(
            f"observation is {obs.age_min:.0f} min old (limit {MAX_AGE_MIN})"
        )
    if obs.wind_kn is not None and not (0 <= obs.wind_kn <= 100):
        raise SourceUnavailable(f"implausible wind speed {obs.wind_kn:.1f} kn")


def fetch_sagamore() -> Observation:
    """
    Try each backend in order. Raises SourceUnavailable with every reason
    collected, so the failure shows up in the Actions log instead of vanishing.
    """
    reasons = []
    for backend in (fetch_v2, fetch_embed):
        try:
            obs = backend()
            log.info("Sagamore OK via %s: %s", obs.backend, obs.summary())
            return obs
        except SourceUnavailable as e:
            reasons.append(f"{backend.__name__}: {e}")
            log.warning("Sagamore backend %s unavailable — %s", backend.__name__, e)
    raise SourceUnavailable("; ".join(reasons))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        print(fetch_sagamore().summary())
    except SourceUnavailable as e:
        print(f"UNAVAILABLE — {e}")
        raise SystemExit(1)
