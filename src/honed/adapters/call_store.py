"""CallStore on SQLite: the LLM call cache and the usage ledger, in the dataset's SQLite file (own tables).

Safe to share between the job runner's worker threads: one connection, serialized by a lock.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
from collections.abc import Collection
from pathlib import Path

from honed.ports.call_store import CachedCall, LedgerEntry
from honed.ports.llm import CallKey, LLMCall, LLMResult, Usage

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_cache (
  key TEXT PRIMARY KEY, model TEXT NOT NULL, effort TEXT, sample INTEGER NOT NULL, system_hash TEXT NOT NULL,
  input_hash TEXT NOT NULL, schema_hash TEXT NOT NULL, stage TEXT, text TEXT NOT NULL, data TEXT,
  stop_reason TEXT, answered_by TEXT, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
  cache_write_tokens INTEGER, cost_usd REAL, duration_s REAL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS llm_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, run_id TEXT NOT NULL, stage TEXT NOT NULL, pr TEXT,
  model TEXT NOT NULL, key TEXT NOT NULL, cached INTEGER NOT NULL, ok INTEGER NOT NULL,
  input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
  cost_usd REAL, duration_s REAL, error TEXT);
CREATE INDEX IF NOT EXISTS llm_ledger_run ON llm_ledger (run_id);
"""


class SqliteCallStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def get(self, key: CallKey) -> LLMResult | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM llm_cache WHERE key = ?", (key.digest,)).fetchone()
        if row is None:
            return None
        return LLMResult(
            text=row["text"],
            data=json.loads(row["data"]) if row["data"] is not None else None,
            usage=Usage(
                input_tokens=row["input_tokens"] or 0,
                output_tokens=row["output_tokens"] or 0,
                cache_read_tokens=row["cache_read_tokens"] or 0,
                cache_write_tokens=row["cache_write_tokens"] or 0,
                cost_usd=row["cost_usd"] or 0.0,
                duration_s=row["duration_s"] or 0.0,
            ),
            model=row["answered_by"] or row["model"],
            stop_reason=row["stop_reason"],
            cached=True,
        )

    def put(self, key: CallKey, call: LLMCall, result: LLMResult) -> None:
        u = result.usage
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO llm_cache VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (key.digest, key.model, key.effort, key.sample, key.system_hash, key.input_hash, key.schema_hash,
                 call.stage, result.text, None if result.data is None else json.dumps(result.data),
                 result.stop_reason, result.model, u.input_tokens, u.output_tokens, u.cache_read_tokens,
                 u.cache_write_tokens, u.cost_usd, u.duration_s, dt.datetime.now(dt.UTC).isoformat()),
            )  # fmt: skip

    def cached(self, models: Collection[str]) -> list[CachedCall]:
        wanted = sorted(set(models))
        if not wanted:
            return []
        marks = ",".join("?" * len(wanted))
        with self._lock:
            rows = self._db.execute(f"SELECT * FROM llm_cache WHERE model IN ({marks}) ORDER BY created_at, key",
                                    wanted).fetchall()  # fmt: skip
        return [
            CachedCall(
                key=r["key"], model=r["model"], effort=r["effort"] or "", sample=r["sample"],
                system_hash=r["system_hash"], input_hash=r["input_hash"], schema_hash=r["schema_hash"],
                stage=r["stage"] or "", text=r["text"], data=json.loads(r["data"]) if r["data"] is not None else None,
                stop_reason=r["stop_reason"], answered_by=r["answered_by"] or r["model"],
                input_tokens=r["input_tokens"] or 0, output_tokens=r["output_tokens"] or 0,
                cache_read_tokens=r["cache_read_tokens"] or 0, cache_write_tokens=r["cache_write_tokens"] or 0,
                cost_usd=r["cost_usd"] or 0.0, duration_s=r["duration_s"] or 0.0, created_at=r["created_at"],
            )
            for r in rows
        ]  # fmt: skip

    def add_cached(self, entry: CachedCall) -> bool:
        e = entry
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO llm_cache VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (e.key, e.model, e.effort, e.sample, e.system_hash, e.input_hash, e.schema_hash, e.stage, e.text,
                 None if e.data is None else json.dumps(e.data), e.stop_reason, e.answered_by, e.input_tokens,
                 e.output_tokens, e.cache_read_tokens, e.cache_write_tokens, e.cost_usd, e.duration_s, e.created_at),
            )  # fmt: skip
            return cursor.rowcount > 0

    def record(self, entry: LedgerEntry) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO llm_ledger (at, run_id, stage, pr, model, key, cached, ok, input_tokens, output_tokens,"
                " cache_read_tokens, cache_write_tokens, cost_usd, duration_s, error)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (entry.at, entry.run_id, entry.stage, entry.pr, entry.model, entry.key, entry.cached, entry.ok,
                 entry.input_tokens, entry.output_tokens, entry.cache_read_tokens, entry.cache_write_tokens,
                 entry.cost_usd, entry.duration_s, entry.error),
            )  # fmt: skip

    def ledger(self) -> list[LedgerEntry]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM llm_ledger ORDER BY id").fetchall()
        return [
            LedgerEntry(
                at=r["at"], run_id=r["run_id"], stage=r["stage"], pr=r["pr"], model=r["model"], key=r["key"],
                cached=bool(r["cached"]), ok=bool(r["ok"]), input_tokens=r["input_tokens"] or 0,
                output_tokens=r["output_tokens"] or 0, cache_read_tokens=r["cache_read_tokens"] or 0,
                cache_write_tokens=r["cache_write_tokens"] or 0, cost_usd=r["cost_usd"] or 0.0,
                duration_s=r["duration_s"] or 0.0, error=r["error"],
            )
            for r in rows
        ]  # fmt: skip
