"""行级校验：严格递增、日期真实性、长度、枚举、金额、必填、列数。"""
from __future__ import annotations

from app import contract, rows as rows_mod

HEADER = ["site", "category", "order_id", "order_date", "amount", "qty"]
USED = list(HEADER)


def validate(data: list[list[str]]) -> rows_mod.RowValidation:
    return rows_mod.validate(data, HEADER, USED)


def codes(result: rows_mod.RowValidation) -> list[str]:
    return result.codes()


def test_valid_rows_pass_and_are_parsed():
    result = validate([
        ["beijing", "grocery", "1001", "2026-11-05", "128.50", "3"],
        ["shanghai", "apparel", "1002", "2026-02-28", "0.99", "1"],
    ])
    assert result.ok is True
    assert result.error_count == 0
    assert [r["order_id"] for r in result.rows] == [1001, 1002]
    assert result.rows[0]["amount_cents"] == 12850
    assert result.rows[0]["line_no"] == 2            # 文件行号（表头是第 1 行）


def test_duplicate_primary_key():
    result = validate([
        ["beijing", "grocery", "1001", "2026-11-05", "10.00", "1"],
        ["beijing", "grocery", "1001", "2026-11-06", "10.00", "1"],
    ])
    assert result.ok is False
    assert codes(result) == ["duplicate"]
    assert result.errors[0].column == "order_id"
    assert result.errors[0].line_no == 3
    assert "严格递增" in result.errors[0].reason


def test_out_of_order_primary_key():
    result = validate([
        ["beijing", "grocery", "1005", "2026-11-05", "10.00", "1"],
        ["beijing", "grocery", "1003", "2026-11-06", "10.00", "1"],
    ])
    assert codes(result) == ["out_of_order"]
    assert result.errors[0].raw == "1003"


def test_unreal_date_2026_02_30_is_invalid():
    result = validate([["beijing", "grocery", "1001", "2026-02-30", "10.00", "1"]])
    assert codes(result) == ["date_invalid"]
    assert result.errors[0].raw == "2026-02-30"
    assert "2 月没有 30 日" in result.errors[0].reason


def test_date_format_slash_is_rejected():
    result = validate([["beijing", "grocery", "1001", "2026/11/05", "10.00", "1"]])
    assert codes(result) == ["date_format"]


def test_site_over_length_limit():
    too_long = "beijing-haidian-west-zone-warehouse-0001"
    assert len(too_long) > contract.COLUMN_SPECS["site"]["max_length"]
    result = validate([[too_long, "grocery", "1001", "2026-11-05", "10.00", "1"]])
    assert codes(result) == ["length"]          # 长度先于枚举，只报一条
    assert result.errors[0].column == "site"
    assert "超过上限" in result.errors[0].reason


def test_enum_out_of_range_lists_allowed_values():
    result = validate([["beijing", "furniture", "1001", "2026-11-05", "10.00", "1"]])
    assert codes(result) == ["enum"]
    assert "electronics" in result.errors[0].reason


def test_amount_scale_type_and_range():
    three_dp = validate([["beijing", "grocery", "1001", "2026-11-05", "10.005", "1"]])
    assert codes(three_dp) == ["decimal_scale"]

    with_comma = validate([["beijing", "grocery", "1001", "2026-11-05", "1,000.00", "1"]])
    assert codes(with_comma) == ["decimal_type"]

    zero = validate([["beijing", "grocery", "1001", "2026-11-05", "0.00", "1"]])
    assert codes(zero) == ["decimal_range"]


def test_required_order_id_and_qty_range():
    missing = validate([["beijing", "grocery", "", "2026-11-05", "10.00", "1"]])
    assert codes(missing) == ["required"]

    bad_qty = validate([["beijing", "grocery", "1001", "2026-11-05", "10.00", "0"]])
    assert codes(bad_qty) == ["int_range"]

    non_int = validate([["beijing", "grocery", "1001", "2026-11-05", "10.00", "1.0"]])
    assert codes(non_int) == ["int_type"]


def test_column_count_mismatch():
    result = validate([["beijing", "grocery", "1001", "2026-11-05", "10.00"]])
    assert codes(result) == ["column_count"]
    assert result.errors[0].line_no == 2


def test_empty_data_is_reported():
    result = validate([])
    assert result.ok is False
    assert codes(result) == ["empty_data"]


def test_repo_broken_sample_has_exactly_five_row_errors():
    from app import encoding

    from app import settings
    raw = (settings.SAMPLE_DIR / "broken.csv").read_bytes()
    text = encoding.probe(raw).text
    data = [ln.split(",") for ln in text.strip().splitlines()[1:]]
    result = validate(data)
    assert result.ok is False
    assert result.error_count == 5
    assert sorted(codes(result)) == sorted([
        "date_invalid", "length", "enum", "duplicate", "out_of_order",
    ])
    assert [e.line_no for e in result.errors] == [4, 6, 7, 8, 10]
