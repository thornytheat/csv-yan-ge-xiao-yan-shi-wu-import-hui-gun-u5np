"""模块 2 · 表头契约。

规则：列顺序无关；表头去首尾空白；必需列缺失=错误；额外列按 reject|warn 策略；
重复列名=错误；额外列在 warn 策略下被忽略但不影响入库。
"""
from __future__ import annotations

SITES = ["beijing", "shanghai", "guangzhou", "shenzhen", "chengdu", "hangzhou"]
CATEGORIES = ["electronics", "grocery", "apparel"]

REQUIRED = ["site", "category", "order_id", "order_date", "amount", "qty"]

COLUMN_SPECS: dict[str, dict] = {
    "site": {"required": True, "type": "enum", "enum": SITES, "max_length": 32, "label": "站点"},
    "category": {"required": True, "type": "enum", "enum": CATEGORIES, "max_length": 32, "label": "品类"},
    "order_id": {"required": True, "type": "int", "max_length": 32, "label": "订单号"},
    "order_date": {"required": True, "type": "date", "max_length": 10,
                   "format": "YYYY-MM-DD", "label": "下单日期"},
    "amount": {"required": True, "type": "decimal", "max_length": 16, "scale": 2,
               "min": "0.01", "label": "金额"},
    "qty": {"required": True, "type": "int", "max_length": 8, "min": 1, "max": 100000,
            "label": "件数"},
    "memo": {"required": False, "type": "string", "max_length": 60, "label": "备注"},
}

POLICIES = ("reject", "warn")


def normalize_header(cells: list[str]) -> list[str]:
    """去掉首尾空白（含 BOM 已剥离后的 \\ufeff、全角空格）。"""
    out: list[str] = []
    for cell in cells:
        cleaned = (cell or "").replace("\ufeff", "").strip()
        out.append(cleaned)
    return out


def check(header_cells: list[str], extra_policy: str = "reject") -> dict:
    """返回契约检查结论；ok=True 才允许进入行校验。"""
    if extra_policy not in POLICIES:
        extra_policy = "reject"

    header = normalize_header(header_cells)
    known = set(COLUMN_SPECS)

    missing = [c for c in REQUIRED if c not in header]
    extra = [c for c in header if c not in known]
    duplicate = sorted({c for c in header if c and header.count(c) > 1}, key=header.index)
    blank = [i + 1 for i, c in enumerate(header) if not c]

    errors: list[dict] = []
    warnings: list[dict] = []

    if not header:
        errors.append({"code": "empty_header", "message": "表头为空：第 1 行没有读到任何列名。"})
    if blank:
        errors.append({
            "code": "blank_column_name",
            "message": f"表头第 {', '.join(map(str, blank))} 个列名为空，无法建索引。",
            "columns": [str(i) for i in blank],
        })
    if duplicate:
        errors.append({
            "code": "duplicate_column",
            "message": f"表头存在重复列：{', '.join(duplicate)}（同名列无法确定取值列）。",
            "columns": duplicate,
        })
    if missing:
        errors.append({
            "code": "missing_column",
            "message": f"缺少必需列：{', '.join(missing)}。契约要求：{', '.join(REQUIRED)}。",
            "columns": missing,
        })
    if extra:
        if extra_policy == "reject":
            errors.append({
                "code": "extra_column",
                "message": f"出现契约外列：{', '.join(extra)}（当前策略=reject，整份文件拒绝）。",
                "columns": extra,
            })
        else:
            warnings.append({
                "code": "extra_column_ignored",
                "message": f"契约外列 {', '.join(extra)} 已按 warn 策略忽略，不参与校验与入库。",
                "columns": extra,
            })

    usable = [c for c in header if c in known and c not in duplicate]
    reordered = [c for c in REQUIRED if c in usable] != [c for c in usable if c in REQUIRED]

    return {
        "ok": not errors,
        "header": header,
        "missing": missing,
        "extra": extra,
        "duplicate": duplicate,
        "blank_index": blank,
        "extra_policy": extra_policy,
        "errors": errors,
        "warnings": warnings,
        "columns_used": usable,
        "column_contract": [
            {"name": name, "required": spec["required"], "type": spec["type"],
             "label": spec["label"], "max_length": spec.get("max_length"),
             "enum": spec.get("enum")}
            for name, spec in COLUMN_SPECS.items()
        ],
    }


def index_of(header: list[str], columns_used: list[str]) -> dict[str, int]:
    """列顺序无关：按列名建索引，只索引契约内且未被判重复的列。"""
    return {name: header.index(name) for name in columns_used}
