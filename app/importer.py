"""模块 5 · 事务导入（含重复文件与回滚）。

语义：
- 单事务写入：BEGIN IMMEDIATE → 批次 + 明细 + 聚合 → COMMIT；任一语句异常 → ROLLBACK，
  明细行数保持写入前值（test_importer 用 fail_after 注入故障证明）。
- 重复文件：同 sha256 已入库 → duplicate_file，不写入任何行，返回既有批次号。
- force_new_batch=true：显式覆盖策略，新建批次号（-F2/-F3…）重新入库；
  旧批次保留并把 superseded_by 指向新批次，便于追溯「同一份文件被重灌了几次」。
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime

from . import store


def file_sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _new_batch_id(conn: sqlite3.Connection, sha256: str, forced: bool) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"B-{stamp}"
    if forced:
        prior = conn.execute(
            "SELECT COUNT(*) AS c FROM batches WHERE sha256 = ?", (sha256,)
        ).fetchone()["c"]
        base = f"{base}-F{prior + 1}"
    candidate = base
    suffix = 1
    while conn.execute("SELECT 1 FROM batches WHERE batch_id = ?", (candidate,)).fetchone():
        suffix += 1
        candidate = f"{base}-{suffix}"
    return candidate


def _aggregate_totals(aggregates: dict) -> tuple[int, int]:
    return int(aggregates["total_amount_cents"]), int(aggregates["total_qty"])


def import_batch(
    conn: sqlite3.Connection,
    *,
    filename: str,
    sha256: str,
    rows: list[dict],
    aggregates: dict,
    encoding: str,
    bom: bool,
    force_new_batch: bool = False,
    fail_after: int | None = None,
    note: str = "",
    batch_id: str | None = None,
) -> dict:
    """返回导入结论；不抛业务异常（故障也以 rolled_back 结果返回，便于页面展示）。"""
    existing = store.find_batch_by_sha(conn, sha256)
    rows_before = store.count_rows(conn)

    if existing and not force_new_batch:
        attempt_id = store.record_attempt(
            conn, filename=filename, sha256=sha256, status="duplicate_file",
            encoding=encoding, row_count=len(rows), batch_id=existing["batch_id"],
            detail="同 sha256 文件已入库，未写入任何行",
        )
        return {
            "status": "duplicate_file",
            "batch_id": existing["batch_id"],
            "existing_filename": existing["filename"],
            "existing_created_at": existing["created_at"],
            "attempt_id": attempt_id,
            "rows_before": rows_before,
            "rows_after": rows_before,
            "written": 0,
            "duplicate_hint": (
                "同一份文件（sha256 相同）已在库中。若确认要作为新批次重灌，"
                "勾选「force_new_batch」后重新提交；旧批次会保留并被标记 superseded。"
            ),
        }

    total_cents, total_qty = _aggregate_totals(aggregates)
    by_site_cents = sum(int(g["amount_cents"]) for g in aggregates["groups"])
    if total_cents != by_site_cents or len(rows) != aggregates["row_count"]:
        detail = (f"聚合自检未通过：明细 {len(rows)} 行 / 聚合 {aggregates['row_count']} 行，"
                  f"金额 {total_cents} 分 vs 分组和 {by_site_cents} 分；未写入。")
        store.record_attempt(conn, filename=filename, sha256=sha256, status="rolled_back",
                             encoding=encoding, error_count=1, row_count=len(rows),
                             detail=detail)
        return {"status": "rolled_back", "detail": detail,
                "rows_before": rows_before, "rows_after": rows_before, "written": 0}

    batch_id = batch_id or _new_batch_id(conn, sha256, forced=bool(existing))
    written = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO batches(batch_id, filename, sha256, encoding, bom, row_count,"
            " total_amount_cents, total_qty, forced, superseded_by, created_at, note)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (batch_id, filename, sha256, encoding, 1 if bom else 0, len(rows),
             total_cents, total_qty, 1 if existing else 0, None, store.now_iso(), note),
        )
        for row in rows:
            conn.execute(
                "INSERT INTO fact_orders(batch_id, row_no, order_id, site, category,"
                " order_date, amount_cents, qty, memo) VALUES(?,?,?,?,?,?,?,?,?)",
                (batch_id, int(row["row_no"]), int(row["order_id"]), row["site"],
                 row["category"], row["order_date"], int(row["amount_cents"]),
                 int(row["qty"]), row.get("memo") or None),
            )
            written += 1
            if fail_after is not None and written >= fail_after:
                raise RuntimeError(f"注入故障：写入第 {written} 行后中断（用于证明事务回滚）")

        for group in aggregates["groups"]:
            conn.execute(
                "INSERT INTO batch_aggregates(batch_id, site, amount_cents, qty, row_count)"
                " VALUES(?,?,?,?,?)",
                (batch_id, group["site"], int(group["amount_cents"]), int(group["qty"]),
                 int(group["row_count"])),
            )

        if existing:
            conn.execute(
                "UPDATE batches SET superseded_by = ? WHERE sha256 = ? AND superseded_by IS NULL",
                (batch_id, sha256),
            )

        conn.execute("COMMIT")
    except Exception as exc:  # noqa: BLE001 - 事务失败必须整体回滚
        conn.execute("ROLLBACK")
        rows_after = store.count_rows(conn)
        detail = f"事务回滚：{exc}；明细行数 {rows_before} → {rows_after}（应保持不变）。"
        store.record_attempt(conn, filename=filename, sha256=sha256, status="rolled_back",
                             encoding=encoding, error_count=1, row_count=len(rows),
                             detail=detail)
        return {"status": "rolled_back", "detail": detail, "batch_id": batch_id,
                "rows_before": rows_before, "rows_after": rows_after, "written": 0,
                "error": str(exc)}

    rows_after = store.count_rows(conn)
    attempt_id = store.record_attempt(
        conn, filename=filename, sha256=sha256, status="committed", encoding=encoding,
        row_count=len(rows), batch_id=batch_id,
        detail=("force_new_batch 覆盖导入" if existing else "首次入库"),
    )
    return {
        "status": "committed",
        "batch_id": batch_id,
        "attempt_id": attempt_id,
        "rows_before": rows_before,
        "rows_after": rows_after,
        "written": written,
        "forced": bool(existing),
        "superseded_batch_id": existing["batch_id"] if existing else None,
    }
