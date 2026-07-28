"""
sagamore.py — Sagamore Yacht Club WeatherLink station fetcher for the WFC monitor.

Pulls live observations from the public JSON endpoint behind SYC's embeddable
WeatherLink page. No API key required.

Returns a normalized dict (knots, degrees, °F) or None if the station is
unreachable or the data is stale — the caller should fall back to NDBC/KPTN6
whenever this returns None.
"""

import json
import time
import urllib.request

SAGAMORE_URL = (
    "https://www.weatherlink.com/embeddablePage/getData/"
    "e9aef99860fc4aecb73f08d0d9cb3e37"
)

# If the station hasn't reported in this many minutes, treat it as offline.
STALE_MINUTES = 15

# Compass names -> degrees, in case the feed reports direction as text.
_COMPASS = {
    "N": 0, "NNE": 22.5, "NE": 45, "ENE": 67.5,
    "E": 90, "ESE": 112.5, "SE": 135, "SSE": 157.5,
    "S": 180, "SSW": 202.5, "SW": 225, "WSW": 247.5,
    "W": 270, "WNW": 292.5, "NW": 315, "NNW": 337.5,
}


def _to_float(v):
    """Best-effort float conversion; returns None on failure."""
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _to_knots(value, unit):
    """Convert a wind speed to knots given the unit label from the feed."""
    v = _to_float(value)
    if v is None:
        return None
    u = (unit or "").lower().strip()
    if "knot" in u or u == "kt" or u == "kts":
        return v
    if "km" in u:                      # km/h
        return v * 0.539957
    if "m/s" in u or "mps" in u:       # meters per second
        return v * 1.943844
    # WeatherLink default for US stations is mph
    return v * 0.868976


def _direction_degrees(v):
    """Direction may arrive as degrees or compass text; normalize to degrees."""
    d = _to_float(v)
    if d is not None:
        return d % 360
    if isinstance(v, str):
        return _COMPASS.get(v.strip().upper())
    return None


def fetch_sagamore(timeout=15):
    """
    Fetch current conditions from the Sagamore YC station.

    Returns dict with keys:
        wind_kn, gust_kn, wind_dir_deg, air_temp_f, observed_epoch, source
    or None if unavailable/stale (caller falls back to NDBC/KPTN6).
    """
    try:
        req = urllib.request.Request(
            SAGAMORE_URL,
            headers={"User-Agent": "WFC-Weather-Monitor (safety operations)"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[sagamore] fetch failed: {e}")
        return None

    try:
        return _parse_payload(payload)
    except Exception as e:
        print(f"[sagamore] parse failed: {e}")
        return None


def _parse_payload(payload):
    """
    WeatherLink embeddable getData payload:
      {
        "lastReceived": <epoch ms>,
        "currConditionValues": [
            {"sensorDataName": "Wind Speed", "convertedValue": "6", "unitLabel": "mph", ...},
            ...
        ],
        "highLowValues": [...]
      }
    Field names vary slightly by station config, so match loosely.
    """
    # --- staleness check -------------------------------------------------
    last_ms = payload.get("lastReceived")
    observed_epoch = None
    if last_ms:
        observed_epoch = float(last_ms) / 1000.0
        age_min = (time.time() - observed_epoch) / 60.0
        if age_min > STALE_MINUTES:
            print(f"[sagamore] data stale ({age_min:.0f} min old) — ignoring")
            return None

    values = payload.get("currConditionValues") or []
    if not values:
        print("[sagamore] no current condition values in payload")
        return None

    def find(*needles, exclude=()):
        """Return (convertedValue, unitLabel) for the first sensor whose
        name contains all needles and none of the excluded words."""
        for item in values:
            name = (item.get("sensorDataName") or "").lower()
            if all(n in name for n in needles) and not any(x in name for x in exclude):
                return item.get("convertedValue"), item.get("unitLabel")
        return None, None

    # Sustained wind: prefer a 10-min or 2-min average over the instantaneous read.
    wind_raw, wind_unit = find("avg", "wind", "speed")
    if wind_raw is None:
        wind_raw, wind_unit = find("wind", "speed", exclude=("high", "gust"))

    # Gust: "10 Min High Wind Speed" on most Davis stations, or anything "gust".
    gust_raw, gust_unit = find("high", "wind", "speed")
    if gust_raw is None:
        gust_raw, gust_unit = find("gust")

    dir_raw, _ = find("wind", "direction")
    temp_raw, temp_unit = find("temp", exclude=("in", "dew", "wind", "feel", "heat"))

    wind_kn = _to_knots(wind_raw, wind_unit)
    gust_kn = _to_knots(gust_raw, gust_unit)
    if wind_kn is None and gust_kn is None:
        print("[sagamore] no usable wind data in payload")
        return None

    # Gust should never read below sustained; fix ordering artifacts.
    if wind_kn is not None and gust_kn is not None and gust_kn < wind_kn:
        gust_kn = wind_kn

    air_temp_f = _to_float(temp_raw)
    if air_temp_f is not None and temp_unit and "c" in temp_unit.lower():
        air_temp_f = air_temp_f * 9 / 5 + 32

    return {
        "wind_kn": round(wind_kn, 1) if wind_kn is not None else None,
        "gust_kn": round(gust_kn, 1) if gust_kn is not None else None,
        "wind_dir_deg": _direction_degrees(dir_raw),
        "air_temp_f": round(air_temp_f, 1) if air_temp_f is not None else None,
        "observed_epoch": observed_epoch,
        "source": "Sagamore YC station (WeatherLink)",
    }


if __name__ == "__main__":
    obs = fetch_sagamore()
    print(json.dumps(obs, indent=2) if obs else "No usable Sagamore data (would fall back to buoys).")
