"""Live ADS-B ingest — multi-source, with an OpenSky fallback and a last-good cache.

Primary sources speak the readsb JSON dialect:
    GET /v2/lat/{lat}/lon/{lon}/dist/{nm}
    -> { "ac": [ {hex, flight, lat, lon, alt_baro, nic, nac_p, ...} ], "now": ms }

Those carry NIC/NACp — the navigation-integrity fields the GPS-interference
detector depends on — so they are always tried first.

Why the fallback exists: the public readsb aggregators rate-limit or block
shared cloud/datacenter egress IPs. From a container the same request that
works from a laptop can quietly return an empty list, which silently blanked
the whole board. OpenSky answers from datacenter ranges, so it keeps the
picture alive — but it does NOT publish NIC/NACp, so integrity detection is
degraded while it is the active source. That is surfaced, never hidden.

Order of resort:
    1. readsb aggregators (full fidelity)
    2. OpenSky bbox (partial fidelity — no NIC/NACp)
    3. last-good cache (stale, clearly marked)
"""
from __future__ import annotations

import json
import logging
import math
import os
import time

import socket
import httpx

from .config import config

log = logging.getLogger("adsb")

# ICAO 24-bit address country allocations (major blocks; enough for a flag).
_ICAO_RANGES = [
    (0x000000, 0x0FFFFF, "Africa/other"), (0x100000, 0x1FFFFF, "Russia"),
    (0x201000, 0x2FFFFF, "Africa"), (0x300000, 0x33FFFF, "Italy"),
    (0x340000, 0x37FFFF, "Spain"), (0x380000, 0x3BFFFF, "France"),
    (0x3C0000, 0x3FFFFF, "Germany"), (0x400000, 0x43FFFF, "United Kingdom"),
    (0x440000, 0x447FFF, "Austria"), (0x448000, 0x44FFFF, "Belgium"),
    (0x450000, 0x457FFF, "Denmark"), (0x458000, 0x45FFFF, "Finland"),
    (0x460000, 0x467FFF, "Sweden"), (0x468000, 0x46FFFF, "Greece"),
    (0x470000, 0x477FFF, "Hungary"), (0x478000, 0x47FFFF, "Norway"),
    (0x480000, 0x487FFF, "Netherlands"), (0x488000, 0x48FFFF, "Poland"),
    (0x490000, 0x497FFF, "Portugal"), (0x4A0000, 0x4A7FFF, "Switzerland"),
    (0x4B0000, 0x4B7FFF, "Turkey"), (0x4C0000, 0x4C7FFF, "Ukraine"),
    (0x500000, 0x5FFFFF, "Europe/other"), (0x600000, 0x6FFFFF, "Mideast/S-Asia"),
    (0x700000, 0x7FFFFF, "Asia"), (0x800000, 0x83FFFF, "India"),
    (0x880000, 0x88FFFF, "Thailand"), (0x8A0000, 0x8A7FFF, "Indonesia"),
    (0x900000, 0x9FFFFF, "Oceania/other"), (0xA00000, 0xAFFFFF, "United States"),
    (0xC00000, 0xC3FFFF, "Canada"), (0xC80000, 0xC87FFF, "New Zealand"),
    (0xE00000, 0xE3FFFF, "Argentina"), (0xE40000, 0xE7FFFF, "Brazil"),
]

M_TO_FT = 3.28084
MS_TO_KT = 1.94384

# One pooled, IPv4-pinned client for every outbound feed call.
#
# Why this is so defensive: inside the container the feed hosts resolve to
# AAAA (IPv6) records, but the task has no routable IPv6 address. Binding the
# socket to IPv4 alone then fails at connect time with "[Errno -9] Address
# family for hostname not supported", so resolution itself has to prefer IPv4.
# A fresh connection per poll (7 regions, every few seconds) also burned
# ephemeral ports, hence the shared keepalive pool.
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_first(host, port, family=0, type=0, proto=0, flags=0):
    """Resolve A records first; fall back to the OS answer for v6-only hosts."""
    try:
        res = _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
        if res:
            return res
    except socket.gaierror:
        pass
    return _orig_getaddrinfo(host, port, family, type, proto, flags)


socket.getaddrinfo = _ipv4_first

_LIMITS = httpx.Limits(max_connections=16, max_keepalive_connections=8,
                       keepalive_expiry=30.0)
_client: httpx.Client | None = None


