"""Corroboration — the step that turns a detection into a case.

`detectors.py` decides, deterministically, that aircraft over a region are
reporting degraded navigation integrity. That is a finding about *telemetry*.
It is not yet a case, because it cannot answer the question that decides what
anyone does next:

    Is this interference, or is there a natural explanation?

That distinction matters more here than it looks. GNSS integrity does not only
degrade because someone is jamming it. **Space weather degrades it too.** A
geomagnetic storm drives ionospheric scintillation that scatters the L-band
signals GPS depends on, and a solar radio burst can raise the noise floor across
whole continents at once. Both produce exactly the signature this system watches
for — low NIC, low NACp, on many aircraft, over a wide area.

A monitor that skips that check will confidently report jamming every time the
sun is active. So before Airwatch escalates anything, it asks the sun first.

Two sources, in order of what they can rule out:

    SPACE WEATHER   NOAA SWPC — planetary Kp index and live alerts. Free, no
                    key, global. This is the *exculpatory* check: if a
                    geomagnetic storm is running, degraded integrity may be
                    natural and the case should say so.
    NOTAMs          FAA NOTAM API — the authoritative operational record.
                    Optional (needs a free key); when present it is the
                    strongest *corroborating* evidence available, because it
                    means an aviation authority has already published the
                    interference.

Nothing here decides anything. The verdict stays with the detectors; this module
attaches what the physical and official record says, with sources, so a human
can escalate or dismiss with the reason attached.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any

import httpx

from .config import config

log = logging.getLogger("corroborate")

SWPC_KP = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
SWPC_ALERTS = "https://services.swpc.noaa.gov/products/alerts.json"
FAA_NOTAM = "https://external-api.faa.gov/notamapi/v1/notams"
_TIMEOUT = httpx.Timeout(20.0, connect=8.0)

# Kp is the planetary geomagnetic index, 0–9. NOAA's G-scale starts at Kp 5;
# scintillation strong enough to affect GNSS integrity is generally reported
# from about Kp 5 upward, with Kp 4 worth flagging as unsettled rather than
# clean. These thresholds are deliberately conservative: over-calling a storm
# would hand every real jamming event a free excuse.
KP_STORM = 5.0
KP_UNSETTLED = 4.0

# SWPC message codes that matter to GNSS: geomagnetic storms (K-index warnings/
# alerts) and solar radio blackouts (R-scale), which raise the L-band noise floor.
_GNSS_RELEVANT = re.compile(
    r"(geomagnetic|K-index|Kp|radio blackout|X-ray|solar radiation storm|"
    r"proton event|scintillation)", re.I)


def _get_json(url: str, **kw) -> Any:
    try:
        r = httpx.get(url, timeout=_TIMEOUT,
                      headers={"User-Agent": "airwatch/1.0"}, **kw)
        if r.status_code != 200:
            log.warning("%s -> HTTP %s", url.split("/")[2], r.status_code)
            return None
        return r.json()
    except Exception as e:                                    # noqa: BLE001
        log.warning("%s -> %s", url.split("/")[2], type(e).__name__)
        return None


def _space_weather() -> dict[str, Any]:
    """Current geomagnetic state and any live GNSS-relevant SWPC alerts."""
    out: dict[str, Any] = {"available": False, "kp": None, "condition": "unknown",
                           "alerts": [], "source": "NOAA SWPC"}

    kp_rows = _get_json(SWPC_KP)
    if isinstance(kp_rows, list) and kp_rows:
        # Rows arrive oldest-first, either as dicts or as header+list rows.
        last = kp_rows[-1]
        kp = None
        if isinstance(last, dict):
            kp = last.get("Kp")
            when = last.get("time_tag")
        else:
            try:
                kp, when = float(last[1]), last[0]
            except (IndexError, TypeError, ValueError):
                when = None
        try:
            kp = float(kp) if kp is not None else None
        except (TypeError, ValueError):
            kp = None
        if kp is not None:
            out.update(available=True, kp=round(kp, 2), observed_at=when)
            out["condition"] = ("storm" if kp >= KP_STORM
                                else "unsettled" if kp >= KP_UNSETTLED else "quiet")

    alerts = _get_json(SWPC_ALERTS)
    if isinstance(alerts, list):
        fresh = []
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=48)
        for a in alerts[:60]:
            msg = str(a.get("message", ""))
            if not _GNSS_RELEVANT.search(msg):
                continue
            raw = str(a.get("issue_datetime", ""))[:19]
            try:
                issued = dt.datetime.fromisoformat(raw).replace(tzinfo=dt.timezone.utc)
                if issued < cutoff:
                    continue
            except ValueError:
                pass
            headline = " ".join(msg.split())[:200]
            fresh.append({"issued": raw, "message": headline})
        out["alerts"] = fresh[:4]
    return out


def _notams(icao_hint: str | None) -> list[dict[str, Any]]:
    """GPS-interference NOTAMs for a location, when a FAA key is configured."""
    key_id = getattr(config, "FAA_CLIENT_ID", "")
    key_secret = getattr(config, "FAA_CLIENT_SECRET", "")
    if not (key_id and key_secret and icao_hint):
        return []
    data = _get_json(
        f"{FAA_NOTAM}?icaoLocation={icao_hint}&pageSize=30",
        headers={"client_id": key_id, "client_secret": key_secret,
                 "User-Agent": "airwatch/1.0"})
    items = ((data or {}).get("items") or []) if isinstance(data, dict) else []
    out = []
    for it in items:
        core = (((it.get("properties") or {}).get("coreNOTAMData") or {})
                .get("notam") or {})
        text = str(core.get("text", ""))
        if not re.search(r"\bGPS\b|\bGNSS\b|interference|unreliable", text, re.I):
            continue
        out.append({"id": core.get("number"), "text": " ".join(text.split())[:220],
                    "effective": core.get("effectiveStart"),
                    "location": core.get("location")})
    return out[:5]


def configured() -> bool:
    """Space weather needs no key, so corroboration is always available."""
    return True


def corroborate(region_name: str, findings: list[str],
                icao_hint: str | None = None, **_) -> dict[str, Any]:
    """Attach the physical and official record to a detector finding.

    Never raises: if every source is unreachable the case degrades to
    UNCORROBORATED rather than breaking the watch.
    """
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    space = _space_weather()
    notams = _notams(icao_hint)

    kp = space.get("kp")
    storm = space.get("condition") == "storm"
    unsettled = space.get("condition") == "unsettled"

    if notams:
        status = "NOTAM_CONFIRMED"
        summary = (f"{len(notams)} published NOTAM(s) reference GPS/GNSS "
                   f"interference for this area — the authorities have it on record.")
    elif storm:
        status = "SPACE_WEATHER_CONFOUND"
        summary = (f"Geomagnetic storm in progress (Kp {kp}). Ionospheric "
                   f"scintillation can degrade GNSS integrity on its own — treat "
                   f"this as a natural cause until it is excluded.")
    elif unsettled:
        status = "SPACE_WEATHER_UNSETTLED"
        summary = (f"Geomagnetic conditions unsettled (Kp {kp}). Not enough to "
                   f"explain a localised cluster, but worth noting in the record.")
    elif space.get("available"):
        status = "NATURAL_CAUSE_EXCLUDED"
        summary = (f"Geomagnetic conditions quiet (Kp {kp}) with no GNSS-relevant "
                   f"space-weather alerts. A natural cause does not explain this.")
    else:
        status = "UNCORROBORATED"
        summary = ("Corroboration sources unreachable; the finding rests on "
                   "telemetry alone.")

    return {
        "status": status,
        "checked_at": stamp,
        "summary": summary,
        "space_weather": space,
        "notams": notams,
        "sources": [
            {"name": "NOAA Space Weather Prediction Center",
             "url": "https://services.swpc.noaa.gov", "role": "natural-cause check",
             "used": bool(space.get("available"))},
            {"name": "FAA NOTAM API", "url": "https://api.faa.gov",
             "role": "official interference notices", "used": bool(notams)},
        ],
    }
