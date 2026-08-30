"""GridDB schema for the airspace investigation engine.

Time-native by design: the airspace anomaly index is a per-region time series,
flagged-aircraft positions are timestamped rows, and every ruling is hash-chained.

Containers:
  aircraft                COLLECTION   latest state per live aircraft
  positions               COLLECTION   timestamped fixes for flagged aircraft
  detections              COLLECTION   every detector firing
  anomaly_index_<region>  TIME_SERIES  region anomaly trajectory
  evidence_log            COLLECTION   append-only, SHA-256 hash-chained
"""
from __future__ import annotations

from .griddb import GridDBClient

AIRCRAFT_COLUMNS = [
    {"name": "hex", "type": "STRING"},
    {"name": "region", "type": "STRING"},
    {"name": "flight", "type": "STRING"},
    {"name": "reg", "type": "STRING"},
    {"name": "type", "type": "STRING"},
    {"name": "country", "type": "STRING"},
    {"name": "lat", "type": "DOUBLE"},
    {"name": "lon", "type": "DOUBLE"},
    {"name": "alt", "type": "INTEGER"},
    {"name": "gs", "type": "DOUBLE"},
    {"name": "track", "type": "DOUBLE"},
    {"name": "squawk", "type": "STRING"},
    {"name": "nic", "type": "INTEGER"},
    {"name": "nac_p", "type": "INTEGER"},
    {"name": "anomaly", "type": "DOUBLE"},
    {"name": "band", "type": "STRING"},
    {"name": "flags", "type": "STRING"},
    {"name": "updated", "type": "TIMESTAMP"},
]

POSITIONS_COLUMNS = [
    {"name": "id", "type": "STRING"},
    {"name": "ts", "type": "TIMESTAMP"},
    {"name": "region", "type": "STRING"},
    {"name": "hex", "type": "STRING"},
    {"name": "lat", "type": "DOUBLE"},
    {"name": "lon", "type": "DOUBLE"},
    {"name": "alt", "type": "INTEGER"},
    {"name": "anomaly", "type": "DOUBLE"},
]

DETECTIONS_COLUMNS = [
    {"name": "id", "type": "STRING"},
    {"name": "ts", "type": "TIMESTAMP"},
    {"name": "region", "type": "STRING"},
    {"name": "hex", "type": "STRING"},
    {"name": "flight", "type": "STRING"},
    {"name": "code", "type": "STRING"},
    {"name": "score", "type": "DOUBLE"},
    {"name": "label", "type": "STRING"},
    {"name": "detail", "type": "STRING"},
    {"name": "lat", "type": "DOUBLE"},
    {"name": "lon", "type": "DOUBLE"},
]

INDEX_COLUMNS = [
    {"name": "ts", "type": "TIMESTAMP"},
    {"name": "anomaly", "type": "DOUBLE"},
    {"name": "degraded", "type": "INTEGER"},
    {"name": "emergencies", "type": "INTEGER"},
    {"name": "holding", "type": "INTEGER"},
    {"name": "dark", "type": "INTEGER"},
    {"name": "zones", "type": "INTEGER"},
    {"name": "total", "type": "INTEGER"},
]

EVIDENCE_COLUMNS = [
    {"name": "seq", "type": "INTEGER"},
    {"name": "ts", "type": "TIMESTAMP"},
    {"name": "scenario", "type": "STRING"},   # region id
    {"name": "actor", "type": "STRING"},
    {"name": "kind", "type": "STRING"},
    {"name": "summary", "type": "STRING"},
    {"name": "payload", "type": "STRING"},
    {"name": "prev_hash", "type": "STRING"},
    {"name": "hash", "type": "STRING"},
]


def index_container(region: str) -> str:
    return f"anomaly_index_{region}"


def init_core_containers(db: GridDBClient) -> list[str]:
    db.ensure_container("aircraft", AIRCRAFT_COLUMNS, "COLLECTION", rowkey=True)
    db.ensure_container("positions", POSITIONS_COLUMNS, "COLLECTION", rowkey=True)
    db.ensure_container("detections", DETECTIONS_COLUMNS, "COLLECTION", rowkey=True)
    db.ensure_container("evidence_log", EVIDENCE_COLUMNS, "COLLECTION", rowkey=True)
    return ["aircraft", "positions", "detections", "evidence_log"]
