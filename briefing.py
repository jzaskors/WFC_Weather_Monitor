#!/usr/bin/env python3
"""
briefing.py — the WFC storm briefing.

PURPOSE: protecting boats, docks and gear. This is NOT the go / no-go monitor
(monitor.py does that every 10 minutes). The briefing looks up to three days
ahead and emails a short heads-up when the forecast models AGREE that sustained
wind in West Harbor will reach the briefing threshold (22 kn), so there is
time to secure the moorings, floats, racks and boats before it arrives.

How it decides
  - Pulls the hourly forecast for the dock from every model listed under
    "briefing" in sites/wfc.json (HRRR, NAM, GFS, ECMWF, AIFS, ICON, NBM ...).
    These are point forecasts for West Harbor itself, not the open Sound.
  - An hour counts as "agreed" when at least agree_fraction of the models
    that cover that hour show sustained wind at or above the threshold.
  - Agreed hours close together form one EVENT with a start, peak and end.
  - Wave height in West Harbor is ESTIMATED from the forecast wind and the
    open-water distance (fetch) in that direction. No model resolves the
    harbor, so treat it as a rough guide.

Ways to run it
  python briefing.py --due       exit 0 if a scheduled check is due now, else 1
  python briefing.py --auto      scheduled check. Silent unless a storm is
                                 agreed: then ONE email per storm, at most one
                                 update if it gets materially worse, and a
                                 stand-down note if it drops out before it
                                 arrives
  python briefing.py --backtest  how often the rule would have fired over the
                                 past year, for a range of levels (no email)
  python briefing.py --preview   always email the current picture, marked
                                 PREVIEW; changes nothing
  python briefing.py --print     print the analysis, send nothing

State lives under the "briefing" key of state.json (monitor.py preserves it).
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
SITE_FILE = Path(os.environ.get("SITE_FILE") or ROOT / "sites" / "wfc.json")
STATE_FILE = ROOT / "state.json"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_PAST = "https://historical-forecast-api.open-meteo.com/v1/forecast"

COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]

DEFAULTS = {
    "wind_kn": 22, "min_hours": 3, "horizon_hours": 72, "agree_fraction": 0.6, "min_models": 2,
    "check_hours": [6, 15], "lead_hours": 6, "wave_flag_ft": 2.0, "merge_gap_hours": 3,
    "models": [
        {"name": "HRRR", "id": "ncep_hrrr_conus"},
        {"name": "NAM", "id": "ncep_nam_conus"},
        {"name": "GFS", "id": "gfs_seamless"},
        {"name": "ECMWF", "id": "ecmwf_ifs025"},
        {"name": "AIFS", "id": "ecmwf_aifs025_single"},
        {"name": "ICON", "id": "icon_seamless"},
        {"name": "NBM", "id": "ncep_nbm_conus"},
    ],
    # Open-water distance from the docks, nautical miles, by the direction the
    # wind comes FROM. Estimates: correct them from local knowledge.
    "fetch_nm": {"N": 1.6, "NNE": 1.2, "NE": 1.5, "ENE": 2.5, "E": 1.2, "ESE": 0.5,
                 "SE": 0.2, "SSE": 0.2, "S": 0.1, "SSW": 0.1, "SW": 0.1, "WSW": 0.1,
                 "W": 0.2, "WNW": 0.3, "NW": 0.5, "NNW": 0.9},
    "assets": [],
}


def compass(d) -> str:
    return "—" if d is None else COMPASS[round((d % 360) / 22.5) % 16]


def load_config() -> tuple:
    raw = json.loads(SITE_FILE.read_text())
    cfg = dict(DEFAULTS)
    cfg.update(raw.get("briefing") or {})
    return raw, cfg


# ============================================================================
# Pure analysis (no network, no clock): tested offline
# ============================================================================
def circ_mean(degs: list) -> Optional[float]:
    vals = [d for d in degs if d is not None]
    if not vals:
        return None
    x = sum(math.sin(math.radians(d)) for d in vals)
    y = sum(math.cos(math.radians(d)) for d in vals)
    if abs(x) < 1e-9 and abs(y) < 1e-9:
        return None
    return math.degrees(math.atan2(x, y)) % 360


def median(vals: list) -> Optional[float]:
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def fetch_for(direction: Optional[float], fetch_nm: dict) -> float:
    """Fetch (nm) for a wind direction, interpolated between the 16 points."""
    if direction is None:
        return 0.0
    pos = (direction % 360) / 22.5
    i = int(math.floor(pos)) % 16
    frac = pos - math.floor(pos)
    a = float(fetch_nm.get(COMPASS[i], 0) or 0)
    b = float(fetch_nm.get(COMPASS[(i + 1) % 16], 0) or 0)
    return a + (b - a) * frac


def wave_ft(wind_kn: Optional[float], fetch_nm: float) -> Optional[float]:
    """Fetch-limited significant wave height, feet (deep-water SMB/JONSWAP
    growth curve). A harbor estimate from wind and fetch only: no swell, no
    shoaling, no reflection off bulkheads."""
    if wind_kn is None or wind_kn <= 0 or fetch_nm <= 0:
        return 0.0 if wind_kn is not None else None
    g, u, f = 9.81, wind_kn * 0.514444, fetch_nm * 1852.0
    hs = 0.0016 * u * math.sqrt(f / g)
    hs = min(hs, 0.2433 * u * u / g)            # fully-developed cap
    return hs * 3.28084


def analyze(times: list, models: dict, cfg: dict) -> dict:
    """times: list of aware datetimes (hourly). models: {name: {"wind": [...],
    "gust": [...], "dir": [...]}} aligned to times, None where a model has no
    value. Returns the consensus series, per-model summary and agreed events."""
    thr, frac, min_models = cfg["wind_kn"], cfg["agree_fraction"], cfg["min_models"]
    n = len(times)
    cons_w, cons_g, cons_d, lo, hi, agree, covered, waves = [], [], [], [], [], [], [], []
    for i in range(n):
        w = [m["wind"][i] for m in models.values() if m["wind"][i] is not None]
        g = [m["gust"][i] for m in models.values() if m["gust"][i] is not None]
        d = [m["dir"][i] for m in models.values() if m["wind"][i] is not None]
        cw, cd = median(w), circ_mean(d)
        cons_w.append(cw); cons_g.append(median(g)); cons_d.append(cd)
        lo.append(min(w) if w else None); hi.append(max(w) if w else None)
        covered.append(len(w))
        over = sum(1 for x in w if x >= thr)
        agree.append(len(w) >= min_models and over >= math.ceil(len(w) * frac - 1e-9) and over > 0)
        waves.append(wave_ft(cw, fetch_for(cd, cfg["fetch_nm"])))

    # contiguous agreed hours -> events, merging short gaps
    events, start = [], None
    for i in range(n + 1):
        on = i < n and agree[i]
        if on and start is None:
            start = i
        if not on and start is not None:
            events.append([start, i - 1]); start = None
    merged = []
    for ev in events:
        if merged and ev[0] - merged[-1][1] - 1 <= cfg["merge_gap_hours"]:
            merged[-1][1] = ev[1]
        else:
            merged.append(ev)
    # a storm, not a passing gust front: it has to last
    merged = [ev for ev in merged if ev[1] - ev[0] + 1 >= cfg.get("min_hours", 1)]

    out_events = []
    for a, b in merged:
        idx = range(a, b + 1)
        pk = max(idx, key=lambda i: cons_w[i] or 0)
        gusts = [cons_g[i] for i in idx if cons_g[i] is not None]
        wv = [(waves[i] or 0, i) for i in idx]
        top = max(max((m["wind"][i] or 0) for m in models.values()) for i in idx)
        top_g = max(max((m["gust"][i] or 0) for m in models.values()) for i in idx)
        out_events.append({
            "start": times[a], "end": times[b] + timedelta(hours=1), "hours": b - a + 1,
            "i0": a, "i1": b,
            "peak_kn": cons_w[pk], "peak_time": times[pk],
            "peak_gust_kn": max(gusts) if gusts else None,
            "top_model_kn": top, "top_model_gust_kn": top_g or None,
            "dir": circ_mean([cons_d[i] for i in idx]),
            "wave_ft": max(wv)[0], "wave_time": times[max(wv)[1]],
        })

    per_model = []
    for name, m in models.items():
        w = [x for x in m["wind"] if x is not None]
        g = [x for x in m["gust"] if x is not None]
        over = [i for i, x in enumerate(m["wind"]) if x is not None and x >= thr]
        per_model.append({
            "name": name, "peak_kn": max(w) if w else None, "peak_gust_kn": max(g) if g else None,
            "hours_over": len(over), "first_over": times[over[0]] if over else None,
            "hours_covered": len(w)})

    split = [times[i] for i in range(n) if not agree[i]
             and any((m["wind"][i] or 0) >= thr for m in models.values())]
    return {"times": times, "wind": cons_w, "gust": cons_g, "dir": cons_d, "lo": lo, "hi": hi,
            "agree": agree, "covered": covered, "wave": waves, "events": out_events,
            "per_model": per_model, "split_hours": split,
            # the FIRST storm is the one to act on; later ones are listed after it
            "main": out_events[0] if out_events else None}


def secure_by(start: datetime, lead_hours: float) -> datetime:
    """When to have everything secured: lead_hours before onset, pulled back
    to 5 PM if that would land overnight (nobody secures a float at 2 AM)."""
    t = start - timedelta(hours=lead_hours)
    if t.hour < 7:
        t = (t - timedelta(days=1)).replace(hour=17, minute=0)
    elif t.hour >= 19:
        t = t.replace(hour=17, minute=0)
    return t


def assets_to_secure(ev: dict, an: dict, cfg: dict) -> list:
    """Asset lines whose trigger is met during the event. A trigger may set
    gust_kn, wind_kn and/or wave_ft, plus an optional list of directions."""
    out = []
    idx = range(ev["i0"], ev["i1"] + 1)
    for a in cfg.get("assets") or []:
        t = a.get("when") or {}
        dirs = set(t.get("dirs") or [])
        hit = False
        for i in idx:
            if dirs and compass(an["dir"][i]) not in dirs:
                continue
            ok = True
            if "wind_kn" in t and (an["wind"][i] or 0) < t["wind_kn"]: ok = False
            if "gust_kn" in t and (an["gust"][i] or 0) < t["gust_kn"]: ok = False
            if "wave_ft" in t and (an["wave"][i] or 0) < t["wave_ft"]: ok = False
            if ok:
                hit = True
                break
        if hit:
            out.append((a.get("label", "Asset"), a.get("action", "")))
    return out


def event_signature(ev: Optional[dict]) -> Optional[dict]:
    if ev is None:
        return None
    return {"start": ev["start"].isoformat(timespec="minutes"),
            "end": ev["end"].isoformat(timespec="minutes"), "peak": round(ev["peak_kn"] or 0)}


def _t(s) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def same_storm(prev: Optional[dict], cur: Optional[dict], slack_hours: float = 12) -> bool:
    """True when the storm in today's forecast is the one already briefed:
    the two time windows overlap, allowing for the forecast sliding a bit."""
    if not prev or not cur:
        return False
    ps, pe, cs, ce = _t(prev.get("start")), _t(prev.get("end")), _t(cur.get("start")), _t(cur.get("end"))
    if None in (ps, pe, cs, ce):
        return False
    slack = timedelta(hours=slack_hours)
    return cs <= pe + slack and ce >= ps - slack


def materially_worse(prev: dict, cur: dict) -> bool:
    """Worth a second email: a clearly stronger peak, or arriving much sooner."""
    ps, cs = _t(prev.get("start")), _t(cur.get("start"))
    sooner = ps is not None and cs is not None and (ps - cs) >= timedelta(hours=6)
    return cur.get("peak", 0) >= prev.get("peak", 0) + 5 or sooner


# ============================================================================
# Wording
# ============================================================================
def fmt_when(t: datetime, now: datetime) -> str:
    """'Fri 2 AM', 'Thu noon', 'Sat midnight' — always the weekday, so an
    email read the next morning still means what it said."""
    if t.minute == 0 and t.hour == 12:
        hour = "noon"
    elif t.minute == 0 and t.hour == 0:
        hour = "midnight"
    else:
        hour = t.strftime("%-I %p") if t.minute == 0 else t.strftime("%-I:%M %p")
    return f"{t.strftime('%a')} {hour}"


def r(v) -> str:
    return "—" if v is None else f"{round(v)}"


def build_bullets(an: dict, cfg: dict, now: datetime) -> tuple:
    """(headline, [bullet, ...]) for the main event, or a quiet summary."""
    ev, thr = an["main"], cfg["wind_kn"]
    n_models = len(an["per_model"])
    if ev is None:
        peak = max((x for x in an["wind"] if x is not None), default=None)
        i = an["wind"].index(peak) if peak is not None else None
        head = f"No {thr}+ kn sustained wind agreed for West Harbor in the next {cfg['horizon_hours']} hours"
        bullets = []
        if peak is not None:
            bullets.append(f"Strongest wind the models agree on: {r(peak)} kn from the "
                           f"{compass(an['dir'][i])}, {fmt_when(an['times'][i], now)}.")
        lone = [m for m in an["per_model"] if m["hours_over"]]
        if lone:
            bullets.append("Not agreed, but worth watching: " + ", ".join(
                f"{m['name']} reaches {r(m['peak_kn'])} kn" for m in lone) +
                f" ({len(lone)} of {n_models} models).")
        return head, bullets

    over = [m for m in an["per_model"] if m["hours_over"]]
    under = [m for m in an["per_model"] if not m["hours_over"]]
    d = compass(ev["dir"])
    gust = f", gusts to {r(ev['peak_gust_kn'])}" if ev["peak_gust_kn"] else ""
    head = (f"{d} wind {thr}+ kn sustained in West Harbor: "
            f"{fmt_when(ev['start'], now)} to {fmt_when(ev['end'], now)}")
    sb = secure_by(ev["start"], cfg["lead_hours"])
    bullets = [
        f"What: {d} wind peaking near {r(ev['peak_kn'])} kn sustained{gust}. "
        f"The strongest model shows {r(ev['top_model_kn'])} kn.",
        f"Starts: {fmt_when(ev['start'], now)} (first hour the models agree on {thr}+ kn).",
        f"Peaks: {fmt_when(ev['peak_time'], now)}.",
        f"Ends: {fmt_when(ev['end'], now)}, after about {ev['hours']} hours at or above {thr} kn.",
    ]
    wave = ev["wave_ft"]
    if wave is not None:
        flag = " That is above your flag level." if wave >= cfg["wave_flag_ft"] else ""
        bullets.append(f"Waves in West Harbor: about {wave:.1f} ft at the peak, estimated from wind and "
                       f"open water to the {d}.{flag}")
    agree = f"Model agreement: {len(over)} of {n_models} models reach {thr}+ kn"
    if under:
        agree += "; " + ", ".join(f"{m['name']} tops out at {r(m['peak_kn'])}" for m in under)
    bullets.append(agree + ".")
    if sb > now:
        bullets.append(f"Have everything secured by {fmt_when(sb, now)}.")
    else:
        bullets.append("Onset is close: secure what you can now.")
    if len(an["events"]) > 1:
        others = [e for e in an["events"] if e is not ev]
        bullets.append("After that: " + "; ".join(
            f"another {thr}+ kn spell {fmt_when(e['start'], now)} to {fmt_when(e['end'], now)}, "
            f"peaking near {r(e['peak_kn'])} kn" for e in others) + ".")
    return head, bullets


# ============================================================================
# Graphic
# ============================================================================
def render_png(an: dict, cfg: dict, now: datetime, headline: str, tag: str = "") -> Optional[bytes]:
    try:
        import io
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle
    except Exception as e:
        print(f"graphic skipped (matplotlib unavailable: {e})")
        return None

    NAVY, RED, MIST = "#071D49", "#C10016", "#C0CFD7"
    INK, MUTED, GRID, BG = "#14212b", "#5a6b78", "#e3e8ec", "#ffffff"
    WIND, GUST, BAND, WAVE = "#1a66ad", "#cf6310", "#d5e3ef", "#7fa8c9"
    times, thr = an["times"], cfg["wind_kn"]
    x = [(t - times[0]).total_seconds() / 3600 for t in times]
    num = lambda s: [float("nan") if v is None else v for v in s]

    fig = plt.figure(figsize=(8, 6.4), dpi=150, facecolor=BG)
    gs = fig.add_gridspec(3, 1, height_ratios=[0.55, 3.0, 1.15], hspace=0.34,
                          left=0.08, right=0.975, top=0.985, bottom=0.085)

    ax = fig.add_subplot(gs[0]); ax.axis("off"); ax.set_xlim(0, 1); ax.set_ylim(0, 1)
    ax.add_patch(Rectangle((0, 0), 1, 1, color=NAVY, transform=ax.transAxes, clip_on=False))
    ax.add_patch(Rectangle((0, 0), 0.012, 1, color=RED, transform=ax.transAxes, clip_on=False))
    ax.text(0.035, 0.66, "WFC STORM BRIEFING", color="#ffffff", fontsize=14, fontweight="bold", va="center")
    ax.text(0.035, 0.26, headline[:98], color="#dfe7ee", fontsize=8.4, va="center")
    if tag:
        ax.text(0.965, 0.66, tag, color="#ffd9a8", fontsize=10, fontweight="bold", va="center", ha="right")

    ax = fig.add_subplot(gs[1]); ax.set_facecolor(BG)
    top_val = max([v for v in an["hi"] + an["gust"] if v is not None] + [thr + 6])
    ymax = math.ceil((top_val + 5) / 5) * 5
    ax.set_xlim(x[0], x[-1]); ax.set_ylim(0, ymax)
    for ev in an["events"]:
        ax.axvspan(x[ev["i0"]], x[ev["i1"]] + 1, color="#fbeaea", zorder=0)
        ax.text((x[ev["i0"]] + x[ev["i1"]] + 1) / 2, ymax * 0.985, f"{thr}+ KN AGREED", color=RED,
                fontsize=7.2, fontweight="bold", ha="center", va="top")
    for y in range(10, int(ymax), 10):
        ax.axhline(y, color=GRID, lw=0.8, zorder=1)
    ax.fill_between(x, num(an["lo"]), num(an["hi"]), color=BAND, zorder=2, lw=0)
    ax.plot(x, num(an["gust"]), color=GUST, lw=1.6, ls=(0, (4, 2)), zorder=4)
    ax.plot(x, num(an["wind"]), color=WIND, lw=2.2, zorder=5)
    ax.axhline(thr, color=INK, lw=0.9, ls=(0, (5, 3)), zorder=3)
    ax.text(x[-1] - 0.4, thr + ymax * 0.012, f"Briefing level: {thr} kn sustained", color=INK,
            fontsize=7.2, ha="right", va="bottom")
    # day dividers and labels
    ticks, labels = [], []
    for xi, t in zip(x, times):
        if t.hour == 0:
            ax.axvline(xi, color=MIST, lw=1.0, zorder=1)
        if t.hour % 6 == 0:
            ticks.append(xi)
            labels.append(t.strftime("%a\n12 AM") if t.hour == 0 else t.strftime("%-I %p"))
    ax.set_xticks(ticks); ax.set_xticklabels(labels, fontsize=7.4, color=INK)
    ax.set_yticks(range(0, int(ymax) + 1, 10))
    ax.set_yticklabels([f"{v}" if v < ymax - 9 else f"{v} kn" for v in range(0, int(ymax) + 1, 10)],
                       fontsize=7.4, color=MUTED)
    ax.tick_params(length=0)
    for s in ax.spines.values():
        s.set_visible(False)
    # direction arrows every 6 hours (arrow points where the wind is going)
    for xi, t, d in zip(x, times, an["dir"]):
        if t.hour % 6 == 3 and d is not None and x[0] + 1 < xi < x[-1] - 1:
            to = math.radians((d + 180) % 360)
            L = (x[-1] - x[0]) * 0.016
            dx, dy = math.sin(to) * L, math.cos(to) * L * (ymax / (x[-1] - x[0])) * 2.3
            ax.annotate("", xy=(xi + dx, ymax * 0.085 + dy), xytext=(xi - dx, ymax * 0.085 - dy),
                        arrowprops=dict(arrowstyle="-|>", color=NAVY, lw=1.1, mutation_scale=8), zorder=6)
            ax.text(xi, ymax * 0.008, compass(d), color=MUTED, fontsize=6.4, ha="center", va="bottom", zorder=6)
    ax.text(0.0, 1.085, "WIND IN WEST HARBOR", color=NAVY, fontsize=8.6, fontweight="bold",
            va="bottom", transform=ax.transAxes)
    key = [(WIND, "sustained, model consensus", "-"), (GUST, "gusts, model consensus", (0, (4, 2)))]
    for i, (c, lab, ls) in enumerate(key):
        x0 = 0.0 + i * 0.3
        ax.plot([x0, x0 + 0.035], [1.035, 1.035], color=c, lw=2, ls=ls, transform=ax.transAxes, clip_on=False)
        ax.text(x0 + 0.045, 1.035, lab, color=INK, fontsize=7.2, va="center", transform=ax.transAxes)
    ax.add_patch(Rectangle((0.6, 1.02), 0.035, 0.03, color=BAND, transform=ax.transAxes, clip_on=False))
    ax.text(0.645, 1.035, "range across all models", color=INK, fontsize=7.2, va="center", transform=ax.transAxes)

    ax = fig.add_subplot(gs[2]); ax.set_facecolor(BG)
    wv = num(an["wave"])
    wmax = max([v for v in an["wave"] if v is not None] + [cfg["wave_flag_ft"] + 0.5])
    ax.set_xlim(x[0], x[-1]); ax.set_ylim(0, math.ceil(wmax + 0.6))
    for ev in an["events"]:
        ax.axvspan(x[ev["i0"]], x[ev["i1"]] + 1, color="#fbeaea", zorder=0)
    ax.fill_between(x, 0, wv, color=WAVE, zorder=2, lw=0)
    ax.axhline(cfg["wave_flag_ft"], color=INK, lw=0.9, ls=(0, (5, 3)), zorder=3)
    ax.text(x[-1] - 0.4, cfg["wave_flag_ft"] + 0.06, f"Flag level: {cfg['wave_flag_ft']:g} ft", color=INK,
            fontsize=7.2, ha="right", va="bottom")
    for xi, t in zip(x, times):
        if t.hour == 0:
            ax.axvline(xi, color=MIST, lw=1.0, zorder=1)
    ax.set_xticks(ticks); ax.set_xticklabels([l.split("\n")[-1] for l in labels], fontsize=7.4, color=INK)
    yt = list(range(0, int(math.ceil(wmax + 0.6)) + 1))
    ax.set_yticks(yt); ax.set_yticklabels([f"{v}" if v != yt[-1] else f"{v} ft" for v in yt], fontsize=7.4, color=MUTED)
    ax.tick_params(length=0)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.text(0.0, 1.06, "WAVE HEIGHT IN WEST HARBOR", color=NAVY, fontsize=8.6, fontweight="bold",
            va="bottom", transform=ax.transAxes)
    ax.text(0.36, 1.07, "rough estimate from forecast wind and open-water distance", color=MUTED,
            fontsize=7.2, va="bottom", transform=ax.transAxes)
    fig.text(0.08, 0.02, f"Point forecasts for the WFC dock from {len(an['per_model'])} models. "
             f"Prepared {now.strftime('%a %b %-d, %-I:%M %p')}.", color=MUTED, fontsize=6.8, va="bottom")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    plt.close(fig)
    return buf.getvalue()


# ============================================================================
# Email body
# ============================================================================
def esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_email(headline, bullets, assets, an, cfg, now, has_image, note="") -> tuple:
    thr = cfg["wind_kn"]
    rows = [(m["name"], r(m["peak_kn"]), r(m["peak_gust_kn"]),
             f"{m['hours_over']} h" if m["hours_over"] else "none",
             fmt_when(m["first_over"], now) if m["first_over"] else "—") for m in an["per_model"]]
    text = headline + "\n\n" + "\n".join(f"  • {b}" for b in bullets)
    if assets:
        text += "\n\nSecure before it arrives:\n" + "\n".join(f"  [ ] {l}: {a}" for l, a in assets)
    text += f"\n\nModel by model (peak sustained / peak gust / hours at {thr}+ kn / first hour):\n"
    text += "\n".join(f"  {n:<6} {p:>3} kn / {g:>3} kn / {h:>5} / {f}" for n, p, g, h, f in rows)
    if note:
        text += "\n\n" + note
    text += ("\n\nAutomated briefing from public forecast models for the WFC dock. It is a planning "
             "aid, not a forecaster: check the marine forecast before relying on it.")

    h = ['<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;color:#222;line-height:1.5;max-width:640px">',
         f'<p style="margin:0 0 10px;font-size:17px"><b>{esc(headline)}</b></p>',
         '<ul style="margin:0 0 14px;padding-left:20px">' + "".join(f"<li>{esc(b)}</li>" for b in bullets) + "</ul>"]
    if has_image:
        h.append('<img src="cid:conditions" width="640" alt="Forecast wind and estimated wave height in West Harbor" '
                 'style="width:100%;max-width:640px;height:auto;border:1px solid #ddd;border-radius:6px;margin:4px 0 10px">')
    if assets:
        h.append('<p style="margin:10px 0 4px"><b>Secure before it arrives</b></p>'
                 '<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;font-size:14px">' +
                 "".join(f'<tr><td style="padding:6px 8px;border-top:1px solid #e3e8ec;white-space:nowrap;'
                         f'vertical-align:top"><b>{esc(l)}</b></td><td style="padding:6px 8px;border-top:1px solid #e3e8ec">'
                         f'{esc(a)}</td></tr>' for l, a in assets) + "</table>")
    h.append(f'<p style="margin:14px 0 4px"><b>Model by model</b></p>'
             '<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:13px">'
             '<tr style="color:#555"><td style="padding:4px 10px 4px 0">Model</td><td style="padding:4px 10px">Peak sustained</td>'
             f'<td style="padding:4px 10px">Peak gust</td><td style="padding:4px 10px">Hours at {thr}+ kn</td>'
             '<td style="padding:4px 10px">First hour</td></tr>' +
             "".join(f'<tr><td style="padding:4px 10px 4px 0;border-top:1px solid #e3e8ec"><b>{esc(n)}</b></td>'
                     f'<td style="padding:4px 10px;border-top:1px solid #e3e8ec">{esc(p)} kn</td>'
                     f'<td style="padding:4px 10px;border-top:1px solid #e3e8ec">{esc(g)} kn</td>'
                     f'<td style="padding:4px 10px;border-top:1px solid #e3e8ec">{esc(hh)}</td>'
                     f'<td style="padding:4px 10px;border-top:1px solid #e3e8ec">{esc(f)}</td></tr>'
                     for n, p, g, hh, f in rows) + "</table>")
    if note:
        h.append(f'<p style="margin:12px 0 4px;padding:8px 10px;background:#fff4e5;border-left:4px solid #b87400;'
                 f'font-size:13px">{esc(note)}</p>')
    h.append('<p style="margin:14px 0 0;font-size:12px;color:#777;border-top:1px solid #eee;padding-top:8px">'
             'Automated briefing from public forecast models for the WFC dock. It is a planning aid, not a '
             'forecaster: check the marine forecast before relying on it.</p></div>')
    return text, "".join(h)


# ============================================================================
# Fetching
# ============================================================================
def _col(hourly: dict, base: str) -> list:
    if isinstance(hourly.get(base), list):
        return hourly[base]
    for k, v in hourly.items():
        if k.startswith(base + "_") and isinstance(v, list):
            return v
    return []


def fetch_models(raw_site: dict, cfg: dict, now: datetime) -> tuple:
    """(times, {name: series}, [(name, why failed), ...])"""
    import requests
    tz = ZoneInfo(raw_site["timezone"])
    start = now.replace(minute=0, second=0, microsecond=0)
    times = [start + timedelta(hours=i) for i in range(int(cfg["horizon_hours"]) + 1)]
    index = {t.strftime("%Y-%m-%dT%H:%M"): i for i, t in enumerate(times)}
    days = min(16, int(cfg["horizon_hours"]) // 24 + 2)
    models, failed = {}, []
    for m in cfg["models"]:
        params = {"latitude": raw_site["location"]["lat"], "longitude": raw_site["location"]["lon"],
                  "hourly": "wind_speed_10m,wind_gusts_10m,wind_direction_10m",
                  "wind_speed_unit": "kn", "timezone": raw_site["timezone"],
                  "forecast_days": days, "models": m["id"]}
        try:
            resp = None
            for attempt in (1, 2):
                try:
                    resp = requests.get(OPEN_METEO, params=params, timeout=25,
                                        headers={"User-Agent": "WFC-weather-monitor briefing"})
                    if resp.status_code < 500:
                        break
                except requests.RequestException:
                    if attempt == 2:
                        raise
                time.sleep(3)
            if resp.status_code != 200:
                reason = ""
                try:
                    reason = (resp.json() or {}).get("reason", "")
                except Exception:
                    pass
                raise RuntimeError(f"HTTP {resp.status_code} {reason}".strip())
            hourly = (resp.json() or {}).get("hourly") or {}
            ts = hourly.get("time") or []
            w, g, d = _col(hourly, "wind_speed_10m"), _col(hourly, "wind_gusts_10m"), _col(hourly, "wind_direction_10m")
            series = {"wind": [None] * len(times), "gust": [None] * len(times), "dir": [None] * len(times)}
            for j, stamp in enumerate(ts):
                i = index.get(stamp)
                if i is None:
                    continue
                series["wind"][i] = w[j] if j < len(w) else None
                series["gust"][i] = g[j] if j < len(g) else None
                series["dir"][i] = d[j] if j < len(d) else None
            if not any(v is not None for v in series["wind"]):
                raise RuntimeError("no wind values in the forecast window")
            models[m["name"]] = series
        except Exception as e:
            failed.append((m["name"], f"{type(e).__name__}: {e}"[:140]))
            print(f"briefing: model {m['name']} unavailable: {e}")
    return times, models, failed


# ============================================================================
# Backtest: how often would this rule have fired?
# ============================================================================
def fetch_history(raw_site: dict, cfg: dict, days: int, today: datetime) -> tuple:
    """Hourly model data for the past `days` days from Open-Meteo's archive of
    past forecasts. (times, {name: series}, [(name, why), ...])"""
    import requests
    tz = ZoneInfo(raw_site["timezone"])
    end = (today - timedelta(days=1)).date()
    start = end - timedelta(days=days - 1)
    first = datetime(start.year, start.month, start.day, tzinfo=tz)
    times = [first + timedelta(hours=i) for i in range(days * 24)]
    index = {t.strftime("%Y-%m-%dT%H:%M"): i for i, t in enumerate(times)}
    models, failed = {}, []
    for m in cfg["models"]:
        params = {"latitude": raw_site["location"]["lat"], "longitude": raw_site["location"]["lon"],
                  "hourly": "wind_speed_10m,wind_gusts_10m,wind_direction_10m",
                  "wind_speed_unit": "kn", "timezone": raw_site["timezone"],
                  "start_date": start.isoformat(), "end_date": end.isoformat(), "models": m["id"]}
        try:
            resp = requests.get(OPEN_METEO_PAST, params=params, timeout=90,
                                headers={"User-Agent": "WFC-weather-monitor briefing"})
            if resp.status_code != 200:
                reason = ""
                try:
                    reason = (resp.json() or {}).get("reason", "")
                except Exception:
                    pass
                raise RuntimeError(f"HTTP {resp.status_code} {reason}".strip())
            hourly = (resp.json() or {}).get("hourly") or {}
            ts = hourly.get("time") or []
            w, g, d = _col(hourly, "wind_speed_10m"), _col(hourly, "wind_gusts_10m"), _col(hourly, "wind_direction_10m")
            series = {"wind": [None] * len(times), "gust": [None] * len(times), "dir": [None] * len(times)}
            for j, stamp in enumerate(ts):
                i = index.get(stamp)
                if i is None:
                    continue
                series["wind"][i] = w[j] if j < len(w) else None
                series["gust"][i] = g[j] if j < len(g) else None
                series["dir"][i] = d[j] if j < len(d) else None
            have = sum(1 for v in series["wind"] if v is not None)
            if have < 24:
                raise RuntimeError("no archived data for this model")
            models[m["name"]] = series
            print(f"  {m['name']:<6} {have / 24:.0f} days of data")
        except Exception as e:
            failed.append((m["name"], f"{type(e).__name__}: {e}"[:140]))
            print(f"  {m['name']:<6} unavailable ({e})")
    return times, models, failed


def runs(flags: list, min_hours: int, gap: int) -> list:
    """[(first index, last index), ...] for runs of True at least min_hours
    long, after merging runs separated by `gap` hours or less."""
    out, start = [], None
    for i in range(len(flags) + 1):
        on = i < len(flags) and flags[i]
        if on and start is None:
            start = i
        if not on and start is not None:
            out.append([start, i - 1]); start = None
    out = [r_ for r_ in out if r_[1] - r_[0] + 1 >= min_hours]
    merged = []
    for a, b in out:
        if merged and a - merged[-1][1] - 1 <= gap:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return [tuple(m) for m in merged]


def backtest(raw_site: dict, cfg: dict, now: datetime, days: int = 365) -> int:
    print(f"Backtest: the past {days} days at the WFC dock, hour by hour.\n"
          f"A storm = at least {cfg['min_hours']} hours where {cfg['agree_fraction']:.0%} or more of the "
          f"models are at or above the level. Storms less than a day apart count as one.\n")
    print("Models:")
    times, models, failed = fetch_history(raw_site, cfg, days, now)
    if len(models) < cfg["min_models"]:
        print("\nNot enough models answered to run the backtest.")
        return 1
    months = days / 30.44
    DAY = 24

    print(f"\nSUSTAINED WIND (the briefing rule). Target: one or two storms a month.")
    print(f"  {'Level':>8}  {'Storms':>7}  {'Per month':>10}")
    at_level = {}
    for level in (12, 14, 16, 18, 20, 22, 24, 26):
        c = dict(cfg, wind_kn=level, merge_gap_hours=0, min_hours=1)
        an = analyze(times, models, c)
        storms = runs(an["agree"], cfg["min_hours"], DAY)
        at_level[level] = (an, storms)
        mark = "   <- current setting" if level == cfg["wind_kn"] else ""
        print(f"  {level:>5} kn  {len(storms):>7}  {len(storms) / months:>10.1f}{mark}")

    an = at_level[min(at_level, key=lambda L: abs(L - cfg["wind_kn"]))][0]
    print(f"\nGUSTS instead (model-consensus gust, same {cfg['min_hours']}-hour rule):")
    print(f"  {'Level':>8}  {'Storms':>7}  {'Per month':>10}")
    for level in (25, 30, 35, 40, 45):
        storms = runs([(g or 0) >= level for g in an["gust"]], cfg["min_hours"], DAY)
        print(f"  {level:>5} kn  {len(storms):>7}  {len(storms) / months:>10.1f}")

    def describe(a, b):
        idx = range(a, b + 1)
        pk = max(idx, key=lambda i: an["wind"][i] or 0)
        gust = max((an["gust"][i] or 0) for i in idx)
        return (f"  {times[a]:%a %b %d %Y}  {b - a + 1:>3} h  peak {r(an['wind'][pk])} kn sustained, "
                f"gusts {r(gust)} kn, {compass(an['dir'][pk])}")

    for level in sorted({cfg["wind_kn"], 18, 16}, reverse=True):
        if level not in at_level:
            continue
        storms = at_level[level][1]
        print(f"\nEvery storm at the {level} kn level ({len(storms)}):")
        for a, b in storms[-40:]:
            print(describe(a, b))
        if not storms:
            print("  none")

    print("\nThe 15 windiest days in the period (model consensus), to compare with what you remember:")
    by_day = {}
    for i, t in enumerate(times):
        if an["wind"][i] is not None:
            k = t.date()
            if k not in by_day or an["wind"][i] > an["wind"][by_day[k]]:
                by_day[k] = i
    for k, i in sorted(by_day.items(), key=lambda kv: -an["wind"][kv[1]])[:15]:
        gust = max((an["gust"][j] or 0) for j in range(len(times)) if times[j].date() == k)
        print(f"  {times[i]:%a %b %d %Y}  {r(an['wind'][i])} kn sustained, gusts {r(gust)} kn, {compass(an['dir'][i])}")
    if failed:
        print("\nModels with no archive: " + ", ".join(n for n, _ in failed))
    return 0


# ============================================================================
# State (the "briefing" key of state.json)
# ============================================================================
def read_bstate() -> dict:
    try:
        raw = json.loads(STATE_FILE.read_text())
        b = raw.get("briefing")
        return b if isinstance(b, dict) else {}
    except Exception:
        return {}


def write_bstate(b: dict) -> None:
    try:
        raw = json.loads(STATE_FILE.read_text())
        if not isinstance(raw, dict):
            raw = {}
    except Exception:
        raw = {}
    raw["briefing"] = b
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(raw, indent=2) + "\n")
    os.replace(tmp, STATE_FILE)


def current_slot(now: datetime, check_hours: list) -> Optional[str]:
    past = [h for h in sorted(check_hours) if h <= now.hour]
    return f"{now:%Y-%m-%d}-{past[-1]:02d}" if past else None


def is_due(now: datetime, cfg: dict, b: dict) -> bool:
    slot = current_slot(now, cfg["check_hours"])
    return slot is not None and b.get("slot") != slot


# ============================================================================
# Entry points
# ============================================================================
def send(subject, text, html, png) -> Optional[bool]:
    import monitor
    to = (os.environ.get("BRIEFING_TO") or "").strip()
    if to:
        os.environ["EMAIL_TO"] = to
    return monitor.send_email(subject, text, html, png)


def main(argv: list) -> int:
    raw_site, cfg = load_config()
    tz = ZoneInfo(raw_site["timezone"])
    now = datetime.now(tz)
    tag = raw_site.get("short_name", "WFC")
    mode = next((a for a in argv if a in ("--due", "--auto", "--preview", "--print", "--backtest")), "--print")
    b = read_bstate()

    if mode == "--backtest":
        return backtest(raw_site, cfg, now)

    if mode == "--due":
        return 0 if is_due(now, cfg, b) else 1
    if mode == "--auto" and not is_due(now, cfg, b):
        print("briefing: not due")
        return 0

    times, models, failed = fetch_models(raw_site, cfg, now)
    slot = current_slot(now, cfg["check_hours"])
    if len(models) < cfg["min_models"]:
        print(f"briefing: only {len(models)} model(s) answered; cannot judge agreement")
        if mode == "--auto":
            tries = b.get("tries", 0) + 1 if b.get("try_slot") == slot else 1
            b.update(try_slot=slot, tries=tries)
            if tries >= 3:
                b["slot"] = slot                     # give up on this slot
            write_bstate(b)
        return 1

    an = analyze(times, models, cfg)
    headline, bullets = build_bullets(an, cfg, now)
    ev = an["main"]
    assets = assets_to_secure(ev, an, cfg) if ev else []
    note = ""
    if failed:
        note = "Models that did not answer this run: " + ", ".join(n for n, _ in failed) + "."
    print(headline)
    for line in bullets:
        print("  •", line)
    for m in an["per_model"]:
        print(f"  {m['name']:<6} peak {r(m['peak_kn'])} kn, gust {r(m['peak_gust_kn'])} kn, "
              f"{m['hours_over']} h at {cfg['wind_kn']}+")
    if mode == "--print":
        return 0

    if mode == "--preview":
        png = render_png(an, cfg, now, headline, "PREVIEW")
        text, html = build_email("PREVIEW (manual test). " + headline, bullets, assets, an, cfg, now,
                                 png is not None, note)
        ok = send(f"🧪 {tag} storm briefing (preview) — {now:%a %-I:%M %p}", text, html, png)
        return 0 if ok is True else 1

    # --auto
    # One email per storm. The check runs twice a day; almost every run is silent.
    sig, prev = event_signature(ev), b.get("sent") if isinstance(b.get("sent"), dict) else None
    p_start, p_end = (_t(prev.get("start")), _t(prev.get("end"))) if prev else (None, None)

    def deliver(kind: str) -> bool:
        png = render_png(an, cfg, now, headline, "UPDATE" if kind == "update" else "")
        text, html = build_email(headline, bullets, assets, an, cfg, now, png is not None, note)
        lead = "Update: " if kind == "update" else ""
        return send(f"⚠️ {tag} storm briefing — {lead}{compass(ev['dir'])} {cfg['wind_kn']}+ kn "
                    f"{fmt_when(ev['start'], now)}", text, html, png) is True

    if ev is not None and not same_storm(prev, sig):
        if not deliver("new"):
            print("briefing: email not sent; will retry on the next run")
            return 1
        b["sent"] = dict(sig, updates=0)
    elif ev is not None:
        arrived = p_start is not None and now >= p_start
        if (not arrived and prev.get("updates", 0) < 1 and materially_worse(prev, sig)):
            if not deliver("update"):
                return 1
            b["sent"] = dict(sig, updates=prev.get("updates", 0) + 1)
        else:
            # same storm, already briefed: stay quiet, but keep tracking its window
            prev["end"] = max(prev.get("end", ""), sig["end"])
            b["sent"] = prev
            print("briefing: this storm has already been briefed; nothing sent")
    elif prev is not None:
        if p_start is not None and now < p_start:
            # briefed, and it has dropped out of the forecast before arriving
            png = render_png(an, cfg, now, headline, "STAND DOWN")
            text, html = build_email("Stand down: the storm briefed earlier is no longer expected. " + headline,
                                     bullets, [], an, cfg, now, png is not None, note)
            if send(f"✅ {tag} storm briefing — stand down, {cfg['wind_kn']}+ kn no longer agreed",
                    text, html, png) is not True:
                return 1
            b["sent"] = None
        elif p_end is None or now > p_end + timedelta(hours=12):
            b["sent"] = None                       # the storm has come and gone
            print("briefing: earlier storm has passed; cleared")
        else:
            print("briefing: earlier storm under way or just ended; nothing sent")
    else:
        print("briefing: nothing agreed; no email")
    b.update(slot=slot, try_slot=None, tries=0)
    write_bstate(b)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
