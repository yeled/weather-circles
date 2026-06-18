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


def fetch(lat, lon, tz):
    params = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "current": "weather_code,temperature_2m,cloud_cover,"
                   "wind_speed_10m,wind_direction_10m",
        "hourly": "weather_code,temperature_2m,cloud_cover,"
                  "wind_speed_10m,wind_direction_10m",
        "daily": "temperature_2m_max,temperature_2m_min",
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


def build_payload(data, name, count, step):
    cur = data["current"]
    h   = data["hourly"]

    # index of the current hour within the hourly arrays
    hour_key = cur["time"][:13] + ":00"
    try:
        start = h["time"].index(hour_key)
    except ValueError:
        start = 0

    hours = []
    for n in range(1, count + 1):                 # upcoming hours, skipping "now"
        j = start + n * step
        if j >= len(h["time"]):
            break
        entry = {k: h[k][j] for k in
                 ("weather_code", "temperature_2m", "cloud_cover",
                  "wind_speed_10m", "wind_direction_10m")}
        c = cell(entry)
        c["label"] = h["time"][j][11:16]          # HH:MM
        hours.append(c)

    current = cell(cur)
    daily = data.get("daily", {})
    if daily.get("temperature_2m_max"):
        current["high"] = round(daily["temperature_2m_max"][0])
        current["low"]  = round(daily["temperature_2m_min"][0])
    else:
        current["high"] = current["low"] = current["temp"]

    return {
        "location":     name,
        "generated_at": cur["time"][11:16],
        "current":      current,
        "hours":        hours,
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
TRMNL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trmnl")


def _resolve(expr, ctx):
    """Look up a dotted path like 'current.svg' in ctx."""
    val = ctx
    for part in expr.split("|")[0].strip().split("."):
        val = val.get(part, "") if isinstance(val, dict) else getattr(val, part, "")
    return "" if val is None else str(val)


def _subst_vars(text, ctx):
    return re.sub(r"{{\s*(.*?)\s*}}", lambda m: _resolve(m.group(1), ctx), text)


def render_liquid(template, ctx):
    """Tiny Liquid subset: {% comment %}, {% for x in list %}, {{ a.b }}.

    Enough to render our own templates so the local preview is byte-for-byte
    the markup TRMNL renders. Not a general Liquid implementation.
    """
    template = re.sub(r"{%-?\s*comment\s*-?%}.*?{%-?\s*endcomment\s*-?%}",
                      "", template, flags=re.S)

    def for_block(m):
        var, coll, body = m.group(1), m.group(2), m.group(3)
        out = []
        for item in ctx.get(coll, []):
            local = dict(ctx, **{var: item})
            out.append(_subst_vars(body, local))
        return "".join(out)

    template = re.sub(
        r"{%-?\s*for\s+(\w+)\s+in\s+(\w+)\s*-?%}(.*?){%-?\s*endfor\s*-?%}",
        for_block, template, flags=re.S)
    return _subst_vars(template, ctx)


def render_layout(layout, payload):
    """Render a layout's .liquid file, wrapped in a TRMNL-sized frame."""
    with open(os.path.join(TRMNL_DIR, f"{layout}.liquid")) as f:
        inner = render_liquid(f.read(), payload)
    w, h = LAYOUTS[layout]
    return (f'<!DOCTYPE html><html><head><meta charset="utf-8"><style>'
            f'html,body{{margin:0;padding:0}}*{{box-sizing:border-box}}'
            f'.trmnl{{width:{w}px;height:{h}px;background:#fff;color:#000;'
            f'overflow:hidden}}</style></head><body>'
            f'<div class="trmnl">{inner}</div></body></html>')


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
    ap.add_argument("--lat", type=float, default=51.5074)
    ap.add_argument("--lon", type=float, default=-0.1278)
    ap.add_argument("--name", default="London")
    ap.add_argument("--tz", default="Europe/London")
    ap.add_argument("--count", type=int, default=6, help="number of forecast cells")
    ap.add_argument("--step", type=int, default=2, help="hours between cells")
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
        data = fetch(args.lat, args.lon, args.tz)
    except Exception as e:                       # noqa: BLE001
        sys.exit(f"weather fetch failed: {e}")

    payload = build_payload(data, args.name, args.count, args.step)

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
