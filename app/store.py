"""Persistence, no ORM: payments, the hash-chained audit log, small kv.

Two backends with the same SQL: a local SQLite file (default), or Turso/libSQL
over HTTP (TURSO_DATABASE_URL) so serverless instances share one database.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    status TEXT NOT NULL,
    amount_usd REAL NOT NULL,
    data TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_payments_agent ON payments(agent_id, created_at);
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    payment_id TEXT NOT NULL DEFAULT '',
    event TEXT NOT NULL,
    data TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""

GENESIS = "0" * 64


class SQLiteBackend:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()

    def script(self, sql: str) -> None:
        with self._lock:
            self._conn.executescript(sql)
            self._conn.commit()

    def execute(self, sql: str, args: tuple = ()) -> int | None:
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur.lastrowid

    def query(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]


class TursoBackend:
    """libSQL remote protocol (Hrana over HTTP, /v2/pipeline). Sync on purpose:
    the store is called from sync code paths and each call is one round trip."""

    def __init__(self, url: str, token: str = "", timeout: float = 10.0):
        import httpx

        base = url.strip().rstrip("/")
        for scheme in ("libsql://", "wss://", "ws://"):
            if base.startswith(scheme):
                base = ("http://" if scheme == "ws://" else "https://") + base[len(scheme):]
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._http = httpx.Client(base_url=base, headers=headers, timeout=timeout)

    @staticmethod
    def _arg(v: Any) -> dict[str, Any]:
        if v is None:
            return {"type": "null"}
        if isinstance(v, bool) or isinstance(v, int):
            return {"type": "integer", "value": str(int(v))}
        if isinstance(v, float):
            return {"type": "float", "value": v}
        if isinstance(v, bytes):
            return {"type": "blob", "base64": base64.b64encode(v).decode()}
        return {"type": "text", "value": str(v)}

    @staticmethod
    def _val(v: dict[str, Any]) -> Any:
        t = v.get("type")
        if t == "integer":
            return int(v["value"])
        if t == "float":
            return float(v["value"])
        if t == "blob":
            return base64.b64decode(v["base64"])
        if t == "null":
            return None
        return v.get("value")

    def _pipeline(self, stmts: list[tuple[str, tuple]]) -> list[dict[str, Any]]:
        reqs: list[dict[str, Any]] = [
            {"type": "execute", "stmt": {"sql": sql, "args": [self._arg(a) for a in args]}} for sql, args in stmts
        ]
        r = self._http.post("/v2/pipeline", json={"requests": reqs + [{"type": "close"}]})
        r.raise_for_status()
        out = []
        for res in r.json()["results"][: len(stmts)]:
            if res.get("type") != "ok":
                raise RuntimeError(f"libSQL error: {res.get('error', {}).get('message', res)}")
            out.append(res["response"]["result"])
        return out

    def script(self, sql: str) -> None:
        stmts = [s.strip() for s in _strip_comments(sql).split(";") if s.strip()]
        self._pipeline([(s, ()) for s in stmts])

    def execute(self, sql: str, args: tuple = ()) -> int | None:
        rowid = self._pipeline([(sql, args)])[0].get("last_insert_rowid")
        return int(rowid) if rowid is not None else None

    def query(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        res = self._pipeline([(sql, args)])[0]
        names = [c["name"] for c in res["cols"]]
        return [dict(zip(names, (self._val(v) for v in row))) for row in res["rows"]]


def _strip_comments(sql: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


class Store:
    def __init__(self, path: str = ":memory:", turso_url: str = "", turso_token: str = ""):
        self._db: SQLiteBackend | TursoBackend = (
            TursoBackend(turso_url, turso_token) if turso_url else SQLiteBackend(path)
        )
        self.backend = "turso" if turso_url else "sqlite"
        self._db.script(SCHEMA)
        self._audit_lock = threading.Lock()

    def _exec(self, sql: str, args: tuple = ()) -> int | None:
        return self._db.execute(sql, args)

    def _all(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        return self._db.query(sql, args)

    # kv -------------------------------------------------------------------
    def kv_get(self, key: str) -> Any:
        rows = self._all("SELECT v FROM kv WHERE k = ?", (key,))
        return json.loads(rows[0]["v"]) if rows else None

    def kv_set(self, key: str, value: Any) -> None:
        self._exec(
            "INSERT INTO kv(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (key, json.dumps(value)),
        )

    # payments -------------------------------------------------------------
    def save_payment(self, p: dict[str, Any]) -> None:
        now = time.time()
        p.setdefault("created_at", now)
        p["updated_at"] = now
        self._exec(
            """INSERT INTO payments(id, agent_id, status, amount_usd, data, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET status = excluded.status, data = excluded.data,
                                             updated_at = excluded.updated_at""",
            (p["id"], p.get("ledger") or p["agent_id"], p["status"], float(p["amount_usd"]), json.dumps(p), p["created_at"], now),
        )

    def get_payment(self, payment_id: str) -> dict[str, Any] | None:
        rows = self._all("SELECT data FROM payments WHERE id = ?", (payment_id,))
        return json.loads(rows[0]["data"]) if rows else None

    def list_payments(self, agent_id: str | None = None, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql, args = "SELECT data FROM payments WHERE 1=1", []
        if agent_id:
            sql += " AND agent_id = ?"
            args.append(agent_id)
        else:  # game rounds are throwaway simulations; keep them out of the ledger view
            sql += " AND agent_id NOT LIKE 'game~%'"
        if status:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return [json.loads(r["data"]) for r in self._all(sql, tuple(args))]

    def spent_since(self, agent_id: str, since: float, statuses: tuple[str, ...], all_ledgers: bool = False) -> tuple[float, int]:
        """Sum and count for one ledger; all_ledgers adds the agent's demo sandboxes (agent~sid)."""
        marks = ",".join("?" * len(statuses))
        who = "(agent_id = ? OR agent_id LIKE ?)" if all_ledgers else "agent_id = ?"
        ids = (agent_id, agent_id + "~%") if all_ledgers else (agent_id,)
        rows = self._all(
            f"SELECT COALESCE(SUM(amount_usd), 0) AS s, COUNT(*) AS n FROM payments "
            f"WHERE {who} AND created_at >= ? AND status IN ({marks})",
            (*ids, since, *statuses),
        )
        return float(rows[0]["s"] or 0), int(rows[0]["n"] or 0)

    # audit log --------------------------------------------------------------
    # Every decision is appended to a hash chain: hash = sha256(prev_hash || seq ||
    # event || canonical json). Editing or deleting any past row breaks verify().
    @staticmethod
    def _hash(prev: str, seq: int, ts: float, payment_id: str, event: str, data: str) -> str:
        return hashlib.sha256(f"{prev}|{seq}|{ts:.6f}|{payment_id}|{event}|{data}".encode()).hexdigest()

    def audit(self, event: str, data: dict[str, Any], payment_id: str = "") -> dict[str, Any]:
        body = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
        with self._audit_lock:
            for _ in range(5):  # another instance may append concurrently; seq is the PK
                last = self._all("SELECT seq, hash FROM audit ORDER BY seq DESC LIMIT 1")
                seq = (last[0]["seq"] + 1) if last else 1
                prev = last[0]["hash"] if last else GENESIS
                ts = round(time.time(), 6)
                h = self._hash(prev, seq, ts, payment_id, event, body)
                try:
                    self._exec(
                        "INSERT INTO audit(seq, ts, payment_id, event, data, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (seq, ts, payment_id, event, body, prev, h),
                    )
                    return {"seq": seq, "hash": h}
                except Exception as exc:  # unique violation -> retry with the new head
                    if "UNIQUE" not in str(exc).upper() and "PRIMARY" not in str(exc).upper():
                        raise
        raise RuntimeError("audit log is contended")

    def audit_log(self, payment_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if payment_id:
            rows = self._all("SELECT * FROM audit WHERE payment_id = ? ORDER BY seq", (payment_id,))
        else:
            rows = self._all("SELECT * FROM audit ORDER BY seq DESC LIMIT ?", (limit,))
        for r in rows:
            r["data"] = json.loads(r["data"])
        return rows

    def verify_audit(self) -> dict[str, Any]:
        rows = self._all("SELECT * FROM audit ORDER BY seq")
        prev = GENESIS
        for i, r in enumerate(rows, start=1):
            ok = r["seq"] == i and r["prev_hash"] == prev and r["hash"] == self._hash(
                prev, r["seq"], r["ts"], r["payment_id"], r["event"], r["data"]
            )
            if not ok:
                return {"ok": False, "entries": len(rows), "broken_at": r["seq"], "head": None}
            prev = r["hash"]
        return {"ok": True, "entries": len(rows), "head": prev}
