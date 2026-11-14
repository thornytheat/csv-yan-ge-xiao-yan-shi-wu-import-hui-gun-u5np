"""HTTP 端到端：健康检查、上传成功、5 条行级错误 422、回滚行数不变、重复文件、页面主流程。"""
from __future__ import annotations

import io

from app import contract

SIX_CODES = {"duplicate", "out_of_order", "date_invalid", "length", "enum"}


def upload(client, path, name, **form):
    data = {"extra_policy": "reject", "mode": "legacy", **form}
    return client.post("/api/import", files={"file": (name, path.read_bytes(), "text/csv")}, data=data)


def test_health_and_seed(client):
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["seed_version"].startswith("seed-")
    assert body["fact_rows"] == 60          # seed 的历史批次 60 行


def test_pages_render(client):
    for path, needle in [("/", "按日发布"), ("/days", "发布单元"),
                         ("/workbench", "表头自检"), ("/review", "上传记录")]:
        res = client.get(path)
        assert res.status_code == 200, path
        assert needle in res.text, path


def test_upload_normal_500_commits(client, sample_dir):
    res = upload(client, sample_dir / "normal_500.csv", "normal_500.csv")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "committed"
    assert body["written"] == 500
    assert body["rows_before"] == 60
    assert body["rows_after"] == 560
    assert body["aggregate"]["consistent"] is True
    assert len(body["aggregate"]["groups"]) == 6
    assert body["aggregate"]["total_amount_cents"] == sum(
        g["amount_cents"] for g in body["aggregate"]["groups"]
    )
    assert client.get("/health").json()["fact_rows"] == 560


def test_upload_broken_returns_422_with_five_row_errors(client, sample_dir):
    before = client.get("/health").json()["fact_rows"]
    res = upload(client, sample_dir / "broken.csv", "broken.csv")

    assert res.status_code == 422
    body = res.json()
    assert body["status"] == "rejected"
    assert body["stage"] == "rows"
    assert body["report"]["total"] == 5
    assert {e["code"] for e in body["report"]["rows"]} == SIX_CODES
    assert body["report"]["first_line"] == 4
    for err in body["report"]["rows"]:
        assert err["line_no"] >= 2 and err["reason"] and err["label"]
        assert err["column"] in {"site", "category", "order_id", "order_date"}
    # 回滚：库内行数不变
    assert client.get("/health").json()["fact_rows"] == before
    assert body["rows_before"] == body["rows_after"] == before


def test_duplicate_file_then_force_new_batch(client, sample_dir):
    first = upload(client, sample_dir / "normal_500.csv", "normal_500.csv").json()
    assert first["status"] == "committed"
    rows_after_first = first["rows_after"]

    duplicate = upload(client, sample_dir / "normal_500.csv", "normal_500.csv")
    assert duplicate.status_code == 200
    dup = duplicate.json()
    assert dup["status"] == "duplicate_file"
    assert dup["batch_id"] == first["batch_id"]
    assert dup["written"] == 0
    assert dup["rows_after"] == rows_after_first
    assert "force_new_batch" in dup["duplicate_hint"]
    assert client.get("/health").json()["fact_rows"] == rows_after_first

    forced = upload(client, sample_dir / "normal_500.csv", "normal_500.csv",
                    force_new_batch="true")
    assert forced.status_code == 200
    forced_body = forced.json()
    assert forced_body["status"] == "committed"
    assert forced_body["batch_id"] != first["batch_id"]
    assert forced_body["rows_after"] == rows_after_first + 500

    old = client.get(f"/result/{first['batch_id']}")
    assert old.status_code == 200
    assert forced_body["batch_id"] in old.text      # superseded_by 指向新批次


def test_gbk_upload_uses_gb18030(client, sample_dir):
    res = upload(client, sample_dir / "gbk_120.csv", "gbk_120.csv")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "committed"
    assert body["encoding"]["encoding"] == "gb18030"
    assert body["encoding"]["attempts"][0]["ok"] is False
    assert body["written"] == 120
    assert body["rows_after"] == 180


def test_illegal_encoding_returns_400_with_suggestion(client):
    files = {"file": ("broken_encoding.csv", b"site,category\n\x80\x81\xff\xfe,x\n", "text/csv")}
    res = client.post("/api/import",
                      files=files, data={"extra_policy": "reject", "mode": "legacy"})
    assert res.status_code == 400
    body = res.json()
    assert body["status"] == "encoding_failed"
    assert "建议" in body["detail"]
    assert body["encoding"]["sample_hex"].startswith("73 69 74 65")   # "site" 的十六进制


