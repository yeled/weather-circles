#!/usr/bin/env python3
"""Emit a UK-style weather station circle as a transparent SVG.

Queries Open-Meteo for current conditions and renders the station model:
cloud cover (oktas), wind barb (direction + speed in knots), a precipitation
glyph, and the temperature. No background — just the circle.

Examples:
    ./weather_circle.py                         # London -> stdout
    ./weather_circle.py --lat 53.48 --lon -2.24 --name Manchester
    ./weather_circle.py --ink "#eef2f8" -o circle.svg   # light ink for dark bg
    ./weather_circle.py --no-temp                # cloud + wind only
"""

import argparse
import json
import math
import sys
import urllib.parse
import urllib.request

# ── Geometry (SVG user units) ──────────────────────────────────────────
SIZE      = 260
CX = CY   = SIZE / 2
R         = 64          # station-circle radius
BARB_LEN  = 50          # wind-shaft length beyond the circle
STROKE    = 7           # circle / wedge outline weight
# R + BARB_LEN (the shaft tip's reach from centre) is kept ~equal to the old
# 48 + 66 = 114 so the barb still fits the viewBox in any wind direction; the
# bigger R / shorter shaft just spends more of that fixed box on the circle
# itself (≈37% → ≈49% of the width) so the oktas fill reads larger on e-ink.


# ── Open-Meteo ─────────────────────────────────────────────────────────
def fetch_current(lat, lon, tz):
    params = urllib.parse.urlencode({
        "latitude": lat,
        "longitude": lon,
        "current": "weather_code,temperature_2m,cloud_cover,"
                   "wind_speed_10m,wind_direction_10m",
        "wind_speed_unit": "kn",
        "timezone": tz,
    })
    url = f"https://api.open-meteo.com/v1/forecast?{params}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.load(resp)["current"]


# ── Weather-code -> precipitation glyph key (UK station-model) ─────────
def precip_for(code):
    if code in (51, 53, 55):                 return "drizzle"
    if code in (56, 57, 66, 67):             return "sleet"
    if code in (61, 63, 80, 81):             return "rain"
    if code in (65, 82):                     return "heavy_rain"
    if code in (71, 73, 75, 77):             return "snow"
    if code in (85, 86):                     return "snow_shower"
    if code in (95, 96, 99):                 return "thunder"
    if code == 45:                           return "mist"
    if code == 48:                           return "fog"
    return None

PRECIP_TINT = {
    "drizzle": "#0ea5e9", "rain": "#2563eb", "heavy_rain": "#1d4ed8",
    "sleet": "#6366f1",   "snow": "#0284c7", "snow_shower": "#0369a1",
    "thunder": "#ca8a04", "mist": "#64748b", "fog": "#475569",
}


# ── SVG path helpers ───────────────────────────────────────────────────
def pt(cx, cy, r, deg_from_top):
    """Point on a circle, angle measured clockwise from 12 o'clock."""
    a = math.radians(deg_from_top)
    return cx + r * math.sin(a), cy - r * math.cos(a)

def wedge(cx, cy, r, start_deg, sweep_deg, fill):
    """Filled pie slice, clockwise from start_deg spanning sweep_deg."""
    x0, y0 = pt(cx, cy, r, start_deg)
    x1, y1 = pt(cx, cy, r, start_deg + sweep_deg)
    large = 1 if sweep_deg > 180 else 0
    return (f'<path d="M{cx:.2f},{cy:.2f} L{x0:.2f},{y0:.2f} '
            f'A{r:.2f},{r:.2f} 0 {large} 1 {x1:.2f},{y1:.2f} Z" '
            f'fill="{fill}"/>')

def line(x1, y1, x2, y2, color, w, cap="round"):
    return (f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
            f'stroke="{color}" stroke-width="{w}" stroke-linecap="{cap}"/>')


# ── Cloud cover (oktas) ────────────────────────────────────────────────
def draw_oktas(oktas, code, ink):
    parts = [f'<circle cx="{CX}" cy="{CY}" r="{R}" fill="none" '
             f'stroke="{ink}" stroke-width="{STROKE}"/>']

    if code == 48:                                   # sky obscured -> X
        d = R * 0.6
        parts.append(line(CX - d, CY - d, CX + d, CY + d, ink, STROKE))
        parts.append(line(CX + d, CY - d, CX - d, CY + d, ink, STROKE))
        return parts

    vline = lambda: line(CX, CY - R, CX, CY + R, ink, STROKE)
    hline = lambda: line(CX - R, CY, CX + R, CY, ink, STROKE)

    if   oktas <= 0: pass
    elif oktas == 1: parts.append(vline())
    elif oktas == 2: parts.append(wedge(CX, CY, R, 0, 90, ink))
    elif oktas == 3: parts += [wedge(CX, CY, R, 0, 90, ink), vline()]
    elif oktas == 4: parts.append(wedge(CX, CY, R, 0, 180, ink))
    elif oktas == 5: parts += [wedge(CX, CY, R, 0, 180, ink), hline()]
    elif oktas == 6: parts.append(wedge(CX, CY, R, 0, 270, ink))
    elif oktas == 7: parts.append(wedge(CX, CY, R, 0, 315, ink))
    else:            parts.append(wedge(CX, CY, R, 0, 359.99, ink))
    return parts


