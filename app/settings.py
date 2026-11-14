"""设置与路径。"""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "csvgate · CSV 严格校验与事务导入"
APP_SHORT = "csvgate"
BASE_DIR = Path(__file__).resolve().parent.parent
SAMPLE_DIR = BASE_DIR / "sample_data"
DATA_DIR = Path(os.environ.get("CSVGATE_DATA_DIR", str(BASE_DIR / "data")))
DB_PATH = DATA_DIR / "csvgate.sqlite3"

SEED_VERSION = "seed-2026-11-07.1"
SEED_BATCH_ID = "B-20261107-001"
SEED_FILENAME = "seed_20261107.csv"

# 按日发布单元的历史演示数据：完整的一天 + 只到 2 个站点的一天
DAY_SEED_VERSION = "day-seed-2026-11-08.1"
DAY_SEED_DATE = "2026-11-08"
DAY_SEED_FILES = [f"day_20261108_{site}.csv" for site in
                  ["beijing", "shanghai", "guangzhou", "shenzhen", "chengdu", "hangzhou"]]
PENDING_SEED_DATE = "2026-11-09"
PENDING_SEED_FILES = [f"pending_20261109_{site}.csv" for site in ["beijing", "shanghai"]]


SAMPLE_FILES = [
    {"name": "normal_500.csv", "title": "正常 500 行（UTF-8）",
     "desc": "主键严格递增、6 个站点，应整体入库成功"},
    {"name": "gbk_120.csv", "title": "GBK 120 行",
     "desc": "GB18030 编码 + 中文备注列，验证编码探测与解码"},
    {"name": "broken.csv", "title": "5 类错误样例",
     "desc": "重复主键/倒序/非法日期/超长/枚举越界各 1 条，应整体回滚"},
    {"name": "contract.json", "title": "列契约 contract.json",
     "desc": "列定义、类型、枚举与主键规则"},
]

# 按日发布演示文件：完整一天的 6 个站点 + 待补齐一天的 6 个站点 + 单站重发样本
DAY_SAMPLE_FILES = [
    {"name": f"day_20261108_{site}.csv", "site": site,
     "title": f"2026-11-08 · {site}", "desc": "完整发布单元中的一站（6 站齐才切换）"}
    for site in ["beijing", "shanghai", "guangzhou", "shenzhen", "chengdu", "hangzhou"]
] + [
    {"name": f"pending_20261109_{site}.csv", "site": site,
     "title": f"2026-11-09 · {site}", "desc": "该日只 seed 了 2 站；补齐其余站点才会发布"}
    for site in ["beijing", "shanghai", "guangzhou", "shenzhen", "chengdu", "hangzhou"]
] + [
    {"name": "resend_20261108_beijing.csv", "site": "beijing",
     "title": "2026-11-08 · beijing 重发", "desc": "单站重发：只替换北京，生成新版本"},
]

ALL_SAMPLE_FILES = SAMPLE_FILES + DAY_SAMPLE_FILES

EXTRA_POLICIES = [
    {"value": "reject", "label": "reject（默认）：出现契约外列即拒绝整份文件"},
    {"value": "warn", "label": "warn：契约外列忽略并告警，其余照常校验"},
]