def _http() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(
            transport=httpx.HTTPTransport(retries=1),
            limits=_LIMITS,
            timeout=httpx.Timeout(12.0, connect=8.0),
            headers={"User-Agent": "clustral-airspace/1.0"},
            follow_redirects=True,
        )
    return _client

# Feed health, surfaced through /api/health so a blank board is never a mystery.
FEED_STATUS: dict = {
    "source": None,          # which source last produced aircraft
    "degraded": False,       # True when NIC/NACp are unavailable (OpenSky)
    "stale": False,          # True when serving the last-good cache
    "last_success": None,    # epoch seconds
    "attempts": [],          # [{source, ok, n, note}] from the most recent fetch
}

# Public aggregators answer 429 when polled too hard. Park a source that says
# so instead of hammering it (and burning the whole fallback chain) every tick.
_COOLDOWN: dict[str, float] = {}      # source -> epoch until which to skip
_COOLDOWN_SECS = 180.0


def _cooling(src: str, attempts: list) -> bool:
    until = _COOLDOWN.get(src, 0.0)
    if time.time() < until:
        attempts.append({"source": src, "ok": False, "n": 0,
                         "note": f"cooling down {int(until - time.time())}s after 429"})
        return True
    return False


def _penalise(src: str) -> None:
    _COOLDOWN[src] = time.time() + _COOLDOWN_SECS


# region-key -> {"ts": epoch, "ac": [...], "source": str, "degraded": bool}
_CACHE: dict[str, dict] = {}
_CACHE_TTL = 900.0           # 15 min: stale data still beats an empty screen

# The in-memory cache died with every new revision, so a redeploy could land on
# a throttled upstream and show a blank board. Mirror it to disk.
_CACHE_FILE = os.getenv("ADSB_CACHE_FILE", "/tmp/adsb_cache.json")


def _cache_load() -> None:
    try:
        with open(_CACHE_FILE) as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            _CACHE.update(data)
            log.info("adsb cache restored: %d regions", len(data))
    except (OSError, ValueError):
        pass


def _cache_save() -> None:
    try:
        tmp = _CACHE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(_CACHE, fh)
        os.replace(tmp, _CACHE_FILE)
    except OSError:
        pass


def icao_country(hexid: str) -> str:
    try:
        v = int(hexid, 16)
    except (ValueError, TypeError):
        return "?"
    for lo, hi, name in _ICAO_RANGES:
        if lo <= v <= hi:
            return name
    return "?"


def _num(x):
    return x if isinstance(x, (int, float)) else None


def _normalize(a: dict) -> dict:
    """readsb dialect -> internal shape (full fidelity, includes NIC/NACp)."""
    alt_raw = a.get("alt_baro")
    ground = (alt_raw == "ground")
    emerg = a.get("emergency")
    if emerg in ("none", "", None):
        emerg = None
    return {
        "hex": a.get("hex", "").strip(),
        "flight": (a.get("flight") or "").strip(),
        "reg": a.get("r", ""), "type": a.get("t", ""),
        "source": a.get("type", ""),   # adsb_icao | mlat | tisb | adsr (position source)
        "country": icao_country(a.get("hex", "")),
        "lat": _num(a.get("lat")), "lon": _num(a.get("lon")),
        "alt": 0 if ground else _num(alt_raw), "alt_geom": _num(a.get("alt_geom")),
        "gs": _num(a.get("gs")), "track": _num(a.get("track")),
        "track_rate": _num(a.get("track_rate")), "roll": _num(a.get("roll")),
        "baro_rate": _num(a.get("baro_rate")), "geom_rate": _num(a.get("geom_rate")),
        "squawk": a.get("squawk"), "emergency": emerg,
        "category": a.get("category", ""),
        "nic": _num(a.get("nic")), "nac_p": _num(a.get("nac_p")),
        "sil": _num(a.get("sil")), "rc": _num(a.get("rc")), "gva": _num(a.get("gva")),
        "seen_pos": _num(a.get("seen_pos")), "messages": _num(a.get("messages")),
        "dst": _num(a.get("dst")), "dir": _num(a.get("dir")),
        "ground": ground,
    }


