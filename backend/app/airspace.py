"""Airspace analysis — score every aircraft, then find the interference ZONES.

A single degraded aircraft is noise; a spatial cluster of them is a GPS-jamming
zone — the truth that no single track reveals. Also rolls up a region anomaly
index (0–100) from interference prevalence, emergencies, and zone intensity.
"""
from __future__ import annotations

from collections import defaultdict

from . import detectors

WATCH, HORIZON = 40.0, 75.0


def band(score: float) -> str:
    if score >= HORIZON:
        return "critical"
    if score >= WATCH:
        return "elevated"
    return "normal"


def analyze(aircraft: list[dict], history: dict[str, list[dict]]) -> dict:
    scored = []
    for ac in aircraft:
        hist = history.get(ac["hex"], [])
        dets = detectors.run_all(ac, hist[:-1] if hist else None)
        score = detectors.anomaly_score(dets)
        scored.append({**ac, "anomaly": score, "band": band(score),
                       "flags": [d["code"] for d in dets], "dets": dets})

    airborne = [a for a in scored if not a["ground"]]
    n = max(1, len(airborne))
    degraded = [a for a in airborne if "gps" in a["flags"] or "altdesync" in a["flags"]]
    emergencies = [a for a in scored if "emergency" in a["flags"]]
    holding = [a for a in scored if "holding" in a["flags"]]
    dark = [a for a in scored if "dark" in a["flags"]]
    jumps = [a for a in scored if "jump" in a["flags"]]

    zones = _zones(degraded)
    degraded_frac = len(degraded) / n
    # index is driven by the ABSOLUTE count of severely-jammed enroute jets (even a
    # handful means active interference) plus zone intensity and any emergencies.
    zone_intensity = min(1.0, (max((z["count"] for z in zones), default=0)) / 5.0)
    index = min(100.0, 100 * (0.55 * min(1.0, len(degraded) / 8.0) + 0.45 * zone_intensity)
                + min(30.0, len(emergencies) * 15.0))

    return {
        "aircraft": scored,
        "zones": zones,
        "index": {
            "anomaly": round(index, 1), "band": band(index),
            "total": len(scored), "airborne": len(airborne),
            "degraded": len(degraded), "degraded_pct": round(100 * degraded_frac, 1),
            "emergencies": len(emergencies), "holding": len(holding),
            "dark": len(dark), "jumps": len(jumps),
        },
        "top": sorted(scored, key=lambda a: a["anomaly"], reverse=True)[:12],
    }


def _zones(degraded: list[dict], cell: float = 1.0, min_count: int = 2) -> list[dict]:
    """Grid-bin degraded aircraft; cells with >= min_count => interference zone."""
    bins: dict[tuple, list[dict]] = defaultdict(list)
    for a in degraded:
        key = (round(a["lat"] / cell), round(a["lon"] / cell))
        bins[key].append(a)
    zones = []
    for (gy, gx), members in bins.items():
        if len(members) < min_count:
            continue
        clat = sum(m["lat"] for m in members) / len(members)
        clon = sum(m["lon"] for m in members) / len(members)
        mean_anom = sum(m["anomaly"] for m in members) / len(members)
        severity = min(100.0, len(members) * 12 + mean_anom * 0.4)
        zones.append({
            "lat": round(clat, 3), "lon": round(clon, 3),
            "count": len(members), "severity": round(severity, 1),
            "kind": "gps_interference", "band": band(severity),
            "aircraft": [m["hex"] for m in members][:20],
        })
    return sorted(zones, key=lambda z: z["severity"], reverse=True)
