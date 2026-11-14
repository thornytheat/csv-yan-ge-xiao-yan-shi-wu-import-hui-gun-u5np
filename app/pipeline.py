"""编排：字节 → 探测 → 契约 → 行校验 → 聚合 → 写入。

两个入口：
- `prepare()`：纯校验，不写库；返回编码证据、契约结论、逐行错误与聚合结果。
  按日发布（`dayrelease`）用它先把整份站点文件校验干净，再决定是否暂存/发布。
- `run()`：旧口径「一份文件 = 一个批次」的单事务导入，行为与历史版本一致。
"""
from __future__ import annotations

import csv
import io
import sqlite3

from . import aggregate, contract, encoding, importer, report, rows as rows_mod, store


def parse_csv(text: str) -> tuple[list[str], list[list[str]], list[int]]:
    """返回 (表头, 数据行, 被跳过的空行行号)。空行只跳过不报错，单独计数。"""
    reader = csv.reader(io.StringIO(text))
    records: list[list[str]] = []
    for record in reader:
        records.append(record)
    if not records:
        return [], [], []
    header = records[0]
    data: list[list[str]] = []
    blank_lines: list[int] = []
    for offset, record in enumerate(records[1:], start=2):
        if not any((cell or "").strip() for cell in record):
            blank_lines.append(offset)
            continue
        data.append(record)
    return header, data, blank_lines


def _key_hint(data: list[list[str]], contract_result: dict) -> dict:
    """从原始数据行提取日期/站点候选，供按日发布判断「单站单日」。

    只做轻量提取（按列名定位），不做类型校验；校验失败的文件也能给出候选。
    """
    columns_used = contract_result.get("columns_used") or []
    header = contract_result.get("header") or []
    if "order_date" not in columns_used or "site" not in columns_used or not header:
        return {"dates": [], "sites": []}
    idx_date = header.index("order_date")
    idx_site = header.index("site")
    dates: set[str] = set()
    sites: set[str] = set()
    for record in data:
        if len(record) <= max(idx_date, idx_site):
            continue
        date_cell = (record[idx_date] or "").strip()
        site_cell = (record[idx_site] or "").strip()
        if date_cell:
            dates.add(date_cell)
        if site_cell:
            sites.add(site_cell)
    return {"dates": sorted(dates), "sites": sorted(sites)}


def _derive_release_key(data: list[list[str]], contract_result: dict) -> dict:
    """按原始数据判断发布键，返回 {key, day, sites, dates, error}。"""
    hint = _key_hint(data, contract_result)
    dates, sites = hint["dates"], hint["sites"]
    if len(dates) == 1 and len(sites) == 1:
        return {"key": (dates[0], sites[0]), "day": dates[0], "site": sites[0],
                "dates": dates, "sites": sites, "error": ""}
    errors = []
    if len(dates) != 1:
        errors.append(f"文件包含 {len(dates)} 个下单日期")
    if len(sites) != 1:
        errors.append(f"文件包含 {len(sites)} 个站点")
    return {"key": None, "day": None, "site": None,
            "dates": dates, "sites": sites, "error": "、".join(errors)}


def prepare(raw: bytes, filename: str, extra_policy: str = "reject") -> dict:
    """只校验、不落库。

    status:
    - ok               全部通过，`rows` / `summary` 可用
    - encoding_failed  编码无法解码
    - rejected         表头契约或行级校验未通过
    """
    sha = importer.file_sha256(raw)
    result: dict = {
        "filename": filename,
        "sha256": sha,
        "bytes": len(raw),
        "extra_policy": extra_policy,
        "sample_hex": encoding.sample_hex(raw),
    }

    # 1) 编码探测
    try:
        probe = encoding.probe(raw)
    except encoding.EncodingError as exc:
        result.update({
            "status": "encoding_failed",
            "stage": "encoding",
            "detail": exc.message,
            "encoding": {
                "encoding": None,
                "bom": raw.startswith(encoding.UTF8_BOM),
                "sample_hex": exc.sample_hex,
                "attempts": exc.attempts,
            },
            "suggestion": exc.message,
            "report": report.build_report([]),
        })
        return result

    result["encoding"] = probe.evidence()

    # 2) 表头契约
    header, data, blank_lines = parse_csv(probe.text)
    contract_result = contract.check(header, extra_policy)
    result["contract"] = contract_result
    result["data_row_count"] = len(data)
    result["skipped_blank_lines"] = blank_lines
    result["release_key"] = _derive_release_key(data, contract_result)

    if not contract_result["ok"]:
        detail = "；".join(e["message"] for e in contract_result["errors"])
        result.update({
            "status": "rejected",
            "stage": "contract",
            "detail": detail,
            "report": report.build_report([]),
            "header_errors": [
                {"line_no": 1, "row_no": 0, "column": ",".join(e.get("columns", [])),
                 "code": e["code"], "label": "表头契约", "raw": "", "reason": e["message"]}
                for e in contract_result["errors"]
            ],
        })
        return result

    # 3) 行级校验
    validation = rows_mod.validate(data, contract_result["header"], contract_result["columns_used"])
    result["validation"] = validation
    result["row_validation"] = {
        "ok": validation.ok,
        "row_count": validation.row_count,
        "error_count": validation.error_count,
        "codes": sorted(set(validation.codes())),
    }
    result["report"] = report.build_report(validation.error_dicts())

    if not validation.ok:
        result.update({
            "status": "rejected",
            "stage": "rows",
            "detail": (f"表头契约通过，但 {validation.error_count} 处行级错误，"
                       f"整份文件未写入（首处：第 {result['report']['first_line']} 行）。"),
        })
        return result

    # 4) 聚合 + 一致性自证
    summary = aggregate.summarize(validation.rows)
    result.update({
        "status": "ok",
        "stage": "ready",
        "detail": "",
        "rows": validation.rows,
        "summary": summary,
        "aggregate": summary,
        # 用已校验的值再确认发布键，避免与原始候选口径不一致
        "release_key": _derive_release_key_from_rows(validation.rows),
    })
    return result


