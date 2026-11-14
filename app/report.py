"""模块 6 · 错误报告：把行级错误整理成值班同学可直接照着改的清单。"""
from __future__ import annotations

CODE_ADVICE = {
    "required": "回上游补全该字段；空字符串也视为缺失。",
    "length": "截断或与上游确认口径（长度按去掉首尾空白后的字符数计）。",
    "int_type": "确认导出未把整数写成 1,000 或 1.0 这类格式。",
    "int_range": "确认件数单位；单行件数上限 100000。",
    "date_format": "统一为 YYYY-MM-DD，不要 2026/11/5 或 20261105。",
    "date_invalid": "上游日期算错（如月末 +1 天）；需要业务侧确认真实日期后重导。",
    "decimal_type": "金额不要带货币符号、千分位；用 128.50 这样的纯数字。",
    "decimal_scale": "在上游先四舍五入到分，再导出。",
    "decimal_range": "确认金额是否被换算成「分」或写错数量级。",
    "enum": "按契约枚举值重导；枚举定义见工作台「列契约」区块。",
    "duplicate": "主键重复通常是同一批数据被分片重发，需要上游去重或改用增量批次。",
    "out_of_order": "文件行序被改动（排序/合并）；请按主键升序重新导出。",
    "column_count": "检查该行是否多/少逗号，或字段内含未加引号的逗号。",
    "empty_data": "文件只有表头；确认导出范围。",
}


def build_report(errors: list[dict]) -> dict:
    """errors 为 RowError.as_dict() 列表。"""
    ordered = sorted(errors, key=lambda e: (e["line_no"], e["column"]))
    by_code: dict[str, dict] = {}
    for err in ordered:
        bucket = by_code.setdefault(err["code"], {
            "code": err["code"],
            "label": err["label"],
            "count": 0,
            "advice": CODE_ADVICE.get(err["code"], "按错误原因修正后重导。"),
            "examples": [],
        })
        bucket["count"] += 1
        if len(bucket["examples"]) < 3:
            bucket["examples"].append(err["line_no"])
    by_column: dict[str, int] = {}
    for err in ordered:
        if err["column"]:
            by_column[err["column"]] = by_column.get(err["column"], 0) + 1

    return {
        "total": len(ordered),
        "rows": ordered,
        "by_code": sorted(by_code.values(), key=lambda b: (-b["count"], b["code"])),
        "by_column": sorted(
            ({"column": col, "count": cnt} for col, cnt in by_column.items()),
            key=lambda b: (-b["count"], b["column"]),
        ),
        "first_line": ordered[0]["line_no"] if ordered else None,
        "advice": "整份文件不回滚；按上面清单修正后重新上传同一批次即可，库内数据未发生变化。",
    }
