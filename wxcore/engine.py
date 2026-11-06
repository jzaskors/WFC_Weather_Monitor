"""
The decision engine: given a site and the conditions, make the GO / CAUTION /
HALT call for every activity and every craft.

Pure functions only — no network, no email, no clock reads. Every caller
(the alert monitor, the status feed, the app's backend) gets the same answer
from the same inputs, and the rules can be tested without the internet.

Ported from monitor.py / render_conditions.py (Nov 2026). tests/test_parity.py
checks it makes the same call as the original across thousands of cases.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .config import Limits, Site, Unit

LEVELS = ("GO", "CAUTION", "STOP")
ORDER = {lvl: i for i, lvl in enumerate(LEVELS)}
LEVEL_WORD = {"GO": "GO", "CAUTION": "CAUTION", "STOP": "HALT"}


def worst(*levels: str) -> str:
    return max(levels, key=ORDER.__getitem__) if levels else "GO"


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
@dataclass
class Conditions:
    """Forecast-model conditions at the site. Any field may be None if the
    model didn't supply it; a missing value never triggers a call."""
    wind_kn: Optional[float] = None
    gust_kn: Optional[float] = None
    dir_deg: Optional[float] = None
    precip_in_hr: Optional[float] = None
    vis_mi: Optional[float] = None
    cape: Optional[float] = None
    storm: bool = False            # thunderstorm weather code overhead


@dataclass
class Observation:
    """A measured reading from one station."""
    wind_kn: float
    gust_kn: Optional[float]
    dir_deg: Optional[float]
    station: str
    age_min: float
    exposure_offset_kn: float = 0
    gust_monitored: bool = True    # False = station doesn't report live gust
    peak_gust_kn: Optional[float] = None


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
@dataclass
class Finding:
    """One reason behind a call.

    code is stable and machine-readable (the app keys icons and translations
    off it); text is the human sentence used in alerts.
    """
    level: str
    code: str
    text: str
    value: Optional[float] = None

    def to_dict(self) -> dict:
        d = {"level": self.level, "code": self.code, "text": self.text}
        if self.value is not None:
            d["value"] = round(self.value, 1)
        return d


@dataclass
class Call:
    level: str = "GO"
    findings: list = field(default_factory=list)

    def add(self, f: Finding) -> None:
        self.findings.append(f)
        self.level = worst(self.level, f.level)

    def extend(self, other: "Call") -> None:
        for f in other.findings:
            self.add(f)

    def headline_reasons(self) -> list:
        """Reasons at the call's own severity — a CAUTION alert lists the
        caution reasons, not the GO-level noise. Falls back to all reasons."""
        top = [f.text for f in self.findings if f.level == self.level]
        return top or [f.text for f in self.findings]


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
def classify_nws(alert_names, site: Site) -> tuple:
    """(stop, caution) from active NWS alert names. A thunderstorm watch is
    caution only when nothing in the list is a hard stop."""
    stop = any(k in a for a in alert_names for k in site.nws.stop)
    caution = (not stop) and any(k in a for a in alert_names for k in site.nws.caution)
    return stop, caution


def hazard_findings(cond: Conditions, site: Site, nws_stop: bool, nws_caution: bool) -> Call:
    """Non-wind hazards; these apply to every activity."""
    c, s = Call(), site.shared
    if cond.storm:
        c.add(Finding("STOP", "thunderstorm", "Thunderstorm overhead — clear the water"))
    if nws_stop:
        c.add(Finding("STOP", "nws_warning", "Active NWS marine/storm warning"))
    if nws_caution:
        c.add(Finding("CAUTION", "nws_thunderstorm",
                      "Severe thunderstorm watch/warning in effect — monitor radar closely"))
    if cond.precip_in_hr is not None:
        if cond.precip_in_hr >= s.rain_stop_in_hr:
            c.add(Finding("STOP", "rain", f"Heavy rain ({cond.precip_in_hr:.2f} in/hr)", cond.precip_in_hr))
        elif cond.precip_in_hr >= s.rain_caution_in_hr:
            c.add(Finding("CAUTION", "rain", f"Rain ({cond.precip_in_hr:.2f} in/hr)", cond.precip_in_hr))
    if cond.vis_mi is not None and cond.vis_mi < s.visibility_stop_mi:
        c.add(Finding("STOP", "visibility", f"Low visibility ({cond.vis_mi:.1f} mi)", cond.vis_mi))
    if cond.cape is not None and cond.cape >= s.cape_watch:
        c.add(Finding("CAUTION", "instability",
                      f"Unstable air (CAPE {round(cond.cape)}) — storms possible", cond.cape))
    return c


def wind_findings(limits: Limits, wind, gust, direction, site: Site) -> Call:
    """Offshore wind, sustained wind, and gusts against one set of limits."""
    c = Call()
    if limits.has_offshore and wind is not None and site.is_offshore(direction):
        if wind >= limits.offshore_stop:
            c.add(Finding("STOP", "offshore_wind",
                          f"Offshore wind {round(wind)} kn — pushes paddlers out", wind))
        elif wind >= limits.offshore_caution:
            c.add(Finding("CAUTION", "offshore_wind", f"Offshore wind {round(wind)} kn", wind))
    if wind is not None:
        if wind >= limits.wind_stop:
            c.add(Finding("STOP", "wind", f"Sustained wind {round(wind)} kn", wind))
        elif wind >= limits.wind_caution:
            c.add(Finding("CAUTION", "wind", f"Sustained wind {round(wind)} kn", wind))
    if gust is not None:
        if gust >= limits.gust_stop:
            c.add(Finding("STOP", "gust", f"Gusts {round(gust)} kn", gust))
        elif gust >= limits.gust_caution:
            c.add(Finding("CAUTION", "gust", f"Gusts {round(gust)} kn", gust))
    return c


