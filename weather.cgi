#!/usr/bin/env python3
"""Apache CGI wrapper — generates the polling payload per request.

Use this only if you want live-per-request generation instead of the
recommended cron + static-file approach. Requires CGI enabled in Apache
(mod_cgi/mod_cgid, ExecCGI on the directory) and this file executable.

    Options +ExecCGI
    AddHandler cgi-script .cgi

Edit REPO_DIR to the absolute path of this checkout, and LAT/LON/NAME/TZ
to taste.
"""
import json
import os
import sys

REPO_DIR = "/Users/yeled/src/weather-circles"   # <-- absolute path to checkout
LAT, LON, NAME, TZ = 51.5074, -0.1278, "London", "Europe/London"
DAYS = 2

sys.path.insert(0, REPO_DIR)
os.chdir(REPO_DIR)                                # build_svg etc. use relative nothing, but be safe

import trmnl_report as tr                          # noqa: E402

try:
    data    = tr.fetch(LAT, LON, TZ)
    payload = tr.build_payload(data, NAME, DAYS)
    body    = json.dumps(payload, separators=(",", ":"))
    sys.stdout.write("Content-Type: application/json\r\n")
    sys.stdout.write("Cache-Control: max-age=300\r\n\r\n")
    sys.stdout.write(body)
except Exception as e:                             # noqa: BLE001
    sys.stdout.write("Status: 502 Bad Gateway\r\n")
    sys.stdout.write("Content-Type: application/json\r\n\r\n")
    sys.stdout.write(json.dumps({"error": str(e)}))
