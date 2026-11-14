"""按日发布单元：一天内各站点文件共同构成一个发布单元。

问题背景
--------
上游按站点分批送达当天的订单 CSV，且同一站点会重发、内容可能变化。旧口径「一到就写批次」
会让日汇总在站点不齐时提前变化，也会把重发的订单再次累计。

本模块的语义
------------
1. **暂存**：一份站点文件通过编码/契约/行校验后，按 (business_date, site) 写成不可变快照
   （site_uploads + site_upload_rows）。快照写入不等于进入当前结果。
2. **完整性**：契约要求的所有站点都已有快照，才允许发布。
3. **原子切换**：发布在一个事务里写 day_releases + day_release_sites +
   day_release_aggregates，并把 current_days 指针切到新版本。明细与站点汇总同生共死，
   不会出现「汇总已变、明细没变」的中间态。
4. **单站替换**：某站重发并通过校验时，新版本只引用该站的新快照，其余站点沿用上一版快照；
   旧版本永久保留，可按版本号回查。最新的成功上传总是胜出（即使内容回到了更早的版本），
   任何被替换的数据都仍能从历史版本查到，不会丢失。
5. **失败保留上一版**：任何校验失败/发布失败都不动 current_days，上一版继续生效；
   已写入的快照不会因发布失败被当作「当前数据」。
6. **幂等重试**：同一 (日期, 站点) 的最新快照 sha256 与本次相同 → 不新增快照、不新增版本。
7. **未补齐日期不可见**：没有全部站点快照的日期不会出现在 current_days / 日汇总里。
"""
from __future__ import annotations

import sqlite3

from . import contract, pipeline, store

EXPECTED_SITES: tuple[str, ...] = tuple(contract.SITES)

INGEST_STATUSES = (
    "published", "staged", "unchanged", "rejected", "encoding_failed", "rolled_back",
)


