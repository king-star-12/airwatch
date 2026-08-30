"""Open-source corroboration — the step that turns a detection into a case.

The detectors in `detectors.py` decide, deterministically, that aircraft over a
region are reporting degraded navigation integrity. That is a finding about
*telemetry*. It is not yet a case, because it cannot answer the first question
any reviewer asks: **is anyone else seeing this, and is there an innocent
explanation?**

GNSS interference is unusual among anomalies in that the corroborating record is
public and live: aviation authorities publish interference NOTAMs, EASA and the
FAA issue safety bulletins, and incidents get reported within hours. So the
corroboration layer runs a live web search, scoped to the region and the time
window of the finding, and sorts what it gets back into three buckets:

    CORROBORATING  independent reporting consistent with interference here
    EXCULPATORY    a benign published cause — an announced exercise, a scheduled
                   GNSS test, a known outage. This is the bucket that matters
                   most, because an investigation that cannot clear a region is
                   just an alarm.
    CONTEXT        background on the area, neither confirming nor clearing

Nothing here decides anything. Classification is keyword-deterministic and the
verdict still comes from the detectors; this module only attaches *what the open
record says*, with citations, so a human can act on the finding or dismiss it.

Search provider: SerpApi (structured, real-time, no scraping). Absent an API key
the module degrades to UNCORROBORATED and the rest of the system is unaffected —
the same posture as every other optional dependency in this codebase.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any

import httpx

from .config import config

log = logging.getLogger("corroborate")

SERPAPI = "https://serpapi.com/search"
_TIMEOUT = httpx.Timeout(20.0, connect=8.0)

# Terms that mark a result as independently reporting interference.
_CORROBORATE = (
    "gps jamming", "gps spoofing", "gnss jamming", "gnss spoofing",
    "gps interference", "gnss interference", "navigation interference",
    "loss of gnss", "position unreliable", "rfi", "jammed",
)

# Terms that mark a published, benign explanation. Deliberately narrow: a false
# "exculpatory" is far more costly than a missed one, because it clears a region
# that may genuinely be degraded.
_EXCULPATE = (
    "scheduled gps test", "gps testing", "planned exercise", "military exercise",
    "notam cancelled", "exercise notam", "training exercise", "gps interference testing",
)

# Authorities whose word carries more weight than a news aggregator.
_AUTHORITATIVE = (
    "faa.gov", "easa.europa.eu", "eurocontrol.int", "icao.int", "notams.aim.faa.gov",
    "ntsb.gov", "gpsjam.org", "flightradar24.com", "ainonline.com", "aviationweek.com",
    "opsgroup.aero", "skybrary.aero",
)


def configured() -> bool:
    return bool(getattr(config, "SERPAPI_KEY", ""))


def _bucket(text: str) -> str:
    t = text.lower()
    if any(k in t for k in _EXCULPATE):
        return "exculpatory"
    if any(k in t for k in _CORROBORATE):
        return "corroborating"
    return "context"


def _authoritative(link: str) -> bool:
    return any(d in (link or "").lower() for d in _AUTHORITATIVE)


def _queries(region_name: str, findings: list[str]) -> list[str]:
    """Two angles, deliberately: confirm, and try to clear.

    Searching only for confirmation is how an investigation talks itself into a
    conclusion. The second query exists to find the reason this is *nothing*.
    """
    place = re.sub(r"\s*·.*$", "", region_name).strip()
    qs = [f"{place} GPS jamming OR GNSS interference aircraft"]
    if any("spoof" in f.lower() or "jump" in f.lower() for f in findings):
        qs.append(f"{place} GPS spoofing aircraft position")
    qs.append(f"{place} NOTAM GPS interference OR military exercise")
    return qs


def _search(query: str, recency: str = "w") -> list[dict[str, Any]]:
    """One SerpApi call. `recency` maps to Google's tbs date filter."""
    params = {
        "engine": "google",
        "q": query,
        "api_key": config.SERPAPI_KEY,
        "num": 10,
        "tbs": f"qdr:{recency}",   # d/w/m — findings are time-scoped by nature
        "hl": "en",
    }
    try:
        r = httpx.get(SERPAPI, params=params, timeout=_TIMEOUT)
        if r.status_code != 200:
            log.warning("serpapi HTTP %s", r.status_code)
            return []
        data = r.json()
    except Exception as e:                                   # noqa: BLE001
        log.warning("serpapi error: %s", type(e).__name__)
        return []

    out: list[dict[str, Any]] = []
    # Google News boxes carry the freshest signal; organic results carry the
    # authorities. Both are worth reading.
    for item in (data.get("top_stories") or []) + (data.get("organic_results") or []):
        title = item.get("title") or ""
        snippet = item.get("snippet") or item.get("source") or ""
        link = item.get("link") or ""
        if not (title and link):
            continue
        out.append({
            "title": title[:180],
            "snippet": str(snippet)[:300],
            "link": link,
            "source": (item.get("source") or item.get("displayed_link") or "")[:80],
            "date": item.get("date") or "",
            "bucket": _bucket(f"{title} {snippet}"),
            "authoritative": _authoritative(link),
        })
    return out


def corroborate(region_name: str, findings: list[str],
                recency: str = "w", max_items: int = 8) -> dict[str, Any]:
    """Attach the open-source record to a detector finding.

    Returns a citation block for the evidence ledger. Never raises: a search
    failure downgrades the case to UNCORROBORATED rather than breaking the watch.
    """
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    if not configured():
        return {"status": "UNCONFIGURED", "checked_at": stamp, "queries": [],
                "citations": [], "summary": "No search provider configured; "
                "finding rests on telemetry alone."}

    seen: set[str] = set()
    citations: list[dict[str, Any]] = []
    queries = _queries(region_name, findings)
    for q in queries:
        for item in _search(q, recency):
            if item["link"] in seen:
                continue
            seen.add(item["link"])
            item["query"] = q
            citations.append(item)

    # Authorities first, then anything that actually classified, then the rest.
    citations.sort(key=lambda c: (not c["authoritative"], c["bucket"] == "context"))
    citations = citations[:max_items]

    corro = [c for c in citations if c["bucket"] == "corroborating"]
    excul = [c for c in citations if c["bucket"] == "exculpatory"]

    if excul:
        status = "BENIGN_EXPLANATION_FOUND"
        summary = (f"{len(excul)} published source(s) offer a benign cause "
                   f"(exercise or scheduled test). Review before escalating.")
    elif corro:
        status = "CORROBORATED"
        summary = (f"{len(corro)} independent source(s) report interference in "
                   f"this area within the window.")
    elif citations:
        status = "CONTEXT_ONLY"
        summary = "No direct reporting found; only background context."
    else:
        status = "UNCORROBORATED"
        summary = ("No open-source reporting found. The finding rests on "
                   "telemetry alone — which is normal for a signal detected early.")

    return {
        "status": status,
        "checked_at": stamp,
        "window": {"d": "24h", "w": "7d", "m": "30d"}.get(recency, recency),
        "queries": queries,
        "counts": {"corroborating": len(corro), "exculpatory": len(excul),
                   "total": len(citations)},
        "citations": citations,
        "summary": summary,
    }
