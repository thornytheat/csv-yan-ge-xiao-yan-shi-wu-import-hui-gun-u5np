"""启动即用的历史批次 seed（幂等：以 meta.seed_version 为准）。

seed 走与真实上传完全相同的管道，因此样例库里的聚合与明细天然自洽。
"""
from __future__ import annotations

import sqlite3

from . import pipeline, settings, store


def ensure_seed(conn: sqlite3.Connection) -> dict:
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