# ── Wind barb ──────────────────────────────────────────────────────────
def draw_barb(dir_deg, knots, ink, halo=None):
    # Bold feathers so they survive being scaled down to e-ink cell size.
    # Half barb = 5 kt, full barb = 10 kt, pennant (triangle) = 50 kt.
    # Calm: extra ring, no shaft.
    #
    # When `halo` (a background colour) is given, every stroke is drawn as a
    # wider halo-coloured casing first, then the ink stroke on top. The barb
    # is drawn *over* the cloud fill (see build_svg), so on a fully-overcast
    # (solid black) circle the white casing keeps the black barb from
    # vanishing into the black disc — readable across a room.
    if knots < 1:
        ring = (f'<circle cx="{CX}" cy="{CY}" r="{R + 8}" fill="none" '
                f'stroke="%s" stroke-width="%d"/>')
        out = []
        if halo:
            out.append(ring % (halo, 9))
        out.append(ring % (ink, 4))
        return out

    a = math.radians(dir_deg)                 # direction wind blows FROM
    ux, uy = math.sin(a), -math.cos(a)        # outward unit vector
    px, py = -uy, ux                          # perpendicular (barb side)

    sx, sy = CX + ux * R,             CY + uy * R
    ex, ey = CX + ux * (R + BARB_LEN), CY + uy * (R + BARB_LEN)
    segs = [(sx, sy, ex, ey, 7)]              # shaft; (x1,y1,x2,y2,width)

    def at(f):                                # point a fraction along the shaft
        d = R + BARB_LEN * f
        return CX + ux * d, CY + uy * d

    kt   = round(knots / 5) * 5
    t    = 1.0                                # 1 = tip, 0 = circle edge
    step = 13 / BARB_LEN
    FB, HB = 24, 13                           # full / half barb length

    penns = []                                # pennant triangles (pt-triples)
    while kt >= 50:                           # pennant (filled triangle)
        bx, by = at(t)
        cx2, cy2 = at(t - step * 1.7)
        penns.append((bx, by, bx + px*FB, by + py*FB, cx2, cy2))
        t -= step * 2.0; kt -= 50
    while kt >= 10:                           # full barb
        bx, by = at(t)
        segs.append((bx, by, bx + px*FB + ux*6, by + py*FB + uy*6, 7))
        t -= step; kt -= 10
    if kt >= 5:                               # half barb
        if t > 0.85: t -= step                # keep it off the very tip
        bx, by = at(t)
        segs.append((bx, by, bx + px*HB + ux*3, by + py*HB + uy*3, 7))

    def penn(pts, fill, stroke=None, w=0):
        s = f' stroke="{stroke}" stroke-width="{w}" stroke-linejoin="round"' if stroke else ''
        return (f'<polygon points="{pts[0]:.2f},{pts[1]:.2f} {pts[2]:.2f},{pts[3]:.2f} '
                f'{pts[4]:.2f},{pts[5]:.2f}" fill="{fill}"{s}/>')

    out = []
    if halo:                                  # halo casing underneath
        out += [line(x1, y1, x2, y2, halo, w + 5) for x1, y1, x2, y2, w in segs]
        out += [penn(p, halo, halo, 5) for p in penns]
    out += [line(x1, y1, x2, y2, ink, w) for x1, y1, x2, y2, w in segs]
    out += [penn(p, ink) for p in penns]
    return out


