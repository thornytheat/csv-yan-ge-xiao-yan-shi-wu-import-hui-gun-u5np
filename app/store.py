"""SQLite 访问（薄封装，无 ORM）。

表：
- meta(key,value)                  seed 版本
- batches(...)                     已入库批次，sha256 用于重复文件判定
- fact_orders(...)                 明细事实，仅 committed 批次写入
- batch_aggregates(...)            按 site 聚合快照
- import_attempts(...)             每次上传的结论（含回滚记录）/review 用
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
  batch_id TEXT PRIMARY KEY,
  filename TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  encoding TEXT NOT NULL,
  bom INTEGER NOT NULL DEFAULT 0,
  row_count INTEGER NOT NULL,
  total_amount_cents INTEGER NOT NULL,
  total_qty INTEGER NOT NULL,
  forced INTEGER NOT NULL DEFAULT 0,
  superseded_by TEXT,
  created_at TEXT NOT NULL,
  note TEXT
);
CREATE INDEX IF NOT EXISTS idx_batches_sha ON batches(sha256);

CREATE TABLE IF NOT EXISTS fact_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id TEXT NOT NULL,
  row_no INTEGER NOT NULL,
  order_id INTEGER NOT NULL,
  site TEXT NOT NULL,
  category TEXT NOT NULL,
  order_date TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  qty INTEGER NOT NULL,
  memo TEXT
);
CREATE INDEX IF NOT EXISTS idx_fact_batch ON fact_orders(batch_id);

CREATE TABLE IF NOT EXISTS batch_aggregates (
  batch_id TEXT NOT NULL,
  site TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  qty INTEGER NOT NULL,
  row_count INTEGER NOT NULL,
  PRIMARY KEY (batch_id, site)
);

CREATE TABLE IF NOT EXISTS import_attempts (
  attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
  filename TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  encoding TEXT,
  status TEXT NOT NULL,
  error_count INTEGER NOT NULL DEFAULT 0,
  row_count INTEGER NOT NULL DEFAULT 0,
  batch_id TEXT,
  detail TEXT,
  errors_json TEXT,
  created_at TEXT NOT NULL
);
"""

# 允许的上传策略（避免路由随意传值）
STATUSES = ("committed", "duplicate_file", "rejected", "encoding_failed", "rolled_back")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    target = Path(path) if path else settings.DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), isolation_level=None, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def count_rows(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM fact_orders").fetchone()["c"]


def find_batch_by_sha(conn: sqlite3.Connection, sha256: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM batches WHERE sha256 = ? ORDER BY created_at ASC LIMIT 1", (sha256,)
    ).fetchone()


def list_batches(conn: sqlite3.Connection, limit: int = 30) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM batches ORDER BY created_at DESC, batch_id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [
        {
            "batch_id": r["batch_id"],
            "filename": r["filename"],
            "sha256": r["sha256"],
            "encoding": r["encoding"],
            "bom": bool(r["bom"]),
            "row_count": r["row_count"],
            "total_amount": f"{r['total_amount_cents'] // 100}.{r['total_amount_cents'] % 100:02d}",
            "total_qty": r["total_qty"],
            "forced": bool(r["forced"]),
            "superseded_by": r["superseded_by"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def get_batch(conn: sqlite3.Connection, batch_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
    if not row:
        return None
    agg = [
        {
            "site": a["site"],
            "amount": f"{a['amount_cents'] // 100}.{a['amount_cents'] % 100:02d}",
            "amount_cents": a["amount_cents"],
            "qty": a["qty"],
            "row_count": a["row_count"],
        }
        for a in conn.execute(
            "SELECT * FROM batch_aggregates WHERE batch_id = ? ORDER BY amount_cents DESC",
            (batch_id,),
        ).fetchall()
    ]
    sample = [
        dict(r) for r in conn.execute(
            "SELECT row_no, order_id, site, category, order_date, amount_cents, qty, memo "
            "FROM fact_orders WHERE batch_id = ? ORDER BY row_no LIMIT 20",
            (batch_id,),
        ).fetchall()
    ]
    for r in sample:
        r["amount"] = f"{r['amount_cents'] // 100}.{r['amount_cents'] % 100:02d}"
    return {
        "batch_id": row["batch_id"],
        "filename": row["filename"],
        "sha256": row["sha256"],
        "encoding": row["encoding"],
        "bom": bool(row["bom"]),
        "row_count": row["row_count"],
        "total_amount": f"{row['total_amount_cents'] // 100}.{row['total_amount_cents'] % 100:02d}",
        "total_amount_cents": row["total_amount_cents"],
        "total_qty": row["total_qty"],
        "forced": bool(row["forced"]),
        "superseded_by": row["superseded_by"],
        "created_at": row["created_at"],
        "note": row["note"],
        "aggregates": agg,
        "sample_rows": sample,
    }


def list_attempts(conn: sqlite3.Connection, limit: int = 30) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM import_attempts ORDER BY attempt_id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_attempt(conn: sqlite3.Connection, attempt_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM import_attempts WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()
    if not row:
        return None
    data = dict(row)
    try:
        data["errors"] = json.loads(data.get("errors_json") or "[]")
    except json.JSONDecodeError:
        data["errors"] = []
    return data


def record_attempt(conn: sqlite3.Connection, *, filename: str, sha256: str, status: str,
                   encoding: str = "", error_count: int = 0, row_count: int = 0,
                   batch_id: str | None = None, detail: str = "",
                   errors: list[dict] | None = None) -> int:
    payload = json.dumps(errors[:200] if errors else [], ensure_ascii=False)
    cur = conn.execute(
        "INSERT INTO import_attempts(filename, sha256, encoding, status, error_count,"
        " row_count, batch_id, detail, errors_json, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (filename, sha256, encoding, status, error_count, row_count, batch_id, detail,
         payload, now_iso()),
    )
    return int(cur.lastrowid or 0)


def stats(conn: sqlite3.Connection) -> dict:
    batches = conn.execute("SELECT COUNT(*) AS c FROM batches").fetchone()["c"]
    fact = count_rows(conn)
    rejected = conn.execute(
        "SELECT COUNT(*) AS c FROM import_attempts WHERE status IN ('rejected','encoding_failed')"
    ).fetchone()["c"]
    sites = conn.execute("SELECT COUNT(DISTINCT site) AS c FROM fact_orders").fetchone()["c"]
    return {"batches": batches, "fact_rows": fact, "rejected_attempts": rejected, "sites": sites}
