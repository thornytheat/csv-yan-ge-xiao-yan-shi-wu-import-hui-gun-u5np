"""启动即用的历史数据 seed（幂等：以 meta 中的版本键为准）。

两条线：
- `seed_20261107.csv`：旧口径「一份文件 = 一个批次」的历史周批次，走 `pipeline.run`。
- 2026-11-08 的 6 份站点文件：按日发布的历史完整版本（启动时一次发布 v1）；
  2026-11-09 只 seed 2 个站点，故意保持「未补齐」，因此不会出现在当前结果里。
"""
from __future__ import annotations

import sqlite3

from . import dayrelease, pipeline, settings, store


def _seed_legacy_batch(conn: sqlite3.Connection) -> dict:
    if store.get_meta(conn, "seed_version") == settings.SEED_VERSION:
        return {"seeded": False, "reason": "seed_version 已存在，跳过"}

    path = settings.SAMPLE_DIR / settings.SEED_FILENAME
    if not path.exists():
        return {"seeded": False, "reason": f"样例文件缺失：{path.name}"}

    raw = path.read_bytes()
    result = pipeline.run(
        raw, settings.SEED_FILENAME, extra_policy="reject", conn=conn,
        batch_id=settings.SEED_BATCH_ID,
    )

    if result.get("status") == "committed":
        store.set_meta(conn, "seed_version", settings.SEED_VERSION)
        store.set_meta(conn, "seed_batch", settings.SEED_BATCH_ID)
        return {"seeded": True, "batch_id": settings.SEED_BATCH_ID,
                "row_count": result.get("written", 0)}
    if result.get("status") == "duplicate_file":
        store.set_meta(conn, "seed_version", settings.SEED_VERSION)
        return {"seeded": False, "reason": "同 sha256 已入库（重复启动）",
                "batch_id": result.get("batch_id")}
    return {"seeded": False, "reason": f"seed 未写入：{result.get('detail', '')}"}


def _seed_days(conn: sqlite3.Connection) -> dict:
    if store.get_meta(conn, "day_seed_version") == settings.DAY_SEED_VERSION:
        return {"seeded": False, "reason": "day_seed_version 已存在，跳过"}

    complete = [settings.SAMPLE_DIR / name for name in settings.DAY_SEED_FILES]
    pending = [settings.SAMPLE_DIR / name for name in settings.PENDING_SEED_FILES]
    missing = [p.name for p in complete + pending if not p.exists()]
    if missing:
        return {"seeded": False, "reason": f"样例文件缺失：{', '.join(missing)}"}

    outcomes = []
    for path in complete:
        outcomes.append(dayrelease.ingest(conn, path.read_bytes(), path.name))
    for path in pending:
        outcomes.append(dayrelease.ingest(conn, path.read_bytes(), path.name))

    published = [o for o in outcomes if o.get("status") == "published"]
    if not published:
        return {"seeded": False,
                "reason": "按日 seed 未发布：" + "; ".join(
                    o.get("detail", o.get("status", "")) for o in outcomes)}

    store.set_meta(conn, "day_seed_version", settings.DAY_SEED_VERSION)
    store.set_meta(conn, "day_seed_date", settings.DAY_SEED_DATE)
    return {"seeded": True, "date": settings.DAY_SEED_DATE,
            "version_no": published[-1].get("version_no"),
            "row_count": sum(o.get("aggregate", {}).get("row_count", 0)
                             for o in outcomes if o.get("status") == "published")}


def ensure_seed(conn: sqlite3.Connection) -> dict:
    legacy = _seed_legacy_batch(conn)
    days = _seed_days(conn)
    return {"legacy": legacy, "days": days}