# ── Precipitation glyphs (left of the circle) ──────────────────────────
def draw_precip(key, x, y, s, color):
    # UK Met Office station-model present-weather symbols (per reference
    # guide): drizzle = comma, rain = one dot, heavy rain = three dots in a
    # triangle. Drawn bold/large so they still read across a room.
    if key == "drizzle":                          # comma: round head, tail seamless with head's right curve
        hx, hy, rh = x, y - s*0.16, s*0.40
        return [
            f'<circle cx="{hx:.2f}" cy="{hy:.2f}" r="{rh:.2f}" fill="{color}"/>',
            f'<path d="M{x + s*0.360:.2f},{y + s*0.016:.2f} '
            f'C{x + s*0.52:.2f},{y + s*0.50:.2f} {x + s*0.26:.2f},{y + s*0.92:.2f} '
            f'{x + s*0.00:.2f},{y + s*0.88:.2f} '
            f'C{x + s*0.00:.2f},{y + s*0.44:.2f} {x - s*0.096:.2f},{y + s*0.232:.2f} '
            f'{x - s*0.19:.2f},{y + s*0.13:.2f} Z" fill="{color}"/>',
        ]
    if key == "rain":                             # single filled dot
        return [f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{s*0.5:.2f}" fill="{color}"/>']
    if key == "heavy_rain":                        # three dots, triangle (apex up)
        r = s * 0.32
        g = s * 0.42
        pts = ((x, y - g), (x - g, y + g*0.6), (x + g, y + g*0.6))
        return [f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r:.2f}" fill="{color}"/>'
                for cx, cy in pts]
    if key in ("snow", "snow_shower", "sleet"):
        parts = []
        def star(sx, sy, ss):
            out = []
            for i in range(3):
                ang = math.radians(i * 60)
                dx, dy = math.cos(ang) * ss * 0.5, math.sin(ang) * ss * 0.5
                out.append(line(sx - dx, sy - dy, sx + dx, sy + dy, color, 4))
            return out
        if key == "snow":
            parts += star(x, y, s)
        elif key == "sleet":
            parts.append(f'<circle cx="{x:.2f}" cy="{y - s*0.45:.2f}" '
                         f'r="{s*0.22:.2f}" fill="{color}"/>')
            parts += star(x, y + s*0.3, s*0.8)
        else:  # snow_shower
            parts += star(x, y - s*0.4, s*0.8)
            parts += draw_precip("shower", x, y + s*0.5, s*0.8, color)
        return parts
    if key == "shower":
        return [f'<polygon points="{x:.2f},{y - s*0.5:.2f} '
                f'{x - s*0.45:.2f},{y + s*0.4:.2f} {x + s*0.45:.2f},{y + s*0.4:.2f}" '
                f'fill="none" stroke="{color}" stroke-width="4" stroke-linejoin="round"/>']
    if key == "thunder":
        return [f'<polygon points="{x + s*0.18:.2f},{y - s*0.55:.2f} '
                f'{x - s*0.30:.2f},{y + s*0.10:.2f} {x - s*0.02:.2f},{y + s*0.05:.2f} '
                f'{x - s*0.20:.2f},{y + s*0.55:.2f} {x + s*0.34:.2f},{y - s*0.12:.2f} '
                f'{x + s*0.04:.2f},{y - s*0.06:.2f}" fill="{color}"/>']
    if key in ("mist", "fog"):
        ys = (-0.18, 0.18) if key == "mist" else (-0.3, 0.0, 0.3)
        return [line(x - s*0.5, y + s*t, x + s*0.5, y + s*t, color, 4) for t in ys]
    return []


# ── Assemble SVG ───────────────────────────────────────────────────────
def build_svg(cur, ink, mono, show_temp):
    code   = cur["weather_code"]
    temp   = round(cur["temperature_2m"])
    cloud  = cur.get("cloud_cover") or 0
    wspd   = cur.get("wind_speed_10m") or 0
    wdir   = cur.get("wind_direction_10m") or 0
    oktas  = round(cloud / 12.5)

    # Draw order: cloud fill, then the wind barb on top (with a white halo in
    # mono/white-bg mode so it stays visible over a solid-black overcast disc),
    # then precipitation on top of everything so the dots stay crisp.
    halo = "#ffffff" if mono else None
    body = []
    body += draw_oktas(oktas, code, ink)
    body += draw_barb(wdir, wspd, ink, halo)

    key = precip_for(code)
    if key:
        color = ink if mono else PRECIP_TINT.get(key, ink)
        body += draw_precip(key, CX - R - 26, CY, 28, color)

    if show_temp:
        body.append(
            f'<text x="{CX + R*0.5:.2f}" y="{CY - R - 6:.2f}" '
            f'font-family="-apple-system,Helvetica,Arial,sans-serif" '
            f'font-size="26" font-weight="700" fill="{ink}">{temp}°</text>')

    return (f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{SIZE}" height="{SIZE}" viewBox="0 0 {SIZE} {SIZE}">\n  '
            + "\n  ".join(body) + "\n</svg>\n")


def main():
    ap = argparse.ArgumentParser(description="Emit a weather station circle as transparent SVG.")
    ap.add_argument("--lat",  type=float, default=51.5074)
    ap.add_argument("--lon",  type=float, default=-0.1278)
    ap.add_argument("--name", default="London", help="label (used only in a comment)")
    ap.add_argument("--tz",   default="Europe/London")
    ap.add_argument("--ink",  default="#1a1a2e", help="symbol colour (default dark)")
    ap.add_argument("--mono", action="store_true", help="draw precip in ink, no tint")
    ap.add_argument("--no-temp", dest="temp", action="store_false", help="omit temperature")
    ap.add_argument("-o", "--out", help="output file (default: stdout)")
    args = ap.parse_args()

    try:
        cur = fetch_current(args.lat, args.lon, args.tz)
    except Exception as e:                      # noqa: BLE001
        sys.exit(f"weather fetch failed: {e}")

    svg = (f"<!-- {args.name}: {cur['time']}  code={cur['weather_code']}  "
           f"{cur['temperature_2m']}°C  cloud={cur.get('cloud_cover')}%  "
           f"wind={cur.get('wind_speed_10m')}kn@{cur.get('wind_direction_10m')}° -->\n"
           + build_svg(cur, args.ink, args.mono, args.temp))

    if args.out:
        with open(args.out, "w") as f:
            f.write(svg)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(svg)


if __name__ == "__main__":
    main()