def _normalize_opensky(s: list, now: float) -> dict:
    """OpenSky state vector -> internal shape.

    OpenSky publishes no NIC/NACp, so `nic`/`nac_p` stay None and the
    integrity detector simply does not fire. Altitudes arrive in metres and
    velocity in m/s; both are converted so downstream thresholds (feet, knots)
    stay valid.
    """
    # 0 icao24 · 1 callsign · 2 country · 3 time_position · 4 last_contact
    # 5 lon · 6 lat · 7 baro_alt(m) · 8 on_ground · 9 velocity(m/s)
    # 10 true_track · 11 vertical_rate(m/s) · 13 geo_alt(m) · 14 squawk
    def at(i):
        return s[i] if len(s) > i else None

    ground = bool(at(8))
    baro_m, geo_m = _num(at(7)), _num(at(13))
    vel, vrate = _num(at(9)), _num(at(11))
    tpos = _num(at(3)) or _num(at(4))
    return {
        "hex": (at(0) or "").strip(),
        "flight": (at(1) or "").strip(),
        "reg": "", "type": "", "source": "opensky",
        "country": icao_country(at(0) or ""),
        "lat": _num(at(6)), "lon": _num(at(5)),
        "alt": 0 if ground else (round(baro_m * M_TO_FT) if baro_m is not None else None),
        "alt_geom": round(geo_m * M_TO_FT) if geo_m is not None else None,
        "gs": round(vel * MS_TO_KT, 1) if vel is not None else None,
        "track": _num(at(10)),
        "track_rate": None, "roll": None,
        "baro_rate": round(vrate * M_TO_FT * 60) if vrate is not None else None,
        "geom_rate": None,
        "squawk": at(14), "emergency": None, "category": "",
        # Not published by OpenSky — integrity detection is degraded, not faked.
        "nic": None, "nac_p": None, "sil": None, "rc": None, "gva": None,
        "seen_pos": round(now - tpos, 1) if tpos else None,
        "messages": None, "dst": None, "dir": None,
        "ground": ground,
    }


def _bbox(lat: float, lon: float, radius_nm: float) -> tuple[float, float, float, float]:
    """Point+radius -> (lamin, lomin, lamax, lomax) for OpenSky."""
    dlat = radius_nm / 60.0
    dlon = radius_nm / (60.0 * max(math.cos(math.radians(lat)), 0.1))
    return (lat - dlat, lon - dlon, lat + dlat, lon + dlon)


def _readsb_sources() -> list[str]:
    seen, out = set(), []
    for b in (config.ADSB_BASE, config.ADSB_FALLBACK, "https://opendata.adsb.fi/api"):
        b = (b or "").rstrip("/")
        if b and b not in seen:
            seen.add(b)
            out.append(b)
    return out


def _try_readsb(base: str, lat, lon, radius_nm, attempts: list) -> list[dict]:
    path = f"/v2/lat/{lat}/lon/{lon}/dist/{radius_nm}"
    if _cooling(base, attempts):
        return []
    try:
        r = _http().get(base + path)
        if r.status_code == 429:
            _penalise(base)
        if r.status_code != 200:
            attempts.append({"source": base, "ok": False, "n": 0,
                             "note": f"HTTP {r.status_code}"})
            return []
        ac = r.json().get("ac", []) or []
        out = [_normalize(a) for a in ac]
        out = [a for a in out if a["hex"] and a["lat"] is not None and a["lon"] is not None]
        attempts.append({"source": base, "ok": bool(out), "n": len(out),
                         "note": "" if out else "empty (rate-limited or no coverage)"})
        return out
    except Exception as e:                                    # noqa: BLE001
        attempts.append({"source": base, "ok": False, "n": 0,
                         "note": f"{type(e).__name__}: {str(e)[:80]}"})
        return []



# --- OpenSky OAuth2 (client credentials) ------------------------------------
# OpenSky retired basic auth in 2025; new accounts must use the client
# credentials flow. Tokens live ~30 min, so one is cached and refreshed a
# minute early. Legacy user/pass is still honoured if that is all that is set.
_OPENSKY_TOKEN_URL = ("https://auth.opensky-network.org/auth/realms/"
                      "opensky-network/protocol/openid-connect/token")
_os_token: dict = {"access_token": None, "expires_at": 0.0}


def _opensky_bearer() -> str | None:
    """Return a valid access token, minting one if needed. None if unconfigured."""
    cid = getattr(config, "OPENSKY_CLIENT_ID", "")
    secret = getattr(config, "OPENSKY_CLIENT_SECRET", "")
    if not (cid and secret):
        return None
    if _os_token["access_token"] and time.time() < _os_token["expires_at"]:
        return _os_token["access_token"]
    try:
        r = _http().post(
            _OPENSKY_TOKEN_URL,
            data={"grant_type": "client_credentials",
                  "client_id": cid, "client_secret": secret},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=httpx.Timeout(12.0, connect=8.0),
        )
        if r.status_code != 200:
            log.warning("opensky token HTTP %s", r.status_code)
            return None
        js = r.json() or {}
        tok = js.get("access_token")
        if not tok:
            return None
        _os_token["access_token"] = tok
        _os_token["expires_at"] = time.time() + max(60, int(js.get("expires_in", 1800)) - 60)
        return tok
    except Exception as e:                                    # noqa: BLE001
        log.warning("opensky token error: %s", e)
        return None


