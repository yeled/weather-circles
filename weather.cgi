#!/usr/bin/env python3
"""Apache CGI endpoint — builds the polling payload per request, with a
dynamic location taken from the query string.

Point a TRMNL polling plugin at e.g.:
    .../weather.cgi?q=Manchester
    .../weather.cgi?q=Paris
    .../weather.cgi?lat=51.5&lon=-0.13&name=London   (lat/lon override q)
    .../weather.cgi                                  (defaults to London)
    .../weather.cgi?days=1&hours=6,9,12,15,18,21,0,3  (1-day/8-slot, custom hours)
    .../weather.cgi?days=2&hours=7,11,15,19           (2-day/4-slot, custom hours)
    .../weather.cgi?rolling=1                         (rolling now..+2h slots; hours ignored)

Returns the unwrapped days[] JSON the Liquid layouts expect.

This is a public, unauthenticated endpoint, so it is written defensively:
every parameter is validated before use (see BadRequest), responses are
cached on disk, and the number of concurrent upstream fetches is capped.
Failures never echo internal detail back to the caller -- that goes to the
Apache error log instead.

Requires CGI enabled in Apache (mod_cgi/mod_cgid, ExecCGI on the directory)
and this file executable:

    Options +ExecCGI
    AddHandler cgi-script .cgi

REPO_DIR defaults to this script's own directory (deploy weather.cgi inside
the checkout, alongside trmnl_report.py). Override with WEATHER_CIRCLES_DIR
if you keep them apart. Cache location defaults to a temp dir; override with
WEATHER_CIRCLES_CACHE. Neither is settable by a client -- Apache only exposes
request data as HTTP_*/QUERY_STRING.
"""
import fcntl
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
import traceback
import urllib.parse

REPO_DIR = (os.environ.get("WEATHER_CIRCLES_DIR")
            or os.path.dirname(os.path.abspath(__file__)))
DAYS = 2

# TRMNL devices poll on a ~15 minute cycle and the underlying data is hourly,
# so anything within CACHE_TTL is served off disk without touching Open-Meteo.
# Past that, an entry is still kept as a fallback for CACHE_GRACE: a slightly
# old forecast beats a 502 on a screen that just wants something to draw.
CACHE_TTL     = 300
CACHE_GRACE   = 86400
CACHE_VERSION = "1"       # bump when the payload shape changes
CACHE_DIR = (os.environ.get("WEATHER_CIRCLES_CACHE")
             or os.path.join(tempfile.gettempdir(), "weather-circles-cache"))

# Cap on simultaneous upstream fetches. Without it, N concurrent requests pin N
# Apache CGI workers for the length of a round-trip, which is a cheap way for
# anyone to exhaust the worker pool. Excess requests get a fast 503 (or a stale
# cache entry) instead of a slow worker.
MAX_UPSTREAM = 4

# `name` is reflected into payload["location"], which the Liquid templates
# render as raw markup -- Liquid's {{ }} does not escape, and the templates
# rely on that to inline the SVGs. Keep it to characters that can appear in a
# place name and nothing that can open a tag or an entity. The templates also
# escape it; this is the primary guard, that is defence in depth.
NAME_MAX  = 64
NAME_DROP = re.compile(r"[^\w \-'./,()]", re.UNICODE)
Q_MAX     = 100
TZ_RE     = re.compile(r"\A[A-Za-z0-9_+\-/]{1,64}\Z")

sys.path.insert(0, REPO_DIR)
os.chdir(REPO_DIR)

import trmnl_report as tr                          # noqa: E402

tr.UPSTREAM_TIMEOUT = 5    # 2 sequential calls -> 10s worst case, not 30s


class BadRequest(ValueError):
    """Bad input from the caller -> 400. Never reported as an upstream 502."""


# ── Parameter validation ───────────────────────────────────────────────
def _param(qs, key):
    vals = qs.get(key)
    return vals[0].strip() if vals and vals[0].strip() else None


def _coord(raw, limit, label):
    if raw is None:
        return None
    try:
        val = float(raw)
    except ValueError:
        raise BadRequest(f"{label} must be a number")
    # float() accepts "nan", "inf" and "1e999" (-> inf); those would go
    # upstream as latitude=nan and come back as an error we'd blame on
    # Open-Meteo. Reject them here, along with out-of-range coordinates.
    if not math.isfinite(val) or abs(val) > limit:
        raise BadRequest(f"{label} must be a number between -{limit} and {limit}")
    return val


def _clean_name(raw):
    return NAME_DROP.sub("", raw)[:NAME_MAX].strip() or None if raw else None


def _clean_tz(raw):
    if raw is not None and not TZ_RE.match(raw):
        raise BadRequest("tz must be an IANA timezone name")
    return raw


def _clean_q(raw):
    if raw is not None and len(raw) > Q_MAX:
        raise BadRequest("q is too long")
    return raw


