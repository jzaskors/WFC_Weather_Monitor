"""
render_conditions.py — builds the conditions graphic for WFC alert emails.

Draws a compass rose showing wind speed/direction with the offshore arc shaded,
plus a per-asset GO / CAUTION / HALT readout underneath.

Returns PNG bytes; monitor.py embeds it inline in the alert email.
Requires Pillow (pip install pillow).

>>> EDIT THE FLEET TABLE BELOW to match your real operating limits. <<<
"""

from io import BytesIO
import math

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None


# ---------------------------------------------------------------------------
# FLEET LIMITS  — knots. Edit these to your actual policy.
#   caution / stop  = sustained wind
#   gustCaution / gustStop = gusts
#   offCaution / offStop  = sustained wind when it's blowing OFFSHORE
#                           (out of the harbor). Only set for craft that can
#                           be blown out and can't get back — paddle craft.
#                           Leave as None for keelboats.
# ---------------------------------------------------------------------------
FLEET = [
    # label,              caution, stop, gustCaution, gustStop, offCaution, offStop
    ("Kayaks",                12,   16,   16,   20,    8,   13),
    ("Paddleboards",          10,   14,   14,   18,    7,   11),
    ("Sonars",                16,   22,   22,   28,  None, None),
]

# Wind blowing FROM this arc pushes craft out of the harbor (north-facing launch)
OFFSHORE_ARC = (150, 240)

ORDER = {"GO": 0, "CAUTION": 1, "STOP": 2}

# Palette (matches the dashboard)
INK = (11, 27, 43)
PANEL = (18, 42, 64)
LINE = (40, 69, 94)
TEXT = (238, 241, 234)
DIM = (147, 167, 184)
GO = (47, 163, 107)
CAUTION = (232, 161, 60)
STOP = (214, 69, 69)
LEVEL_COLOR = {"GO": GO, "CAUTION": CAUTION, "STOP": STOP}
LEVEL_WORD = {"GO": "GO", "CAUTION": "CAUTION", "STOP": "HALT"}


def _in_arc(d, a, b):
    if d is None:
        return False
    d = (d % 360 + 360) % 360
    return (a <= d <= b) if a <= b else (d >= a or d <= b)


def evaluate_asset(row, wind, gust, direction):
    """Return (level, reason) for one asset given measured wind."""
    label, cau, stop, gcau, gstop, ocau, ostop = row
    level, reason = "GO", ""

    def bump(lvl, why):
        nonlocal level, reason
        if ORDER[lvl] > ORDER[level]:
            level, reason = lvl, why

    offshore = _in_arc(direction, *OFFSHORE_ARC)
    if wind is not None and ostop is not None and offshore:
        if wind >= ostop:
            bump("STOP", f"offshore {round(wind)} kn")
        elif wind >= ocau:
            bump("CAUTION", f"offshore {round(wind)} kn")
    if wind is not None:
        if wind >= stop:
            bump("STOP", f"wind {round(wind)} kn")
        elif wind >= cau:
            bump("CAUTION", f"wind {round(wind)} kn")
    if gust is not None:
        if gust >= gstop:
            bump("STOP", f"gusts {round(gust)} kn")
        elif gust >= gcau:
            bump("CAUTION", f"gusts {round(gust)} kn")
    return level, reason


def fleet_status(wind, gust, direction, force_stop_reason=None):
    """Status for every asset. force_stop_reason (e.g. lightning) halts all."""
    out = []
    for row in FLEET:
        if force_stop_reason:
            out.append((row[0], "STOP", force_stop_reason))
        else:
            lvl, why = evaluate_asset(row, wind, gust, direction)
            out.append((row[0], lvl, why))
    return out


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------
def _font(size, bold=False):
    base = "/usr/share/fonts/truetype/dejavu/DejaVuSans"
    for path in ((base + "-Bold.ttf",) if bold else ()) + (base + ".ttf",):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _text(d, xy, s, font, fill, anchor="la"):
    d.text(xy, s, font=font, fill=fill, anchor=anchor)


