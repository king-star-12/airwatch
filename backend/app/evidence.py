"""Tamper-evident evidence ledger — append-only, SHA-256 hash-chained (doc §5).

Each row carries the SHA-256 of the previous row, so any silent edit breaks the
chain. Every LLM ruling, pattern shift, and detector firing that matters to a
case is written here with model + version — this IS the governance demo.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json

from .griddb import GridDBClient, to_iso

GENESIS = "0" * 64


def _hash(seq: int, ts: str, scenario: str, actor: str, kind: str,
          summary: str, payload: str, prev_hash: str) -> str:
    blob = f"{seq}|{ts}|{scenario}|{actor}|{kind}|{summary}|{payload}|{prev_hash}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class EvidenceLedger:
    def __init__(self, db: GridDBClient):
        self.db = db

    def _tail(self) -> tuple[int, str]:
        rows = self.db.tql_dicts("evidence_log", "select * order by seq desc limit 1")
        if not rows:
            return 0, GENESIS
        return int(rows[0]["seq"]), rows[0]["hash"]

    def append(self, scenario: str, actor: str, kind: str, summary: str,
               payload: dict | None = None, ts: dt.datetime | None = None) -> dict:
        seq, prev = self._tail()
        seq += 1
        ts = ts or dt.datetime.now(dt.timezone.utc)
        ts_iso = to_iso(ts)
        payload_str = json.dumps(payload or {}, separators=(",", ":"), sort_keys=True, default=str)
        h = _hash(seq, ts_iso, scenario, actor, kind, summary, payload_str, prev)
        row = [seq, ts_iso, scenario, actor, kind, summary, payload_str, prev, h]
        self.db.put_rows("evidence_log", [row])
        return {
            "seq": seq, "ts": ts_iso, "scenario": scenario, "actor": actor,
            "kind": kind, "summary": summary, "payload": payload or {},
            "prev_hash": prev, "hash": h,
        }

    def chain(self, scenario: str | None = None) -> list[dict]:
        rows = self.db.tql_dicts("evidence_log", "select * order by seq asc")
        out = []
        for r in rows:
            if scenario and r.get("scenario") != scenario:
                continue
            try:
                r["payload"] = json.loads(r.get("payload") or "{}")
            except Exception:
                pass
            out.append(r)
        return out

    def verify(self, scenario: str | None = None) -> dict:
        """Recompute the chain and report the first break, if any."""
        rows = self.db.tql_dicts("evidence_log", "select * order by seq asc")
        prev = GENESIS
        broken_at = None
        checked = 0
        for r in rows:
            payload_str = r.get("payload") or "{}"
            recomputed = _hash(int(r["seq"]), r["ts"], r["scenario"], r["actor"],
                               r["kind"], r["summary"], payload_str, prev)
            checked += 1
            if r.get("prev_hash") != prev or r.get("hash") != recomputed:
                broken_at = int(r["seq"])
                break
            prev = r["hash"]
        return {"intact": broken_at is None, "rows_checked": checked, "broken_at": broken_at}
