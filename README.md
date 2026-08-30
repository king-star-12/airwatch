# Airwatch

**Satellite navigation is being jammed over civilian airspace every day. Airwatch watches for it in live aircraft telemetry, then goes and finds out whether anyone else noticed.**

A product of [Clustral AI Labs](https://clustralai.com) · DevNetwork [API + Cloud + AI] Hackathon 2026

---

## The problem

GPS is the invisible utility under aviation, shipping, telecom timing and the power grid. It is also trivially jammable, and interference over the Baltic, the Black Sea and the Eastern Mediterranean has gone from rare to routine. Aircraft fly through it constantly.

The signal is hiding in plain sight. Every ADS-B-equipped aircraft broadcasts two integrity fields — **NIC** (navigation integrity category) and **NACp** (positional accuracy) — that say, in effect, *how much do I trust my own position right now*. When a jet at 38,000 ft reports `NIC 0, NACp 0`, its GPS has stopped being trustworthy.

Nobody is turning that into something a human can act on.

## The gap Airwatch closes

Existing tools stop at a heat map: *this area looks bad.* That is a **score**, and a score creates work — someone still has to figure out whether it means anything.

The two questions any reviewer asks next are:

1. **Is anyone else seeing this?**
2. **Is there a published, innocent explanation?** (an announced military exercise, a scheduled GNSS test)

Question 2 is the one that matters most. An investigation that can only ever say "suspicious" is an alarm, not an investigation. **Airwatch answers both, with citations, and will clear a region as readily as it will escalate one.**

## How it works

```
WATCH ────────► DETECT ────────► CORROBORATE ────────► DECIDE ────────► DOCUMENT
live ADS-B      deterministic    space weather +       human, with      signed case
telemetry       detectors        official NOTAMs       the evidence     file (PDF)
```

**1 · Watch.** Live ADS-B for seven known interference regions, pulled from public community aggregators with multi-source failover and a last-good cache, so the board never silently goes blank.

**2 · Detect.** Six deterministic detectors — degraded GPS integrity, baro/geometric altitude desync, impossible position jumps, stale-position "dark" aircraft, sustained holding, emergency squawks. Crucially these are filtered to *enroute ADS-B jets*, because low integrity on a light aircraft on final is normal and low integrity at cruise is not. **No model sits in the decision path.** The same inputs always produce the same finding, and it can be replayed.

**3 · Corroborate — ask the sun first.** This is the step most GPS-interference monitors skip, and skipping it is how they end up crying wolf.

GNSS integrity does not only degrade because someone is jamming it. **Space weather degrades it too.** A geomagnetic storm drives ionospheric scintillation that scatters the L-band signals GPS depends on; a solar radio burst raises the noise floor across whole continents. Both produce exactly the signature this system watches for — low NIC, low NACp, many aircraft, wide area.

So before Airwatch escalates anything, it checks whether the sun did it:

| Source | Role | Key needed |
|---|---|---|
| **NOAA SWPC** — planetary Kp index + live alerts | **natural-cause check** — rules the storm in or out | none |
| **FAA NOTAM API** | **official record** — has an authority already published interference here? | free, optional |

That produces a verdict a reviewer can actually use:

| Status | Meaning |
|---|---|
| `NOTAM_CONFIRMED` | an aviation authority has already published interference here |
| `SPACE_WEATHER_CONFOUND` | geomagnetic storm running — natural cause is live, **hold** |
| `NATURAL_CAUSE_EXCLUDED` | conditions quiet — the sun does not explain this |

**4 · Decide.** A human gets a case file: what fired, on which aircraft, at what altitude, what the open record says, what was ruled out, and a recommended action — with every claim carrying a citation and a hash-chained evidence trail.

**4 · Decide — and hand over a document.** A dashboard finding has a short life. The moment it has to travel — to an ops centre, an insurer, a regulator — it has to become a document.

The case is rendered as a **Navigation Integrity Case** report and converted to PDF by the **Nutrient DWS Processor API**. It carries its own **SHA-256 integrity digest**, computed over the case before rendering and printed on the document: re-hash the exported case and compare, and a single altered field shows up as a mismatch. The report does not merely describe the evidence — it *is* checkable evidence.

Without a Nutrient key the same document is served as printable HTML. A case is never withheld because a third-party service is unavailable.

## Live example

Real output, live data, no synthetic inputs:

```
CASE:     AW-BALTIC-1788114021       Baltic · Kaliningrad
Observed: 82 aircraft   Flagged: 3   Geomagnetic Kp: 2.67 (quiet)

  EWG7QZ   Degraded GPS integrity   (95.0, critical)
           Enroute at 38000 ft with NIC 0, NACp 0
  CCA878   Degraded GPS integrity   (95.0, critical)
  BTI15M   Tight turn               (45.0, elevated)

NATURAL-CAUSE CHECK: quiet (Kp 2.67), no GNSS-relevant alerts
ACTION:   ESCALATE — 2 aircraft at critical integrity loss with
          geomagnetic conditions quiet. Natural causes do not
          explain this.
```

That last line is the whole product. Not *"something looks wrong"* — a conclusion with the alternative explicitly ruled out.

## Run it

```bash
git clone https://github.com/king-star-12/airwatch && cd airwatch
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
cp .env.example .env          # optional keys; runs without any
cd backend && ../.venv/bin/python -m uvicorn app.main:app --port 8099
```

Open <http://localhost:8099>. **No API key is required** — watch, detect and the natural-cause check all run on public, keyless data. Adding `NUTRIENT_API_KEY` upgrades the case report from HTML to PDF; adding FAA credentials adds official NOTAMs.

```bash
# the case endpoint — detection plus the open record
curl localhost:8099/api/region/baltic/case | jq
```

| Endpoint | Purpose |
|---|---|
| `GET /api/regions` | monitored regions and current index |
| `GET /api/region/{id}/live` | live aircraft, detections, index |
| **`GET /api/region/{id}/case`** | **full case file with corroboration** |
| **`GET /api/region/{id}/report`** | **the case as a PDF/HTML document with integrity digest** |
| `GET /api/region/{id}/history` | index over time |
| `GET /api/evidence` | hash-chained evidence ledger |
| `GET /api/health` | feed health and per-source diagnostics |

## Design decisions worth defending

**The model never decides.** Detection is deterministic code. A language model can narrate a case and answer questions about it, but it cannot create, suppress or alter a detection. That is what makes a finding replayable and auditable — and it is why the system still works with the model switched off entirely.

**Corroboration cannot manufacture a detection.** It moves the recommended *action*, never the finding. A geomagnetic storm downgrades urgency; it does not erase what the telemetry said.

**Rule out before you escalate.** The natural-cause check runs on every case, including the quiet ones. An investigation that can only ever say "suspicious" is an alarm.

**Degrade, never blank.** Multi-source feed failover, per-source cooldown on rate limits, and a disk-backed last-good cache. A monitoring tool that shows an empty screen when a dependency wobbles is worse than useless, because silence reads as "all clear."

**Runs where the data is.** Small footprint, no GPU, no mandatory cloud service. Point it at a local model endpoint and the whole thing runs air-gapped.

## Limitations

Stated plainly, because a monitoring tool that oversells itself is dangerous:

- ADS-B integrity fields are *reported by the aircraft*. They are strong evidence of GNSS trouble, not proof of deliberate jamming.
- Community ADS-B coverage is uneven; sparse regions (active conflict airspace especially) genuinely have few aircraft, and the tool says so rather than inventing coverage.
- The space-weather check uses a *global* index. A planetary Kp cannot prove a specific region was unaffected, only that no planet-wide driver was active — which is why quiet conditions **exclude** a natural cause rather than confirming interference.
- NOTAM coverage depends on the issuing authority and a configured key; no NOTAM is not evidence of no interference.
- **Situational-awareness tool. Not a navigation aid, not for operational flight use.**

## Built on

Python · Starlette · live community ADS-B feeds · **NOAA SWPC** space weather · **FAA NOTAM API** · **Nutrient DWS** for document generation · optional OpenAI-compatible model endpoint for narrative · SHA-256 hash-chained evidence ledger

---

**Airwatch is a Clustral AI product.** Clustral builds investigative agents for regulated environments — systems that watch continuously with small models, investigate what they find, and produce evidence that holds up when someone asks you to justify why you acted when you did. Airwatch applies that pattern to navigation integrity.

`business@clustralai.com` · [clustralai.com](https://clustralai.com)