def _fmt_cents(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


# ---------------------------------------------------------------- 快照与版本


def _latest_snapshot(conn: sqlite3.Connection, business_date: str,
                     site: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM site_uploads WHERE business_date = ? AND site = ?"
        " ORDER BY upload_id DESC LIMIT 1",
        (business_date, site),
    ).fetchone()


def _latest_snapshots(conn: sqlite3.Connection, business_date: str) -> dict[str, sqlite3.Row]:
    """每个站点取最新快照；这是构造候选版本的基础。"""
    rows = conn.execute(
        "SELECT * FROM site_uploads WHERE business_date = ? ORDER BY site, upload_id",
        (business_date,),
    ).fetchall()
    latest: dict[str, sqlite3.Row] = {}
    for row in rows:  # 按 upload_id 升序，后者覆盖前者
        latest[row["site"]] = row
    return latest


def _current_version(conn: sqlite3.Connection, business_date: str) -> int | None:
    row = conn.execute(
        "SELECT version_no FROM current_days WHERE business_date = ?", (business_date,)
    ).fetchone()
    return int(row["version_no"]) if row else None


def _version_snapshot_ids(conn: sqlite3.Connection, business_date: str,
                          version_no: int) -> dict[str, int]:
    rows = conn.execute(
        "SELECT site, upload_id FROM day_release_sites"
        " WHERE business_date = ? AND version_no = ?",
        (business_date, version_no),
    ).fetchall()
    return {r["site"]: int(r["upload_id"]) for r in rows}


def _verify_snapshot(conn: sqlite3.Connection, upload: sqlite3.Row) -> None:
    """发布前用明细重新算一遍，拒绝任何与快照头不一致的数据。"""
    stats = conn.execute(
        "SELECT COUNT(*) AS c, COALESCE(SUM(amount_cents), 0) AS amount,"
        " COALESCE(SUM(qty), 0) AS qty, COUNT(DISTINCT site) AS sites,"
        " MIN(site) AS one_site"
        " FROM site_upload_rows WHERE upload_id = ?",
        (upload["upload_id"],),
    ).fetchone()
    problems: list[str] = []
    if int(stats["c"]) != int(upload["row_count"]):
        problems.append(f"行数 {stats['c']} != {upload['row_count']}")
    if int(stats["amount"]) != int(upload["total_amount_cents"]):
        problems.append(f"金额 {stats['amount']} != {upload['total_amount_cents']}")
    if int(stats["qty"]) != int(upload["total_qty"]):
        problems.append(f"件数 {stats['qty']} != {upload['total_qty']}")
    if int(stats["sites"]) != 1 or stats["one_site"] != upload["site"]:
        problems.append(
            f"快照应只含 {upload['site']} 一个站点，实际 {stats['sites']} 个"
            f"（{stats['one_site']}）"
        )
    if problems:
        raise ValueError("快照自检未通过：" + "；".join(problems))


def _publish(conn: sqlite3.Connection, business_date: str,
             snapshots: dict[str, sqlite3.Row], reason: str,
             expected_sites: tuple[str, ...] = EXPECTED_SITES) -> int:
    """在调用方的事务内写版本与指针；失败由调用方回滚。返回新版本号。"""
    missing = [site for site in expected_sites if site not in snapshots]
    extra = [site for site in snapshots if site not in expected_sites]
    if missing or extra:
        raise ValueError(
            f"发布清单不完整：缺失 {missing or '无'}，多余 {extra or '无'}"
        )
    for snapshot in snapshots.values():
        _verify_snapshot(conn, snapshot)

    version_no = int(conn.execute(
        "SELECT COALESCE(MAX(version_no), 0) AS v FROM day_releases WHERE business_date = ?",
        (business_date,),
    ).fetchone()["v"]) + 1

    conn.execute(
        "INSERT INTO day_releases(business_date, version_no, status, reason,"
        " created_at, published_at) VALUES(?,?,?,?,?,?)",
        (business_date, version_no, "published", reason, store.now_iso(), store.now_iso()),
    )
    for site, snapshot in sorted(snapshots.items()):
        conn.execute(
            "INSERT INTO day_release_sites(business_date, version_no, site, upload_id)"
            " VALUES(?,?,?,?)",
            (business_date, version_no, site, snapshot["upload_id"]),
        )
        conn.execute(
            "INSERT INTO day_release_aggregates(business_date, version_no, site,"
            " amount_cents, qty, row_count) VALUES(?,?,?,?,?,?)",
            (business_date, version_no, site, int(snapshot["total_amount_cents"]),
             int(snapshot["total_qty"]), int(snapshot["row_count"])),
        )

    conn.execute(
        "INSERT INTO current_days(business_date, version_no, published_at) VALUES(?,?,?)"
        " ON CONFLICT(business_date) DO UPDATE SET"
        " version_no = excluded.version_no, published_at = excluded.published_at",
        (business_date, version_no, store.now_iso()),
    )
    return version_no


# ---------------------------------------------------------------- 查询（当前 / 历史）


def day_summary(conn: sqlite3.Connection, business_date: str) -> dict | None:
    """当前生效的日汇总；未发布（站点不齐）返回 None。"""
    version = _current_version(conn, business_date)
    if version is None:
        return None
    release = conn.execute(
        "SELECT * FROM day_releases WHERE business_date = ? AND version_no = ?",
        (business_date, version),
    ).fetchone()
    aggregates = conn.execute(
        "SELECT site, amount_cents, qty, row_count FROM day_release_aggregates"
        " WHERE business_date = ? AND version_no = ? ORDER BY site",
        (business_date, version),
    ).fetchall()
    sites = [
        {
            "site": a["site"],
            "amount": _fmt_cents(int(a["amount_cents"])),
            "amount_cents": int(a["amount_cents"]),
            "qty": int(a["qty"]),
            "row_count": int(a["row_count"]),
        }
        for a in aggregates
    ]
    total_cents = sum(s["amount_cents"] for s in sites)
    total_qty = sum(s["qty"] for s in sites)
    total_rows = sum(s["row_count"] for s in sites)
    return {
        "business_date": business_date,
        "version_no": version,
        "published_at": release["published_at"] if release else None,
        "sites": sites,
        "site_count": len(sites),
        "row_count": total_rows,
        "total_amount_cents": total_cents,
        "total_amount": _fmt_cents(total_cents),
        "total_qty": total_qty,
    }


def current_days(conn: sqlite3.Connection) -> list[dict]:
    dates = [r["business_date"] for r in conn.execute(
        "SELECT business_date FROM current_days ORDER BY business_date DESC"
    ).fetchall()]
    return [summary for summary in (day_summary(conn, d) for d in dates) if summary]


def pending_days(conn: sqlite3.Connection, expected_sites: tuple[str, ...] = EXPECTED_SITES) -> list[dict]:
    """有快照但尚未发布的日期：明确列出已到站点与缺失站点，绝不进入日汇总。"""
    dates = [r["business_date"] for r in conn.execute(
        "SELECT DISTINCT business_date FROM site_uploads"
        " WHERE business_date NOT IN (SELECT business_date FROM current_days)"
        " ORDER BY business_date DESC"
    ).fetchall()]
    pending = []
    for day in dates:
        latest = _latest_snapshots(conn, day)
        staged = sorted(latest)
        pending.append({
            "business_date": day,
            "staged_sites": staged,
            "missing_sites": [s for s in expected_sites if s not in latest],
            "complete": all(s in latest for s in expected_sites),
            "snapshot_count": int(conn.execute(
                "SELECT COUNT(*) AS c FROM site_uploads WHERE business_date = ?", (day,)
            ).fetchone()["c"]),
        })
    return pending


def list_versions(conn: sqlite3.Connection, business_date: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM day_releases WHERE business_date = ? ORDER BY version_no DESC",
        (business_date,),
    ).fetchall()
    current = _current_version(conn, business_date)
    versions = []
    for row in rows:
        ids = _version_snapshot_ids(conn, business_date, int(row["version_no"]))
        versions.append({
            "business_date": business_date,
            "version_no": int(row["version_no"]),
            "status": row["status"],
            "reason": row["reason"],
            "created_at": row["created_at"],
            "published_at": row["published_at"],
            "is_current": current == int(row["version_no"]),
            "sites": sorted(ids),
            "site_upload_ids": ids,
        })
    return versions


def get_version(conn: sqlite3.Connection, business_date: str, version_no: int,
                row_limit: int = 20) -> dict | None:
    """按版本号回查历史发布：站点汇总 + 每个站点前 N 行明细（旧版永不删除）。"""
    release = conn.execute(
        "SELECT * FROM day_releases WHERE business_date = ? AND version_no = ?",
        (business_date, version_no),
    ).fetchone()
    if not release:
        return None
    current = _current_version(conn, business_date)
    ids = _version_snapshot_ids(conn, business_date, version_no)
    sites = []
    for site, upload_id in sorted(ids.items()):
        upload = conn.execute(
            "SELECT * FROM site_uploads WHERE upload_id = ?", (upload_id,)
        ).fetchone()
        sample = [
            dict(r) for r in conn.execute(
                "SELECT row_no, order_id, site, category, order_date, amount_cents, qty, memo"
                " FROM site_upload_rows WHERE upload_id = ? ORDER BY row_no LIMIT ?",
                (upload_id, row_limit),
            ).fetchall()
        ]
        for item in sample:
            item["amount"] = _fmt_cents(int(item["amount_cents"]))
        sites.append({
            "site": site,
            "upload_id": int(upload_id),
            "filename": upload["filename"] if upload else None,
            "sha256": upload["sha256"] if upload else None,
            "encoding": upload["encoding"] if upload else None,
            "bom": bool(upload["bom"]) if upload else False,
            "row_count": int(upload["row_count"]) if upload else 0,
            "amount": _fmt_cents(int(upload["total_amount_cents"])) if upload else "0.00",
            "amount_cents": int(upload["total_amount_cents"]) if upload else 0,
            "qty": int(upload["total_qty"]) if upload else 0,
            "snapshot_created_at": upload["created_at"] if upload else None,
            "sample_rows": sample,
        })
    return {
        "business_date": business_date,
        "version_no": int(release["version_no"]),
        "status": release["status"],
        "reason": release["reason"],
        "created_at": release["created_at"],
        "published_at": release["published_at"],
        "is_current": current == int(release["version_no"]),
        "sites": sites,
        "row_count": sum(s["row_count"] for s in sites),
        "total_amount": _fmt_cents(sum(s["amount_cents"] for s in sites)),
        "total_amount_cents": sum(s["amount_cents"] for s in sites),
        "total_qty": sum(s["qty"] for s in sites),
    }


# ---------------------------------------------------------------- 摄入（暂存 + 可能发布）


def _record(conn: sqlite3.Connection, prepared: dict, status: str, *,
            error_count: int = 0, row_count: int = 0, detail: str = "",
            errors: list[dict] | None = None) -> int:
    detail = detail
    if prepared.get("release_key", {}).get("key"):
        release_key = prepared["release_key"]
        detail = f"[{release_key['day']}/{release_key['site']}] {detail}"
    return store.record_attempt(
        conn,
        filename=prepared.get("filename", "upload.csv"),
        sha256=prepared.get("sha256") or "",
        status=status,
        encoding=(prepared.get("encoding") or {}).get("encoding") or "",
        error_count=error_count,
        row_count=row_count,
        detail=detail,
        errors=errors,
    )


def _derive_key(rows: list[dict], release_key: dict | None = None) -> tuple[str | None, str | None, str]:
    """单站、单日文件才能进入按日发布；返回 (日期, 站点, 错误说明)。"""
    if release_key and release_key.get("key"):
        return release_key["day"], release_key["site"], ""
    dates = sorted({r["order_date"] for r in rows})
    sites = sorted({r["site"] for r in rows})
    if len(dates) != 1:
        return None, None, (
            f"文件包含 {len(dates)} 个下单日期（{', '.join(dates[:5])}"
            f"{'…' if len(dates) > 5 else ''}）；按日发布要求每份文件只含一天的数据。"
        )
    if len(sites) != 1:
        return None, None, (
            f"文件包含 {len(sites)} 个站点（{', '.join(sites[:5])}"
            f"{'…' if len(sites) > 5 else ''}）；按日发布要求每份文件只含一个站点，"
            "请按站点拆分后重传。"
        )
    return dates[0], sites[0], ""


def is_release_unit(prepared: dict) -> bool:
    """调用方（mode=auto）判断一份文件是否应按日发布。

    以契约+行校验后的发布键为准；文件校验失败时退回旧口径报告，保持历史结论不变。
    """
    return prepared.get("status") == "ok" and bool(
        (prepared.get("release_key") or {}).get("key")
    )


def ingest(
    conn: sqlite3.Connection,
    raw: bytes,
    filename: str,
    extra_policy: str = "reject",
    expected_sites: tuple[str, ...] = EXPECTED_SITES,
    fail_after: int | None = None,
    prepared: dict | None = None,
) -> dict:
    """摄入一份站点日文件：校验 → 暂存快照 → （站点齐全时）原子发布。

    `prepared` 允许调用方复用已经算好的 pipeline.prepare 结果，避免重复解析。
    `fail_after` 仅用于测试：在快照写入 k 行后抛错，证明失败不会改变上一版。
    """
    prepared = prepared or pipeline.prepare(raw, filename, extra_policy)
    base = {
        "business_date": None,
        "site": None,
        "expected_sites": list(expected_sites),
        "filename": prepared["filename"],
        "sha256": prepared["sha256"],
        "bytes": prepared["bytes"],
        "extra_policy": extra_policy,
        "release_key": prepared.get("release_key"),
    }

    # 编码 / 契约 / 行校验失败：不写快照，不碰 current_days。
    if prepared["status"] == "encoding_failed":
        attempt_id = _record(conn, prepared, "encoding_failed", detail=prepared["detail"])
        base.update({
            "status": "encoding_failed",
            "stage": "encoding",
            "detail": prepared["detail"],
            "encoding": prepared["encoding"],
            "suggestion": prepared["suggestion"],
            "attempt_id": attempt_id,
            "version_created": False,
            "snapshot_created": False,
        })
        return base

    if prepared["status"] == "rejected":
        stage = prepared["stage"]
        errors = (prepared.get("header_errors")
                  if stage == "contract" else prepared["validation"].error_dicts())
        attempt_id = _record(
            conn, prepared, "rejected",
            error_count=len(errors), row_count=prepared.get("data_row_count", 0),
            detail=prepared["detail"], errors=errors,
        )
        base.update({
            "status": "rejected",
            "stage": stage,
            "detail": prepared["detail"],
            "encoding": prepared["encoding"],
            "contract": prepared["contract"],
            "row_validation": prepared.get("row_validation"),
            "report": prepared["report"],
            "attempt_id": attempt_id,
            "version_created": False,
            "snapshot_created": False,
        })
        return base

    business_date, site, key_error = _derive_key(
        prepared["rows"], prepared.get("release_key")
    )
    if key_error:
        attempt_id = _record(
            conn, prepared, "rejected", error_count=1,
            row_count=prepared["data_row_count"], detail=key_error,
        )
        base.update({
            "status": "rejected",
            "stage": "release-key",
            "detail": key_error,
            "encoding": prepared["encoding"],
            "report": prepared["report"],
            "attempt_id": attempt_id,
            "version_created": False,
            "snapshot_created": False,
        })
        return base

    base.update({
        "business_date": business_date,
        "site": site,
        "encoding": prepared["encoding"],
        "aggregate": prepared["summary"],
        "row_validation": prepared["row_validation"],
    })

    summary = prepared["summary"]
    if not summary.get("consistent"):
        detail = ("聚合自检未通过（分组/明细/Decimal 口径不一致）；"
                  "该站点文件不暂存、不发布，上一版保持不变。")
        attempt_id = _record(conn, prepared, "rejected", error_count=1,
                             row_count=prepared["data_row_count"], detail=detail)
        base.update({
            "status": "rejected",
            "stage": "aggregate",
            "detail": detail,
            "attempt_id": attempt_id,
            "snapshot_created": False,
            "version_created": False,
        })
        return base

    total_cents, total_qty = int(summary["total_amount_cents"]), int(summary["total_qty"])
    snapshot_created = False
    version_created = False
    status = "staged"
    previous_version: int | None = None

    try:
        conn.execute("BEGIN IMMEDIATE")
        previous_version = _current_version(conn, business_date)

        latest = _latest_snapshot(conn, business_date, site)
        reused_idempotent = bool(latest and latest["sha256"] == prepared["sha256"])

        if reused_idempotent:
            upload_id = int(latest["upload_id"])
        else:
            upload_id = _insert_snapshot(
                conn, business_date, site, prepared, total_cents, total_qty, fail_after,
            )
            snapshot_created = True

        candidate = _latest_snapshots(conn, business_date)
        missing = [s for s in expected_sites if s not in candidate]
        complete = not missing

        if complete:
            current_ids = (_version_snapshot_ids(conn, business_date, previous_version)
                           if previous_version is not None else {})
            candidate_ids = {s: int(u["upload_id"]) for s, u in candidate.items()}
            if previous_version is not None and candidate_ids == current_ids:
                # 重试同一份（或站点已全部为当前快照）：不新增版本。
                status = "unchanged"
            else:
                reason = ("重试同一份内容，不新增版本" if reused_idempotent
                          else f"站点齐全（{len(candidate)}/{len(expected_sites)}），发布新版本")
                _publish(conn, business_date, candidate, reason,
                         expected_sites=expected_sites)
                version_created = True
                status = "published"
        else:
            # 站点未齐：只有在新增了快照时才算推进；重试同一份仍是 unchanged。
            status = "unchanged" if reused_idempotent else "staged"

        conn.execute("COMMIT")
    except Exception as exc:  # noqa: BLE001 - 发布失败必须回滚到上一版
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass  # 事务可能已被显式回滚
        detail = (f"按日发布失败：{exc}；本次未切换，"
                  f"{business_date} 保持上一版"
                  f"{'（版本 ' + str(previous_version) + '）' if previous_version else '（尚未发布）'}。")
        attempt_id = _record(conn, prepared, "rolled_back", error_count=1,
                             row_count=prepared["data_row_count"], detail=detail)
        base.update({
            "status": "rolled_back",
            "stage": "publish",
            "detail": detail,
            "attempt_id": attempt_id,
            "snapshot_created": False,
            "version_created": False,
            "previous_version": previous_version,
            "current_version": _current_version(conn, business_date),
        })
        return base

    version_no = _current_version(conn, business_date)
    attempt_id = _record(
        conn, prepared, status, row_count=prepared["data_row_count"],
        detail=(f"{status}；version={version_no}；"
                f"snapshot={'new' if snapshot_created else 'reused'}"),
    )
    base.update({
        "status": status,
        "stage": "release",
        "upload_id": upload_id,
        "snapshot_created": snapshot_created,
        "version_created": version_created,
        "version_no": version_no,
        "previous_version": previous_version,
        "current_version": version_no,
        "missing_sites": missing,
        "staged_sites": sorted(candidate),
        "attempt_id": attempt_id,
        "day": day_summary(conn, business_date),
    })
    return base


def _insert_snapshot(conn: sqlite3.Connection, business_date: str, site: str,
                     prepared: dict, total_cents: int, total_qty: int,
                     fail_after: int | None) -> int:
    rows = prepared["rows"]
    cur = conn.execute(
        "INSERT INTO site_uploads(business_date, site, filename, sha256, encoding, bom,"
        " row_count, total_amount_cents, total_qty, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (business_date, site, prepared["filename"], prepared["sha256"],
         (prepared.get("encoding") or {}).get("encoding") or "",
         1 if (prepared.get("encoding") or {}).get("bom") else 0,
         len(rows), total_cents, total_qty, store.now_iso()),
    )
    upload_id = int(cur.lastrowid or 0)
    for index, row in enumerate(rows, start=1):
        conn.execute(
            "INSERT INTO site_upload_rows(upload_id, row_no, order_id, site, category,"
            " order_date, amount_cents, qty, memo) VALUES(?,?,?,?,?,?,?,?,?)",
            (upload_id, int(row["row_no"]), int(row["order_id"]), row["site"],
             row["category"], row["order_date"], int(row["amount_cents"]),
             int(row["qty"]), row.get("memo") or None),
        )
        if fail_after is not None and index >= fail_after:
            raise RuntimeError(f"注入故障：写入快照第 {index} 行后中断（用于证明不丢上一版）")
    return upload_id


__all__ = [
    "EXPECTED_SITES", "INGEST_STATUSES", "ingest", "is_release_unit",
    "day_summary", "current_days", "pending_days", "list_versions", "get_version",
]
