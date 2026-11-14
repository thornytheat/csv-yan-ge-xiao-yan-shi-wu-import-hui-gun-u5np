"""模块 4 · 分组聚合（按 site 汇总，并用独立口径自证）。

一致性自证用三条互不依赖的路径：
A. 分组累加：每个 site 一组，组内累加 cents
B. 单趟线性累加：遍历全部明细，不分组，得到一个总额
C. Decimal 复核：用原始金额字符串做 Decimal 求和，再 ×100 取整
另外核对「组内行数之和 == 明细行数」。任一不符 → consistent=False。
"""
from __future__ import annotations

from decimal import Decimal


def summarize(rows: list[dict]) -> dict:
    groups: dict[str, dict] = {}
    linear_cents = 0
    linear_qty = 0

    for row in rows:
        site = row["site"]
        bucket = groups.setdefault(site, {
            "site": site, "amount_cents": 0, "qty": 0, "row_count": 0,
        })
        bucket["amount_cents"] += int(row["amount_cents"])
        bucket["qty"] += int(row["qty"])
        bucket["row_count"] += 1

        linear_cents += int(row["amount_cents"])
        linear_qty += int(row["qty"])

    group_cents = sum(b["amount_cents"] for b in groups.values())
    group_qty = sum(b["qty"] for b in groups.values())
    group_rows = sum(b["row_count"] for b in groups.values())

    decimal_cents = 0
    decimal_error = ""
    try:
        total = sum((Decimal(row["amount_raw"]) for row in rows), Decimal("0"))
        decimal_cents = int(total * 100)
    except Exception as exc:  # pragma: no cover - 防御性
        decimal_error = str(exc)

    checks = [
        {
            "name": "组内金额和 == 明细线性累加",
            "ok": group_cents == linear_cents,
            "detail": f"分组 {group_cents} 分 vs 明细 {linear_cents} 分",
        },
        {
            "name": "组内行数之和 == 明细行数",
            "ok": group_rows == len(rows),
            "detail": f"分组 {group_rows} 行 vs 明细 {len(rows)} 行",
        },
        {
            "name": "组内件数之和 == 明细件数累加",
            "ok": group_qty == linear_qty,
            "detail": f"分组 {group_qty} 件 vs 明细 {linear_qty} 件",
        },
        {
            "name": "Decimal 原始字符串复核一致",
            "ok": (not decimal_error) and decimal_cents == linear_cents,
            "detail": (f"Decimal 复核 {decimal_cents} 分 vs 明细 {linear_cents} 分"
                       if not decimal_error else f"Decimal 复核异常：{decimal_error}"),
        },
    ]

    ordered = sorted(groups.values(), key=lambda b: (-b["amount_cents"], b["site"]))
    for bucket in ordered:
        bucket["amount"] = f"{bucket['amount_cents'] // 100}.{bucket['amount_cents'] % 100:02d}"
        bucket["avg_amount"] = (
            f"{bucket['amount_cents'] / bucket['row_count'] / 100:.2f}"
            if bucket["row_count"] else "0.00"
        )

    total = linear_cents
    return {
        "groups": ordered,
        "total_amount_cents": total,
        "total_amount": f"{total // 100}.{total % 100:02d}",
        "total_qty": linear_qty,
        "row_count": len(rows),
        "checks": checks,
        "consistent": all(c["ok"] for c in checks),
    }
