"""SQLite 访问（薄封装，无 ORM）。

表：
- meta(key,value)                  seed 版本
- batches(...)                     旧口径多日批次，sha256 用于重复文件判定
- fact_orders(...)                 旧口径明细事实，仅 committed 批次写入
- batch_aggregates(...)            旧口径按 site 聚合快照
- import_attempts(...)             每次上传的结论（含回滚记录）/review 用
- site_uploads(...)                按日发布：单个 (日期, 站点) 的不可变上传快照
- site_upload_rows(...)            上述快照的明细，发布后仍保留，供旧版本回查
- day_releases(...)                每日发布版本（旧版本永不删除）
- day_release_sites(...)           版本清单：每个站点引用哪一份上传快照
- day_release_aggregates(...)      版本内的站点汇总（与明细同事务写入）
- current_days(...)                当前生效版本指针；未补齐的日期不会出现在这里

按日发布的核心约束：明细和站点汇总只在 day_releases / day_release_sites /
day_release_aggregates 完整写入并切换 current_days 后，才对「当前结果」可见。
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

-- ---------------------------------------------------------------- 按日发布单元

-- 单个 (business_date, site) 上传的完整快照。行只增不改：同一站点重发会追加新快照，
-- 旧快照仍被历史 day_release_sites 引用，因此旧版本始终可查。
CREATE TABLE IF NOT EXISTS site_uploads (
  upload_id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_date TEXT NOT NULL,
  site TEXT NOT NULL,
  filename TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  encoding TEXT NOT NULL,
  bom INTEGER NOT NULL DEFAULT 0,
  row_count INTEGER NOT NULL,
  total_amount_cents INTEGER NOT NULL,
  total_qty INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_site_uploads_day ON site_uploads(business_date, site);
CREATE INDEX IF NOT EXISTS idx_site_uploads_sha ON site_uploads(business_date, site, sha256);

CREATE TABLE IF NOT EXISTS site_upload_rows (
  upload_id INTEGER NOT NULL REFERENCES site_uploads(upload_id),
  row_no INTEGER NOT NULL,
  order_id INTEGER NOT NULL,
  site TEXT NOT NULL,
  category TEXT NOT NULL,
  order_date TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  qty INTEGER NOT NULL,
  memo TEXT,
  PRIMARY KEY (upload_id, row_no)
);
CREATE INDEX IF NOT EXISTS idx_site_upload_rows_upload ON site_upload_rows(upload_id);

-- 每个业务日期每次成功发布一个版本；version_no 从 1 单调递增，永不覆盖旧版本。
CREATE TABLE IF NOT EXISTS day_releases (
  business_date TEXT NOT NULL,
  version_no INTEGER NOT NULL,
  status TEXT NOT NULL,                 -- staged | published | failed
  reason TEXT,
  created_at TEXT NOT NULL,
  published_at TEXT,
  PRIMARY KEY (business_date, version_no)
);

CREATE TABLE IF NOT EXISTS day_release_sites (
  business_date TEXT NOT NULL,
  version_no INTEGER NOT NULL,
  site TEXT NOT NULL,
  upload_id INTEGER NOT NULL REFERENCES site_uploads(upload_id),
  PRIMARY KEY (business_date, version_no, site)
);

CREATE TABLE IF NOT EXISTS day_release_aggregates (
  business_date TEXT NOT NULL,
  version_no INTEGER NOT NULL,
  site TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  qty INTEGER NOT NULL,
  row_count INTEGER NOT NULL,
  PRIMARY KEY (business_date, version_no, site)
);

-- 当前生效版本指针；只有站点齐全且全部校验通过的日期才会写入这里。
-- 任何未补齐日期都不出现在 current_days，因此不会提前影响日汇总。
CREATE TABLE IF NOT EXISTS current_days (
  business_date TEXT PRIMARY KEY,
  version_no INTEGER NOT NULL,
  published_at TEXT NOT NULL,
  FOREIGN KEY (business_date, version_no)
    REFERENCES day_releases(business_date, version_no)
);

-- 「当前结果」的唯一可读口径：明细与站点汇总都从 current_days 指向的版本派生，
-- 因此二者要么同时是旧版，要么同时是新版，不会出现中间态。
CREATE VIEW IF NOT EXISTS v_current_release_rows AS
SELECT c.business_date,
       s.version_no,
       s.site AS release_site,
       r.row_no, r.order_id, r.site, r.category, r.order_date,
       r.amount_cents, r.qty, r.memo
FROM current_days c
JOIN day_release_sites s
  ON s.business_date = c.business_date AND s.version_no = c.version_no
JOIN site_upload_rows r ON r.upload_id = s.upload_id;

CREATE VIEW IF NOT EXISTS v_current_release_aggregates AS
SELECT c.business_date,
       a.version_no,
       a.site,
       a.amount_cents,
       a.qty,
       a.row_count
FROM current_days c
JOIN day_release_aggregates a
  ON a.business_date = c.business_date AND a.version_no = c.version_no;
"""

# 允许的上传策略（避免路由随意传值）
STATUSES = (
    "committed", "duplicate_file", "rejected", "encoding_failed", "rolled_back",
    # 按日发布单元的状态
    "published", "staged", "unchanged",
)


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
        "SELECT COUNT(*) AS c FROM import_attempts"
        " WHERE status IN ('rejected','encoding_failed','rolled_back')"
    ).fetchone()["c"]
    sites = conn.execute("SELECT COUNT(DISTINCT site) AS c FROM fact_orders").fetchone()["c"]
    released_days = conn.execute("SELECT COUNT(*) AS c FROM current_days").fetchone()["c"]
    released_rows = conn.execute(
        "SELECT COUNT(*) AS c FROM v_current_release_rows"
    ).fetchone()["c"]
    pending_days = conn.execute(
        "SELECT COUNT(DISTINCT business_date) AS c FROM site_uploads"
        " WHERE business_date NOT IN (SELECT business_date FROM current_days)"
    ).fetchone()["c"]
    return {
        "batches": batches,
        "fact_rows": fact,
        "rejected_attempts": rejected,
        "sites": sites,
        "released_days": released_days,
        "released_rows": released_rows,
        "pending_days": pending_days,
    }