def forecast_call(unit: Unit, cond: Conditions, site: Site,
                  nws_stop: bool = False, nws_caution: bool = False) -> Call:
    c = hazard_findings(cond, site, nws_stop, nws_caution)
    c.extend(wind_findings(unit.limits, cond.wind_kn, cond.gust_kn, cond.dir_deg, site))
    return c


def observed_call(unit: Unit, obs: Optional[Observation], site: Site) -> Call:
    """Measured wind through the limits. An exposed station is judged against
    limits raised by its offset; the reported numbers stay the true readings."""
    if obs is None:
        return Call()
    off = obs.exposure_offset_kn
    raw = wind_findings(unit.limits.raised_by(off), obs.wind_kn, obs.gust_kn, obs.dir_deg, site)
    tag = (f" — MEASURED at {obs.station} (judged vs +{off:g} kn exposure-adjusted limits)"
           if off else f" — MEASURED at {obs.station}")
    out = Call()
    for f in raw.findings:
        out.add(Finding(f.level, f.code, f.text + tag, f.value))
    return out


def activity_call(unit: Unit, cond: Conditions, obs: Optional[Observation], site: Site,
                  nws_stop: bool = False, nws_caution: bool = False) -> dict:
    """Forecast and measured calls combined — worst case wins, so a measured
    exceedance raises the call even when the models say calm."""
    f = forecast_call(unit, cond, site, nws_stop, nws_caution)
    o = observed_call(unit, obs, site)
    combined = Call()
    combined.extend(f)
    combined.extend(o)
    return {"call": combined, "forecast_level": f.level, "observed_level": o.level}


_SHORT = {"offshore_wind": "offshore {v} kn", "wind": "wind {v} kn", "gust": "gusts {v} kn"}


def craft_call(unit: Unit, wind, gust, direction, site: Site,
               force_stop_reason: Optional[str] = None) -> tuple:
    """(level, short reason) for one craft, from the best available wind.
    Lightning or a hard NWS warning halts every craft regardless of wind."""
    if force_stop_reason:
        return "STOP", force_stop_reason
    c = wind_findings(unit.limits, wind, gust, direction, site)
    if c.level == "GO":
        return "GO", ""
    first = next(f for f in c.findings if f.level == c.level)
    return c.level, _SHORT[first.code].format(v=round(first.value))


def fleet_calls(site: Site, wind, gust, direction, force_stop_reason=None) -> list:
    return [(u, *craft_call(u, wind, gust, direction, site, force_stop_reason))
            for u in site.fleet]


# ---------------------------------------------------------------------------
# Alert transitions
# ---------------------------------------------------------------------------
def is_alert_level(level: str, site: Site) -> bool:
    return level == "STOP" or (site.alert_on_caution and level == "CAUTION")


def transition(prev: str, new: str, site: Site) -> Optional[str]:
    """'alert' when entering an alert level or escalating (GO->CAUTION,
    GO->STOP, CAUTION->STOP); 'clear' when fully back out; else None."""
    if is_alert_level(new, site) and (not is_alert_level(prev, site) or ORDER[new] > ORDER[prev]):
        return "alert"
    if not is_alert_level(new, site) and is_alert_level(prev, site):
        return "clear"
    return None


def advance(record: dict, computed: str, site: Site, can_step_down: bool = True) -> tuple:
    """Step one activity's stored state forward by one run.

    record is {"level": stored level, "below_runs": consecutive runs below it,
    "below_peak": the worst level seen during those runs}. Returns
    (new_record, action); action is 'alert', 'ease', 'clear' or None.

    Going UP is immediate: any escalation is acted on this run.

    Going DOWN needs site.clear_confirm_runs consecutive runs below the stored
    level, and then steps down only to the WORST level seen in that window.
    HALT -> CAUTION -> GO therefore eases to CAUTION first, and needs a full
    window of GO before an all-clear. This stops wind hovering at a limit from
    flapping, and gives a storm time to move off before an all-clear goes out.

    can_step_down=False (some check failed this run) holds the stored level
    without counting the run toward the window. Missing data never clears.

    'ease' = stepped down but still at an alert level (HALT -> CAUTION).
    """
    stored = record.get("level", "GO")
    if stored not in ORDER:
        stored = "GO"
    held = {"level": stored, "below_runs": 0, "below_peak": "GO"}
    if ORDER[computed] > ORDER[stored]:
        return {"level": computed, "below_runs": 0, "below_peak": "GO"}, \
            transition(stored, computed, site)
    if computed == stored:
        return held, None
    if not can_step_down:
        return dict(record, level=stored), None
    below = record.get("below_runs", 0)
    below = below + 1 if isinstance(below, int) and below >= 0 else 1
    prev_peak = record.get("below_peak") if record.get("below_peak") in ORDER else "GO"
    peak = worst(computed, prev_peak) if below > 1 else computed
    if below < site.clear_confirm_runs:
        return {"level": stored, "below_runs": below, "below_peak": peak}, None
    action = transition(stored, peak, site)
    if action is None and is_alert_level(peak, site) and ORDER[peak] < ORDER[stored]:
        action = "ease"
    return {"level": peak, "below_runs": 0, "below_peak": "GO"}, action


def in_operating_hours(site: Site, local_hour: int) -> bool:
    return site.open_hour <= local_hour < site.close_hour