def _derive_release_key_from_rows(rows: list[dict]) -> dict:
    dates = sorted({r["order_date"] for r in rows})
    sites = sorted({r["site"] for r in rows})
    if len(dates) == 1 and len(sites) == 1:
        return {"key": (dates[0], sites[0]), "day": dates[0], "site": sites[0],
                "dates": dates, "sites": sites, "error": ""}
    errors = []
    if len(dates) != 1:
        errors.append(f"文件包含 {len(dates)} 个下单日期")
    if len(sites) != 1:
        errors.append(f"文件包含 {len(sites)} 个站点")
    return {"key": None, "day": None, "site": None,
            "dates": dates, "sites": sites, "error": "、".join(errors)}


def run(
    raw: bytes,
    filename: str,
    extra_policy: str = "reject",
    force_new_batch: bool = False,
    conn: sqlite3.Connection | None = None,
    fail_after: int | None = None,
    batch_id: str | None = None,
) -> dict:
    """旧口径单份文件导入；返回结构与历史版本保持一致。"""
    own_conn = conn is None
    conn = conn or store.connect()
    try:
        prepared = prepare(raw, filename, extra_policy)
        sha = prepared["sha256"]

        result: dict = {
            "filename": prepared["filename"],
            "sha256": sha,
            "bytes": prepared["bytes"],
            "extra_policy": extra_policy,
            "force_new_batch": force_new_batch,
            "sample_hex": prepared["sample_hex"],
        }

        if prepared["status"] == "encoding_failed":
            attempt_id = store.record_attempt(
                conn, filename=filename, sha256=sha, status="encoding_failed",
                detail=prepared["detail"],
            )
            result.update({
                "status": "encoding_failed",
                "detail": prepared["detail"],
                "encoding": prepared["encoding"],
                "suggestion": prepared["suggestion"],
                "attempt_id": attempt_id,
                "rows_before": store.count_rows(conn),
                "rows_after": store.count_rows(conn),
            })
            return result

        result["encoding"] = prepared["encoding"]
        result["contract"] = prepared["contract"]
        result["data_row_count"] = prepared["data_row_count"]
        result["skipped_blank_lines"] = prepared["skipped_blank_lines"]

        if prepared["status"] == "rejected" and prepared["stage"] == "contract":
            attempt_id = store.record_attempt(
                conn, filename=filename, sha256=sha, status="rejected",
                encoding=result["encoding"].get("encoding"),
                error_count=len(prepared["contract"]["errors"]),
                row_count=prepared["data_row_count"],
                detail=f"表头契约未通过：{prepared['detail']}",
                errors=prepared.get("header_errors") or [],
            )
            result.update({
                "status": "rejected",
                "stage": "contract",
                "detail": prepared["detail"],
                "attempt_id": attempt_id,
                "report": prepared["report"],
                "rows_before": store.count_rows(conn),
                "rows_after": store.count_rows(conn),
            })
            return result

        if prepared["status"] == "rejected" and prepared["stage"] == "rows":
            validation = prepared["validation"]
            result["row_validation"] = prepared["row_validation"]
            result["report"] = prepared["report"]
            attempt_id = store.record_attempt(
                conn, filename=filename, sha256=sha, status="rejected",
                encoding=result["encoding"].get("encoding"),
                error_count=validation.error_count,
                row_count=prepared["data_row_count"],
                detail=prepared["detail"],
                errors=validation.error_dicts(),
            )
            result.update({
                "status": "rejected",
                "stage": "rows",
                "detail": prepared["detail"],
                "attempt_id": attempt_id,
                "rows_before": store.count_rows(conn),
                "rows_after": store.count_rows(conn),
            })
            return result

        result["row_validation"] = prepared["row_validation"]
        result["report"] = prepared["report"]
        result["aggregate"] = prepared["summary"]

        # 5) 事务导入（含重复文件判定）
        outcome = importer.import_batch(
            conn, filename=filename, sha256=sha, rows=prepared["rows"],
            aggregates=prepared["summary"], encoding=result["encoding"].get("encoding") or "",
            bom=bool(result["encoding"].get("bom")),
            force_new_batch=force_new_batch, fail_after=fail_after, batch_id=batch_id,
        )
        result["import"] = outcome
        result["status"] = outcome["status"]
        result["batch_id"] = outcome.get("batch_id")
        result["rows_before"] = outcome.get("rows_before")
        result["rows_after"] = outcome.get("rows_after")
        result["written"] = outcome.get("written", 0)
        if outcome["status"] in ("duplicate_file", "rolled_back"):
            result["detail"] = outcome.get("duplicate_hint") or outcome.get("detail", "")
        if outcome.get("duplicate_hint"):
            result["duplicate_hint"] = outcome["duplicate_hint"]
        return result
    finally:
        if own_conn:
            conn.close()
