"""Central configuration for the Airwatch backend.

All secrets come from environment variables (loaded from a local .env when
present). Non-secret defaults for this specific Azure + GridDB Cloud deployment
are baked in so the service runs out of the box for the demo, but every value
is overridable via the environment (that is how the Container App is wired).
"""
from __future__ import annotations

import os
from pathlib import Path


def _load_dotenv() -> None:
    """Tiny zero-dependency .env loader (KEY=VALUE, ignores # comments)."""
    for candidate in (
        Path(__file__).resolve().parent.parent / ".env",   # backend/.env
        Path.cwd() / ".env",
    ):
        if not candidate.exists():
            continue
        for line in candidate.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)


_load_dotenv()


class Config:
    # --- GridDB Cloud Web API -------------------------------------------------
    GRIDDB_BASE_URL: str = os.getenv(
        "GRIDDB_BASE_URL",
        "https://cloud5197.griddb.com:443/griddb/v2/gs_clustermfcloud5197/dbs/P17OyGMD",
    )
    GRIDDB_USER: str = os.getenv("GRIDDB_USER", "M01GK7M0Zs-admin")
    GRIDDB_PASSWORD: str = os.getenv("GRIDDB_PASSWORD", "")
    GRIDDB_TIMEOUT: float = float(os.getenv("GRIDDB_TIMEOUT", "30"))

    # --- Azure AI Foundry (Grok, non-OpenAI) ---------------------------------
    # Endpoint is the unified Azure AI Model Inference route for the AIServices
    # resource `clustral-foundry`. Models were chosen for investigation quality;
    # see README "Model choice" for the documented reasoning.
    AZURE_INFERENCE_ENDPOINT: str = os.getenv(
        "AZURE_INFERENCE_ENDPOINT",
        "https://clustral-foundry.services.ai.azure.com/models",
    )
    AZURE_INFERENCE_KEY: str = os.getenv("AZURE_INFERENCE_KEY", "")
    AZURE_API_VERSION: str = os.getenv("AZURE_API_VERSION", "2024-05-01-preview")
    # grok-4.3 = flagship reasoning model for adjudication + final case file.
    MODEL_PRIMARY: str = os.getenv("MODEL_PRIMARY", "grok-4-3")
    # grok-4-1-fast-non-reasoning = snappy tier for narration + analyst Q&A
    # (reasoning models add too much latency for interactive tool-calling chat).
    MODEL_FAST: str = os.getenv("MODEL_FAST", "grok-4-1-fast-nr")

    # --- Airspace anomaly bands ----------------------------------------------
    WATCH_BAND: float = 40.0          # "elevated — worth an analyst's eye"
    EVENT_HORIZON: float = 75.0       # "active interference / distress — act now"

    # --- Live ADS-B ingest ---------------------------------------------------
    ADSB_BASE: str = os.getenv("ADSB_BASE", "https://api.adsb.lol")
    ADSB_FALLBACK: str = os.getenv("ADSB_FALLBACK", "https://api.airplanes.live")
    # OpenSky fallback — answers from datacenter IPs where the readsb
    # aggregators rate-limit them. Optional credentials lift the anonymous
    # cap considerably. Note: OpenSky publishes no NIC/NACp, so integrity
    # detection is degraded whenever it is the active source.
    # OpenSky OAuth2 (client credentials) — the supported path since 2025.
    # Open-source corroboration (SerpApi). Optional: without it the
    # system still watches and detects, findings are just UNCORROBORATED.
    SERPAPI_KEY: str = os.getenv("SERPAPI_KEY", "")
    CORROBORATE_WINDOW: str = os.getenv("CORROBORATE_WINDOW", "w")

    OPENSKY_CLIENT_ID: str = os.getenv("OPENSKY_CLIENT_ID", "")
    OPENSKY_CLIENT_SECRET: str = os.getenv("OPENSKY_CLIENT_SECRET", "")
    # Legacy basic auth — still honoured for pre-2025 accounts.
    OPENSKY_USER: str = os.getenv("OPENSKY_USER", "")
    OPENSKY_PASS: str = os.getenv("OPENSKY_PASS", "")
    POLL_SECONDS: float = float(os.getenv("POLL_SECONDS", "120"))   # per-region poll cadence; 8 regions -> ~15s between
    # upstream calls, which public aggregators tolerate (20s earned 429s)
    HISTORY_MINUTES: int = int(os.getenv("HISTORY_MINUTES", "20")) # in-memory track window

    # --- Runtime -------------------------------------------------------------
    LLM_ENABLED: bool = os.getenv("LLM_ENABLED", "1") not in ("0", "false", "False")

    @classmethod
    def masked(cls) -> dict:
        def mask(v: str) -> str:
            return (v[:4] + "…" + v[-3:]) if v and len(v) > 8 else ("set" if v else "")
        return {
            "griddb_base_url": cls.GRIDDB_BASE_URL,
            "griddb_user": cls.GRIDDB_USER,
            "griddb_password": mask(cls.GRIDDB_PASSWORD),
            "azure_endpoint": cls.AZURE_INFERENCE_ENDPOINT,
            "azure_key": mask(cls.AZURE_INFERENCE_KEY),
            "model_primary": cls.MODEL_PRIMARY,
            "model_fast": cls.MODEL_FAST,
            "llm_enabled": cls.LLM_ENABLED,
        }


config = Config()
