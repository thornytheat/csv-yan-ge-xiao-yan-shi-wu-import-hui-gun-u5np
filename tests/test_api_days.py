"""按日发布 HTTP 端到端：暂存不可见、补齐后原子发布、单站重发、重试幂等、历史版本可查。"""
from __future__ import annotations

import pytest

SITES = ["beijing", "shanghai", "guangzhou", "shenzhen", "chengdu", "hangzhou"]


def upload_day(client, raw: bytes, name: str, **form):
    return client.post("/api/day-import", files={"file": (name, raw, "text/csv")},
                       data={"extra_policy": "reject", **form})


def full_day_lines(day: str = "2026-11-10", per_site: int = 3, start: int = 500001):
    """六个站点的文件，各一份字节。"""
    out = {}
    for idx, site in enumerate(SITES):
        lines = ["site,category,order_id,order_date,amount,qty"]
        base = start + idx * 100
        for i in range(per_site):
            lines.append(f"{site},grocery,{base + i},{day},10.00,2")
        out[site] = ("\n".join(lines) + "\n").encode("utf-8")
    return out


@pytest.fixture()
def seeded_days(client):
    """种子数据：2026-11-08 已完整发布；2026-11-09 只有 2 站。"""
    return client


def test_seed_exposes_one_current_and_one_pending_day(seeded_days):
    days = seeded_days.get("/api/days").json()
    assert days["current_count"] == 1
    assert days["pending_count"] == 1
    current = days["current"][0]
    assert current["business_date"] == "2026-11-08"
    assert current["version_no"] == 1
    assert current["site_count"] == 6
    assert current["row_count"] == 240
    pending = days["pending"][0]
    assert pending["business_date"] == "2026-11-09"
    assert pending["complete"] is False
    assert sorted(pending["staged_sites"]) == ["beijing", "shanghai"]
    assert sorted(pending["missing_sites"]) == sorted(
        set(SITES) - {"beijing", "shanghai"}
    )


def test_incomplete_day_is_invisible_in_current_results(seeded_days):
    days = seeded_days.get("/api/days").json()
    assert [d["business_date"] for d in days["current"]] == ["2026-11-08"]

    pending = seeded_days.get("/api/days/2026-11-09").json()
    assert pending["published"] is False
    assert pending["summary"] is None
    assert pending["pending"]["complete"] is False


def test_first_sites_stage_then_last_site_publishes_atomically(client):
    files = full_day_lines()
    results = []
    for site in SITES:
        res = upload_day(client, files[site], f"{site}.csv")
        results.append(res)

    for res in results[:-1]:
        assert res.status_code == 202, res.text
        body = res.json()
        assert body["status"] == "staged"
        assert body["version_created"] is False
        assert body["visible_in_current"] is False
        assert body["business_date"] == "2026-11-10"

    last = results[-1].json()
    assert last["status"] == "published"
    assert last["version_created"] is True
    assert last["version_no"] == 1
    assert last["missing_sites"] == []
    assert last["day"]["row_count"] == 6 * 3

    day = client.get("/api/days/2026-11-10").json()
    assert day["published"] is True
    assert day["summary"]["row_count"] == 18
    assert day["summary"]["total_amount_cents"] == 18 * 1000
    assert day["summary"]["site_count"] == 6


def test_resend_one_site_replaces_only_that_site(client):
    files = full_day_lines()
    for site in SITES:
        upload_day(client, files[site], f"{site}.csv")

    v1 = client.get("/api/days/2026-11-10").json()["summary"]
    beijing_v1 = next(s for s in v1["sites"] if s["site"] == "beijing")

    # 单站重发：北京金额变为 99.00 / 行
    changed = files["beijing"].replace(b"10.00", b"99.00")
    res = upload_day(client, changed, "beijing-v2.csv")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "published"
    assert body["version_no"] == 2
    assert body["previous_version"] == 1

    v2 = client.get("/api/days/2026-11-10").json()["summary"]
    beijing_v2 = next(s for s in v2["sites"] if s["site"] == "beijing")
    assert beijing_v2["amount_cents"] == 3 * 9900
    assert beijing_v2["row_count"] == beijing_v1["row_count"]
    for site in SITES:
        if site == "beijing":
            continue
        before = next(s for s in v1["sites"] if s["site"] == site)
        after = next(s for s in v2["sites"] if s["site"] == site)
        assert before == after                              # 其余站点沿用上一版快照

    # 旧版本仍可查，且是旧金额
    old = client.get("/api/days/2026-11-10/versions/1").json()
    assert old["is_current"] is False
    old_beijing = next(s for s in old["sites"] if s["site"] == "beijing")
    assert old_beijing["amount_cents"] == 3 * 1000
    assert old_beijing["sample_rows"][0]["amount_cents"] == 1000

    versions = client.get("/api/days/2026-11-10").json()["versions"]
    assert [v["version_no"] for v in versions] == [2, 1]
    assert versions[0]["is_current"] is True


