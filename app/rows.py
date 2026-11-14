"""模块 3 · 行级校验（纯函数）。

每行按「必填 → 长度 → 类型/枚举 → 业务规则」顺序校验，同一单元格只报第一个错，
保证 broken.csv 的 5 类错误各报 1 条、不会被重复计数。

主键规则：strictly_increasing，逐行与「上一条主键可解析的行」比较：
- 相等 → duplicate
- 更小 → out_of_order
- 更大 → 通过并更新基线
错误同时给行号：row_no（数据行，1 起）与 line_no（文件行，含表头）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from . import contract

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
INT_RE = re.compile(r"^[+-]?\d+$")
DEC_RE = re.compile(r"^\d{1,10}(\.\d{1,6})?$")   # 小数位上限由 decimal_scale 单独判

CODE_LABELS = {
    "required": "必填为空",
    "length": "超出长度上限",
    "int_type": "整数字段格式错误",
    "int_range": "整数超出范围",
    "date_format": "日期格式错误",
    "date_invalid": "日期不存在",
    "decimal_type": "金额格式错误",
    "decimal_scale": "金额小数位超限",
    "decimal_range": "金额超出允许范围",
    "enum": "枚举值越界",
    "duplicate": "主键重复",
    "out_of_order": "主键倒序",
    "column_count": "列数与表头不一致",
    "empty_data": "没有数据行",
}


@dataclass
class RowError:
    row_no: int
    line_no: int
    column: str
    code: str
    raw: str
    reason: str

    @property
    def label(self) -> str:
        return CODE_LABELS.get(self.code, self.code)

    def as_dict(self) -> dict:
        return {
            "row_no": self.row_no,
            "line_no": self.line_no,
            "column": self.column,
            "code": self.code,
            "label": self.label,
            "raw": self.raw,
            "reason": self.reason,
        }


@dataclass
class RowValidation:
    ok: bool = True
    row_count: int = 0
    errors: list[RowError] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return len(self.errors)

    def error_dicts(self) -> list[dict]:
        return [e.as_dict() for e in self.errors]

    def codes(self) -> list[str]:
        return [e.code for e in self.errors]


def _cell(record: list[str], idx: int) -> str:
    if 0 <= idx < len(record):
        return (record[idx] or "").strip()
    return ""


def validate(records: list[list[str]], header: list[str], columns_used: list[str]) -> RowValidation:
    """records = csv.reader 的数据行（不含表头）；header 为已归一化的表头。"""
    header_len = len(header)
    index = contract.index_of(header, columns_used)
    result = RowValidation(row_count=len(records))
    if not records:
        result.ok = False
        result.errors.append(RowError(0, 1, "", "empty_data", "", "文件只有表头，没有数据行。"))
        return result

    prev_id: int | None = None

    for row_no, record in enumerate(records, start=1):
        line_no = row_no + 1
        per_cell: dict[str, RowError] = {}

        def add(column: str, code: str, raw: str, reason: str) -> None:
            if column in per_cell:
                return
            per_cell[column] = RowError(row_no, line_no, column, code, raw, reason)

        if len(record) != header_len:
            add("", "column_count", str(len(record)),
                f"本行有 {len(record)} 列，表头有 {header_len} 列；请检查是否漏了逗号或字段里含未转义的逗号。")
            result.errors.append(per_cell[""])
            continue

        values: dict[str, str] = {c: _cell(record, index[c]) for c in columns_used}

        # 1) 必填
        for name in contract.REQUIRED:
            if name not in values:
                continue
            if not values[name]:
                add(name, "required", "", f"{contract.COLUMN_SPECS[name]['label']}为空，契约要求必填。")

        # 2) 长度（先于类型，避免超长值又被报成类型错）
        for name, raw in values.items():
            if name in per_cell or not raw:
                continue
            limit = contract.COLUMN_SPECS[name].get("max_length")
            if limit and len(raw) > limit:
                add(name, "length", raw,
                    f"长度 {len(raw)} 超过上限 {limit}（按字符计，含空格已去首尾）。")

        # 3) 类型 / 枚举 / 业务规则
        parsed: dict[str, object] = {}

        for name in ("order_id", "qty"):
            if name not in values or name in per_cell or not values[name]:
                continue
            raw = values[name]
            if not INT_RE.match(raw):
                add(name, "int_type", raw, f"「{raw}」不是整数（只允许可选正负号 + 数字）。")
                continue
            number = int(raw)
            if name == "qty":
                if number < 1 or number > 100000:
                    add(name, "int_range", raw, f"件数需在 1~100000，当前 {number}。")
                    continue
            parsed[name] = number

        if "order_date" in values and "order_date" not in per_cell and values["order_date"]:
            raw = values["order_date"]
            if not DATE_RE.match(raw):
                add("order_date", "date_format", raw, f"日期需为 YYYY-MM-DD，「{raw}」不符合。")
            else:
                try:
                    parsed["order_date"] = date.fromisoformat(raw)
                except ValueError as exc:
                    add("order_date", "date_invalid", raw,
                        f"「{raw}」不是真实存在的日期（{exc}），例如 2 月没有 30 日。")

        if "amount" in values and "amount" not in per_cell and values["amount"]:
            raw = values["amount"]
            if not DEC_RE.match(raw):
                add("amount", "decimal_type", raw,
                    "金额需为纯数字（如 128.50），不允许千分位、货币符号、空格。")
            else:
                try:
                    amount = Decimal(raw)
                except InvalidOperation:
                    add("amount", "decimal_type", raw, f"「{raw}」无法解析为金额。")
                else:
                    exponent = -amount.as_tuple().exponent  # type: ignore[arg-type]
                    if exponent > 2:
                        add("amount", "decimal_scale", raw,
                            f"小数位 {exponent} 位，超过 2 位；请先在上游四舍五入到分。")
                    elif amount <= 0:
                        add("amount", "decimal_range", raw, f"金额需大于 0，当前 {raw}。")
                    elif amount > Decimal("99999999.99"):
                        add("amount", "decimal_range", raw,
                            f"金额 {raw} 超过上限 99999999.99，疑似单位错误。")
                    else:
                        parsed["amount_cents"] = int(amount * 100)

        for name in ("site", "category"):
            if name not in values or name in per_cell or not values[name]:
                continue
            allowed = contract.COLUMN_SPECS[name]["enum"]
            if values[name] not in allowed:
                add(name, "enum", values[name],
                    f"「{values[name]}」不在允许值内：{', '.join(allowed)}。")

        # 4) 主键严格递增（逐行与上一条可解析主键比较）
        if "order_id" not in per_cell and "order_id" in parsed:
            oid = int(parsed["order_id"])  # type: ignore[arg-type]
            raw = values["order_id"]
            if prev_id is not None and oid == prev_id:
                add("order_id", "duplicate", raw,
                    f"主键 {raw} 与上一行相同；契约要求 order_id 严格递增。")
            elif prev_id is not None and oid < prev_id:
                add("order_id", "out_of_order", raw,
                    f"主键 {raw} 小于上一行 {prev_id}；契约要求严格递增（倒序会被判为脏数据）。")
            else:
                prev_id = oid

        for err in per_cell.values():
            result.errors.append(err)

        if not per_cell:
            result.rows.append({
                "row_no": row_no,
                "line_no": line_no,
                "order_id": int(parsed["order_id"]),  # type: ignore[arg-type]
                "site": values.get("site", ""),
                "category": values.get("category", ""),
                "order_date": parsed["order_date"].isoformat(),  # type: ignore[union-attr]
                "amount_cents": int(parsed["amount_cents"]),  # type: ignore[arg-type]
                "amount_raw": values.get("amount", ""),
                "qty": int(parsed["qty"]),  # type: ignore[arg-type]
                "memo": values.get("memo", ""),
            })

    result.ok = not result.errors
    result.rows.sort(key=lambda r: r["row_no"])
    return result
