"""Airspace investigation service — live poll loop + GridDB + Grok + evidence.

A background thread polls each region from adsb.lol, runs the detectors, and keeps
a live snapshot in memory (always served, even when GridDB's IP allow-list blocks
this host). Notable events (emergencies, jamming onset) are written to the
hash-chained evidence log. Persistence is best-effort behind a circuit breaker.
"""
from __future__ import annotations

import datetime as dt
import threading
import time
from collections import deque
from typing import Optional

from . import adsb, airspace, llm, regions
from .config import config
from . import corroborate as _corr
from .evidence import EvidenceLedger
from .griddb import GridDBError, db, to_iso
from .schema import INDEX_COLUMNS, index_container, init_core_containers

UTC = dt.timezone.utc

_STATE: dict[str, dict] = {}          # region_id -> {snapshot, history, index_series, seen_emerg, crit}
_LOCK = threading.Lock()
_STARTED = {"v": False}
_DB_UP = {"v": None}

# Human-scale "Blind Sky" trackers (by aircraft hex), maintained across regions.
_jammed_since: dict[str, float] = {}   # hex -> when it most recently started flying blind
_jammed_hour: dict[str, float] = {}    # hex -> last time seen blind (pruned to 60 min)
_jammed_ever: set[str] = set()         # unique aircraft seen blind since monitoring began
_narration = {"text": "", "ts": 0.0}   # cached Grok one-liner for the hero headline


def _is_blind(ac: dict) -> bool:
    return bool({"gps", "emergency"} & set(ac.get("flags", [])))


# --- circuit breaker ---------------------------------------------------------
def _db_ok() -> bool:
    if _DB_UP["v"] is None:
        try:
            _DB_UP["v"] = db()._client.get("/checkConnection").status_code == 200
        except Exception:
            _DB_UP["v"] = False
    return _DB_UP["v"]


