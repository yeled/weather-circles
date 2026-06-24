#!/usr/bin/env python3
"""Build a UK-station-circle weather report and push it to a TRMNL plugin.

TRMNL screens are 1-bit e-ink (800x480), so the station circles are drawn
in monochrome black. This script:

  1. fetches current + hourly conditions from Open-Meteo,
  2. pre-renders each station circle to inline SVG (reusing weather_circle.py),
  3. assembles the merge-variable payload,
  4. POSTs it to your TRMNL private-plugin webhook (or writes JSON / an HTML
     preview so you can see it before wiring anything up).

Set up a TRMNL "Private Plugin" with strategy = Webhook, paste markup.liquid
as the markup, then run this on a schedule (cron) pointed at the webhook URL.

There are four responsive layouts in trmnl/ (full, half_horizontal,
half_vertical, quadrant) — paste each into the matching TRMNL markup field.

Examples:
    ./trmnl_report.py --png out.png                   # full layout PNG (800x480)
    ./trmnl_report.py --layout quadrant --png q.png   # any layout, native size
    ./trmnl_report.py --preview report.html           # local HTML preview
    ./trmnl_report.py --json                          # print payload to stdout
    ./trmnl_report.py --webhook https://usetrmnl.com/api/custom_plugins/<uuid>
    TRMNL_WEBHOOK_URL=... ./trmnl_report.py           # webhook from env
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

import weather_circle as wc          # reuse the SVG station-circle renderer

INK = "#000000"                      # 1-bit e-ink: pure black

WMO = {
    0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light showers", 81: "Showers", 82: "Violent showers",
    85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm, hail", 99: "Thunderstorm, hail",
}
COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def geocode(query):
    """Resolve a place name to coordinates via Open-Meteo's geocoding API."""
    params = urllib.parse.urlencode(
        {"name": query, "count": 1, "language": "en", "format": "json"})
    url = f"https://geocoding-api.open-meteo.com/v1/search?{params}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        results = (json.load(resp).get("results") or [])
    if not results:
        raise ValueError(f"no location found for {query!r}")
    r = results[0]
    label = ", ".join(p for p in (r.get("name"), r.get("country_code")) if p)
    return r["latitude"], r["longitude"], label, r.get("timezone", "auto")


def resolve_location(q=None, lat=None, lon=None, name=None, tz=None):
    """Pick a location: explicit lat/lon wins, then a place-name lookup,
    else default to London. Returns (lat, lon, name, tz)."""
    if lat is not None and lon is not None:
        lat, lon = float(lat), float(lon)
        return lat, lon, name or f"{lat:.4g},{lon:.4g}", tz or "auto"
    if q:
        glat, glon, gname, gtz = geocode(q)
        return glat, glon, name or gname, tz or gtz
    return 51.5074, -0.1278, name or "London", tz or "Europe/London"


def fetch(lat, lon, tz):
    params = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "current": "weather_code,temperature_2m,cloud_cover,"
                   "wind_speed_10m,wind_direction_10m",
        "hourly": "weather_code,temperature_2m,cloud_cover,"
                  "wind_speed_10m,wind_direction_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                 "wind_speed_10m_max,wind_direction_10m_dominant",
        "wind_speed_unit": "kn", "forecast_days": 2, "timezone": tz,
    })
    url = f"https://api.open-meteo.com/v1/forecast?{params}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.load(resp)


def wind_text(knots, deg):
    if not knots or knots < 1:
        return "Calm"
    return f"{round(knots)} kt {COMPASS[round(deg / 45) % 8]}"


def cell(entry):
    """entry: dict with the five Open-Meteo fields -> report cell."""
    return {
        "svg":     wc.build_svg(entry, INK, mono=True, show_temp=False),
        "temp":    round(entry["temperature_2m"]),
        "summary": WMO.get(entry["weather_code"], "—"),
        "wind":    wind_text(entry.get("wind_speed_10m"),
                             entry.get("wind_direction_10m") or 0),
        "oktas":   round((entry.get("cloud_cover") or 0) / 12.5),
    }


HOURLY_FIELDS = ("weather_code", "temperature_2m", "cloud_cover",
                 "wind_speed_10m", "wind_direction_10m")
DAY_LABELS = ["Today", "Tomorrow"]


def _ampm(hh):
    suffix = "am" if hh < 12 else "pm"
    h12 = hh % 12 or 12
    return f"{h12}{suffix}"


# Precipitation severity, worst to mildest, for picking the one hour that
# best represents a multi-hour slot (see _hour_cell).
PRECIP_SEVERITY = {
    "thunder": 9, "heavy_rain": 8, "snow_shower": 7, "snow": 6, "sleet": 5,
    "rain": 4, "drizzle": 3, "fog": 2, "mist": 1,
}


def _severity(code, cloud):
    return (PRECIP_SEVERITY.get(wc.precip_for(code), 0), cloud or 0)


