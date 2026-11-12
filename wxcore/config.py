"""
Site configuration: everything that differs from one club to the next.

A site is one JSON file in sites/. The engine never hard-codes a location,
threshold, or station — it reads them from here, so adding a second club
means adding a second file, not editing code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

SCHEMA_VERSION = 1


class SiteConfigError(ValueError):
    """The site file is missing something or contradicts itself."""


@dataclass(frozen=True)
class Limits:
    """Wind limits in knots for one activity or one craft.

    offshore_* apply only when the wind blows from one of the site's offshore
    arcs, and only for craft that can be blown out and not get back (paddle
    craft). Leave them out for keelboats.
    """
    wind_caution: float
    wind_stop: float
    gust_caution: float
    gust_stop: float
    offshore_caution: Optional[float] = None
    offshore_stop: Optional[float] = None

    @property
    def has_offshore(self) -> bool:
        return self.offshore_stop is not None

    def raised_by(self, offset_kn: float) -> "Limits":
        """Every limit raised by offset_kn — used for exposed stations
        (buoys, coastal sites) that read windier than the sheltered launch."""
        if not offset_kn:
            return self
        bump = lambda v: None if v is None else v + offset_kn
        return replace(self,
                       wind_caution=self.wind_caution + offset_kn,
                       wind_stop=self.wind_stop + offset_kn,
                       gust_caution=self.gust_caution + offset_kn,
                       gust_stop=self.gust_stop + offset_kn,
                       offshore_caution=bump(self.offshore_caution),
                       offshore_stop=bump(self.offshore_stop))


@dataclass(frozen=True)
class Unit:
    """Something that gets its own GO / CAUTION / HALT call:
    an activity (alerted on) or a craft in the fleet (shown in the status)."""
    id: str
    label: str
    limits: Limits


@dataclass(frozen=True)
class Arc:
    """Wind directions (degrees FROM, clockwise) that blow craft off the launch."""
    start: float
    end: float

    def contains(self, deg: Optional[float]) -> bool:
        if deg is None:
            return False
        d = (deg % 360 + 360) % 360
        a, b = self.start, self.end
        return (a <= d <= b) if a <= b else (d >= a or d <= b)


@dataclass(frozen=True)
class SharedLimits:
    """Hazards that apply to every activity regardless of wind."""
    rain_caution_in_hr: float = 0.10
    rain_stop_in_hr: float = 0.30
    cape_watch: float = 1000
    visibility_stop_mi: float = 0.5
    # Measured lightning (from a station with a lightning sensor). A strike
    # within lightning_stop_mi halts everything until lightning_hold_min
    # minutes after the LAST strike inside that radius. None = rule off.
    lightning_stop_mi: Optional[float] = None
    lightning_hold_min: float = 30


@dataclass(frozen=True)
class NwsRouting:
    """Which NWS alert names halt operations and which only call for caution.
    Matching is substring-based, same as the original monitor."""
    stop: tuple = ()
    caution: tuple = ()


@dataclass(frozen=True)
class ObservationSource:
    """A live station, in priority order. exposure_offset_kn > 0 means the
    station sits in more exposed water than the launch."""
    id: str
    type: str
    label: str
    exposure_offset_kn: float = 0
    max_age_min: float = 90
    has_live_gust: bool = True
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ForecastModel:
    name: str
    open_meteo_id: str
    label: str


@dataclass(frozen=True)
class Site:
    id: str
    name: str
    short_name: str
    area_name: str
    lat: float
    lon: float
    timezone: str
    open_hour: int
    close_hour: int
    alert_on_caution: bool
    clear_confirm_runs: int
    health_alert_after_runs: int
    nws_marine_zones: tuple
    offshore_arcs: tuple
    shared: SharedLimits
    nws: NwsRouting
    activities: tuple
    fleet: tuple
    observation_sources: tuple
    forecast_models: tuple

    def is_offshore(self, deg: Optional[float]) -> bool:
        return any(arc.contains(deg) for arc in self.offshore_arcs)

    def activity(self, unit_id: str) -> Unit:
        for u in self.activities:
            if u.id == unit_id:
                return u
        raise KeyError(unit_id)


# ---------------------------------------------------------------------------
# Loading + validation
# ---------------------------------------------------------------------------
def _limits(raw: dict, where: str) -> Limits:
    try:
        lim = Limits(**raw)
    except TypeError as e:
        raise SiteConfigError(f"{where}: {e}") from None
    pairs = [("wind", lim.wind_caution, lim.wind_stop),
             ("gust", lim.gust_caution, lim.gust_stop)]
    if (lim.offshore_caution is None) != (lim.offshore_stop is None):
        raise SiteConfigError(f"{where}: give both offshore_caution and offshore_stop, or neither")
    if lim.has_offshore:
        pairs.append(("offshore", lim.offshore_caution, lim.offshore_stop))
    for name, cau, stop in pairs:
        if cau > stop:
            raise SiteConfigError(f"{where}: {name}_caution ({cau}) is above {name}_stop ({stop})")
    return lim


def _units(raw_list: list, kind: str) -> tuple:
    seen, out = set(), []
    for i, u in enumerate(raw_list):
        uid = u.get("id")
        if not uid:
            raise SiteConfigError(f"{kind}[{i}] has no id")
        if uid in seen:
            raise SiteConfigError(f"{kind}: duplicate id '{uid}'")
        seen.add(uid)
        out.append(Unit(uid, u.get("label", uid), _limits(u["limits"], f"{kind} '{uid}'")))
    return tuple(out)


def _positive_int(d: dict, key: str, default: int) -> int:
    v = d.get(key, default)
    if not isinstance(v, int) or isinstance(v, bool) or v < 1:
        raise SiteConfigError(f"{key} must be a whole number of at least 1 (got {v!r})")
    return v


_SOURCE_KEYS = {"id", "type", "label", "exposure_offset_kn", "max_age_min", "has_live_gust",
                "enabled", "note"}


def site_from_dict(d: dict) -> Site:
    v = d.get("schema_version", SCHEMA_VERSION)
    if v != SCHEMA_VERSION:
        raise SiteConfigError(f"site schema_version {v} not supported (expected {SCHEMA_VERSION})")
    for key in ("id", "name", "location", "timezone", "activities"):
        if key not in d:
            raise SiteConfigError(f"site file is missing '{key}'")

    hours = d.get("operating_hours", {"open": 0, "close": 24})
    if not (0 <= hours["open"] < hours["close"] <= 24):
        raise SiteConfigError("operating_hours must satisfy 0 <= open < close <= 24")

    sources = []
    for s in d.get("observation_sources", []):
        # "enabled": false keeps a station on file without polling it (e.g. a
        # sensor that isn't mounted yet). It is dropped here, so it never shows
        # up in alerts as a "skipped" backup.
        if s.get("enabled", True) is False:
            continue
        sources.append(ObservationSource(
            id=s["id"], type=s["type"], label=s.get("label", s["id"]),
            exposure_offset_kn=s.get("exposure_offset_kn", 0),
            max_age_min=s.get("max_age_min", 90),
            has_live_gust=s.get("has_live_gust", True),
            params={k: v for k, v in s.items() if k not in _SOURCE_KEYS}))

    nws = d.get("nws_routing", {})
    return Site(
        id=d["id"], name=d["name"],
        short_name=d.get("short_name", d["name"]),
        area_name=d.get("area_name", d["name"]),
        lat=d["location"]["lat"], lon=d["location"]["lon"],
        timezone=d["timezone"],
        open_hour=hours["open"], close_hour=hours["close"],
        alert_on_caution=d.get("alert_on_caution", False),
        clear_confirm_runs=_positive_int(d, "clear_confirm_runs", 1),
        health_alert_after_runs=_positive_int(d, "health_alert_after_runs", 3),
        nws_marine_zones=tuple(d.get("nws_marine_zones", ())),
        offshore_arcs=tuple(Arc(a["from"], a["to"]) for a in d.get("offshore_arcs", [])),
        shared=SharedLimits(**d.get("shared_limits", {})),
        nws=NwsRouting(tuple(nws.get("stop", ())), tuple(nws.get("caution", ()))),
        activities=_units(d["activities"], "activities"),
        fleet=_units(d.get("fleet", []), "fleet"),
        observation_sources=tuple(sources),
        forecast_models=tuple(ForecastModel(**m) for m in d.get("forecast_models", [])),
    )


def load_site(path) -> Site:
    with open(Path(path)) as f:
        return site_from_dict(json.load(f))
