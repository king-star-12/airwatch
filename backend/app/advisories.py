"""FAA GPS Flight Advisories — the published prediction, parsed.

Before a military GPS test, the FAA publishes a Flight Advisory saying where and
when satellite navigation may stop working. The geometry is precise and the
areas are enormous:

    366NM RADIUS CENTERED AT 333638N1063320W (TCS048041) FL400-UNL
    326NM RADIUS AT FL250
    240NM RADIUS AT 10000FT
    210NM RADIUS AT 50FT AGL

A 366-nautical-mile circle covers most of New Mexico, west Texas and slices of
Arizona and Colorado. The radius shrinks with altitude because the interference
is line-of-sight: an aircraft at FL400 sees a transmitter three hundred miles
away that an aircraft at 4,000 feet never will.

That advisory is a *forecast*, and by the admission of the people who help write
the standards it is a conservative one — RTCA's task-group cochair has said the
published areas are "not realistic" because terrain blocks most of it. Pilots
learn that the circle rarely bites, start discounting it, and are then surprised
by the occasions when it does.

Nobody measures the outcome. This module parses the prediction so the rest of
the system can compare it against what aircraft actually reported.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("advisories")
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# "333638N1063320W" -> 33°36'38"N, 106°33'20"W
_COORD = re.compile(r"(\d{2})(\d{2})(\d{2})([NS])\s*(\d{3})(\d{2})(\d{2})([EW])")
# Radius and altitude are stated on the same line, so parse per line rather
# than across the document — a lazy match spanning newlines silently pairs a
# radius with the wrong altitude, or with none at all.
_RADIUS_LINE = re.compile(r"(\d{2,3})\s*NM\s+RADIUS", re.I)
_ALT_TOKEN = re.compile(r"(FL\s?\d{2,3}|\d{2,5}\s*FT(?:\s+AGL)?|UNL)", re.I)
_SITE = re.compile(r"([A-Z]{3,8})\s+GPS\s+(\d{2}-\d{2})")


def _dms(d: str, m: str, s: str, hemi: str) -> float:
    v = int(d) + int(m) / 60 + int(s) / 3600
    return -v if hemi in ("S", "W") else v


def _alt_ft(token: str | None) -> int:
    """Normalise an altitude token to feet. Missing/UNL means 'anything above'."""
    if not token:
        return 45000
    t = token.upper().replace(" ", "")
    if t == "UNL":
        return 60000
    if t.startswith("FL"):
        return int(re.sub(r"\D", "", t)[:3]) * 100
    m = re.match(r"(\d+)FT", t)
    return int(m.group(1)) if m else 45000


@dataclass
class Band:
    """One published radius, valid up to `alt_ft`."""
    radius_nm: int
    alt_ft: int


@dataclass
class Advisory:
    id: str
    site: str
    lat: float
    lon: float
    bands: list[Band] = field(default_factory=list)
    source_url: str = ""
    raw_window: str = ""

    @property
    def max_radius_nm(self) -> int:
        return max((b.radius_nm for b in self.bands), default=0)

    def predicted_radius_for(self, alt_ft: float | None) -> int:
        """The radius the FAA published for an aircraft at this altitude.

        Bands are 'radius R applies at altitude A'; the smallest band whose
        altitude is at or above the aircraft governs, because the published
        list runs from the widest/highest down to the narrowest/lowest.
        """
        if not self.bands:
            return 0
        a = alt_ft if isinstance(alt_ft, (int, float)) else 0
        eligible = [b for b in sorted(self.bands, key=lambda b: b.alt_ft) if b.alt_ft >= a]
        return (eligible[0] if eligible else max(self.bands, key=lambda b: b.alt_ft)).radius_nm

    def as_dict(self) -> dict:
        return {
            "id": self.id, "site": self.site, "lat": round(self.lat, 4),
            "lon": round(self.lon, 4), "max_radius_nm": self.max_radius_nm,
            "bands": [{"radius_nm": b.radius_nm, "alt_ft": b.alt_ft} for b in
                      sorted(self.bands, key=lambda b: -b.radius_nm)],
            "source_url": self.source_url, "window": self.raw_window,
        }


def parse(text: str, source_url: str = "") -> Advisory | None:
    """Extract the published footprint from advisory text."""
    flat = re.sub(r"[ \t]+", " ", text)

    m = _COORD.search(flat)
    if not m:
        log.warning("no coordinate in advisory %s", source_url)
        return None
    lat = _dms(m.group(1), m.group(2), m.group(3), m.group(4))
    lon = _dms(m.group(5), m.group(6), m.group(7), m.group(8))

    bands: list[Band] = []
    seen: set[tuple[int, int]] = set()
    for line in flat.splitlines():
        rm = _RADIUS_LINE.search(line)
        if not rm:
            continue
        # take the altitude stated after the radius on this same line
        tail = line[rm.end():]
        am = _ALT_TOKEN.search(tail)
        b = (int(rm.group(1)), _alt_ft(am.group(1) if am else None))
        if b not in seen:
            seen.add(b)
            bands.append(Band(radius_nm=b[0], alt_ft=b[1]))
    if not bands:
        return None

    sm = _SITE.search(flat)
    site_code, number = (sm.group(1), sm.group(2)) if sm else ("UNKNOWN", "")
    name = ""
    for line in flat.splitlines():
        line = line.strip()
        if re.search(r"(RANGE|PROVING|AFB|BASE|,\s*[A-Z]{2}$)", line) and len(line) < 70:
            name = line.title()
            break

    win = ""
    wm = re.search(r"B\.\s*Dates and times[^\n]*\n(.{0,220})", flat, re.S)
    if wm:
        win = " ".join(wm.group(1).split())[:200]

    return Advisory(
        id=f"{site_code} GPS {number}".strip(),
        site=name or site_code,
        lat=lat, lon=lon, bands=bands,
        source_url=source_url, raw_window=win,
    )


def fetch(url: str) -> Advisory | None:
    """Download and parse one advisory PDF."""
    try:
        r = httpx.get(url, timeout=_TIMEOUT, headers={"User-Agent": "aeroscan/1.0"},
                      follow_redirects=True)
        if r.status_code != 200:
            return None
        from pypdf import PdfReader
        from io import BytesIO
        text = "".join((p.extract_text() or "") for p in PdfReader(BytesIO(r.content)).pages)
        return parse(text, url)
    except Exception as e:                                    # noqa: BLE001
        log.warning("advisory fetch %s -> %s", url, type(e).__name__)
        return None


# Known 2026 advisories. The FAA publishes these as individual PDFs with no
# index API, so the set is carried explicitly and refreshed as new ones appear.
KNOWN_URLS = [
    "https://www.faasafety.gov/files/notices/2026/May/WSMRNM_26-13_GPS_Flight_Advisory.pdf",
    "https://www.faasafety.gov/files/notices/2026/Feb/DPG_26-01_GPS_Flight_Advisory.pdf",
    "https://www.faasafety.gov/files/notices/2026/Feb/AFCAK_26-01_GPS_Flight_Advisory.pdf",
    "https://www.faasafety.gov/files/notices/2026/Mar/FTPKVA_26-28_GPS_Flight_Advisory.pdf",
    "https://www.faasafety.gov/files/notices/2026/Mar/SCTTR__26-03_GPS_Flight_Advisory.pdf",
    "https://www.faasafety.gov/files/notices/2026/Mar/CPBLFL_26-27_GPS_Flight_Advisory.pdf",
]

_CACHE: dict[str, Advisory] = {}


def load_all(force: bool = False) -> list[Advisory]:
    if _CACHE and not force:
        return list(_CACHE.values())
    for url in KNOWN_URLS:
        a = fetch(url)
        if a:
            _CACHE[a.id] = a
    return list(_CACHE.values())


def get(advisory_id: str) -> Advisory | None:
    load_all()
    return _CACHE.get(advisory_id)