def _hour_cell(h, date, hh, step):
    """Build a forecast cell for the [hh, hh+step) window, or None.

    Scans every hour in the window rather than just hh itself, and uses the
    worst (most severe precipitation, then cloudiest) hour's snapshot — so
    rain that falls between two slot hours still shows up in the icon.
    """
    idxs = []
    for t in range(hh, hh + step):
        try:
            idxs.append(h["time"].index(f"{date}T{t:02d}:00"))
        except ValueError:
            pass
    if not idxs:
        return None
    j = max(idxs, key=lambda i: _severity(h["weather_code"][i], h["cloud_cover"][i]))
    c = cell({k: h[k][j] for k in HOURLY_FIELDS})
    c["label"] = _ampm(hh)
    return c


def _day_svg(h, date, daily, idx):
    """A summary station circle for the whole day (mean cloud + daily wind)."""
    clouds = [h["cloud_cover"][i] for i, t in enumerate(h["time"])
              if t.startswith(date) and h["cloud_cover"][i] is not None]
    entry = {
        "weather_code":       daily["weather_code"][idx],
        "temperature_2m":     daily["temperature_2m_max"][idx],
        "cloud_cover":        sum(clouds) / len(clouds) if clouds else 0,
        "wind_speed_10m":     daily["wind_speed_10m_max"][idx],
        "wind_direction_10m": daily["wind_direction_10m_dominant"][idx],
    }
    return wc.build_svg(entry, INK, mono=True, show_temp=False)


def build_payload(data, name, days_count=1, slots=(8, 10, 12, 14, 16, 18, 20, 22)):
    cur, h, daily = data["current"], data["hourly"], data["daily"]

    current = cell(cur)
    current["high"] = round(daily["temperature_2m_max"][0])
    current["low"]  = round(daily["temperature_2m_min"][0])

    step = slots[1] - slots[0] if len(slots) > 1 else 4

    days = []
    for idx in range(min(days_count, len(daily["time"]))):
        date = daily["time"][idx]
        cells = [c for c in (_hour_cell(h, date, hh, step) for hh in slots) if c]
        days.append({
            "label": DAY_LABELS[idx] if idx < len(DAY_LABELS) else date,
            "high":  round(daily["temperature_2m_max"][idx]),
            "low":   round(daily["temperature_2m_min"][idx]),
            "svg":   _day_svg(h, date, daily, idx),
            "cells": cells,
        })

    return {
        "location":     name,
        "generated_at": cur["time"][11:16],
        "current":      current,
        "days":         days,
    }


# ── TRMNL layouts ──────────────────────────────────────────────────────
# Native pixel size of each responsive slot (used for the local preview;
# on-device TRMNL supplies the frame).
LAYOUTS = {
    "full":            (800, 480),
    "half_horizontal": (800, 240),
    "half_vertical":   (400, 480),
    "quadrant":        (400, 240),
}
TRMNL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
FRAMEWORK_VERSION = "3.1.1"   # matches src/settings.yml


def _lookup(expr, ctx):
    """Resolve a dotted path like 'd.cells' in ctx; returns the raw value."""
    val = ctx
    for part in expr.split("|")[0].strip().split("."):
        val = val.get(part) if isinstance(val, dict) else getattr(val, part, None)
        if val is None:
            return None
    return val


def _subst_vars(text, ctx):
    def rep(m):
        v = _lookup(m.group(1), ctx)
        return "" if v is None else str(v)
    return re.sub(r"{{\s*(.*?)\s*}}", rep, text)


_FOR_OPEN = re.compile(r"{%-?\s*for\s+(\w+)\s+in\s+([\w.]+)\s*-?%}")
_FOR_TAG  = re.compile(r"{%-?\s*for\s+\w+\s+in\s+[\w.]+\s*-?%}|{%-?\s*endfor\s*-?%}")


def render_liquid(template, ctx):
    """Tiny Liquid subset: {% comment %}, nested {% for x in list %}, {{ a.b }}.

    Enough to render our own templates so the local preview matches the markup
    TRMNL renders. Not a general Liquid implementation.
    """
    template = re.sub(r"{%-?\s*comment\s*-?%}.*?{%-?\s*endcomment\s*-?%}",
                      "", template, flags=re.S)
    return _render(template, ctx)


def _render(s, ctx):
    out, pos = [], 0
    while True:
        m = _FOR_OPEN.search(s, pos)
        if not m:
            out.append(_subst_vars(s[pos:], ctx))
            return "".join(out)
        out.append(_subst_vars(s[pos:m.start()], ctx))
        var, coll_expr = m.group(1), m.group(2)
        body, pos = _match_endfor(s, m.end())
        for item in _lookup(coll_expr, ctx) or []:
            out.append(_render(body, dict(ctx, **{var: item})))


def _match_endfor(s, start):
    """Return (body, index-after-endfor) for the for-loop opened before `start`."""
    depth, pos = 1, start
    while True:
        m = _FOR_TAG.search(s, pos)
        if not m:
            raise ValueError("unclosed {% for %}")
        depth += -1 if "endfor" in m.group(0) else 1
        if depth == 0:
            return s[start:m.start()], m.end()
        pos = m.end()