def _safe(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except (GridDBError, Exception):
        return None


def init_db() -> dict:
    up = _db_ok()
    if up:
        _safe(init_core_containers, db())
    return {"griddb_connected": up,
            "note": None if up else "GridDB Cloud rejected this host's IP (allow-list) — "
            "the live monitor runs from memory; add the egress IP to persist."}


# --- state -------------------------------------------------------------------
def _region_state(rid: str) -> dict:
    if rid not in _STATE:
        _STATE[rid] = {"snapshot": None, "history": {}, "index_series": deque(maxlen=240),
                       "seen_emerg": set(), "crit": False}
    return _STATE[rid]


def poll_region(rid: str) -> dict:
    r = regions.get(rid)
    now = time.time()
    aircraft = adsb.fetch_region(r["lat"], r["lon"], r["radius_nm"])
    st = _region_state(rid)
    hist = st["history"]

    cutoff = now - config.HISTORY_MINUTES * 60
    for ac in aircraft:
        ac["_ts"] = now
        dq = hist.setdefault(ac["hex"], deque(maxlen=120))
        dq.append(ac)
    # prune stale aircraft
    for hx in list(hist):
        while hist[hx] and hist[hx][0]["_ts"] < cutoff:
            hist[hx].popleft()
        if not hist[hx]:
            del hist[hx]

    result = airspace.analyze(aircraft, {h: list(d) for h, d in hist.items()})

    # update global "flying blind" trackers by hex
    for a in result["aircraft"]:
        hx = a["hex"]
        if _is_blind(a):
            _jammed_since.setdefault(hx, now)
            _jammed_hour[hx] = now
            _jammed_ever.add(hx)
        else:
            _jammed_since.pop(hx, None)
    hcut = now - 3600
    for hx in [h for h, t in _jammed_hour.items() if t < hcut]:
        _jammed_hour.pop(hx, None)

    def _mins(hx):
        return round((now - _jammed_since[hx]) / 60.0, 1) if hx in _jammed_since else 0.0

    ts_iso = dt.datetime.fromtimestamp(now, UTC).isoformat()
    snapshot = {
        "region": {k: r[k] for k in ("id", "name", "lat", "lon", "radius_nm", "bbox", "why")},
        "updated": ts_iso, "epoch": now,
        "index": result["index"], "zones": result["zones"],
        "aircraft": [{**_trim(a), "blind_min": _mins(a["hex"]) if _is_blind(a) else 0.0}
                     for a in result["aircraft"]],
        "top": [{**_trim(a, full=True), "blind_min": _mins(a["hex"])} for a in result["top"]],
        "detections": [_flat_det(a) for a in result["aircraft"] if a["dets"]],
    }
    st["snapshot"] = snapshot
    st["index_series"].append({"ts": ts_iso, "anomaly": result["index"]["anomaly"],
                               "degraded": result["index"]["degraded"],
                               "emergencies": result["index"]["emergencies"]})
    _persist(rid, snapshot, result)
    _log_events(rid, snapshot, result)
    return snapshot


def _trim(a: dict, full: bool = False) -> dict:
    out = {k: a.get(k) for k in ("hex", "flight", "reg", "type", "country", "lat", "lon",
           "alt", "gs", "track", "squawk", "emergency", "nic", "nac_p", "anomaly", "band",
           "flags", "ground")}
    if full:
        out["dets"] = a.get("dets", [])
    return out


def _flat_det(a: dict) -> dict:
    return {"hex": a["hex"], "flight": a["flight"], "anomaly": a["anomaly"],
            "lat": a["lat"], "lon": a["lon"], "band": a["band"],
            "dets": [{"code": d["code"], "label": d["label"], "detail": d["detail"],
                      "score": d["score"]} for d in a["dets"]]}


# --- persistence (best-effort) ----------------------------------------------
def _persist(rid: str, snapshot: dict, result: dict) -> None:
    if not _db_ok():
        return
    d = db()
    _safe(init_core_containers, d)
    idx = snapshot["index"]
    name = index_container(rid)
    _safe(d.ensure_container, name, INDEX_COLUMNS, "TIME_SERIES", True)
    _safe(d.put_rows, name, [[snapshot["updated"], idx["anomaly"], idx["degraded"],
          idx["emergencies"], idx["holding"], idx["dark"], len(snapshot["zones"]), idx["total"]]])
    # flagged aircraft: snapshot rows + timestamped positions
    flagged = [a for a in result["aircraft"] if a["dets"]]
    if flagged:
        _safe(d.put_rows, "aircraft", [[a["hex"], rid, a["flight"], a["reg"], a["type"],
              a["country"], a["lat"], a["lon"], int(a["alt"] or 0), a["gs"] or 0.0,
              a["track"] or 0.0, str(a["squawk"] or ""), int(a["nic"] or -1),
              int(a["nac_p"] if a["nac_p"] is not None else -1), a["anomaly"], a["band"],
              ",".join(a["flags"]), snapshot["updated"]] for a in flagged])
        _safe(d.put_rows, "positions", [[f"{a['hex']}-{int(snapshot['epoch'])}", snapshot["updated"],
              rid, a["hex"], a["lat"], a["lon"], int(a["alt"] or 0), a["anomaly"]] for a in flagged])


def _log_events(rid: str, snapshot: dict, result: dict) -> None:
    """Write only notable *changes* to the evidence chain (not every poll)."""
    if not _db_ok():
        return
    st = _region_state(rid)
    led = EvidenceLedger(db())
    for a in result["aircraft"]:
        if "emergency" in a["flags"] and a["hex"] not in st["seen_emerg"]:
            st["seen_emerg"].add(a["hex"])
            _safe(led.append, rid, "detector:emergency", "emergency",
                  f"{a['flight'] or a['hex']} squawk {a['squawk']} in {snapshot['region']['name']}",
                  {"hex": a["hex"], "squawk": a["squawk"], "lat": a["lat"], "lon": a["lon"]})
    crit = snapshot["index"]["band"] == "critical"
    if crit and not st["crit"]:
        st["crit"] = True
        _safe(led.append, rid, "engine", "interference_onset",
              f"{snapshot['region']['name']} entered CRITICAL — {snapshot['index']['degraded']} "
              f"jets with degraded GPS integrity, {len(snapshot['zones'])} zones.",
              {"index": snapshot["index"]["anomaly"], "zones": len(snapshot["zones"])})
    elif not crit:
        st["crit"] = False


# --- poll loop ---------------------------------------------------------------
def _refresh_advisories() -> None:
    """Warm ONE advisory per cycle — the stalest.

    Measuring a footprint costs nine upstream queries. Refreshing every
    advisory on every cycle cost thirty six, which on top of the region polls
    exceeded the aggregator rate limit, tripped its cooldown and starved the
    live map of aircraft entirely. Warming the single stalest advisory keeps
    the dashboard responsive within a query budget the feed will actually
    tolerate.
    """
    from . import advisories as _advs, footprint as _fp
    advs = _advs.load_all()
    if not advs:
        return
    stalest = min(advs, key=lambda a: _fp.measured_at(a.id))
    _safe(_fp.measure_cached, stalest)


def _loop():
    rids = list(regions.POLL_REGIONS)
    gap = max(3.0, config.POLL_SECONDS / max(1, len(rids)))
    i = 0
    while True:
        rid = rids[i % len(rids)]
        _safe(poll_region, rid)
        i += 1
        if i % len(rids) == 0:
            _safe(_refresh_narration)
        # One advisory every fifth region cycle. Measurement is the most
        # expensive thing this process does and the footprint it measures
        # changes on the scale of a test window, not a poll.
        if i % (len(rids) * 5) == 0:
            _safe(_refresh_advisories)
        time.sleep(gap)


def start_polling():
    if _STARTED["v"]:
        return
    _STARTED["v"] = True
    # non-blocking: the loop primes each region within one cycle; get_live() also
    # polls on demand, so startup never hangs on the upstream feed.
    threading.Thread(target=_loop, daemon=True).start()


# --- read API ----------------------------------------------------------------
def list_regions() -> dict:
    out = []
    for r in regions.list_regions():
        snap = _STATE.get(r["id"], {}).get("snapshot")
        out.append({**r, "index": snap["index"]["anomaly"] if snap else None,
                    "band": snap["index"]["band"] if snap else None,
                    "loaded": snap is not None})
    return {"regions": out, "default": regions.DEFAULT_REGION}


def get_live(rid: str) -> dict:
    snap = _region_state(rid)["snapshot"]
    if snap is None:
        snap = poll_region(rid)
    return snap


def get_history(rid: str) -> dict:
    return {"series": list(_region_state(rid)["index_series"])}


# --- Blind Sky theatre (the single, human-scale view) ------------------------
def _plain_line(a: dict) -> str:
    who = a.get("flight") or a.get("hex")
    t = a.get("type") or "aircraft"
    place = a.get("place") or regions.nearest_place(a["lat"], a["lon"])
    m = a.get("blind_min", 0) or 0
    if "emergency" in a.get("flags", []):
        return f"{who} near {place} is squawking an emergency code right now."
    if m >= 1:
        return f"{who}, a {t} near {place}, hasn't known its true position for {int(m)} min."
    return f"{who}, a {t} near {place}, just lost reliable GPS."


def theatre() -> dict:
    now = time.time()
    snaps = [s for rid in regions.THEATRE if (s := _STATE.get(rid, {}).get("snapshot"))]
    jammed: dict[str, dict] = {}
    all_ac: list[dict] = []
    zones: list[dict] = []
    seen: set[str] = set()
    for s in snaps:
        pl = s["region"]["name"]
        for z in s["zones"]:
            zones.append({**z, "place": regions.nearest_place(z["lat"], z["lon"]), "region": pl})
        for a in s["aircraft"]:
            if a["hex"] in seen:
                continue
            seen.add(a["hex"])
            all_ac.append({**a, "region": pl})
            if _is_blind(a):
                jammed[a["hex"]] = {**a, "region": pl, "region_id": s["region"]["id"],
                                    "place": regions.nearest_place(a["lat"], a["lon"])}

    worst = sorted(jammed.values(), key=lambda a: (a.get("blind_min", 0), a["anomaly"]),
                   reverse=True)
    worst_cases = [{"hex": a["hex"], "flight": a["flight"] or a["hex"], "type": a["type"],
                    "country": a["country"], "region": a["region"], "region_id": a["region_id"],
                    "place": a["place"], "blind_min": a.get("blind_min", 0),
                    "lat": a["lat"], "lon": a["lon"], "anomaly": a["anomaly"],
                    "flags": a["flags"], "line": _plain_line(a)}
                   for a in worst[:8]]

    healthy = [a for a in all_ac if not _is_blind(a)][:700]
    map_ac = [{"hex": a["hex"], "lat": a["lat"], "lon": a["lon"], "track": a.get("track"),
               "blind": _is_blind(a), "emerg": "emergency" in a.get("flags", []),
               "blind_min": a.get("blind_min", 0), "flight": a["flight"]}
              for a in (list(jammed.values()) + healthy)]

    return {
        "updated": dt.datetime.fromtimestamp(now, UTC).isoformat(),
        "bbox": regions.THEATRE_BBOX, "cities": regions.CITIES,
        "now_blind": len(jammed), "rolling_hour": len(_jammed_hour),
        "session_total": len(_jammed_ever),
        "emergencies": sum(1 for a in jammed.values() if "emergency" in a["flags"]),
        "tracked": len(all_ac),
        "zones": sorted(zones, key=lambda z: -z["severity"]),
        "worst_cases": worst_cases, "aircraft": map_ac,
        "headline": _narration["text"] or (worst_cases[0]["line"] if worst_cases else
                    "Skies clear in the watched airspace — no aircraft reporting GPS loss right now."),
        "regions": [{"id": s["region"]["id"], "name": s["region"]["name"],
                     "blind": s["index"]["degraded"], "zones": len(s["zones"]),
                     "lat": s["region"]["lat"], "lon": s["region"]["lon"]} for s in snaps],
    }


def _refresh_narration():
    if not llm.available():
        return
    now = time.time()
    if now - _narration["ts"] < 30:
        return
    th = theatre()
    if not th["worst_cases"]:
        _narration.update(text="", ts=now)
        return
    txt = llm.narrate_headline({"cases": th["worst_cases"][:4], "now_blind": th["now_blind"],
                                "rolling_hour": th["rolling_hour"]})
    if txt:
        _narration.update(text=txt, ts=now)


def get_aircraft(rid: str, hexid: str) -> dict:
    st = _region_state(rid)
    snap = st["snapshot"] or {}
    ac = next((a for a in snap.get("top", []) if a["hex"] == hexid), None) \
        or next((a for a in snap.get("aircraft", []) if a["hex"] == hexid), None)
    track = [{"ts": h["_ts"], "lat": h["lat"], "lon": h["lon"], "alt": h["alt"],
              "nic": h.get("nic"), "nac_p": h.get("nac_p")}
             for h in st["history"].get(hexid, [])]
    if ac is None:
        raise KeyError(hexid)
    return {"aircraft": ac, "track": track}


# --- LLM actions -------------------------------------------------------------
def investigate_aircraft(rid: str, hexid: str, ac_override: dict | None = None) -> dict:
    # Adjudicate exactly what the analyst is looking at: prefer the aircraft snapshot
    # the client sent (GPS jamming is intermittent — the live state may have already
    # recovered by the time Grok runs), else fall back to the current live snapshot.
    if ac_override and ac_override.get("hex"):
        ac = ac_override
    else:
        ac = get_aircraft(rid, hexid)["aircraft"]
    ctx = {k: ac.get(k) for k in ("hex", "flight", "reg", "type", "country", "lat", "lon",
           "alt", "gs", "track", "squawk", "emergency", "nic", "nac_p", "anomaly", "flags")}
    ctx["detectors"] = ac.get("dets", [])
    ctx["region"] = regions.get(rid)["name"]
    result = llm.investigate_aircraft(ctx)
    if _db_ok():
        _safe(EvidenceLedger(db()).append, rid, f"llm:{result.get('model','?')}", "adjudication",
              f"{ac.get('flight') or hexid}: {result.get('likely_cause')} "
              f"(conf {result.get('confidence')})",
              {"is_interference": result.get("is_interference"), "hex": hexid})
    return {"aircraft": ac, "investigation": result}


def brief_region(rid: str) -> dict:
    snap = get_live(rid)
    ctx = {"region_name": snap["region"]["name"], "why": snap["region"]["why"],
           "index": snap["index"], "zones": snap["zones"],
           "aircraft_of_interest": snap["top"][:8]}
    result = llm.region_brief(ctx)
    if _db_ok():
        _safe(EvidenceLedger(db()).append, rid, f"llm:{result.get('model','?')}", "brief",
              f"Airspace intelligence brief for {snap['region']['name']}.",
              {"index": snap["index"]["anomaly"]})
    return {"markdown": result["markdown"], "model": result["model"]}


# --- evidence ----------------------------------------------------------------
def evidence(rid: Optional[str] = None) -> dict:
    if not _db_ok():
        return {"chain": [], "verification": {"intact": None,
                "note": "GridDB unreachable (IP allow-list) — the evidence chain writes once the egress IP is allowed."}}
    led = EvidenceLedger(db())
    try:
        return {"chain": led.chain(rid), "verification": led.verify(rid)}
    except Exception as e:
        return {"chain": [], "verification": {"intact": None, "error": str(e)}}


def reset() -> dict:
    if _db_ok():
        d = db()
        _safe(d.drop_containers, ["aircraft", "positions", "detections", "evidence_log"])
        for rid in regions.REGIONS:
            _safe(d.drop_containers, [index_container(rid)])
        _safe(init_core_containers, d)
    for st in _STATE.values():
        st["seen_emerg"].clear()
        st["crit"] = False
    return {"reset": True}


# --- analyst Q&A tools -------------------------------------------------------
TOOLS_SCHEMA = [
    {"type": "function", "function": {
        "name": "get_region_state", "description": "Live anomaly index, zones, and counts for a region.",
        "parameters": {"type": "object", "properties": {"region": {"type": "string"}},
                       "required": ["region"]}}},
    {"type": "function", "function": {
        "name": "get_top_aircraft", "description": "The most anomalous aircraft in a region with their flags.",
        "parameters": {"type": "object", "properties": {"region": {"type": "string"}},
                       "required": ["region"]}}},
    {"type": "function", "function": {
        "name": "get_aircraft", "description": "Full detail for one aircraft by hex.",
        "parameters": {"type": "object", "properties": {
            "region": {"type": "string"}, "hex": {"type": "string"}}, "required": ["region", "hex"]}}},
]


def _tool_region(region: str):
    snap = _STATE.get(region, {}).get("snapshot")
    if not snap:
        return {"error": "region not loaded"}
    return {"region": snap["region"]["name"], "index": snap["index"], "zones": snap["zones"]}


def _tool_top(region: str):
    snap = _STATE.get(region, {}).get("snapshot")
    return snap["top"] if snap else {"error": "region not loaded"}


def _tool_aircraft(region: str, hex: str):
    try:
        return get_aircraft(region, hex)["aircraft"]
    except KeyError:
        return {"error": "aircraft not found"}


def ask(question: str, rid: str) -> dict:
    impls = {"get_region_state": _tool_region, "get_top_aircraft": _tool_top,
             "get_aircraft": _tool_aircraft}
    return llm.answer(question, TOOLS_SCHEMA, impls,
                      context_hint=f"Active region is '{rid}'.")


# Most significant case seen per region, so the record survives the event.
# Interference runs to a schedule and stops; "nothing right now" is not the
# same statement as "nothing here", and only one of them is useful to an
# operator deciding whether to route freight through this airspace.
_PEAK: dict[str, dict] = {}
_PEAK_TTL = 12 * 3600.0


def _remember(rid: str, case: dict) -> None:
    flagged = case.get("aircraft_flagged") or 0
    if not flagged:
        return
    prev = _PEAK.get(rid)
    now = time.time()
    if prev and (now - prev["at"]) < _PEAK_TTL and prev["flagged"] >= flagged:
        return
    _PEAK[rid] = {
        "at": now,
        "flagged": flagged,
        "case_id": case.get("case_id"),
        "observed_at": case.get("generated_at"),
        "aircraft_observed": case.get("aircraft_observed"),
        "recommended_action": case.get("recommended_action"),
        "findings": (case.get("findings") or [])[:6],
    }


def peak_case(rid: str) -> dict | None:
    p = _PEAK.get(rid)
    if not p or (time.time() - p["at"]) > _PEAK_TTL:
        return None
    return {k: v for k, v in p.items() if k != "at"}


# --- the case ----------------------------------------------------------------
def build_case(rid: str, recency: str | None = None) -> dict:
    """A detection plus the open record around it — the unit a human can act on.

    Aeroscan on its own answers "is the telemetry degraded here?". That is a
    score, and a score creates work. A case answers the next two questions a
    reviewer actually asks — is anyone else seeing this, and is there a published
    innocent explanation — and carries the citations for both.
    """
    snap = get_live(rid)
    region = snap.get("region", {}) or {}
    idx = snap.get("index", {}) or {}
    dets = snap.get("detections", []) or []

    # Flatten per-aircraft detector firings into human-readable findings, worst
    # first, so the case leads with the aircraft a reviewer should look at.
    findings = []
    for d in sorted(dets, key=lambda x: -(x.get("anomaly") or 0)):
        who = (d.get("flight") or d.get("hex") or "unknown").strip()
        for sub in (d.get("dets") or []):
            findings.append({
                "aircraft": who,
                "code": sub.get("code"),
                "finding": sub.get("label"),
                "detail": sub.get("detail"),
                "confidence": sub.get("score"),
                "anomaly": d.get("anomaly"),
                "band": d.get("band"),
                "position": [d.get("lat"), d.get("lon")],
            })

    corr = _corr.corroborate(
        region.get("name", rid),
        [f["finding"] or "" for f in findings],
        icao_hint=region.get("icao_hint"),
    )

    # The verdict stays with the deterministic layer. Corroboration only moves
    # the recommended ACTION — it can never create or erase a detection.
    critical = [f for f in findings if f.get("band") == "critical"]
    st = corr["status"]
    if not findings:
        action = "NO ACTION — no detector fired in this window."
    elif st == "NOTAM_CONFIRMED":
        action = (f"CONFIRMED — {len(findings)} aircraft degraded and an aviation "
                  f"authority has already published interference NOTAMs here.")
    elif st == "SPACE_WEATHER_CONFOUND":
        action = ("HOLD — a geomagnetic storm can produce this signature on its own. "
                  "Exclude the natural cause before calling it interference.")
    elif critical and st == "NATURAL_CAUSE_EXCLUDED":
        action = (f"ESCALATE — {len(critical)} aircraft at critical integrity loss with "
                  f"geomagnetic conditions quiet. Natural causes do not explain this.")
    elif critical:
        action = (f"MONITOR — {len(critical)} aircraft at critical integrity loss; "
                  f"corroboration incomplete.")
    else:
        action = "MONITOR — detectors firing below critical; watch the trend."

    case = {
        "case_id": f"AW-{rid.upper()}-{int(snap.get('epoch') or 0)}",
        "region": region,
        "generated_at": corr["checked_at"],
        "index": idx,
        "aircraft_observed": len(snap.get("aircraft", []) or []),
        "aircraft_flagged": len(dets),
        "findings": findings,
        "corroboration": corr,
        "recommended_action": action,
        "provenance": {
            "telemetry": "live ADS-B (community aggregators, multi-source failover)",
            "detection": "deterministic detectors — no model in the decision path",
            "corroboration": "NOAA SWPC space weather" + (
                " + FAA NOTAM" if corr.get("notams") else ""),
        },
    }
    _remember(rid, case)
    case["last_significant"] = peak_case(rid)
    return case
