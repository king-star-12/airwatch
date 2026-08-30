"""Regions of interest — the world's real GPS-interference hotspots.

Each region is one adsb.lol point+radius query (max 250 nm). The THEATRE is the
set aggregated into the single "Blind Sky" view; `hub` is a clean control kept
only for the analyst view. Coordinates centre on airspaces where GPS
jamming/spoofing is publicly documented (Kaliningrad, Black Sea, Eastern Med,
Iraq/Levant, the Gulf, the Caucasus).
"""
from __future__ import annotations

import math

REGIONS: dict[str, dict] = {
    "baltic": {"id": "baltic", "name": "Baltic · Kaliningrad", "lat": 55.2, "lon": 20.6,
               "radius_nm": 250, "why": "Persistent jamming around Kaliningrad & the Baltic states.",
               "bbox": [52.0, 14.5, 59.5, 27.5]},
    "blacksea": {"id": "blacksea", "name": "Black Sea", "lat": 44.4, "lon": 34.0,
                 "radius_nm": 250, "why": "Contested airspace with recurrent navigation interference.",
                 "bbox": [41.0, 28.0, 47.5, 41.0]},
    "eastmed": {"id": "eastmed", "name": "Eastern Mediterranean · Cyprus", "lat": 34.8, "lon": 33.6,
                "radius_nm": 250, "why": "Intense, well-documented GPS spoofing near the Levant.",
                "bbox": [31.0, 29.5, 37.5, 37.5]},
    "levant": {"id": "levant", "name": "Levant · Iraq", "lat": 33.3, "lon": 43.8,
               "radius_nm": 250, "why": "Some of the strongest sustained GPS spoofing in the world.",
               "bbox": [29.5, 39.0, 37.0, 48.5]},
    "gulf": {"id": "gulf", "name": "Persian Gulf", "lat": 26.8, "lon": 52.0,
             "radius_nm": 250, "why": "Heavy spoofing across Gulf approaches.",
             "bbox": [23.0, 47.5, 30.5, 56.5]},
    "caucasus": {"id": "caucasus", "name": "Caucasus", "lat": 41.5, "lon": 44.8,
                 "radius_nm": 250, "why": "Recurrent interference over the Caucasus corridor.",
                 "bbox": [38.5, 40.0, 44.5, 49.5]},
    "hub": {"id": "hub", "name": "London TMA · control", "lat": 51.45, "lon": -0.2,
            "radius_nm": 180, "why": "Dense, well-behaved airspace — the clean baseline.",
            "bbox": [49.8, -3.2, 53.0, 2.2]},
}

# Aggregated into the single "Blind Sky" view (hub excluded — it's the control).
THEATRE = ["baltic", "blacksea", "eastmed", "levant", "gulf", "caucasus"]
POLL_REGIONS = THEATRE + ["hub"]
DEFAULT_REGION = "eastmed"

# Overall map frame for the theatre (lat/lon), and city anchors for legibility.
THEATRE_BBOX = [22.0, 12.0, 60.0, 57.0]   # latMin, lonMin, latMax, lonMax
CITIES = [
    {"name": "London", "lat": 51.5, "lon": -0.1}, {"name": "Kaliningrad", "lat": 54.7, "lon": 20.5},
    {"name": "Kyiv", "lat": 50.45, "lon": 30.5}, {"name": "Moscow", "lat": 55.75, "lon": 37.6},
    {"name": "Istanbul", "lat": 41.0, "lon": 28.98}, {"name": "Cyprus", "lat": 35.1, "lon": 33.4},
    {"name": "Cairo", "lat": 30.0, "lon": 31.2}, {"name": "Baghdad", "lat": 33.3, "lon": 44.4},
    {"name": "Tehran", "lat": 35.7, "lon": 51.4}, {"name": "Dubai", "lat": 25.2, "lon": 55.3},
    {"name": "Tbilisi", "lat": 41.7, "lon": 44.8}, {"name": "Warsaw", "lat": 52.23, "lon": 21.0},
    {"name": "Helsinki", "lat": 60.17, "lon": 24.94}, {"name": "Beirut", "lat": 33.9, "lon": 35.5},
]


def get(region_id: str) -> dict:
    return REGIONS[region_id]


def list_regions() -> list[dict]:
    return [{"id": r["id"], "name": r["name"], "lat": r["lat"], "lon": r["lon"],
             "radius_nm": r["radius_nm"], "why": r["why"], "bbox": r["bbox"]}
            for r in REGIONS.values()]


def nearest_place(lat: float, lon: float) -> str:
    best, bestd = "open sea", 1e9
    for c in CITIES:
        d = (lat - c["lat"]) ** 2 + ((lon - c["lon"]) * math.cos(math.radians(lat))) ** 2
        if d < bestd:
            bestd, best = d, c["name"]
    return best