def _try_opensky(lat, lon, radius_nm, attempts: list) -> list[dict]:
    lamin, lomin, lamax, lomax = _bbox(lat, lon, radius_nm)
    url = ("https://opensky-network.org/api/states/all"
           f"?lamin={lamin:.4f}&lomin={lomin:.4f}&lamax={lamax:.4f}&lomax={lomax:.4f}")
    if _cooling("opensky", attempts):
        return []
    auth = None
    headers = {}
    mode = "anon"
    bearer = _opensky_bearer()
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
        mode = "oauth2"
    elif config.OPENSKY_USER and config.OPENSKY_PASS:
        auth = (config.OPENSKY_USER, config.OPENSKY_PASS)
        mode = "basic"
    try:
        r = _http().get(url, auth=auth, headers=headers,
                        timeout=httpx.Timeout(12.0, connect=8.0))
        if r.status_code == 429:
            _penalise("opensky")
        if r.status_code != 200:
            attempts.append({"source": "opensky", "ok": False, "n": 0,
                             "note": f"HTTP {r.status_code} ({mode})"})
            return []
        js = r.json() or {}
        now = js.get("time") or time.time()
        states = js.get("states") or []
        out = [_normalize_opensky(s, now) for s in states]
        out = [a for a in out if a["hex"] and a["lat"] is not None and a["lon"] is not None]
        attempts.append({"source": "opensky", "ok": bool(out), "n": len(out),
                         "note": mode if out else f"empty ({mode})"})
        return out
    except Exception as e:                                    # noqa: BLE001
        attempts.append({"source": "opensky", "ok": False, "n": 0,
                         "note": f"{type(e).__name__}: {str(e)[:80]}"})
        return []


def fetch_region(lat: float, lon: float, radius_nm: int,
                 budget_s: float = 22.0) -> list[dict]:
    """Normalized aircraft for one point+radius query.

    Never raises. Falls back through the source ladder and finally to the
    last-good cache, so a feed outage degrades the picture instead of
    erasing it. Inspect FEED_STATUS to see which source answered.
    """
    key = f"{lat:.3f},{lon:.3f},{radius_nm}"
    attempts: list[dict] = []

    deadline = time.time() + budget_s

    for base in _readsb_sources():
        if time.time() >= deadline:
            attempts.append({"source": base, "ok": False, "n": 0,
                             "note": "skipped — fetch budget spent"})
            continue
        out = _try_readsb(base, lat, lon, radius_nm, attempts)
        if out:
            _CACHE[key] = {"ts": time.time(), "ac": out, "source": base, "degraded": False}
            _cache_save()
            FEED_STATUS.update(source=base, degraded=False, stale=False,
                               last_success=time.time(), attempts=attempts)
            return out

    out = ([] if time.time() >= deadline
           else _try_opensky(lat, lon, radius_nm, attempts))
    if out:
        _CACHE[key] = {"ts": time.time(), "ac": out, "source": "opensky", "degraded": True}
        _cache_save()
        FEED_STATUS.update(source="opensky", degraded=True, stale=False,
                           last_success=time.time(), attempts=attempts)
        log.warning("adsb: readsb sources empty; serving OpenSky (no NIC/NACp) for %s", key)
        return out

    cached = _CACHE.get(key)
    if cached and (time.time() - cached["ts"]) < _CACHE_TTL:
        age = round(time.time() - cached["ts"])
        attempts.append({"source": "cache", "ok": True, "n": len(cached["ac"]),
                         "note": f"stale {age}s"})
        FEED_STATUS.update(source=f'{cached["source"]} (cached)', degraded=cached["degraded"],
                           stale=True, attempts=attempts)
        log.warning("adsb: every source empty for %s — serving %ss-old cache", key, age)
        return cached["ac"]

    FEED_STATUS.update(source=None, degraded=False, stale=False, attempts=attempts)
    log.error("adsb: no aircraft for %s — attempts=%s", key, attempts)
    return []
