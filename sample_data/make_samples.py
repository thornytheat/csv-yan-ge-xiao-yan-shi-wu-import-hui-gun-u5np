#!/usr/bin/env python3
"""生成 csvgate 演示样例（可重复执行，输出字节稳定）。

- normal_500.csv : UTF-8 无 BOM，500 行，order_id 严格递增
- gbk_120.csv    : GB18030(GBK 超集) 编码，120 行，memo 列含中文备注
- seed_20261107.csv : 历史周批次，60 行（启动时幂等 seed 用）
- broken.csv     : 5 类错误各 1 条，其余行合法
- contract.json  : 列契约
- day_20261108_<site>.csv      : 按日发布演示，6 个站点各一份，构成完整的 2026-11-08
- pending_20261109_<site>.csv  : 2026-11-09 的 6 个站点；seed 只导入 2 站，演示「未补齐不可见 / 补齐后发布」
- resend_20261108_beijing.csv  : 2026-11-08 北京站的单站重发版本（金额整体上调），演示只替换该站

CSV 一律 \n 换行、无引号包裹（字段不含逗号/引号），行号与文件行可直接对应。
"""
from __future__ import annotations

import json
import random
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent

HEADER = ["site", "category", "order_id", "order_date", "amount", "qty"]
SITES = ["beijing", "shanghai", "guangzhou", "shenzhen", "chengdu", "hangzhou"]
CATEGORIES = ["electronics", "grocery", "apparel"]

CONTRACT = {
    "contract_version": "2026.11",
    "dataset": "daily_site_orders",
    "delimiter": ",",
    "encoding": ["utf-8-sig", "utf-8", "gb18030"],
    "primary_key": "order_id",
    "primary_key_rule": "strictly_increasing",
    "extra_column_policy": "reject",
    "columns": [
        {"name": "site", "required": True, "type": "enum", "enum": SITES, "max_length": 32,
         "label": "站点"},
        {"name": "category", "required": True, "type": "enum", "enum": CATEGORIES, "max_length": 32,
         "label": "品类"},
        {"name": "order_id", "required": True, "type": "int", "max_length": 32,
         "label": "订单号（主键，需严格递增）"},
        {"name": "order_date", "required": True, "type": "date", "max_length": 10,
         "format": "YYYY-MM-DD", "label": "下单日期"},
        {"name": "amount", "required": True, "type": "decimal", "max_length": 16,
         "scale": 2, "min": "0.01", "label": "金额"},
        {"name": "qty", "required": True, "type": "int", "max_length": 8,
         "min": 1, "max": 100000, "label": "件数"},
        {"name": "memo", "required": False, "type": "string", "max_length": 60,
         "label": "备注（可选）"},
    ],
}


def _amount(i: int) -> str:
    cents = 1200 + (i * 137) % 880000
    return f"{cents // 100}.{cents % 100:02d}"


def _qty(i: int) -> int:
    return 1 + (i * 17) % 9


def rows(count: int, start_id: int, day0: date, days: int, memo: bool) -> list[list[str]]:
    rng = random.Random(20261114)
    out = []
    for i in range(count):
        day = day0 + timedelta(days=(i * days // max(count, 1)) % days)
        row = [
            SITES[i % len(SITES)],
            CATEGORIES[(i * 7) % len(CATEGORIES)],
            str(start_id + i),
            day.isoformat(),
            _amount(i + rng.randint(0, 3) * 0),
            str(_qty(i)),
        ]
        if memo:
            idx = 1 + (i * 5) % 4
            row.append(f"华东仓第{idx}批补货")
        out.append(row)
    return out


def write_csv(path: Path, header: list[str], data: list[list[str]], encoding: str) -> None:
    text = ",".join(header) + "\n" + "".join(",".join(r) + "\n" for r in data)
    path.write_bytes(text.encode(encoding))


def day_rows(site: str, day: date, count: int, start_id: int,
             amount_bonus: int = 0) -> list[list[str]]:
    """按日发布演示：同一站、同一天、主键严格递增。"""
    out = []
    for i in range(count):
        cents = 1500 + (i * 173 + start_id) % 500000 + amount_bonus
        out.append([
            site,
            CATEGORIES[(i + start_id) % len(CATEGORIES)],
            str(start_id + i),
            day.isoformat(),
            f"{cents // 100}.{cents % 100:02d}",
            str(1 + (i * 11) % 7),
        ])
    return out


def broken_rows() -> list[list[str]]:
    """5 类错误各 1 条，其余行合法；错误行之间不互相传染主键基线。"""
    base = 900001
    good = lambda n, oid, **kw: [  # noqa: E731
        kw.get("site", "beijing"),
        kw.get("category", "grocery"),
        str(oid),
        kw.get("order_date", "2026-11-05"),
        kw.get("amount", "128.50"),
        kw.get("qty", "3"),
    ]
    return [
        good(1, base),
        good(2, base + 1),
        # 1) 日期不真实
        good(3, base + 2, order_date="2026-02-30", site="shanghai"),
        good(4, base + 3),
        # 2) site 超长（>32）
        good(5, base + 4, site="beijing-haidian-west-zone-warehouse-0001"),
        # 3) 枚举越界
        good(6, base + 5, category="furniture"),
        # 4) 主键重复（与上一行相同）
        good(7, base + 5, site="guangzhou"),
        good(8, base + 6),
        # 5) 主键倒序
        good(9, base + 2, site="shenzhen"),
        good(10, base + 7),
        good(11, base + 8, category="apparel"),
        good(12, base + 9, amount="999.99", qty="7"),
    ]


def main() -> None:
    write_csv(HERE / "normal_500.csv", HEADER,
              rows(500, 100001, date(2026, 11, 1), 13, memo=False), "utf-8")
    write_csv(HERE / "gbk_120.csv", HEADER + ["memo"],
              rows(120, 200001, date(2026, 11, 2), 12, memo=True), "gb18030")
    write_csv(HERE / "seed_20261107.csv", HEADER,
              rows(60, 700001, date(2026, 10, 26), 7, memo=False), "utf-8")
    write_csv(HERE / "broken.csv", HEADER, broken_rows(), "utf-8")

    # 按日发布演示：完整的一天（6 站各 40 行）与待补齐的一天（6 站各 25 行，
    # seed 只导入其中北京/上海两站，其余 4 站留给演示「补齐后发布」）
    for idx, site in enumerate(SITES):
        write_csv(HERE / f"day_20261108_{site}.csv", HEADER,
                  day_rows(site, date(2026, 11, 8), 40, 300001 + idx * 1000), "utf-8")
        write_csv(HERE / f"pending_20261109_{site}.csv", HEADER,
                  day_rows(site, date(2026, 11, 9), 25, 400001 + idx * 1000), "utf-8")
    # 单站重发演示：同一天、同一站，金额整体变化
    write_csv(HERE / "resend_20261108_beijing.csv", HEADER,
              day_rows("beijing", date(2026, 11, 8), 40, 300001, amount_bonus=500000), "utf-8")

    (HERE / "contract.json").write_text(
        json.dumps(CONTRACT, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    for path in sorted(HERE.glob("*.csv")):
        blob = path.read_bytes()
        print(f"{path.name}: {len(blob)} bytes, {blob.count(chr(10).encode()) - 1} data rows")


if __name__ == "__main__":
    main()