def render_layout(layout, payload):
    """Render a layout's .liquid file, wrapped in a TRMNL-sized frame."""
    with open(os.path.join(TRMNL_DIR, f"{layout}.liquid")) as f:
        inner = render_liquid(f.read(), payload)
    w, h = LAYOUTS[layout]
    # Replicate TRMNL's real render wrapper so the preview uses the actual
    # framework CSS/fonts: <base> resolves the framework's root-relative font
    # URLs, body.environment.trmnl + .screen--og set the design-system vars,
    # and .view--full fills the (screen - 2*gap) area. Size the screen to the
    # layout so each renders at its native dimensions.
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<base href="https://trmnl.com/">'
        f'<link rel="stylesheet" href="https://trmnl.com/css/{FRAMEWORK_VERSION}/plugins.css">'
        '<style>html,body{margin:0;padding:0}</style></head>'
        '<body class="environment trmnl">'
        f'<div class="screen screen--og" style="--screen-w:{w}px;--screen-h:{h}px;'
        f'width:{w}px;height:{h}px;background:#fff">'
        f'<div class="view view--full">{inner}</div>'
        '</div></body></html>')


def find_chrome():
    if os.environ.get("CHROME"):
        return os.environ["CHROME"]
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            candidates.insert(0, found)
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def render_png(payload, out, layout):
    """Rasterise a layout to its exact native-size PNG via headless Chrome."""
    chrome = find_chrome()
    if not chrome:
        sys.exit("no Chrome/Chromium found; set CHROME=/path/to/chrome")
    w, h = LAYOUTS[layout]
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as f:
        f.write(render_layout(layout, payload))
        html = f.name
    try:
        subprocess.run([
            chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            "--force-device-scale-factor=1", f"--window-size={w},{h}",
            # let the framework CSS + web fonts finish loading before capture
            "--virtual-time-budget=5000", "--default-background-color=FFFFFFFF",
            f"--screenshot={out}", f"file://{html}",
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        os.unlink(html)
    print(f"wrote {out} ({w}x{h}, {layout})", file=sys.stderr)


def post_webhook(url, payload):
    body = json.dumps({"merge_variables": payload}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, resp.read().decode(errors="replace")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--q", help="place name to geocode (e.g. Manchester); "
                                "ignored if --lat/--lon are given")
    ap.add_argument("--lat", type=float, help="latitude (overrides --q)")
    ap.add_argument("--lon", type=float, help="longitude (overrides --q)")
    ap.add_argument("--name", help="location label (default: looked up / London)")
    ap.add_argument("--tz", help="IANA timezone (default: auto / Europe/London)")
    ap.add_argument("--days", type=int, default=1,
                    help="forecast days to show (default 1; the 7-slot day "
                         "already wraps to 2 rows, so 2 days overflows the "
                         "fixed 4-column grid)")
    ap.add_argument("--webhook", default=os.environ.get("TRMNL_WEBHOOK_URL"),
                    help="TRMNL private-plugin webhook URL (or TRMNL_WEBHOOK_URL env)")
    ap.add_argument("--json", action="store_true", help="print payload JSON to stdout")
    ap.add_argument("--poll-json", metavar="FILE", dest="poll_json",
                    help="write unwrapped payload for a TRMNL polling URL "
                         "(use '-' for stdout)")
    ap.add_argument("--layout", choices=sorted(LAYOUTS), default="full",
                    help="which TRMNL layout to preview/render (default full)")
    ap.add_argument("--preview", metavar="FILE", help="write a standalone HTML preview")
    ap.add_argument("--png", metavar="FILE",
                    help="render the layout's exact native-size PNG via headless "
                         "Chrome (set CHROME=/path if not auto-detected)")
    args = ap.parse_args()

    try:
        lat, lon, name, tz = resolve_location(
            args.q, args.lat, args.lon, args.name, args.tz)
        data = fetch(lat, lon, tz)
    except Exception as e:                       # noqa: BLE001
        sys.exit(f"weather fetch failed: {e}")

    payload = build_payload(data, name, args.days)

    if args.preview:
        with open(args.preview, "w") as f:
            f.write(render_layout(args.layout, payload))
        print(f"wrote {args.preview} ({args.layout})", file=sys.stderr)
    if args.png:
        render_png(payload, os.path.abspath(args.png), args.layout)
    if args.json:
        print(json.dumps({"merge_variables": payload}, indent=2))
    if args.poll_json:
        # Polling: TRMNL reads the top-level keys directly, so write the
        # payload unwrapped (no merge_variables envelope). Write atomically
        # so Apache never serves a half-written file.
        blob = json.dumps(payload, separators=(",", ":"))
        if args.poll_json == "-":
            sys.stdout.write(blob + "\n")
        else:
            tmp = args.poll_json + ".tmp"
            with open(tmp, "w") as f:
                f.write(blob)
            os.replace(tmp, args.poll_json)
            print(f"wrote {args.poll_json} ({len(blob)} bytes)", file=sys.stderr)
    if args.webhook:
        status, text = post_webhook(args.webhook, payload)
        print(f"TRMNL webhook -> {status} {text}", file=sys.stderr)
    if not (args.preview or args.png or args.json or args.poll_json or args.webhook):
        print("nothing to do: pass --preview, --png, --json, --poll-json, or --webhook",
              file=sys.stderr)


if __name__ == "__main__":
    main()
