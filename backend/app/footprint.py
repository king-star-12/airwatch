"""Predicted vs observed — measuring the advisory against the sky.

The FAA publishes, ahead of each military GPS test, a circle inside which
satellite navigation "may not be available". The circle is large: 366 nautical
miles at altitude for White Sands. It is a forecast of a worst case, and by the
account of people who help write the standards it is a conservative one, because
terrain blocks most of the propagation it assumes.

Nobody checks it afterwards.

That gap is not academic. Pilots who repeatedly fly through an advisory circle
and see nothing learn to discount the next one, and the FAA's own record of what
actually happens is built from voluntary pilot reports — roughly ninety in eight
years, against a hundred and seventy three logged by a single air traffic
facility in six months. The official picture is an order of magnitude short of
reality, and nothing in the system closes that gap.

Aircraft, though, are already telling us. Every ADS-B transmission carries the
integrity of the position it reports, so the affected area can be measured
directly and continuously, without asking a pilot to file anything.

This module does that measurement and produces three answers:

    OBSERVED RADIUS   how far from the test centre degraded aircraft actually
                      appear, against the radius the FAA published
    OUTSIDE THE BOX   degraded aircraft beyond the published circle — the case
                      the advisory did not cover and nobody is looking for
    IMPACT            how many aircraft, at what altitudes, and which operators

None of it accuses anyone of anything. It measures a published claim against
observable fact and shows the working.
"""
from __future__ import annotations

import logging
import math
from typing import Any

import time

from . import adsb
from .advisories import Advisory

log = logging.getLogger("footprint")

