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
WATCH ─────────► DETECT ─────────► CORROBORATE ─────────► DECIDE
live ADS-B       deterministic     live web search         human, with
telemetry        detectors         (SerpApi)               the evidence
```

**1 · Watch.** Live ADS-B for seven known interference regions, pulled from public community aggregators with multi-source failover and a last-good cache, so the board never silently goes blank.

**2 · Detect.** Six deterministic detectors — degraded GPS integrity, baro/geometric altitude desync, impossible position jumps, stale-position "dark" aircraft, sustained holding, emergency squawks. Crucially these are filtered to *enroute ADS-B jets*, because low integrity on a light aircraft on final is normal and low integrity at cruise is not. **No model sits in the decision path.** The same inputs always produce the same finding, and it can be replayed.

**3 · Corroborate — the part built for this hackathon.** When detectors fire, Airwatch searches the live web via **SerpApi**, scoped to the region and time window, and sorts what comes back:

| Bucket | Meaning |
|---|---|
| **CORROBORATING** | independent reporting consistent with interference here |
| **EXCULPATORY** | a published benign cause — exercise, scheduled test, known outage |
| **CONTEXT** | background, neither confirming nor clearing |

It deliberately runs a *confirming* query and a *clearing* query. Searching only for confirmation is how an investigation talks itself into a conclusion.

**4 · Decide.** A human gets a case file: what fired, on which aircraft, at what altitude, what the open record says, what was ruled out, and a recommended action — with every claim carrying a citation and a hash-chained evidence trail.

### Why SerpApi is load-bearing here

Without it, Airwatch produces `anomaly index 27.5` — a number nobody can act on.
With it, Airwatch produces:

> **ESCALATE** — 4 aircraft reporting degraded navigation integrity over the Baltic, independently corroborated by 3 sources including an EASA advisory.

or, just as valuable:

> **REVIEW** — a published NATO exercise NOTAM covers this window. Read it before escalating.

Live, structured search is what converts telemetry into a defensible conclusion. It is not an enrichment step bolted onto the side; it is the difference between an alarm and an investigation.

## Live example

Real output, live data, no synthetic inputs:

```
CASE:     AW-BALTIC-1788112288
Region:   Baltic · Kaliningrad
Observed: 81 aircraft   Flagged: 4

  CSZ887   Degraded GPS integrity   (anomaly 95.0, critical)
           Enroute at 38000 ft with NIC 0, NACp 0 — position
           integrity consistent with GPS interference.
  PBD6831  Degraded GPS integrity   (anomaly 95.0, critical)
  LBT814   Degraded GPS integrity   (anomaly 95.0, critical)

ACTION:   MONITOR — 3 aircraft at critical integrity loss with no
          open reporting yet. This is what early looks like.
```

## Run it

```bash
git clone https://github.com/<owner>/airwatch && cd airwatch
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
cp .env.example .env          # add SERPAPI_KEY to enable corroboration
cd backend && ../.venv/bin/python -m uvicorn app.main:app --port 8099
```

Open <http://localhost:8099>. No API key is required to see it work — the watch and detect layers run on public data. Adding `SERPAPI_KEY` switches on corroboration and completes the loop.

```bash
# the case endpoint — detection plus the open record
curl localhost:8099/api/region/baltic/case | jq
```

| Endpoint | Purpose |
|---|---|
| `GET /api/regions` | monitored regions and current index |
| `GET /api/region/{id}/live` | live aircraft, detections, index |
| **`GET /api/region/{id}/case`** | **full case file with corroboration** |
| `GET /api/region/{id}/history` | index over time |
| `GET /api/evidence` | hash-chained evidence ledger |
| `GET /api/health` | feed health and per-source diagnostics |

## Design decisions worth defending

**The model never decides.** Detection is deterministic code. A language model can narrate a case and answer questions about it, but it cannot create, suppress or alter a detection. That is what makes a finding replayable and auditable — and it is why the system still works with the model switched off entirely.

**Corroboration cannot manufacture a detection.** Search moves the recommended *action*, never the finding. A benign explanation downgrades urgency; it does not erase what the telemetry said.

**Degrade, never blank.** Multi-source feed failover, per-source cooldown on rate limits, and a disk-backed last-good cache. A monitoring tool that shows an empty screen when a dependency wobbles is worse than useless, because silence reads as "all clear."

**Runs where the data is.** Small footprint, no GPU, no mandatory cloud service. Point it at a local model endpoint and the whole thing runs air-gapped.

## Limitations

Stated plainly, because a monitoring tool that oversells itself is dangerous:

- ADS-B integrity fields are *reported by the aircraft*. They are strong evidence of GNSS trouble, not proof of deliberate jamming.
- Community ADS-B coverage is uneven; sparse regions (active conflict airspace especially) genuinely have few aircraft, and the tool says so rather than inventing coverage.
- Corroboration reflects what is publicly published. Absence of reporting is not absence of interference — early detection means being ahead of the record, which is the entire point.
- **Situational-awareness tool. Not a navigation aid, not for operational flight use.**

## Built on

Python · Starlette · live community ADS-B feeds · **SerpApi** for live corroboration · optional OpenAI-compatible model endpoint for narrative · SHA-256 hash-chained evidence ledger

---

**Airwatch is a Clustral AI product.** Clustral builds investigative agents for regulated environments — systems that watch continuously with small models, investigate what they find, and produce evidence that holds up when someone asks you to justify why you acted when you did. Airwatch applies that pattern to navigation integrity.

`business@clustralai.com` · [clustralai.com](https://clustralai.com)
