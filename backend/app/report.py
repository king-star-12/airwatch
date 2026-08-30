"""Case file → a document someone can act on, file, or put in front of a lawyer.

An investigation that only exists inside a dashboard has a short life. The
moment a finding has to travel — to an airline's ops centre, an insurer, a
regulator, a board — it has to become a document, and a document is trusted
for reasons a web page is not: it is fixed, it is complete, and you can tell
whether it has been altered.

So the report carries its own integrity check. The case JSON is hashed
(SHA-256) before rendering, and that digest is printed on the document itself.
Re-hash the exported case and compare: if a single field was edited after
issue, the digests differ. The PDF does not merely *describe* the evidence, it
*is* checkable evidence.

Rendering is a two-step, and each step is honest about what it needs:

    1. Compose the case as a self-contained HTML document (always works,
       no dependency, no key).
    2. Convert to PDF with the Nutrient DWS Processor API when a key is
       present. Without one the endpoint serves the HTML instead, which
       prints and archives perfectly well — the case is never withheld
       because a third-party service is unavailable.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
from typing import Any

import httpx

from .config import config

log = logging.getLogger("report")

NUTRIENT_BUILD = "https://api.nutrient.io/build"
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)

_STATUS_COPY = {
    "NOTAM_CONFIRMED":        ("#B42318", "Confirmed by published NOTAM"),
    "SPACE_WEATHER_CONFOUND": ("#B54708", "Natural cause possible — geomagnetic storm"),
    "SPACE_WEATHER_UNSETTLED":("#B54708", "Geomagnetic conditions unsettled"),
    "NATURAL_CAUSE_EXCLUDED": ("#B42318", "Natural cause excluded"),
    "UNCORROBORATED":         ("#475467", "Corroboration unavailable"),
}


def case_digest(case: dict[str, Any]) -> str:
    """SHA-256 over the case, key-ordered so the digest is reproducible."""
    blob = json.dumps(case, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def render_html(case: dict[str, Any]) -> str:
    """Compose the case as a standalone, printable document."""
    digest = case_digest(case)
    region = case.get("region") or {}
    corr = case.get("corroboration") or {}
    space = corr.get("space_weather") or {}
    colour, label = _STATUS_COPY.get(corr.get("status", ""), ("#475467", "—"))

    findings = case.get("findings") or []
    rows = "".join(
        f"<tr><td class='m'>{_e(f.get('aircraft'))}</td>"
        f"<td>{_e(f.get('finding'))}<div class='sub'>{_e(f.get('detail'))}</div></td>"
        f"<td class='m num'>{_e(f.get('anomaly'))}</td>"
        f"<td class='m'>{_e(f.get('band'))}</td></tr>"
        for f in findings[:25]
    ) or "<tr><td colspan='4' class='sub'>No detector fired in this window.</td></tr>"

    alerts = "".join(
        f"<li><span class='m'>{_e(a.get('issued'))}</span> — {_e(a.get('message'))}</li>"
        for a in (space.get("alerts") or [])
    ) or "<li class='sub'>No GNSS-relevant space-weather alerts in the last 48 hours.</li>"

    notams = "".join(
        f"<li><span class='m'>{_e(n.get('id'))}</span> {_e(n.get('text'))}</li>"
        for n in (corr.get("notams") or [])
    ) or "<li class='sub'>No published interference NOTAMs retrieved for this area.</li>"

    # Built without an f-string expression: a backslash inside one is a syntax
    # error before Python 3.12, and this ships in a 3.11 image.
    _USED = " <b>(used)</b>"
    _UNUSED = ' <span class="sub">(not used)</span>'
    srcs = "".join(
        "<li>" + _e(s.get("name")) + " — " + _e(s.get("role"))
        + (_USED if s.get("used") else _UNUSED) + "</li>"
        for s in (corr.get("sources") or [])
    )

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>{_e(case.get('case_id'))}</title><style>
@page {{ size: A4; margin: 18mm 16mm; }}
body {{ font: 10.5pt/1.5 -apple-system,"Segoe UI",Helvetica,Arial,sans-serif; color:#101828; }}
h1 {{ font-size:19pt; margin:0 0 2px; letter-spacing:-.01em; }}
h2 {{ font-size:11pt; margin:20px 0 7px; padding-bottom:4px; border-bottom:1px solid #E4E7EC;
      text-transform:uppercase; letter-spacing:.07em; color:#475467; }}
.m {{ font-family:ui-monospace,"SF Mono",Menlo,monospace; font-size:9pt; }}
.sub {{ color:#667085; font-size:9pt; }}
.num {{ text-align:right; }}
.head {{ display:flex; justify-content:space-between; align-items:flex-start;
         border-bottom:2px solid #101828; padding-bottom:9px; }}
.badge {{ display:inline-block; padding:3px 10px; border-radius:11px; font-size:8.5pt;
          font-weight:700; color:#fff; background:{colour}; }}
.action {{ border-left:3px solid {colour}; background:#F9FAFB; padding:11px 14px; margin:12px 0;
           font-size:11.5pt; font-weight:600; }}
table {{ width:100%; border-collapse:collapse; margin-top:6px; }}
th {{ text-align:left; font-size:8.5pt; text-transform:uppercase; letter-spacing:.05em;
      color:#667085; border-bottom:1px solid #E4E7EC; padding:5px 6px; }}
td {{ padding:6px; border-bottom:1px solid #F2F4F7; vertical-align:top; }}
ul {{ margin:6px 0; padding-left:17px; }} li {{ margin-bottom:4px; }}
.kv {{ display:flex; gap:26px; flex-wrap:wrap; margin-top:8px; }}
.kv div {{ font-size:9pt; }} .kv b {{ display:block; color:#667085; font-weight:600;
  font-size:8pt; text-transform:uppercase; letter-spacing:.05em; }}
footer {{ margin-top:22px; padding-top:9px; border-top:1px solid #E4E7EC;
          font-size:8pt; color:#667085; }}
.seal {{ background:#F9FAFB; border:1px solid #E4E7EC; padding:9px 12px; border-radius:6px;
         margin-top:10px; word-break:break-all; }}
</style></head><body>

<div class="head">
  <div>
    <h1>Navigation Integrity Case</h1>
    <div class="sub">{_e(region.get('name'))} · issued {_e(case.get('generated_at'))}</div>
  </div>
  <div style="text-align:right">
    <div class="m"><b>{_e(case.get('case_id'))}</b></div>
    <div style="margin-top:5px"><span class="badge">{_e(label)}</span></div>
  </div>
</div>

<div class="kv">
  <div><b>Aircraft observed</b>{_e(case.get('aircraft_observed'))}</div>
  <div><b>Aircraft flagged</b>{_e(case.get('aircraft_flagged'))}</div>
  <div><b>Anomaly index</b>{_e((case.get('index') or {}).get('anomaly'))}</div>
  <div><b>Geomagnetic Kp</b>{_e(space.get('kp'))} ({_e(space.get('condition'))})</div>
</div>

<div class="action">{_e(case.get('recommended_action'))}</div>

<h2>What the telemetry shows</h2>
<table><thead><tr><th>Aircraft</th><th>Finding</th><th class="num">Anomaly</th><th>Band</th></tr></thead>
<tbody>{rows}</tbody></table>

<h2>Most significant case in the retention window</h2>
<p class="sub">{_e((case.get('last_significant') or {}).get('recommended_action')
   or 'No flagged case recorded in the last 12 hours.')}
{(' · ' + _e((case.get('last_significant') or {}).get('observed_at'))) if case.get('last_significant') else ''}</p>

<h2>Natural-cause check — space weather</h2>
<p class="sub">{_e(corr.get('summary'))}</p>
<ul>{alerts}</ul>

<h2>Official record — NOTAMs</h2>
<ul>{notams}</ul>

<h2>Provenance</h2>
<ul>{srcs}</ul>
<p class="sub">Detection is deterministic — no language model sits in the decision
path. Corroboration adjusts the recommended action; it can neither create nor
suppress a detection.</p>

<div class="seal">
  <b class="sub">INTEGRITY DIGEST (SHA-256)</b>
  <div class="m">{digest}</div>
  <div class="sub" style="margin-top:4px">Re-hash the exported case JSON and compare.
  A mismatch means the record was altered after issue.</div>
</div>

<footer>
Airwatch · a Clustral AI product · clustralai.com — Situational awareness only.
Not a navigation aid and not for operational flight use. ADS-B integrity fields
are self-reported by the aircraft and evidence GNSS trouble, not intent.
</footer>
</body></html>"""


def to_pdf(html_doc: str) -> bytes | None:
    """Convert with Nutrient DWS. Returns None when unconfigured or on failure."""
    key = getattr(config, "NUTRIENT_API_KEY", "")
    if not key:
        return None
    try:
        r = httpx.post(
            NUTRIENT_BUILD,
            headers={"Authorization": f"Bearer {key}"},
            files={"page.html": ("page.html", html_doc, "text/html")},
            data={"instructions": json.dumps({"parts": [{"html": "page.html"}]})},
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            log.warning("nutrient build -> HTTP %s", r.status_code)
            return None
        return r.content
    except Exception as e:                                    # noqa: BLE001
        log.warning("nutrient build -> %s", type(e).__name__)
        return None


def build(case: dict[str, Any]) -> tuple[bytes, str, str]:
    """Render the case. Returns (body, media_type, filename)."""
    doc = render_html(case)
    name = str(case.get("case_id") or "airwatch-case")
    pdf = to_pdf(doc)
    if pdf:
        return pdf, "application/pdf", f"{name}.pdf"
    return doc.encode("utf-8"), "text/html; charset=utf-8", f"{name}.html"
