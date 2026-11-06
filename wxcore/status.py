"""
The status snapshot: one JSON document describing a site right now.

This is the contract between the engine and everything that displays it —
the phone app, a web dashboard, a lock-screen widget, a club's website embed.
Clients render it; they never re-derive a call themselves, so the app can't
disagree with the alerts. See docs/STATUS_FORMAT.md.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from .config import Site
from .engine import (Conditions, Observation, LEVEL_WORD, activity_call, classify_nws,
                     fleet_calls, in_operating_hours, worst)

STATUS_VERSION = 1

COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def compass(d) -> Optional[str]:
    return None if d is None else COMPASS[round((d % 360) / 22.5) % 16]


def _r(v, nd=1):
    return None if v is None else round(v, nd)


def force_stop_reason(cond: Conditions, nws_stop: bool) -> Optional[str]:
    if cond.storm:
        return "thunderstorm overhead"
    if nws_stop:
        return "NWS marine/storm warning"
    return None


def assess(site: Site, cond: Conditions, obs: Optional[Observation], nws_alerts: list,
           now: datetime, forecast_source: Optional[str] = None,
           agreement: Optional[dict] = None) -> dict:
    """Run every rule for the site and return the full snapshot.

    now must be timezone-aware in the site's local zone.
    """
    nws_stop, nws_caution = classify_nws(nws_alerts, site)

    activities = []
    for unit in site.activities:
        r = activity_call(unit, cond, obs, site, nws_stop, nws_caution)
        call = r["call"]
        activities.append({
            "id": unit.id,
            "label": unit.label,
            "level": call.level,
            "word": LEVEL_WORD[call.level],
            "forecast_level": r["forecast_level"],
            "observed_level": r["observed_level"],
            "headline_reasons": call.headline_reasons(),
            "findings": [f.to_dict() for f in call.findings],
        })

    # The fleet panel uses measured wind when a station is reporting,
    # otherwise the forecast — the best data available, same as the email graphic.
    if obs is not None:
        fw, fg, fd, basis = obs.wind_kn, obs.gust_kn, obs.dir_deg, "observed"
    else:
        fw, fg, fd, basis = cond.wind_kn, cond.gust_kn, cond.dir_deg, "forecast"
    fsr = force_stop_reason(cond, nws_stop)
    fleet = [{"id": u.id, "label": u.label, "level": lvl, "word": LEVEL_WORD[lvl], "reason": why}
             for u, lvl, why in fleet_calls(site, fw, fg, fd, fsr)]

    observation = None
    if obs is not None:
        observation = {
            "station": obs.station,
            "age_min": round(obs.age_min),
            "wind_kn": _r(obs.wind_kn),
            "gust_kn": _r(obs.gust_kn),
            "gust_monitored": obs.gust_monitored,
            "peak_gust_kn": _r(obs.peak_gust_kn),
            "dir_deg": _r(obs.dir_deg, 0),
            "dir_compass": compass(obs.dir_deg),
            "offshore": site.is_offshore(obs.dir_deg),
            "exposure_offset_kn": obs.exposure_offset_kn,
        }

    all_levels = [a["level"] for a in activities] + [f["level"] for f in fleet]
    return {
        "status_version": STATUS_VERSION,
        "site": {"id": site.id, "name": site.name, "short_name": site.short_name,
                 "area_name": site.area_name, "timezone": site.timezone,
                 "lat": site.lat, "lon": site.lon},
        "generated_at": now.isoformat(timespec="seconds"),
        "in_operating_hours": in_operating_hours(site, now.hour),
        "overall_level": worst(*all_levels),
        "activities": activities,
        "fleet": fleet,
        "fleet_basis": basis,
        "observation": observation,
        "forecast": {
            "source": forecast_source,
            "wind_kn": _r(cond.wind_kn), "gust_kn": _r(cond.gust_kn),
            "dir_deg": _r(cond.dir_deg, 0), "dir_compass": compass(cond.dir_deg),
            "precip_in_hr": _r(cond.precip_in_hr, 2), "vis_mi": _r(cond.vis_mi),
            "cape": _r(cond.cape, 0), "storm": cond.storm,
            "agreement": agreement,
        },
        "nws_alerts": sorted(set(nws_alerts)),
        "disclaimer": ("Forecast and observation based. Confirm lightning by eye and ear "
                       "and apply the 30-30 rule. This assists the call; it does not make it."),
    }
