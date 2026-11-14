"""按日发布单元：站点齐全才切换；单站重发只替换该站；失败保留上一版；重试不新增版本。"""
from __future__ import annotations

import pytest

from app import dayrelease, pipeline, store

HEADER = ["site", "category", "order_id", "order_date", "amount", "qty"]
SITES = list(dayrelease.EXPECTED_SITES)
DAY = "2026-11-05"


def site_csv(site: str, day: str = DAY, *, count: int = 3, start_id: int = 1000,
             amount: str = "10.00") -> bytes:
    lines = [",".join(HEADER)]
    for i in range(count):
        lines.append(f"{site},grocery,{start_id + i},{day},{amount},2")
    return ("\n".join(lines) + "\n").encode("utf-8")


def ingest(conn, raw: bytes, filename: str = "site.csv", **kwargs):
    return dayrelease.ingest(conn, raw, filename, **kwargs)


@pytest.fixture()
def conn(tmp_path):
    connection = store.connect(tmp_path / "t.sqlite3")
    store.init_schema(connection)
    yield connection
    connection.close()


def _published(conn, day: str = DAY) -> dict | None:
    return dayrelease.day_summary(conn, day)


def test_single_site_is_staged_and_not_visible(conn):
    result = ingest(conn, site_csv("beijing"), "beijing.csv")

    assert result["status"] == "staged"
    assert result["version_created"] is False
    assert result["snapshot_created"] is True
    assert result["staged_sites"] == ["beijing"]
    assert "shanghai" in result["missing_sites"]
    assert _published(conn) is None                       # 日汇总不提前变化
    assert dayrelease.current_days(conn) == []
    pending = dayrelease.pending_days(conn)
    assert [p["business_date"] for p in pending] == [DAY]
    assert pending[0]["complete"] is False
    assert "beijing" in pending[0]["staged_sites"]


def test_all_sites_publish_atomically_once(conn):
    results = [ingest(conn, site_csv(site), f"{site}.csv") for site in SITES]

    for result in results[:-1]:
        assert result["status"] == "staged"
        assert result["day"] is None
    last = results[-1]
    assert last["status"] == "published"
    assert last["version_created"] is True
    assert last["version_no"] == 1

    day = _published(conn)
    assert day is not None
    assert day["version_no"] == 1
    assert day["site_count"] == len(SITES)
    assert day["row_count"] == len(SITES) * 3
    assert day["total_amount_cents"] == len(SITES) * 3 * 1000

    # 明细与站点汇总属于同一版本，可互相核对
    detail_rows = conn.execute(
        "SELECT COUNT(*) AS c FROM v_current_release_rows WHERE business_date = ?", (DAY,)
    ).fetchone()["c"]
    agg_rows = conn.execute(
        "SELECT COALESCE(SUM(row_count),0) AS c FROM v_current_release_aggregates"
        " WHERE business_date = ?", (DAY,)
    ).fetchone()["c"]
    assert detail_rows == agg_rows == day["row_count"]

    versions = dayrelease.list_versions(conn, DAY)
    assert len(versions) == 1
    assert versions[0]["is_current"] is True


def test_single_site_resend_replaces_only_that_site_and_keeps_old_version(conn):
    for site in SITES:
        ingest(conn, site_csv(site), f"{site}.csv")
    v1 = _published(conn)
    assert v1["version_no"] == 1
    beijing_before = next(s for s in v1["sites"] if s["site"] == "beijing")

    # 单站重发，金额变化
    result = ingest(conn, site_csv("beijing", amount="99.00"), "beijing-v2.csv")
    assert result["status"] == "published"
    assert result["version_no"] == 2
    v2 = _published(conn)
    assert v2["version_no"] == 2

    beijing_after = next(s for s in v2["sites"] if s["site"] == "beijing")
    assert beijing_after["amount_cents"] != beijing_before["amount_cents"]
    assert beijing_after["amount_cents"] == 3 * 9900
    # 其余站点沿用上一版快照
    for site in SITES:
        if site == "beijing":
            continue
        before = next(s for s in v1["sites"] if s["site"] == site)
        after = next(s for s in v2["sites"] if s["site"] == site)
        assert before["amount_cents"] == after["amount_cents"]

    # 旧版本仍可按版本号回查，且明细仍是旧金额
    old = dayrelease.get_version(conn, DAY, 1)
    assert old["is_current"] is False
    old_beijing = next(s for s in old["sites"] if s["site"] == "beijing")
    assert old_beijing["amount_cents"] == beijing_before["amount_cents"]
    assert old_beijing["sample_rows"][0]["amount_cents"] == 1000
    new = dayrelease.get_version(conn, DAY, 2)
    assert new["is_current"] is True


def test_retry_same_file_creates_no_new_snapshot_or_version(conn):
    for site in SITES:
        ingest(conn, site_csv(site), f"{site}.csv")
    assert _published(conn)["version_no"] == 1
    snapshots_before = conn.execute("SELECT COUNT(*) AS c FROM site_uploads").fetchone()["c"]
    versions_before = conn.execute(
        "SELECT COUNT(*) AS c FROM day_releases WHERE business_date = ?", (DAY,)
    ).fetchone()["c"]

    retry = ingest(conn, site_csv("beijing"), "beijing.csv")
    assert retry["status"] == "unchanged"
    assert retry["snapshot_created"] is False
    assert retry["version_created"] is False
    assert retry["version_no"] == 1
    assert conn.execute("SELECT COUNT(*) AS c FROM site_uploads").fetchone()["c"] == snapshots_before
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM day_releases WHERE business_date = ?", (DAY,)
    ).fetchone()["c"] == versions_before