def _draw_compass(d, cx, cy, r, wind, gust, direction, station):
    # offshore arc wedge — the thing that matters most for paddle craft.
    a0, a1 = OFFSHORE_ARC
    # PIL angles: 0 = 3 o'clock, clockwise. Compass 0 = up. Convert: pil = comp - 90
    d.pieslice([cx - r, cy - r, cx + r, cy + r],
               a0 - 90, a1 - 90, fill=(58, 44, 16), outline=None)

    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=LINE, width=2)
    d.ellipse([cx - r * 0.62, cy - r * 0.62, cx + r * 0.62, cy + r * 0.62],
              outline=LINE, width=1)

    f_tick = _font(15, bold=True)
    f_small = _font(12)
    for i, lbl in enumerate(["N", "E", "S", "W"]):
        ang = math.radians(i * 90 - 90)
        x1, y1 = cx + math.cos(ang) * r, cy + math.sin(ang) * r
        x0, y0 = cx + math.cos(ang) * r * 0.88, cy + math.sin(ang) * r * 0.88
        d.line([x0, y0, x1, y1], fill=DIM, width=2)
        lx, ly = cx + math.cos(ang) * (r + 15), cy + math.sin(ang) * (r + 15)
        _text(d, (lx, ly), lbl, f_tick, TEXT, anchor="mm")
    for deg in range(0, 360, 30):
        if deg % 90 == 0:
            continue
        ang = math.radians(deg - 90)
        d.line([cx + math.cos(ang) * r * 0.93, cy + math.sin(ang) * r * 0.93,
                cx + math.cos(ang) * r, cy + math.sin(ang) * r], fill=LINE, width=1)

    # offshore label along the wedge
    mid = math.radians(((a0 + a1) / 2) - 90)
    _text(d, (cx + math.cos(mid) * r * 0.80, cy + math.sin(mid) * r * 0.80),
          "OFFSHORE", _font(11, bold=True), CAUTION, anchor="mm")

    # shoreline hint — launch faces north
    _text(d, (cx, cy - r - 34), "harbor / open water ↑", f_small, DIM, anchor="mm")

    # wind arrow: points the way the wind is GOING (from `direction`)
    if direction is not None:
        ang = math.radians(direction - 90)
        tail = (cx + math.cos(ang) * r * 0.93, cy + math.sin(ang) * r * 0.93)
        head = (cx - math.cos(ang) * r * 0.55, cy - math.sin(ang) * r * 0.55)
        d.line([tail, head], fill=TEXT, width=5)
        hl, hw = 18, 9
        back = (head[0] + math.cos(ang) * hl, head[1] + math.sin(ang) * hl)
        perp = ang + math.pi / 2
        d.polygon([head,
                   (back[0] + math.cos(perp) * hw, back[1] + math.sin(perp) * hw),
                   (back[0] - math.cos(perp) * hw, back[1] - math.sin(perp) * hw)],
                  fill=TEXT)

    # center readout
    d.ellipse([cx - r * 0.44, cy - r * 0.44, cx + r * 0.44, cy + r * 0.44],
              fill=INK, outline=LINE, width=1)
    _text(d, (cx, cy - 14), f"{round(wind) if wind is not None else '—'}",
          _font(40, bold=True), TEXT, anchor="mm")
    _text(d, (cx, cy + 13), "kn sustained", _font(11), DIM, anchor="mm")
    if gust is not None:
        _text(d, (cx, cy + 31), f"gusts {round(gust)}", _font(13, bold=True), CAUTION, anchor="mm")


ROW_H = 52            # px per fleet row
HEAD_H = 84           # title block
FOOT_H = 44           # disclaimer strip


def render_conditions_png(wind, gust, direction, station,
                          force_stop_reason=None, headline=None):
    """Build the alert graphic. Returns PNG bytes, or None if Pillow is missing.
    Height adapts to the number of assets so the panel is never stretched."""
    if Image is None:
        return None

    rows = fleet_status(wind, gust, direction, force_stop_reason)
    panel_h = 44 + len(rows) * ROW_H + 8
    compass_h = 268                       # diameter + label headroom
    content_h = max(panel_h, compass_h)
    W = 720
    H = HEAD_H + content_h + FOOT_H

    img = Image.new("RGB", (W, H), INK)
    d = ImageDraw.Draw(img)

    f_h = _font(20, bold=True)
    f_lbl = _font(13, bold=True)
    f_body = _font(15)
    f_small = _font(12)

    _text(d, (24, 22), (headline or "WEST HARBOR CONDITIONS").upper(), f_h, TEXT)
    dir_txt = "—"
    if direction is not None:
        pts = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
               "S","SSW","SW","WSW","W","WNW","NW","NNW"]
        dir_txt = f"{pts[round((direction % 360)/22.5) % 16]} ({round(direction)}°)"
    _text(d, (24, 48), f"Wind from {dir_txt} · measured at {station}", f_small, DIM)
    d.line([24, 72, W - 24, 72], fill=LINE, width=1)

    # compass, vertically centred in the content band
    r = 104
    _draw_compass(d, 168, HEAD_H + content_h / 2, r, wind, gust, direction, station)

    # fleet panel, vertically centred alongside it
    x0 = 330
    y0 = HEAD_H + (content_h - panel_h) / 2
    d.rounded_rectangle([x0, y0, W - 24, y0 + panel_h], 10,
                        fill=PANEL, outline=LINE, width=1)
    _text(d, (x0 + 18, y0 + 16), "FLEET", f_lbl, DIM)
    _text(d, (W - 40, y0 + 16), "STATUS", f_lbl, DIM, anchor="ra")
    d.line([x0 + 14, y0 + 38, W - 38, y0 + 38], fill=LINE, width=1)

    ry = y0 + 48
    for label, lvl, why in rows:
        col = LEVEL_COLOR[lvl]
        d.rounded_rectangle([x0 + 14, ry + 4, x0 + 20, ry + ROW_H - 14], 3, fill=col)
        _text(d, (x0 + 32, ry + 4), label, f_body, TEXT)
        if why:
            _text(d, (x0 + 32, ry + 24), why, f_small, DIM)
        _text(d, (W - 40, ry + 9), LEVEL_WORD[lvl], _font(16, bold=True), col, anchor="ra")
        ry += ROW_H

    _text(d, (24, H - 30),
          "Confirm lightning by eye and ear — 30-30 rule. This graphic assists the call; it does not make it.",
          f_small, DIM)

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


if __name__ == "__main__":
    png = render_conditions_png(17, 23, 205, "Sagamore YC (in harbor)")
    with open("preview.png", "wb") as f:
        f.write(png)
    print("wrote preview.png", len(png), "bytes")