# adsb.lol answers a point+radius query, capped at 250 nm. A 366 nm footprint
# therefore needs several overlapping samples: one on the centre and a ring
# around it. The ring is deliberately pushed past the published edge so the
# "outside the box" question can actually be answered — sampling only the
# advisory area would guarantee finding nothing beyond it.
_QUERY_NM = 250
_SAMPLE_GAP_S = 7.0
_RING_POINTS = 6


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3440.065                                   # earth radius, nautical miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _offset(lat: float, lon: float, bearing_deg: float, dist_nm: float) -> tuple[float, float]:
    r = 3440.065
    br, p1, l1 = math.radians(bearing_deg), math.radians(lat), math.radians(lon)
    d = dist_nm / r
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(br))
    l2 = l1 + math.atan2(math.sin(br) * math.sin(d) * math.cos(p1),
                         math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


def _sample_points(adv: Advisory) -> list[tuple[float, float]]:
    """Centre plus a ring, covering the published circle and a margin past it."""
    pts = [(adv.lat, adv.lon)]
    reach = adv.max_radius_nm
    if reach > _QUERY_NM * 0.8:
        ring = min(reach, _QUERY_NM)               # ring radius, keeps overlap
        for i in range(_RING_POINTS):
            pts.append(_offset(adv.lat, adv.lon, i * (360 / _RING_POINTS), ring))
    return pts


def _degraded(a: dict) -> bool:
    """Integrity loss on an aircraft where it means something.

    The population filter matters more than the threshold. Low integrity on a
    light aircraft on final is ordinary; on a jet at cruise reporting through
    ADS-B it is not. Without this filter the measurement is dominated by old
    transponders and reads as interference everywhere.
    """
    nic, nacp = a.get("nic"), a.get("nac_p")
    if nic is None and nacp is None:
        return False                                # source carries no integrity fields
    alt = a.get("alt")
    if not isinstance(alt, (int, float)) or alt < 10000:
        return False
    src = str(a.get("source") or "")
    if src and not src.startswith("adsb"):
        return False                                # MLAT/TIS-B position is not the aircraft's own
    return (nic == 0) or (nacp == 0)


# Measuring a 366 nm footprint means nine paced queries, so it is far too slow
# to run inside a page load. Results are cached and refreshed in the background;
# a measurement minutes old is still a measurement, and the alternative is a
# dashboard that hangs for half a minute.
_MEASURED: dict[str, dict] = {}
_MEASURE_TTL = 1800.0          # 30 min: a footprint does not move quickly, and
                               # each refresh costs nine upstream queries


def measured_at(advisory_id: str) -> float:
    """Epoch of the last measurement, 0 if never — used to pick the stalest."""
    m = _MEASURED.get(advisory_id)
    return m["_at"] if m else 0.0


def cached(advisory_id: str) -> dict | None:
    m = _MEASURED.get(advisory_id)
    if not m:
        return None
    age = time.time() - m["_at"]
    if age > _MEASURE_TTL * 3:
        return None
    out = {k: v for k, v in m.items() if k != "_at"}
    out["age_seconds"] = round(age)
    return out


def measure_cached(adv: Advisory, force: bool = False) -> dict:
    """Serve the cached measurement, refreshing it when stale."""
    m = _MEASURED.get(adv.id)
    if m and not force and (time.time() - m["_at"]) < _MEASURE_TTL:
        out = {k: v for k, v in m.items() if k != "_at"}
        out["age_seconds"] = round(time.time() - m["_at"])
        return out
    fresh = measure(adv)
    fresh["_at"] = time.time()
    _MEASURED[adv.id] = fresh
    out = {k: v for k, v in fresh.items() if k != "_at"}
    out["age_seconds"] = 0
    return out


def measure(adv: Advisory) -> dict[str, Any]:
    """Compare the published footprint with what aircraft are reporting."""
    seen: dict[str, dict] = {}
    points = _sample_points(adv)
    for i, (lat, lon) in enumerate(points):
        if i:
            # The aggregator limits bursts, not just averages: seven queries at
            # 1.2s apart is ~49/min instantaneously, which trips the limit even
            # though the hourly rate looks modest. Spread them out. A footprint
            # measurement taking a minute is fine; being throttled costs the
            # live map its aircraft, which is not.
            time.sleep(_SAMPLE_GAP_S)
        for a in adsb.fetch_region(lat, lon, _QUERY_NM):
            h = a.get("hex")
            if h and h not in seen and a.get("lat") is not None:
                seen[h] = a

    # A sample is only evidence if the source actually carries integrity fields.
    # OpenSky does not publish NIC/NACp, so aircraft sourced from the fallback
    # can never appear degraded — counting them as "clear" would manufacture
    # confidence out of a blind spot.
    measurable = [a for a in seen.values()
                  if a.get("nic") is not None or a.get("nac_p") is not None]

    inside, outside, affected = [], [], []
    for a in seen.values():
        d = _haversine_nm(adv.lat, adv.lon, a["lat"], a["lon"])
        predicted = adv.predicted_radius_for(a.get("alt"))
        rec = {
            "hex": a.get("hex"), "flight": (a.get("flight") or "").strip(),
            "alt": a.get("alt"), "dist_nm": round(d, 1),
            "predicted_nm": predicted,
            "nic": a.get("nic"), "nac_p": a.get("nac_p"),
            "lat": round(a["lat"], 3), "lon": round(a["lon"], 3),
        }
        within = d <= predicted
        (inside if within else outside).append(rec)
        if _degraded(a):
            rec["within_published_area"] = within
            affected.append(rec)

    deg_in = [r for r in affected if r["within_published_area"]]
    deg_out = [r for r in affected if not r["within_published_area"]]
    observed_nm = max((r["dist_nm"] for r in deg_in), default=None)

    # Only claim a ratio when the sample can support one. A handful of aircraft
    # cannot establish the true edge of an interference footprint, and quoting
    # "8% of published" off three data points would be exactly the kind of
    # confident-but-unfounded number this system exists to argue against.
    ratio = None
    if observed_nm is not None and adv.max_radius_nm and len(deg_in) >= 3:
        ratio = round(observed_nm / adv.max_radius_nm, 3)

    coverage = (len(measurable) / len(seen)) if seen else 0.0

    if not seen:
        confidence, reading = "none", "No aircraft sampled — cannot measure."
    elif not measurable:
        confidence = "none"
        reading = (f"{len(seen)} aircraft sampled, but none from a source that reports "
                   f"navigation integrity. This is a blind spot, not a clear result.")
    elif not affected:
        confidence = ("good" if len(measurable) >= 40 and coverage >= 0.6
                      else "thin")
        reading = (f"No integrity loss observed across {len(measurable)} aircraft that report "
                   f"integrity ({coverage:.0%} of those sampled). Either the test is not running, "
                   f"or it is not producing measurable effects right now.")
    elif ratio is not None:
        reading = (f"Degraded aircraft observed out to {observed_nm:.0f} nm against a published "
                   f"{adv.max_radius_nm} nm — {ratio:.0%} of the advertised radius.")
        confidence = "good" if len(deg_in) >= 8 else "indicative"
    else:
        reading = (f"{len(affected)} aircraft with degraded integrity, too few to establish the "
                   f"true edge of the footprint.")
        confidence = "indicative"

    return {
        "advisory": adv.as_dict(),
        "sampled_aircraft": len(seen),
        "integrity_reporting": len(measurable),
        "integrity_coverage": round(coverage, 3),
        "sample_points": len(_sample_points(adv)),
        "affected_total": len(affected),
        "affected_inside": len(deg_in),
        "affected_outside": len(deg_out),
        "observed_radius_nm": observed_nm,
        "published_radius_nm": adv.max_radius_nm,
        "observed_vs_published": ratio,
        "confidence": confidence,
        "reading": reading,
        "outside_the_box": sorted(deg_out, key=lambda r: -r["dist_nm"])[:10],
        "affected": sorted(affected, key=lambda r: -r["dist_nm"])[:25],
        "method": {
            "integrity": "NIC or NACp reported zero",
            "population": "ADS-B aircraft at or above 10,000 ft (excludes MLAT/TIS-B and low GA)",
            "sampling": f"{len(_sample_points(adv))} overlapping {_QUERY_NM} nm queries",
            "caveat": ("Absence of observed degradation is not proof the test was inactive; "
                       "coverage and traffic density vary."),
        },
    }