def test_retry_same_file_does_not_add_version(client):
    files = full_day_lines()
    for site in SITES:
        upload_day(client, files[site], f"{site}.csv")

    res = upload_day(client, files["beijing"], "beijing.csv")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "unchanged"
    assert body["snapshot_created"] is False
    assert body["version_created"] is False
    assert body["version_no"] == 1

    versions = client.get("/api/days/2026-11-10").json()["versions"]
    assert len(versions) == 1
    day = client.get("/api/days/2026-11-10").json()["summary"]
    assert day["version_no"] == 1


def test_retry_before_complete_does_not_add_snapshot(client):
    files = full_day_lines()
    first = upload_day(client, files["beijing"], "beijing.csv").json()
    second = upload_day(client, files["beijing"], "beijing.csv").json()
    assert first["status"] == "staged"
    assert second["status"] == "unchanged"
    assert second["snapshot_created"] is False
    pending_dates = {p["business_date"] for p in client.get("/api/days").json()["pending"]}
    assert pending_dates == {"2026-11-09", "2026-11-10"}


def test_bad_site_file_keeps_current_version(client):
    files = full_day_lines()
    for site in SITES:
        upload_day(client, files[site], f"{site}.csv")
    before = client.get("/api/days/2026-11-10").json()["summary"]

    bad = files["beijing"].replace(b"grocery", b"furniture")
    res = upload_day(client, bad, "beijing-bad.csv")
    assert res.status_code == 422
    body = res.json()
    assert body["status"] == "rejected"
    assert body["snapshot_created"] is False
    assert body["visible_in_current"] is False

    after = client.get("/api/days/2026-11-10").json()["summary"]
    assert after == before
    versions = client.get("/api/days/2026-11-10").json()["versions"]
    assert len(versions) == 1


def test_multi_site_file_is_rejected_and_never_visible(client):
    raw = (
        "site,category,order_id,order_date,amount,qty\n"
        "beijing,grocery,1001,2026-11-10,10.00,1\n"
        "shanghai,grocery,1002,2026-11-10,10.00,1\n"
    ).encode("utf-8")
    res = upload_day(client, raw, "multi.csv")
    assert res.status_code == 422
    body = res.json()
    assert body["status"] == "rejected"
    assert body["stage"] == "release-key"
    assert "站点" in body["detail"]
    assert client.get("/api/days/2026-11-10").status_code == 404
    pending_dates = {p["business_date"] for p in client.get("/api/days").json()["pending"]}
    assert "2026-11-10" not in pending_dates       # 被拒文件连暂存都没有


def test_current_rows_and_aggregates_are_same_version(client):
    """当前明细视图与当前汇总视图必须同时指向 current_days。"""
    from app import dayrelease, store

    files = full_day_lines()
    for site in SITES:
        upload_day(client, files[site], f"{site}.csv")
    upload_day(client, files["beijing"].replace(b"10.00", b"99.00"), "beijing-v2.csv")

    conn = store.connect()
    try:
        summary = dayrelease.day_summary(conn, "2026-11-10")
        assert summary["version_no"] == 2
        detail_rows = conn.execute(
            "SELECT COUNT(*) AS c FROM v_current_release_rows WHERE business_date = ?",
            ("2026-11-10",),
        ).fetchone()["c"]
        agg_rows = conn.execute(
            "SELECT COALESCE(SUM(row_count), 0) AS c FROM v_current_release_aggregates"
            " WHERE business_date = ?",
            ("2026-11-10",),
        ).fetchone()["c"]
        assert detail_rows == agg_rows == summary["row_count"]
        beijing = conn.execute(
            "SELECT SUM(amount_cents) AS a FROM v_current_release_rows"
            " WHERE business_date = ? AND release_site = 'beijing'",
            ("2026-11-10",),
        ).fetchone()["a"]
        assert beijing == 3 * 9900
    finally:
        conn.close()


def test_days_pages_render(client):
    assert client.get("/days").status_code == 200
    seeded = client.get("/api/days").json()
    for day in seeded["current"]:
        assert client.get(f"/days/{day['business_date']}").status_code == 200
        version = client.get(
            f"/days/{day['business_date']}/v/{day['version_no']}"
        )
        assert version.status_code == 200
        assert "当前生效" in version.text
    for pending in seeded["pending"]:
        assert client.get(f"/days/{pending['business_date']}").status_code == 200


