"""Airspace anomaly detectors — deterministic, per-aircraft.

The truth in a complex airspace hides *between* tracks. No single aircraft
proves GPS jamming; a cluster of degraded-integrity aircraft does (airspace.py
handles the clustering). These detectors flag each aircraft; the LLM only
narrates and adjudicates — it never decides what is anomalous.
"""
from __future__ import annotations

import math

EMERGENCY_MEANING = {"7500": "unlawful interference (hijack)",
                     "7600": "radio communication failure",
                     "7700": "general emergency"}


def _haversine_nm(lat1, lon1, lat2, lon2):
    R = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1); dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


def d_emergency(ac: dict):
    sq = str(ac.get("squawk") or "")
    if sq in EMERGENCY_MEANING:
        return {"code": "emergency", "score": 1.0, "label": f"Emergency squawk {sq}",
                "detail": f"Transponder {sq} — {EMERGENCY_MEANING[sq]}."}
    if ac.get("emergency"):
        return {"code": "emergency", "score": 0.95, "label": "Emergency declared",
                "detail": f"ADS-B emergency status: {ac['emergency']}."}
    return None


GPS_MIN_ALT = 15000     # enroute only: below this, low integrity is usually benign (GA/approach)


def _enroute_adsb(ac: dict) -> bool:
    """Genuine ADS-B jet at cruise — the population where degraded GPS integrity
    means interference, not an old transponder / MLAT / GA on final."""
    return (not ac.get("ground") and ac.get("lat") is not None
            and ac.get("source") == "adsb_icao"
            and ac.get("alt") is not None and ac["alt"] >= GPS_MIN_ALT)


def d_gps_integrity(ac: dict):
    """Low NIC/NACp on an enroute ADS-B jet — the signature of GPS jamming/
    spoofing (the population filter is what gpsjam uses to avoid GA false-positives)."""
    if not _enroute_adsb(ac):
        return None
    nic, nacp = ac.get("nic"), ac.get("nac_p")
    vals = [v for v in (nic, nacp) if v is not None]
    if not vals:
        return None
    worst = min(vals)
    if worst > 1:            # only near-total integrity loss = jamming (0–1); 5–7 is benign kit
        return None
    sev = 0.95 if worst == 0 else 0.85
    return {"code": "gps", "score": round(sev, 2), "label": "Degraded GPS integrity",
            "detail": f"Enroute at {ac['alt']} ft with NIC {nic}, NACp {nacp} — position "
                      f"integrity consistent with GPS interference."}


def d_alt_desync(ac: dict):
    """Barometric vs geometric altitude disagree beyond QNH offset — geometric
    (GNSS) altitude unreliable, a spoofing/jamming tell."""
    ab, ag = ac.get("alt"), ac.get("alt_geom")
    if not _enroute_adsb(ac) or ab in (None, 0) or ag is None:
        return None
    diff = abs(ab - ag)
    if diff < 1200:
        return None
    sev = min(0.9, 0.4 + (diff - 1200) / 4000 * 0.5)
    return {"code": "altdesync", "score": round(sev, 2), "label": "Baro/GNSS altitude desync",
            "detail": f"Barometric {ab} ft vs geometric {ag} ft (Δ{int(diff)} ft) — "
                      f"GNSS altitude unreliable."}


def d_holding(ac: dict, history: list[dict] | None):
    """Sustained tight turning / heading rotation with little net progress = orbit/hold."""
    tr = ac.get("track_rate")
    turning = tr is not None and abs(tr) >= 1.3
    if history and len(history) >= 4:
        pts = [h for h in history if h.get("track") is not None][-8:]
        if len(pts) >= 4:
            rot = 0.0
            for a, b in zip(pts, pts[1:]):
                d = (b["track"] - a["track"] + 540) % 360 - 180
                rot += d
            net_nm = _haversine_nm(pts[0]["lat"], pts[0]["lon"], pts[-1]["lat"], pts[-1]["lon"])
            if abs(rot) >= 240 and net_nm < 8:
                return {"code": "holding", "score": 0.55, "label": "Holding / orbit",
                        "detail": f"{abs(rot):.0f}° of turn with {net_nm:.1f} nm net travel — "
                                  f"circling."}
    if turning:
        return {"code": "holding", "score": 0.45, "label": "Tight turn",
                "detail": f"Turn rate {tr:.1f}°/s."}
    return None


def d_jump(ac: dict, history: list[dict] | None):
    """Position teleport between updates implies impossible speed = spoof/bad fix."""
    if not history:
        return None
    prev = history[-1]
    dt = ac.get("_ts", 0) - prev.get("_ts", 0)
    if dt <= 0 or dt > 120 or prev.get("lat") is None:
        return None
    nm = _haversine_nm(prev["lat"], prev["lon"], ac["lat"], ac["lon"])
    implied = nm / (dt / 3600.0)
    gs = ac.get("gs") or 0
    if implied > max(800, gs * 3 + 200):
        return {"code": "jump", "score": min(1.0, implied / 2000), "label": "Impossible jump",
                "detail": f"Position moved {nm:.0f} nm in {dt:.0f}s (~{implied:.0f} kn) — "
                          f"physically impossible; spoofed fix."}
    return None


def d_dark(ac: dict):
    """Position gone stale while the aircraft is still transmitting — GNSS loss."""
    sp = ac.get("seen_pos")
    if sp is not None and sp > 60 and not ac.get("ground"):
        return {"code": "dark", "score": min(0.8, 0.4 + sp / 300),
                "label": "Position lost (dark)",
                "detail": f"No position for {sp:.0f}s while still transmitting — GNSS dropout."}
    return None


# NOTE: baro vs GNSS altitude legitimately differs by 1000-3000+ ft at cruise
# (pressure altitude vs true height), so altitude "desync" is NOT a jamming signal
# on its own — d_alt_desync is intentionally excluded from the live pipeline.
def run_all(ac: dict, history: list[dict] | None = None) -> list[dict]:
    out = []
    for det in (d_emergency(ac), d_gps_integrity(ac),
                d_holding(ac, history), d_jump(ac, history), d_dark(ac)):
        if det:
            out.append(det)
    return out


def anomaly_score(dets: list[dict]) -> float:
    """Noisy-OR of detector scores -> 0..100."""
    prod = 1.0
    for d in dets:
        prod *= (1.0 - max(0.0, min(1.0, d["score"])))
    return round(100 * (1 - prod), 1)
