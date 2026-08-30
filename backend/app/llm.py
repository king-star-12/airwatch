"""LLM layer — Azure AI Foundry (Grok, non-OpenAI).

Deterministic detectors decide what is anomalous (never the LLM). Grok (a) rules
on a single aircraft with a structured JSON verdict, (b) writes the live airspace
intelligence brief, and (c) answers questions via tools over the live state /
GridDB. Every ruling carries model+version for the hash-chained evidence log.
Env-swappable to any OpenAI-compatible endpoint (e.g. Ollama) for air-gapped use.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Optional

import httpx

from .config import config

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


class LLMUnavailable(RuntimeError):
    pass


def available() -> bool:
    return bool(config.LLM_ENABLED and config.AZURE_INFERENCE_KEY)


def _post(payload: dict, timeout: float = 120.0) -> dict:
    if not available():
        raise LLMUnavailable("LLM disabled or AZURE_INFERENCE_KEY not set")
    url = f"{config.AZURE_INFERENCE_ENDPOINT}/chat/completions?api-version={config.AZURE_API_VERSION}"
    r = httpx.post(url, headers={"Content-Type": "application/json",
                                 "api-key": config.AZURE_INFERENCE_KEY},
                   json=payload, timeout=timeout)
    if r.status_code != 200:
        raise LLMUnavailable(f"inference {r.status_code}: {r.text[:300]}")
    return r.json()


def chat(messages: list[dict], model: Optional[str] = None, max_tokens: int = 3000,
         temperature: float = 0.2, tools: Optional[list] = None) -> dict:
    payload = {"model": model or config.MODEL_PRIMARY, "messages": messages,
               "max_tokens": max_tokens, "temperature": temperature}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    data = _post(payload)
    return {"message": data["choices"][0]["message"],
            "model": data.get("model", payload["model"]), "usage": data.get("usage", {})}


def _extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    m = _JSON_RE.search(text)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


# --- (a) Single-aircraft adjudication (structured) --------------------------
AIRCRAFT_SYS = (
    "You are the adjudication module of a live airspace-integrity monitor. Deterministic "
    "detectors already flagged this aircraft and why; you do NOT re-detect. From the ADS-B "
    "evidence, rule on what is happening and the most likely cause. Use ONLY the evidence. "
    "Reply with STRICT JSON, no prose outside it:\n"
    '{"assessment":"1-2 sentences on what this aircraft is experiencing",'
    '"likely_cause":"gps_jamming|gps_spoofing|emergency|holding|benign|unknown",'
    '"is_interference":true|false,"confidence":0.0-1.0,'
    '"rationale":"cite the specific signals (NIC/NACp, altitude desync, squawk, etc.)",'
    '"recommendation":"what an analyst/controller should do","citations":["signals used"]}'
)


def investigate_aircraft(ctx: dict) -> dict:
    fallback = {
        "assessment": f"{ctx.get('flight') or ctx.get('hex')} flagged by "
                      f"{', '.join(ctx.get('flags', [])) or 'detectors'}.",
        "likely_cause": ("gps_jamming" if "gps" in ctx.get("flags", []) else
                         "emergency" if "emergency" in ctx.get("flags", []) else
                         "holding" if "holding" in ctx.get("flags", []) else "unknown"),
        "is_interference": bool({"gps", "altdesync", "jump"} & set(ctx.get("flags", []))),
        "confidence": 0.6, "rationale": "Deterministic detectors (LLM unavailable).",
        "recommendation": "Cross-check with adjacent aircraft and ground truth.",
        "citations": ctx.get("flags", []), "model": "deterministic-fallback",
    }
    if not available():
        return fallback
    try:
        res = chat([{"role": "system", "content": AIRCRAFT_SYS},
                    {"role": "user", "content": f"AIRCRAFT EVIDENCE:\n{json.dumps(ctx, default=str)[:5000]}"}],
                   model=config.MODEL_PRIMARY, max_tokens=2600, temperature=0.1)
        parsed = _extract_json(res["message"].get("content", "")) or {}
        for k, v in fallback.items():
            parsed.setdefault(k, v)
        parsed["model"] = res["model"]
        return parsed
    except Exception as e:
        fallback["rationale"] = f"{fallback['rationale']} (LLM error: {e})"
        return fallback


# --- (b) Live airspace intelligence brief -----------------------------------
BRIEF_SYS = (
    "You are an airspace-integrity analyst writing a live intelligence brief from real-time "
    "ADS-B evidence. Be specific and quantitative: name the region, the anomaly index, how many "
    "enroute jets show degraded GPS integrity, the interference zones (with coordinates and "
    "aircraft counts), any emergency squawks, and holding. State plainly whether the pattern "
    "indicates active GPS jamming/spoofing and how confident you are, versus a clean control. "
    "Use ONLY the supplied data. Markdown, sections: ## Bottom line, ## The interference picture, "
    "## Aircraft of interest, ## What to watch. Under 320 words."
)


def region_brief(ctx: dict) -> dict:
    if not available():
        return {"markdown": _fallback_brief(ctx), "model": "template-fallback"}
    try:
        res = chat([{"role": "system", "content": BRIEF_SYS},
                    {"role": "user", "content": f"LIVE AIRSPACE:\n{json.dumps(ctx, default=str)[:9000]}"}],
                   model=config.MODEL_FAST, max_tokens=2000, temperature=0.3)
        md = res["message"].get("content", "").strip()
        return {"markdown": md or _fallback_brief(ctx), "model": res["model"]}
    except Exception as e:
        return {"markdown": _fallback_brief(ctx), "model": f"template-fallback ({e})"}


def _fallback_brief(ctx: dict) -> str:
    i = ctx.get("index", {})
    lines = [f"# Airspace brief — {ctx.get('region_name','')}", "",
             "## Bottom line",
             f"Anomaly index **{i.get('anomaly')}** ({i.get('band')}). "
             f"{i.get('degraded')} enroute jets with degraded GPS integrity, "
             f"{i.get('emergencies')} emergencies, {len(ctx.get('zones',[]))} interference zones.", ""]
    if ctx.get("zones"):
        lines.append("## Interference zones")
        for z in ctx["zones"][:5]:
            lines.append(f"- {z['count']} aircraft near {z['lat']}, {z['lon']} (severity {z['severity']})")
    return "\n".join(lines)


# --- (d) Plain-English hero headline ----------------------------------------
HEADLINE_SYS = (
    "You write ONE plain-English sentence for a general audience about live GPS interference "
    "hitting aircraft right now. NO jargon (no NIC/NACp/ADS-B/squawk/index). Make the reader feel "
    "it — real planes, real people, being lied to about where they are, at this moment. Prefer a "
    "specific flight/place from the data. Under 24 words. Return ONLY the sentence, no quotes."
)


def narrate_headline(ctx: dict) -> str:
    if not available():
        return ""
    try:
        res = chat([{"role": "system", "content": HEADLINE_SYS},
                    {"role": "user", "content": json.dumps(ctx, default=str)[:2200]}],
                   model=config.MODEL_FAST, max_tokens=120, temperature=0.5)
        return res["message"].get("content", "").strip().strip('"').strip()
    except Exception:
        return ""


# --- (c) Analyst Q&A with tool-calling --------------------------------------
def answer(question: str, tools_schema: list, tool_impls: dict[str, Callable],
           context_hint: str = "") -> dict:
    if not available():
        return {"answer": "Analyst assistant is offline (no model configured). The live map, "
                          "zones, and detections remain fully available.", "model": "offline",
                "tool_trace": []}
    messages = [
        {"role": "system", "content":
            "You are a live airspace-integrity analyst co-pilot. Answer ONLY from tool results "
            "and context. Be concise; cite callsigns, NIC/NACp, coordinates. " + context_hint},
        {"role": "user", "content": question}]
    trace = []
    try:
        for _ in range(4):
            res = chat(messages, model=config.MODEL_FAST, max_tokens=1800, tools=tools_schema)
            msg = res["message"]
            calls = msg.get("tool_calls") or []
            if not calls:
                return {"answer": msg.get("content", "").strip(), "model": res["model"], "tool_trace": trace}
            messages.append(msg)
            for c in calls:
                fn = c["function"]["name"]
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except Exception:
                    args = {}
                impl = tool_impls.get(fn)
                result = impl(**args) if impl else {"error": f"unknown tool {fn}"}
                trace.append({"tool": fn, "args": args})
                messages.append({"role": "tool", "tool_call_id": c["id"],
                                 "content": json.dumps(result, default=str)[:4000]})
        return {"answer": "(stopped after tool budget)", "model": config.MODEL_FAST, "tool_trace": trace}
    except Exception as e:
        return {"answer": f"Analyst error: {e}", "model": "error", "tool_trace": trace}
