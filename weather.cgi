#!/usr/bin/env python3
"""Apache CGI endpoint — builds the polling payload per request, with a
dynamic location taken from the query string.

Point a TRMNL polling plugin at e.g.:
    .../weather.cgi?q=Manchester
    .../weather.cgi?q=Paris
    .../weather.cgi?lat=51.5&lon=-0.13&name=London   (lat/lon override q)
    .../weather.cgi                                  (defaults to London)

Returns the unwrapped days[] JSON the Liquid layouts expect.

Requires CGI enabled in Apache (mod_cgi/mod_cgid, ExecCGI on the directory)
and this file executable:

    Options +ExecCGI
    AddHandler cgi-script .cgi

REPO_DIR defaults to this script's own directory (deploy weather.cgi inside
the checkout, alongside trmnl_report.py). Override with WEATHER_CIRCLES_DIR
if you keep them apart.
"""
import json
import os
import sys
import urllib.parse

REPO_DIR = (os.environ.get("WEATHER_CIRCLES_DIR")
            or os.path.dirname(os.path.abspath(__file__)))
DAYS = 2

sys.path.insert(0, REPO_DIR)
os.chdir(REPO_DIR)

import trmnl_report as tr                          # noqa: E402


def _param(qs, key):
    vals = qs.get(key)
    return vals[0].strip() if vals and vals[0].strip() else None


try:
    qs = urllib.parse.parse_qs(os.environ.get("QUERY_STRING", ""))
    lat = _param(qs, "lat")
    lon = _param(qs, "lon")
    location = tr.resolve_location(
        q=_param(qs, "q"),
        lat=float(lat) if lat else None,
        lon=float(lon) if lon else None,
        name=_param(qs, "name"),
        tz=_param(qs, "tz"),
    )
    lat, lon, name, tz = location
    data    = tr.fetch(lat, lon, tz)
    payload = tr.build_payload(data, name, DAYS)
    body    = json.dumps(payload, separators=(",", ":"))
    sys.stdout.write("Content-Type: application/json\r\n")
    sys.stdout.write("Cache-Control: max-age=300\r\n\r\n")
    sys.stdout.write(body)
except Exception as e:                             # noqa: BLE001
    sys.stdout.write("Status: 502 Bad Gateway\r\n")
    sys.stdout.write("Content-Type: application/json\r\n\r\n")
    sys.stdout.write(json.dumps({"error": str(e)}))
