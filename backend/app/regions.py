"""The US air cargo network — the nodes the supply chain actually runs through.

Air freight carries roughly a third of world trade by value on about one per
cent of its tonnage, and in the United States almost all of it funnels through a
handful of hubs on a nightly sort cycle. Memphis and Louisville alone move the
bulk of next-day freight in a few hours after midnight. That concentration is
what makes the network fast, and it is also what makes it fragile: an hour lost
over one hub is not an hour lost for one flight, it is a day lost for every
package behind it.

Two kinds of region are watched, for two different reasons.

CARGO HUBS — where disruption compounds. Aircraft holding, diverting or losing
navigation integrity over Memphis during the sort window costs the network far
more than the same event over an ordinary field at noon.

GPS TEST RANGES — where interference is real, deliberate and *published*. The
US military conducts GNSS jamming trials over the southwestern ranges routinely,
and the FAA issues interference NOTAMs for them. These are the ground truth of
this system: a region where degraded integrity is expected and documented, which
makes it the honest control for everything else. If the detectors do not fire
here, they are not working.

Coordinates are field/range centres; radii are one adsb.lol point query (250 nm max).
"""
from __future__ import annotations

import math

REGIONS: dict[str, dict] = {
    # --- cargo hubs -----------------------------------------------------------
    "memphis": {
        "id": "memphis", "name": "Memphis · FedEx World Hub", "lat": 35.04, "lon": -89.98,
        "radius_nm": 160, "icao_hint": "KMEM",
        "why": "The single busiest cargo airport in the US. The nightly sort moves most of "
               "the country's next-day freight in a few hours — delay here compounds nationally.",
        "bbox": [32.4, -93.2, 37.7, -86.7]},
    "louisville": {
        "id": "louisville", "name": "Louisville · UPS Worldport", "lat": 38.17, "lon": -85.74,
        "radius_nm": 150, "icao_hint": "KSDF",
        "why": "UPS's global air hub and the second pillar of US next-day freight. "
               "Runs the same overnight sort window as Memphis.",
        "bbox": [35.7, -88.9, 40.6, -82.6]},
    "indianapolis": {
        "id": "indianapolis", "name": "Indianapolis · FedEx #2", "lat": 39.72, "lon": -86.29,
        "radius_nm": 150, "icao_hint": "KIND",
        "why": "FedEx's second-largest US hub and a major pharmaceutical air-freight node — "
               "temperature-controlled cargo where delay is spoilage.",
        "bbox": [37.2, -89.4, 42.2, -83.1]},
    "socal": {
        "id": "socal", "name": "Ontario · SoCal e-commerce", "lat": 34.06, "lon": -117.60,
        "radius_nm": 150, "icao_hint": "KONT",
        "why": "The West Coast e-commerce gateway, absorbing transpacific freight arriving "
               "through LA/Long Beach into the domestic air network.",
        "bbox": [31.6, -120.6, 36.6, -114.5]},
    "miami": {
        "id": "miami", "name": "Miami · Latin America gateway", "lat": 25.79, "lon": -80.29,
        "radius_nm": 160, "icao_hint": "KMIA",
        "why": "The dominant US gateway for Latin American perishables — flowers, fruit, "
               "fish and vaccines, all of them clock-sensitive.",
        "bbox": [23.1, -83.3, 28.4, -77.1]},
    "anchorage": {
        "id": "anchorage", "name": "Anchorage · Asia freight gateway", "lat": 61.17, "lon": -150.0,
        "radius_nm": 200, "icao_hint": "PANC",
        "why": "The refuelling pivot for transpacific freighters and one of the busiest cargo "
               "airports on earth. Nearly all Asia–US air freight passes overhead.",
        "bbox": [57.8, -156.0, 64.5, -143.5]},

    # --- published GPS interference ranges ------------------------------------
    "whitesands": {
        "id": "whitesands", "name": "White Sands · GPS test range", "lat": 32.90, "lon": -106.40,
        "radius_nm": 180, "icao_hint": "KHMN",
        "why": "A published GPS interference testing area. The FAA issues NOTAMs for "
               "jamming trials here — degraded integrity is expected, documented and legal.",
        "bbox": [30.0, -109.7, 35.8, -103.0]},
    "nttr": {
        "id": "nttr", "name": "Nevada Test & Training Range", "lat": 37.50, "lon": -116.50,
        "radius_nm": 180, "icao_hint": "KLSV",
        "why": "The largest US military training airspace and a routine GNSS-denial exercise "
               "area, immediately upwind of the Las Vegas and SoCal traffic flows.",
        "bbox": [34.6, -119.8, 40.4, -113.1]},
}

# Aggregated into the single network view. The test ranges are included
# deliberately: they are where interference is real and published, and a system
# that cannot see it there cannot be trusted anywhere else.
THEATRE = ["memphis", "louisville", "indianapolis", "socal", "miami",
           "whitesands", "nttr"]
POLL_REGIONS = THEATRE + ["anchorage"]
DEFAULT_REGION = "memphis"

# CONUS frame. Anchorage sits outside it and is watched as its own region.
THEATRE_BBOX = [24.0, -125.0, 50.0, -66.0]
CITIES = [
    {"name": "Memphis", "lat": 35.05, "lon": -90.0}, {"name": "Louisville", "lat": 38.25, "lon": -85.76},
    {"name": "Indianapolis", "lat": 39.77, "lon": -86.16}, {"name": "Chicago", "lat": 41.88, "lon": -87.63},
    {"name": "Dallas", "lat": 32.78, "lon": -96.80}, {"name": "Atlanta", "lat": 33.75, "lon": -84.39},
    {"name": "Miami", "lat": 25.77, "lon": -80.19}, {"name": "Los Angeles", "lat": 34.05, "lon": -118.24},
    {"name": "Ontario CA", "lat": 34.06, "lon": -117.60}, {"name": "Las Vegas", "lat": 36.17, "lon": -115.14},
    {"name": "Phoenix", "lat": 33.45, "lon": -112.07}, {"name": "Denver", "lat": 39.74, "lon": -104.99},
    {"name": "El Paso", "lat": 31.76, "lon": -106.49}, {"name": "New York", "lat": 40.71, "lon": -74.01},
    {"name": "Seattle", "lat": 47.61, "lon": -122.33}, {"name": "Anchorage", "lat": 61.22, "lon": -149.90},
    {"name": "Houston", "lat": 29.76, "lon": -95.37}, {"name": "Salt Lake City", "lat": 40.76, "lon": -111.89},
]


def get(region_id: str) -> dict:
    return REGIONS[region_id]


def list_regions() -> list[dict]:
    return [{"id": r["id"], "name": r["name"], "lat": r["lat"], "lon": r["lon"],
             "radius_nm": r["radius_nm"], "why": r["why"], "bbox": r["bbox"],
             "icao_hint": r.get("icao_hint")}
            for r in REGIONS.values()]


def nearest_place(lat: float, lon: float) -> str:
    best, bestd = "open country", 1e9
    for c in CITIES:
        d = (lat - c["lat"]) ** 2 + ((lon - c["lon"]) * math.cos(math.radians(lat))) ** 2
        if d < bestd:
            bestd, best = d, c["name"]
    return best
