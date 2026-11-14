"""事务导入：成功写入、故障回滚（行数不变）、重复文件、force_new_batch 覆盖。"""
from __future__ import annotations

import sqlite3

import pytest

from app import aggregate, encoding, importer, pipeline, rows as rows_mod, store

HEADER = ["site", "category", "order_id", "order_date", "amount", "qty"]


@pytest.fixture()
def conn(tmp_path):
    connection = store.connect(tmp_path / "t.sqlite3")
    store.init_schema(connection)
    yield connection
    connection.close()


def sample_rows(n: int = 5) -> list[dict]:
    data = [
        ["beijing", "grocery", str(1001 + i), "2026-11-05", "10.00", "2"]
        for i in range(n)
    ]
    result = rows_mod.validate(data, HEADER, HEADER)
    assert result.ok, result.error_dicts()
    return result.rows


def test_successful_import_writes_rows_and_aggregates(conn):
    rows = sample_rows(5)
    agg = aggregate.summarize(rows)
    before = store.count_rows(conn)

    outcome = importer.import_batch(
        conn, filename="a.csv", sha256="a" * 64, rows=rows, aggregates=agg,
        encoding="utf-8", bom=False,
    )

    assert outcome["status"] == "committed"
    assert outcome["written"] == 5
    assert store.count_rows(conn) == before + 5
    batch = store.get_batch(conn, outcome["batch_id"])
    assert batch["row_count"] == 5
    assert batch["total_amount"] == "50.00"
    assert batch["encoding"] == "utf-8"
    assert [g["site"] for g in batch["aggregates"]] == ["beijing"]
    assert batch["aggregates"][0]["qty"] == 10
    attempts = store.list_attempts(conn)
    assert attempts[0]["status"] == "committed"


def test_failure_midway_rolls_back_and_row_count_unchanged(conn):
    rows = sample_rows(6)
    agg = aggregate.summarize(rows)
    before = store.count_rows(conn)

    outcome = importer.import_batch(
        conn, filename="b.csv", sha256="b" * 64, rows=rows, aggregates=agg,
        encoding="utf-8", bom=False, fail_after=3,
    )

    assert outcome["status"] == "rolled_back"
    assert "注入故障" in outcome["error"]
    assert store.count_rows(conn) == before                      # 明细行数不变
    assert conn.execute("SELECT COUNT(*) AS c FROM batches").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM batch_aggregates").fetchone()["c"] == 0
    assert store.list_attempts(conn)[0]["status"] == "rolled_back"
    assert outcome["rows_before"] == outcome["rows_after"] == before


def test_duplicate_file_is_idempotent(conn):
    rows = sample_rows(4)
    agg = aggregate.summarize(rows)
    sha = "c" * 64

    first = importer.import_batch(conn, filename="c.csv", sha256=sha, rows=rows,
                                  aggregates=agg, encoding="utf-8", bom=False)
    assert first["status"] == "committed"
    count_after_first = store.count_rows(conn)

    second = importer.import_batch(conn, filename="c.csv", sha256=sha, rows=rows,
                                   aggregates=agg, encoding="utf-8", bom=False)
    assert second["status"] == "duplicate_file"
    assert second["batch_id"] == first["batch_id"]
    assert second["written"] == 0
    assert store.count_rows(conn) == count_after_first            # 没有新增任何行
    assert second["rows_before"] == second["rows_after"]


def test_force_new_batch_creates_new_batch_and_supersedes(conn):
    rows = sample_rows(3)
    agg = aggregate.summarize(rows)
    sha = "d" * 64

    first = importer.import_batch(conn, filename="d.csv", sha256=sha, rows=rows,
                                  aggregates=agg, encoding="utf-8", bom=False)
    count_after_first = store.count_rows(conn)

    forced = importer.import_batch(conn, filename="d.csv", sha256=sha, rows=rows,
                                   aggregates=agg, encoding="utf-8", bom=False,
                                   force_new_batch=True)
    assert forced["status"] == "committed"
    assert forced["batch_id"] != first["batch_id"]
    assert forced["forced"] is True
    assert store.count_rows(conn) == count_after_first + 3

    old = store.get_batch(conn, first["batch_id"])
    assert old["superseded_by"] == forced["batch_id"]
    new = store.get_batch(conn, forced["batch_id"])
    assert new["forced"] is True


def test_aggregate_guard_blocks_inconsistent_payload(conn):
    rows = sample_rows(4)
    agg = aggregate.summarize(rows)
    agg["total_amount_cents"] += 100          # 人为制造聚合不一致

    outcome = importer.import_batch(conn, filename="e.csv", sha256="e" * 64, rows=rows,
                                    aggregates=agg, encoding="utf-8", bom=False)
    assert outcome["status"] == "rolled_back"
    assert "聚合自检未通过" in outcome["detail"]
    assert store.count_rows(conn) == 0


def test_pipeline_rollback_via_api_path_keeps_rows_unchanged(conn):
    """走完整管道：broken 的字节被拒绝，行数不变，且记录了 rejected。"""
    text = "site,category,order_id,order_date,amount,qty\n"
    text += "beijing,grocery,1001,2026-11-05,10.00,1\n"
    text += "beijing,grocery,1001,2026-11-06,10.00,1\n"        # 重复主键
    raw = text.encode("utf-8")

    before = store.count_rows(conn)
    result = pipeline.run(raw, "dup.csv", conn=conn)

    assert result["status"] == "rejected"
    assert result["stage"] == "rows"
    assert result["report"]["total"] == 1
    assert store.count_rows(conn) == before
    assert store.list_attempts(conn)[0]["status"] == "rejected"
    assert store.list_attempts(conn)[0]["error_count"] == 1


def test_pipeline_commits_and_duplicate_on_second_run(conn):
    rows_text = "site,category,order_id,order_date,amount,qty\n"
    rows_text += "".join(
        f"beijing,grocery,{1001 + i},2026-11-05,10.00,2\n" for i in range(5)
    )
    raw = rows_text.encode("utf-8")

    first = pipeline.run(raw, "ok.csv", conn=conn)
    assert first["status"] == "committed"
    assert first["aggregate"]["consistent"] is True
    assert first["written"] == 5

    second = pipeline.run(raw, "ok.csv", conn=conn)
    assert second["status"] == "duplicate_file"
    assert second["rows_after"] == first["rows_after"]


def test_encoding_failure_recorded_without_writes(conn):
    result = pipeline.run(b"\x80\x81\xff\xfe", "bad.csv", conn=conn)
    assert result["status"] == "encoding_failed"
    assert store.count_rows(conn) == 0
    assert store.list_attempts(conn)[0]["status"] == "encoding_failed"


def test_import_rejects_aggregate_row_count_mismatch(conn):
    rows = sample_rows(2)
    agg = aggregate.summarize(rows)
    agg["row_count"] = 1
    outcome = importer.import_batch(conn, filename="f.csv", sha256="f" * 64, rows=rows,
                                    aggregates=agg, encoding="utf-8", bom=False)
    assert outcome["status"] == "rolled_back"
    assert store.count_rows(conn) == 0


def test_gbk_sample_commits_120_rows(conn):
    from app import settings

    raw = (settings.SAMPLE_DIR / "gbk_120.csv").read_bytes()
    result = pipeline.run(raw, "gbk_120.csv", conn=conn)
    assert result["status"] == "committed"
    assert result["written"] == 120
    assert result["encoding"]["encoding"] == "gb18030"
    batch = store.get_batch(conn, result["batch_id"])
    assert sum(g["row_count"] for g in batch["aggregates"]) == 120
    assert len(batch["aggregates"]) == 6