def test_retry_while_incomplete_does_not_duplicate_snapshot(conn):
    ingest(conn, site_csv("beijing"), "beijing.csv")
    first = ingest(conn, site_csv("beijing"), "beijing.csv")
    assert first["status"] == "unchanged"
    assert first["snapshot_created"] is False
    assert conn.execute("SELECT COUNT(*) AS c FROM site_uploads").fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) AS c FROM day_releases").fetchone()["c"] == 0


def test_failed_validation_keeps_previous_version(conn):
    for site in SITES:
        ingest(conn, site_csv(site), f"{site}.csv")
    before = _published(conn)
    bad = site_csv("beijing").replace(b"10.00", b"not-a-number")
    result = ingest(conn, bad, "beijing-bad.csv")

    assert result["status"] == "rejected"
    assert result["snapshot_created"] is False
    after = _published(conn)
    assert after == before                                 # 上一版仍在生效
    assert dayrelease.list_versions(conn, DAY)[0]["version_no"] == 1


def test_failure_during_snapshot_write_rolls_back_and_keeps_previous(conn):
    for site in SITES:
        ingest(conn, site_csv(site), f"{site}.csv")
    before = _published(conn)
    snapshots_before = conn.execute("SELECT COUNT(*) AS c FROM site_uploads").fetchone()["c"]

    result = ingest(conn, site_csv("beijing", amount="77.00"), "beijing-fail.csv",
                    fail_after=2)
    assert result["status"] == "rolled_back"
    assert "按日发布失败" in result["detail"]
    assert _published(conn) == before
    assert conn.execute("SELECT COUNT(*) AS c FROM site_uploads").fetchone()["c"] == snapshots_before
    assert conn.execute("SELECT COUNT(*) AS c FROM day_releases").fetchone()["c"] == 1


def test_failure_during_publish_keeps_previous_version(conn, monkeypatch):
    for site in SITES:
        ingest(conn, site_csv(site), f"{site}.csv")
    before = _published(conn)
    original = dayrelease._publish

    def boom_after_write(*args, **kwargs):
        original(*args, **kwargs)          # 版本行/汇总/指针都已写入同一事务
        raise RuntimeError("注入发布故障：写入后再失败")

    monkeypatch.setattr(dayrelease, "_publish", boom_after_write)
    result = ingest(conn, site_csv("beijing", amount="77.00"), "beijing-fail.csv")

    assert result["status"] == "rolled_back"
    assert result["version_created"] is False
    assert _published(conn) == before
    # 版本行与指针都随事务回滚；快照插入也在同一事务里回滚，不会留下半份数据
    assert conn.execute("SELECT COUNT(*) AS c FROM day_releases").fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) AS c FROM site_uploads").fetchone()["c"] == len(SITES)
    assert conn.execute("SELECT COUNT(*) AS c FROM current_days").fetchone()["c"] == 1


def test_incomplete_day_never_appears_in_current_results(conn):
    ingest(conn, site_csv("beijing", day="2026-11-05"), "beijing.csv")
    ingest(conn, site_csv("shanghai", day="2026-11-06"), "shanghai.csv")

    assert dayrelease.current_days(conn) == []
    assert dayrelease.day_summary(conn, "2026-11-05") is None
    assert dayrelease.day_summary(conn, "2026-11-06") is None
    assert {p["business_date"] for p in dayrelease.pending_days(conn)} == {
        "2026-11-05", "2026-11-06",
    }


def test_multi_site_or_multi_day_file_is_rejected_without_writes(conn):
    multi_site = (
        "site,category,order_id,order_date,amount,qty\n"
        "beijing,grocery,1001,2026-11-05,10.00,1\n"
        "shanghai,grocery,1002,2026-11-05,10.00,1\n"
    ).encode("utf-8")
    result = ingest(conn, multi_site, "multi.csv")
    assert result["status"] == "rejected"
    assert result["stage"] == "release-key"
    assert "站点" in result["detail"]
    assert conn.execute("SELECT COUNT(*) AS c FROM site_uploads").fetchone()["c"] == 0
    assert dayrelease.current_days(conn) == []

    multi_day = (
        "site,category,order_id,order_date,amount,qty\n"
        "beijing,grocery,1001,2026-11-05,10.00,1\n"
        "beijing,grocery,1002,2026-11-06,10.00,1\n"
    ).encode("utf-8")
    result = ingest(conn, multi_day, "multi_day.csv")
    assert result["status"] == "rejected"
    assert "下单日期" in result["detail"]


def test_days_are_independent(conn):
    for site in SITES:
        ingest(conn, site_csv(site, day="2026-11-05"), f"{site}-05.csv")
    # 第二天的数据不齐，不影响第一天的当前结果
    ingest(conn, site_csv("beijing", day="2026-11-06"), "beijing-06.csv")

    days = dayrelease.current_days(conn)
    assert [d["business_date"] for d in days] == ["2026-11-05"]
    assert days[0]["version_no"] == 1
    assert [p["business_date"] for p in dayrelease.pending_days(conn)] == ["2026-11-06"]


def test_legacy_single_file_import_still_works(conn):
    """旧口径 API 不受影响：一份文件 = 一个批次。"""
    text = "site,category,order_id,order_date,amount,qty\n"
    text += "".join(f"beijing,grocery,{1001 + i},2026-11-05,10.00,2\n" for i in range(4))
    result = pipeline.run(text.encode("utf-8"), "legacy.csv", conn=conn)
    assert result["status"] == "committed"
    assert result["written"] == 4
    assert store.count_rows(conn) == 4
    assert dayrelease.current_days(conn) == []            # 旧口径不进入按日发布结果
