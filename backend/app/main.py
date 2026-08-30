"""Airspace investigation API — Starlette ASGI app (pydantic-free)."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse as _JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import adsb, service
from .config import config

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"


def JSONResponse(content, **kw):
    """Telemetry must never be served from a cache.

    Browsers will happily reuse a minutes-old airspace snapshot — or a stale
    error — which for a live monitor is worse than showing nothing, because a
    cached "all clear" reads as a current one.
    """
    headers = {**kw.pop("headers", {}),
               "Cache-Control": "no-store, no-cache, must-revalidate",
               "Pragma": "no-cache"}
    return _JSONResponse(content, headers=headers, **kw)


async def _run(fn, *a, **kw):
    return await run_in_threadpool(fn, *a, **kw)


async def health(request: Request):
    info = await _run(service.init_db)
    # feed: which ADS-B source last answered, whether integrity fields are
    # available, and whether we are serving cache — a blank board should never
    # be a mystery again.
    return JSONResponse({"ok": True, "griddb": info, "llm_enabled": config.LLM_ENABLED,
                         "feed": adsb.FEED_STATUS, "config": config.masked()})


async def regions_list(request: Request):
    return JSONResponse(await _run(service.list_regions))


async def theatre(request: Request):
    return JSONResponse(await _run(service.theatre))


def _rid(request):
    return request.path_params["rid"]


async def live(request: Request):
    try:
        return JSONResponse(await _run(service.get_live, _rid(request)))
    except KeyError:
        return JSONResponse({"error": "unknown region"}, status_code=404)


async def history(request: Request):
    return JSONResponse(await _run(service.get_history, _rid(request)))


async def aircraft(request: Request):
    try:
        return JSONResponse(await _run(service.get_aircraft, _rid(request),
                                       request.path_params["hex"]))
    except KeyError:
        return JSONResponse({"error": "aircraft not found"}, status_code=404)


async def investigate_aircraft(request: Request):
    body = await request.json()
    hexid = body.get("hex")
    if not hexid:
        return JSONResponse({"error": "hex required"}, status_code=400)
    try:
        return JSONResponse(await _run(service.investigate_aircraft, _rid(request),
                                       hexid, body.get("aircraft")))
    except KeyError:
        return JSONResponse({"error": "aircraft not found"}, status_code=404)


async def case(request: Request):
    """Detection + open-source corroboration, as one citable case file."""
    recency = request.query_params.get("window")
    return JSONResponse(await _run(service.build_case, _rid(request), recency))


async def brief(request: Request):
    return JSONResponse(await _run(service.brief_region, _rid(request)))


async def evidence(request: Request):
    return JSONResponse(await _run(service.evidence, request.query_params.get("region")))


async def reset(request: Request):
    return JSONResponse(await _run(service.reset))


async def ask(request: Request):
    body = await request.json()
    q = (body.get("question") or "").strip()
    rid = body.get("region") or ""
    if not q:
        return JSONResponse({"error": "question required"}, status_code=400)
    return JSONResponse(await _run(service.ask, q, rid))


routes = [
    Route("/api/region/{rid}/case", case),
    Route("/api/health", health),
    Route("/api/regions", regions_list),
    Route("/api/theatre", theatre),
    Route("/api/region/{rid}/live", live),
    Route("/api/region/{rid}/history", history),
    Route("/api/region/{rid}/aircraft/{hex}", aircraft),
    Route("/api/region/{rid}/investigate", investigate_aircraft, methods=["POST"]),
    Route("/api/region/{rid}/brief", brief, methods=["POST"]),
    Route("/api/evidence", evidence),
    Route("/api/reset", reset, methods=["POST"]),
    Route("/api/ask", ask, methods=["POST"]),
]

if FRONTEND_DIR.exists():
    routes.append(Mount("/", app=StaticFiles(directory=str(FRONTEND_DIR), html=True)))

middleware = [Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                         allow_headers=["*"])]


@asynccontextmanager
async def lifespan(app):
    await _run(service.init_db)
    await _run(service.start_polling)     # begins the live ADS-B poll loop
    yield


app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan)
