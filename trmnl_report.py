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

Examples:
    ./trmnl_report.py --preview report.html          # local preview, no TRMNL
    ./trmnl_report.py --json                          # print payload to stdout
    ./trmnl_report.py --webhook https://usetrmnl.com/api/custom_plugins/<uuid>
    TRMNL_WEBHOOK_URL=... ./trmnl_report.py           # webhook from env
"""

import argparse
import json
import os
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
    return {
        "location":     name,
        "generated_at": cur["time"][11:16],
        "current":      current,
        "hours":        hours,
    }


# ── Local HTML preview (mirrors markup.liquid) ─────────────────────────
def render_html(p):
    cells = "".join(
        f'<div class="cell"><div class="hr">{h["label"]}</div>'
        f'<div class="svgwrap">{h["svg"]}</div>'
        f'<div class="t">{h["temp"]}°</div></div>'
        for h in p["hours"])
    cur = p["current"]
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  body {{ margin:0; background:#888; font-family:-apple-system,Helvetica,Arial,sans-serif; }}
  .screen {{ width:800px; height:480px; background:#fff; color:#000; box-sizing:border-box;
            padding:22px 26px; display:flex; flex-direction:column; }}
  .body {{ display:flex; flex:1; gap:24px; }}
  .current {{ flex:0 0 250px; text-align:center; border-right:3px solid #000; padding-right:20px; }}
  .current .svgwrap svg {{ width:170px; height:170px; }}
  .big {{ font-size:64px; font-weight:800; line-height:1; }}
  .sum {{ font-size:22px; font-weight:600; }}
  .wind {{ font-size:18px; }}
  .grid {{ flex:1; display:grid; grid-template-columns:repeat(3,1fr); gap:10px 8px; }}
  .cell {{ text-align:center; }}
  .cell .svgwrap svg {{ width:96px; height:96px; }}
  .hr {{ font-size:18px; font-weight:700; }}
  .t  {{ font-size:20px; font-weight:600; }}
  .bar {{ display:flex; justify-content:space-between; border-top:3px solid #000;
          margin-top:10px; padding-top:8px; font-size:18px; font-weight:600; }}
</style></head><body><div class="screen">
  <div class="body">
    <div class="current">
      <div class="svgwrap">{cur['svg']}</div>
      <div class="big">{cur['temp']}°C</div>
      <div class="sum">{cur['summary']}</div>
      <div class="wind">{cur['wind']} · {cur['oktas']}/8 cloud</div>
    </div>
    <div class="grid">{cells}</div>
  </div>
  <div class="bar"><span>Weather Circles</span>
    <span>{p['location']} · {p['generated_at']}</span></div>
</div></body></html>"""


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


def render_png(payload, out):
    """Rasterise the report to an exact 800x480 PNG via headless Chrome."""
    chrome = find_chrome()
    if not chrome:
        sys.exit("no Chrome/Chromium found; set CHROME=/path/to/chrome")
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as f:
        f.write(render_html(payload))
        html = f.name
    try:
        subprocess.run([
            chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            "--force-device-scale-factor=1", "--window-size=800,480",
            f"--screenshot={out}", f"file://{html}",
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        os.unlink(html)
    print(f"wrote {out}", file=sys.stderr)


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
    ap.add_argument("--preview", metavar="FILE", help="write a standalone HTML preview")
    ap.add_argument("--png", metavar="FILE",
                    help="render an exact 800x480 PNG via headless Chrome "
                         "(set CHROME=/path if not auto-detected)")
    args = ap.parse_args()

    try:
        data = fetch(args.lat, args.lon, args.tz)
    except Exception as e:                       # noqa: BLE001
        sys.exit(f"weather fetch failed: {e}")

    payload = build_payload(data, args.name, args.count, args.step)

    if args.preview:
        with open(args.preview, "w") as f:
            f.write(render_html(payload))
        print(f"wrote {args.preview}", file=sys.stderr)
    if args.png:
        render_png(payload, os.path.abspath(args.png))
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
