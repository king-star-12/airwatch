"""Thin GridDB time-series client.

Aircraft telemetry is a time series per airframe, and the detections that
matter emerge across consecutive rows rather than within any single one.
This client speaks the minimum of the WebAPI needed to store and query it,
and the system runs in-memory when GridDB is not configured.
"""
from __future__ import annotations

import datetime as dt
import time
from typing import Any, Optional

import httpx

from .config import config

# Statuses worth retrying: WAF/App-Gateway rate bans (403), throttling (429), 5xx.
_RETRY_STATUS = {403, 429, 500, 502, 503, 504}


def to_iso(t: dt.datetime) -> str:
    """UTC ISO-8601 with millisecond precision + Z, as GridDB Cloud expects."""
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    t = t.astimezone(dt.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def parse_iso(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


class GridDBError(RuntimeError):
    pass


class GridDBClient:
    def __init__(self, retries: int = 3, backoff: float = 1.5) -> None:
        self._client = httpx.Client(
            base_url=config.GRIDDB_BASE_URL,
            auth=(config.GRIDDB_USER, config.GRIDDB_PASSWORD),
            timeout=config.GRIDDB_TIMEOUT,
            headers={"Content-Type": "application/json"},
        )
        self._retries = retries
        self._backoff = backoff

    def _send(self, method: str, path: str, **kw) -> httpx.Response:
        """Send with exponential backoff on transient WAF/throttle statuses."""
        last: Optional[httpx.Response] = None
        for attempt in range(self._retries + 1):
            r = self._client.request(method, path, **kw)
            if r.status_code not in _RETRY_STATUS:
                return r
            last = r
            if attempt < self._retries:
                time.sleep(self._backoff * (2 ** attempt))
        return last  # type: ignore[return-value]

    # -- connection -----------------------------------------------------------
    def check_connection(self) -> bool:
        return self._send("GET", "/checkConnection").status_code == 200

    # -- containers -----------------------------------------------------------
    def container_exists(self, name: str) -> bool:
        r = self._send("GET", f"/containers/{name}/info")
        return r.status_code == 200

    def create_container(
        self, name: str, columns: list[dict], container_type: str = "COLLECTION",
        rowkey: bool = True,
    ) -> None:
        body = {
            "container_name": name,
            "container_type": container_type,
            "rowkey": rowkey,
            "columns": columns,
        }
        r = self._send("POST", "/containers", json=body)
        if r.status_code not in (200, 201):
            raise GridDBError(f"create_container {name} -> {r.status_code}: {r.text[:300]}")

    def ensure_container(
        self, name: str, columns: list[dict], container_type: str = "COLLECTION",
        rowkey: bool = True,
    ) -> None:
        if not self.container_exists(name):
            self.create_container(name, columns, container_type, rowkey)

    def drop_containers(self, names: list[str]) -> None:
        if not names:
            return
        self._send("DELETE", "/containers", json=names)

    # -- rows -----------------------------------------------------------------
    def put_rows(self, name: str, rows: list[list[Any]]) -> int:
        """Insert/replace rows (multi-put). Returns count written."""
        if not rows:
            return 0
        r = self._send("PUT", f"/containers/{name}/rows", json=rows)
        if r.status_code not in (200, 201):
            raise GridDBError(f"put_rows {name} -> {r.status_code}: {r.text[:300]}")
        try:
            return int(r.json().get("count", len(rows)))
        except Exception:
            return len(rows)

    def tql(self, name: str, stmt: str) -> list[list[Any]]:
        """Run a TQL statement against one container, returning raw result rows."""
        r = self._send("POST", "/tql", json=[{"name": name, "stmt": stmt}])
        if r.status_code != 200:
            raise GridDBError(f"tql {name} -> {r.status_code}: {r.text[:300]}")
        data = r.json()
        if not data:
            return []
        return data[0].get("results", [])

    def tql_dicts(self, name: str, stmt: str) -> list[dict]:
        """TQL returning list-of-dicts keyed by column name."""
        r = self._send("POST", "/tql", json=[{"name": name, "stmt": stmt}])
        if r.status_code != 200:
            raise GridDBError(f"tql {name} -> {r.status_code}: {r.text[:300]}")
        data = r.json()
        if not data:
            return []
        cols = [c["name"] for c in data[0].get("columns", [])]
        return [dict(zip(cols, row)) for row in data[0].get("results", [])]

    def close(self) -> None:
        self._client.close()


# Module-level singleton (created lazily so import never fails offline).
_client: Optional[GridDBClient] = None


def db() -> GridDBClient:
    global _client
    if _client is None:
        _client = GridDBClient()
    return _client