# ── Response cache ─────────────────────────────────────────────────────
def _cache_path(parts):
    key = hashlib.sha256(
        "\0".join([CACHE_VERSION] + [p or "" for p in parts]).encode()).hexdigest()
    return os.path.join(CACHE_DIR, f"{key}.json")


def _cache_read(path, max_age):
    """(body, age) if an entry exists and is younger than max_age, else None."""
    try:
        age = time.time() - os.stat(path).st_mtime
        if age > max_age:
            return None
        with open(path, "rb") as f:
            return f.read(), age
    except OSError:
        return None


def _cache_write(path, body):
    try:
        os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, path)      # atomic: readers see the old or new file
    except OSError as e:
        print(f"weather.cgi: cache write failed: {e}", file=sys.stderr)


_UNLIMITED = object()      # couldn't set up locking; proceed without a cap


def _acquire_slot():
    """Take one of MAX_UPSTREAM concurrent-fetch slots, or None if all busy.

    The returned handle must stay referenced for the rest of the request --
    the flock is released when this CGI process exits, however it exits.
    """
    try:
        os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
    except OSError as e:
        print(f"weather.cgi: no slot dir, running uncapped: {e}", file=sys.stderr)
        return _UNLIMITED
    for i in range(MAX_UPSTREAM):
        fh = None
        try:
            fh = open(os.path.join(CACHE_DIR, f"slot{i}.lock"), "w")
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            if fh is not None:
                fh.close()
    return None


# ── Output ─────────────────────────────────────────────────────────────
def _respond(status, extra, body):
    head = ([f"Status: {status}"] if status else []) + [
        "Content-Type: application/json",
        "X-Content-Type-Options: nosniff",
    ] + extra
    sys.stdout.write("\r\n".join(head) + "\r\n\r\n")
    sys.stdout.flush()
    sys.stdout.buffer.write(body)


def _serve_cached(body, headers):
    _respond(None, headers, body)


def main():
    qs      = urllib.parse.parse_qs(os.environ.get("QUERY_STRING", ""))
    q       = _clean_q(_param(qs, "q"))
    lat     = _coord(_param(qs, "lat"), 90, "lat")
    lon     = _coord(_param(qs, "lon"), 180, "lon")
    name    = _clean_name(_param(qs, "name"))
    tz      = _clean_tz(_param(qs, "tz"))
    hours   = _param(qs, "hours")
    rolling = _param(qs, "rolling") == "1"
    days    = _param(qs, "days")
    days_count = int(days) if days in ("1", "2") else DAYS

    if (lat is None) != (lon is None):
        raise BadRequest("lat and lon must be given together")

    # Keyed on the raw request, before any upstream call -- keying on the
    # resolved location would still cost a geocode per request, and that's
    # the expensive half.
    path = _cache_path((q, repr(lat), repr(lon), name, tz,
                        str(days_count), hours, "1" if rolling else "0"))

    hit = _cache_read(path, CACHE_TTL)
    if hit:
        body, age = hit
        return _serve_cached(body, [f"Cache-Control: max-age={int(CACHE_TTL - age)}",
                                    "X-Cache: hit"])

    slot = _acquire_slot()
    if slot is None:
        stale = _cache_read(path, CACHE_GRACE)
        if stale:
            return _serve_cached(stale[0], ["Cache-Control: max-age=60",
                                            "X-Cache: stale"])
        return _respond("503 Service Unavailable",
                        ["Retry-After: 30", "Cache-Control: no-store"],
                        b'{"error":"busy, try again shortly"}')

    try:
        try:
            rlat, rlon, rname, rtz = tr.resolve_location(
                q=q, lat=lat, lon=lon, name=name, tz=tz)
        except ValueError as e:                    # geocode found nothing
            raise BadRequest("no location found for that query") from e
        slots   = None if rolling else tr.resolve_slots(days_count, hours)
        data    = tr.fetch(rlat, rlon, rtz)
        payload = tr.build_payload(data, rname, days_count, slots, rolling=rolling)
        body    = json.dumps(payload, separators=(",", ":")).encode()
    except BadRequest:
        raise
    except Exception as e:                         # noqa: BLE001 -- upstream
        print(f"weather.cgi: upstream failed: {e!r}", file=sys.stderr)
        stale = _cache_read(path, CACHE_GRACE)
        if stale:
            return _serve_cached(stale[0], ["Cache-Control: max-age=60",
                                            "X-Cache: stale"])
        raise

    _cache_write(path, body)
    _respond(None, [f"Cache-Control: max-age={CACHE_TTL}", "X-Cache: miss"], body)


try:
    main()
except BadRequest as e:
    # Our own message, so there's nothing of the caller's to echo back.
    _respond("400 Bad Request", ["Cache-Control: no-store"],
             json.dumps({"error": str(e)}).encode())
except Exception:                                  # noqa: BLE001
    # Detail to the Apache error log; the caller gets a fixed string, since
    # exception text can carry upstream URLs and local filesystem paths.
    traceback.print_exc(file=sys.stderr)
    _respond("502 Bad Gateway", ["Cache-Control: no-store"],
             b'{"error":"weather data unavailable"}')