def test_extra_column_policy_reject_and_warn(client):
    text = "site,category,order_id,order_date,amount,qty,note\n"
    text += "beijing,grocery,1001,2026-11-05,10.00,1,x\n"
    raw = text.encode("utf-8")

    rejected = client.post("/api/import", files={"file": ("extra.csv", raw, "text/csv")},
                           data={"extra_policy": "reject", "mode": "legacy"})
    assert rejected.status_code == 422
    assert rejected.json()["stage"] == "contract"
    assert [e["code"] for e in rejected.json()["contract"]["errors"]] == ["extra_column"]

    warned = client.post("/api/import", files={"file": ("extra.csv", raw, "text/csv")},
                         data={"extra_policy": "warn", "mode": "legacy"})
    assert warned.status_code == 200
    body = warned.json()
    assert body["status"] == "committed"
    assert body["written"] == 1
    assert body["contract"]["warnings"][0]["code"] == "extra_column_ignored"


def test_workbench_form_redirects_to_result_page(client, sample_dir):
    res = client.post(
        "/workbench",
        files={"file": ("normal_500.csv", (sample_dir / "normal_500.csv").read_bytes(), "text/csv")},
        data={"extra_policy": "reject"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    location = res.headers["location"]
    assert location.startswith("/result/B-")

    page = client.get(location)
    assert page.status_code == 200
    assert "按站点聚合" in page.text
    assert "beijing" in page.text
    assert "合计" in page.text


def test_workbench_form_rejected_renders_error_table(client, sample_dir):
    res = client.post(
        "/workbench",
        files={"file": ("broken.csv", (sample_dir / "broken.csv").read_bytes(), "text/csv")},
        data={"extra_policy": "reject"},
        follow_redirects=False,
    )
    assert res.status_code == 422
    assert "错误报告：共 5 处" in res.text
    assert "主键重复" in res.text
    assert "2026-02-30" in res.text


def test_review_page_lists_rollback_and_replay_check(client, sample_dir):
    upload(client, sample_dir / "broken.csv", "broken.csv")
    page = client.get("/review")
    assert page.status_code == 200
    assert "校验拒绝" in page.text
    assert "被拒绝/回滚的上传次数" in page.text

    attempt_link = page.text.split('/result/attempt/')[1].split('"')[0]
    attempt_page = client.get(f"/result/attempt/{attempt_link}")
    assert attempt_page.status_code == 200
    assert "整体回滚" in attempt_page.text
    assert "逐行依据" in attempt_page.text

    sha = upload(client, sample_dir / "normal_500.csv", "normal_500.csv").json()["sha256"]
    replay = client.post("/api/replay-check", json={"sha256": sha})
    assert replay.status_code == 200
    assert replay.json()["verdict"] == "duplicate_file"

    unknown = client.post("/api/replay-check", json={"sha256": "0" * 64})
    assert unknown.json()["verdict"] == "new_file"


def test_contract_check_api(client):
    ok = client.post("/api/contract-check", json={"header": " qty , site,category,order_id,order_date,amount "})
    assert ok.status_code == 200
    assert ok.json()["ok"] is True

    missing = client.post("/api/contract-check", json={"header": "site,category,order_id,amount,qty"})
    body = missing.json()
    assert body["ok"] is False
    assert body["missing"] == ["order_date"]

    extra = client.post("/api/contract-check",
                        json={"header": "site,category,order_id,order_date,amount,qty,note",
                              "extra_policy": "warn"})
    assert extra.json()["ok"] is True
    assert extra.json()["warnings"][0]["code"] == "extra_column_ignored"


def test_sample_downloads(client):
    csv_res = client.get("/api/samples/normal_500.csv")
    assert csv_res.status_code == 200
    assert csv_res.content.startswith(b"site,category,order_id")
    assert csv_res.content.count(b"\n") == 501

    json_res = client.get("/api/samples/contract.json")
    assert json_res.status_code == 200
    assert b"strictly_increasing" in json_res.content

    assert client.get("/api/samples/../../etc/passwd").status_code == 404


def test_contract_module_keeps_order_independent():
    result = contract.check(["qty", " amount ", "order_date", "order_id", "category", "site"])
    assert result["ok"] is True
    assert result["header"][1] == "amount"
    assert result["duplicate"] == []
    assert set(result["columns_used"]) == set(contract.REQUIRED)