def test_day_version_page_marks_history_readonly(client):
    files = full_day_lines()
    for site in SITES:
        upload_day(client, files[site], f"{site}.csv")
    upload_day(client, files["beijing"].replace(b"10.00", b"99.00"), "beijing-v2.csv")

    page = client.get("/days/2026-11-10/v/1")
    assert page.status_code == 200
    assert "历史版本" in page.text
    assert "已被更新版本替代" in page.text


def test_api_import_auto_routes_site_day_file_to_release_unit(client, sample_dir):
    """默认 mode=auto：单站单日文件按日发布；多站文件退回旧口径批次。"""
    res = client.post(
        "/api/import",
        files={"file": ("beijing.csv", (sample_dir / "pending_20261109_beijing.csv").read_bytes(),
                        "text/csv")},
        data={"extra_policy": "reject"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "unchanged"                  # seed 已有相同快照
    assert body["business_date"] == "2026-11-09"
    multi = client.post(
        "/api/import",
        files={"file": ("normal_500.csv", (sample_dir / "normal_500.csv").read_bytes(),
                        "text/csv")},
        data={"extra_policy": "reject", "mode": "legacy"},
    )
    body = multi.json()
    assert body["status"] == "committed"
    assert body.get("business_date") is None
    assert body["written"] == 500


def test_api_import_daily_mode_rejects_multi_day_file(client):
    raw = (
        "site,category,order_id,order_date,amount,qty\n"
        "beijing,grocery,1001,2026-11-10,10.00,1\n"
        "beijing,grocery,1002,2026-11-11,10.00,1\n"
    ).encode("utf-8")
    res = client.post("/api/import", files={"file": ("multi_day.csv", raw, "text/csv")},
                      data={"extra_policy": "reject", "mode": "daily"})
    assert res.status_code == 422
    body = res.json()
    assert body["status"] == "rejected"
    assert "下单日期" in body["detail"]
    assert body["business_date"] is None
    assert client.get("/api/days").json()["pending_count"] == 1   # 只有 seed 的 11-09


def test_day_samples_are_downloadable(client):
    res = client.get("/api/samples/day_20261108_beijing.csv")
    assert res.status_code == 200
    assert res.content.startswith(b"site,category,order_id")
    for name in ("pending_20261109_beijing.csv", "pending_20261109_chengdu.csv",
                 "resend_20261108_beijing.csv"):
        assert client.get(f"/api/samples/{name}").status_code == 200


def test_completing_pending_day_with_samples_publishes(client, sample_dir):
    """补齐 2026-11-09 余下 4 站后，该日一次性进入当前结果。"""
    for name in ("pending_20261109_guangzhou.csv", "pending_20261109_shenzhen.csv",
                 "pending_20261109_chengdu.csv"):
        res = upload_day(client, (sample_dir / name).read_bytes(), name)
        assert res.status_code == 202, res.text

    res = upload_day(client, (sample_dir / "pending_20261109_hangzhou.csv").read_bytes(),
                     "pending_20261109_hangzhou.csv")
    assert res.json()["status"] == "published"

    days = client.get("/api/days").json()
    dates = [d["business_date"] for d in days["current"]]
    assert "2026-11-09" in dates
    assert days["pending_count"] == 0
    day = client.get("/api/days/2026-11-09").json()["summary"]
    assert day["site_count"] == 6
    assert day["row_count"] == 6 * 25


def test_resend_sample_creates_second_version(client, sample_dir):
    res = upload_day(client, (sample_dir / "resend_20261108_beijing.csv").read_bytes(),
                     "resend_20261108_beijing.csv")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "published"
    assert body["version_no"] == 2
    day = client.get("/api/days/2026-11-08").json()["summary"]
    assert day["version_no"] == 2
    v1 = client.get("/api/days/2026-11-08/versions/1").json()
    v2 = client.get("/api/days/2026-11-08/versions/2").json()
    b1 = next(s for s in v1["sites"] if s["site"] == "beijing")
    b2 = next(s for s in v2["sites"] if s["site"] == "beijing")
    assert b2["amount_cents"] > b1["amount_cents"]
    assert [s["amount_cents"] for s in v1["sites"] if s["site"] != "beijing"] == \
           [s["amount_cents"] for s in v2["sites"] if s["site"] != "beijing"]


def test_upload_form_redirects_when_key_derivable(client):
    files = full_day_lines()
    res = client.post(
        "/days/upload",
        files={"file": ("beijing.csv", files["beijing"], "text/csv")},
        data={"extra_policy": "reject"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"].startswith("/days/2026-11-10")

    page = client.get(res.headers["location"])
    assert page.status_code == 200
    assert "待补齐" in page.text
