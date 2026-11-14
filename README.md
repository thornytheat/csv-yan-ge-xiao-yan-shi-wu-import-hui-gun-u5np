# csvgate · CSV 严格校验与按日发布单元

上游按站点分批给当天的订单 CSV，要求「**全对才入库，错了要知道第几行错哪列**」，
并且**一天内各站文件共同构成一个发布单元**：站点齐全且全部校验通过后，才一次性切换
当日订单明细与站点汇总；单站重发只替换该站，旧版仍可查；失败重试保留上一版，不丢数据。
本工具把这份人工核对变成可复核的后端模块。

使用者：数据平台组入库值班同学。

## 能力

| 模块 | 落地位置 | 做什么 |
|---|---|---|
| 1 上传与编码探测 | `app/encoding.py` | BOM → UTF-8-SIG；严格 UTF-8 失败 → GB18030；都失败 → 400 + 建议。响应记录是否 BOM、前 16 字节摘要、逐次尝试 |
| 2 表头契约 | `app/contract.py` | 必需列缺失=错误；额外列 `reject`/`warn`；列顺序无关；表头去首尾空白；重复列名=错误 |
| 3 行级校验 | `app/rows.py` | 主键严格递增（重复 `duplicate` / 倒序 `out_of_order`，逐行与上一行比较）、int/date/decimal/enum、必填、长度上限、日期真实性 |
| 4 分组聚合 | `app/aggregate.py` | 按 `site` 求和，并用三条独立口径自证一致 |
| 5 按日发布单元 | `app/dayrelease.py` | **单站单日文件 → 不可变快照 → 站点齐全后单事务原子发布**；版本指针、单站替换、失败保留上一版、重试不新增版本 |
| 5' 旧口径事务导入 | `app/importer.py` | 兼容路径：单份文件 = 一个批次；单事务写 批次+明细+聚合；异常即 ROLLBACK；sha256 重复 → `duplicate_file` |
| 6 错误报告 | `app/report.py` | 按错误类型汇总 + 逐行依据（行号/列名/原值/原因）+ 修正建议 |

编排在 `app/pipeline.py`（`prepare` 纯校验供按日发布复用，`run` 旧口径导入），
按日发布语义在 `app/dayrelease.py`，路由 `app/main.py` 只做参数解析与渲染。

## 核心规则

0. **按日发布单元（当前主流程）**
   - 一份文件必须只含**一个站点、一天**的数据（按 `order_date` 与 `site` 推导发布键）；
     混合站点或混合日期 → `rejected`，不写任何快照。
   - 校验通过的文件先写成 `(business_date, site)` 的**不可变快照**（`site_uploads` +
     `site_upload_rows`），此时**不影响当前结果**。
   - 所需站点（契约 6 站）全部到齐后，在同一事务里写 `day_releases` +
     `day_release_sites` + `day_release_aggregates`，并切换 `current_days` 指针；
     **订单明细与站点汇总同属一个版本**，不会出现汇总先变、明细没变的中间态。
   - 单站重发：新版本只引用该站的新快照，其余站点沿用上一版快照；旧版本永久保留，
     可按版本号回查（`/days/{date}/v/{n}`、`/api/days/{date}/versions/{n}`）。
   - 失败重试：校验失败 / 快照写入失败 / 发布失败都 **ROLLBACK 且不动 `current_days`**，
     上一版继续生效；已经写入的旧快照不会被删除，不出现数据丢失。
   - 幂等重试：同 `(日期, 站点)` 的最新快照 sha256 与本次相同 → 不新增快照、不新增版本
     （`unchanged`）；站点未齐时重试同样不重复落快照。
   - 未补齐日期：只出现在「待补齐」列表（`/api/days` 的 `pending`），**不进入任何日汇总**；
     `current_days` 只包含站点齐全的日期。

1. **编码探测**（证据可查）
   - 前 3 字节是 `EF BB BF` → 按 `utf-8-sig` 解码，BOM 剥离；
   - 否则严格 UTF-8；失败再试 `gb18030`（GBK 超集）；
   - 都失败 → HTTP 400，附三次尝试的失败字节偏移与修复建议。
   - 判定是**确定性**的：纯 ASCII 文件一律算 `utf-8`。
2. **行级错误只报第一处**：同一行同一列按「必填 → 长度 → 类型/枚举 → 业务」取第一个失败原因。
3. **主键严格递增**：与「上一条主键可解析的行」比较；相等 → `duplicate`，更小 → `out_of_order`。
4. **日期真实性**：先匹配 `YYYY-MM-DD`，再用 `date.fromisoformat` 判真实存在（`2026-02-30` → `date_invalid`）。
5. **聚合自证**：分组累加 cents、明细单趟线性累加、`Decimal` 原始字符串复核、组内行数和=明细行数，
   四条必须同时成立；按日发布在快照落库后还会用明细重算一遍再允许发布。
6. **旧口径事务与重复文件（兼容路径）**
   - 全对：`BEGIN IMMEDIATE` 写批次 → 明细 → 聚合 → `COMMIT`；中途异常 `ROLLBACK`。
   - 同 `sha256` 已入库且未勾选 `force_new_batch` → 返回 `duplicate_file`，写入 0 行。
   - 勾选 `force_new_batch` → 新建批次（`-F2`/`-F3` 后缀），旧批次保留且 `superseded_by` 指向新批次。

## 启动

```bash
cd batch-20261114b/projects/gb-623
cp .env.example .env          # 可选，默认端口 18212
docker compose up -d --build
curl -s http://127.0.0.1:18212/health
```

- 页面：<http://127.0.0.1:18212/>，按日发布页 <http://127.0.0.1:18212/days>
- 数据卷：`csvgate-data:/app/data`（SQLite `csvgate.sqlite3`）
- 启动即幂等 seed（两类历史数据，重复启动不会重复写入）：
  - `meta.seed_version = seed-2026-11-07.1`，旧口径批次 `B-20261107-001`（60 行历史周批次）；
  - `meta.day_seed_version = day-seed-2026-11-08.1`：2026-11-08 的 6 个站点文件完整发布为 v1；
    2026-11-09 只 seed 2 个站点，故意保持「待补齐」，因此不会出现在当前结果里。
  **重建数据库**：`docker compose down -v && docker compose up -d --build`。

## 演示步骤

### A. 按日发布单元（主流程）

1. 打开 `/days`：看到 2026-11-08 已发布 v1（6 站 240 行），2026-11-09 待补齐（只有北京、上海）。
   首页与 `/api/days` 里，2026-11-09 只出现在 `pending`，不在 `current`，日汇总也不含这一天。
2. 补齐 2026-11-09：依次上传 `pending_20261109_guangzhou.csv`、`..._shenzhen.csv`、
   `..._chengdu.csv` → 均为 202 `staged`，当前结果不变；最后传 `pending_20261109_hangzhou.csv`
   → 200 `published`，该日的明细与站点汇总一次性切换，`pending_count` 归零。
   （也可直接上传 `pending_20261109_beijing.csv` / `..._shanghai.csv` 验证幂等重试：返回 `unchanged`。）
3. 单站重发：上传 `resend_20261108_beijing.csv` → 200 `published`、版本号 v2，只替换北京；
   其余 5 站金额不变。打开 `/days/2026-11-08/v/1` 仍能看到 v1 的北京旧金额与明细。
4. 把 `resend_20261108_beijing.csv` 再传一次：`unchanged`，不新增快照、不新增版本。
5. 传一份坏文件（如把 category 改成 `furniture`）：422 `rejected`，版本号不变，上一版继续生效。
6. 传一份混了多站或多天的文件：422，`stage=release-key`，不写快照、不进当前结果。
7. 2026-11-10 演示「站点逐个到达」：可复制 `day_20261108_<site>.csv` 改日期后逐站上传，
   前 5 站 202 暂存、第 6 站到齐才 200 发布。

### B. 旧口径单文件导入（兼容路径）

1. `/workbench` 下载 `normal_500.csv` 并上传 → 结论「已入库」，得批号、站点聚合表、明细前 20 行。
2. 上传 `broken.csv` → HTTP 422，结论「整体回滚」，列出 5 条逐行错误（重复主键 / 倒序 / 非法日期 / 超长 / 枚举越界）。
   刷新首页或 `/health` 看 `fact_rows` 未变。
3. 上传 `gbk_120.csv` → 编码探测显示 `gb18030`，120 行入库。
4. 再上传一次 `normal_500.csv` → `duplicate_file`，写入 0 行；到 `/review` 点「验证重复导入」看只读判定。
5. 勾选 `force_new_batch` 再传 `normal_500.csv` → 新批次，旧批次标 superseded。

命令行等价（验收脚本用的就是这些）：

```bash
curl -s http://127.0.0.1:18212/health
curl -s -F "file=@sample_data/normal_500.csv" -F "extra_policy=reject" http://127.0.0.1:18212/api/import
curl -s -o /tmp/broken.json -w '%{http_code}\n' -F "file=@sample_data/broken.csv" http://127.0.0.1:18212/api/import
python3 -c "import json;d=json.load(open('/tmp/broken.json'));print(d['report']['total'],[e['code'] for e in d['report']['rows']])"

# 按日发布：首页看当前/待补齐
curl -s http://127.0.0.1:18212/api/days
# 单站重发（seed 已发布的 2026-11-08 北京站）：生成 v2，只替换该站
curl -s -F "file=@sample_data/resend_20261108_beijing.csv" http://127.0.0.1:18212/api/day-import
# 同一份再传一次：unchanged，不新增版本
curl -s -F "file=@sample_data/resend_20261108_beijing.csv" http://127.0.0.1:18212/api/day-import
# 回查上一版
curl -s http://127.0.0.1:18212/api/days/2026-11-08/versions/1
```

## API / 页面

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 + 旧口径明细行数 + 已发布/待补齐日期数 + seed 版本 |
| POST | `/api/day-import` | **按日发布**：multipart `file`、`extra_policy`；200 已发布/无变化、202 已暂存待补齐、422 校验拒绝、400 编码失败、500 发布回滚 |
| GET | `/api/days` | 当前已发布日期（含站点汇总）与待补齐日期（已到/缺失站点） |
| GET | `/api/days/{date}` | 某日：`summary`（未发布为 null）、`pending`、`versions` |
| GET | `/api/days/{date}/versions/{n}` | 按版本号回查历史发布：站点快照 + 明细前 20 行 |
| POST | `/api/import` | 旧口径：multipart `file`、`extra_policy`、`force_new_batch`；200 已入库/重复文件、400 编码失败、422 校验拒绝 |
| POST | `/workbench` | 页面表单同旧口径管道；通过 → 303 跳结果页，拒绝 → 422 直接渲染错误表 |
| POST | `/days/upload` | 按日发布页面表单；可推导发布键 → 303 跳 `/days/{date}` |
| POST | `/api/contract-check` | 只检查表头（含 `warn` 策略演练） |
| POST | `/api/replay-check` | 按 sha256 判定「再次上传会不会重复」 |
| GET | `/api/batches` | 旧口径批次列表 + 统计 |
| GET | `/api/samples/{name}` | 样例下载（含按日发布演示文件） |
| 页面 | `/`、`/days`、`/days/{date}`、`/days/{date}/v/{n}`、`/workbench`、`/result/{batch_id}`、`/result/attempt/{id}`、`/review` | 首页/按日发布/某日/版本回查/工作台/结果/错误明细/历史复核 |

## 目录

```text
app/            main.py settings.py pipeline.py seed.py dayrelease.py
                encoding.py contract.py rows.py aggregate.py importer.py report.py store.py
                templates/ static/
sample_data/    normal_500.csv gbk_120.csv seed_20261107.csv broken.csv contract.json make_samples.py
                day_20261108_<site>.csv ×6  pending_20261109_<site>.csv ×6  resend_20261108_beijing.csv
tests/          test_encoding.py test_rows.py test_importer.py test_api.py
                test_dayrelease.py test_api_days.py conftest.py
Dockerfile docker-compose.yml requirements.txt .env.example
```

## 已知限制

- CSV 解析用标准库 `csv.reader`：支持引号包裹字段，但**行号**按「数据行 + 1」换算，
  字段内含换行时行号会偏移（当前样例与常见导出不含换行字段）。
- 编码只覆盖 UTF-8 / UTF-8-BOM / GB18030；UTF-16/UTF-32 会走到 400 并给出提示，不做自动识别。
- 分隔符固定逗号，不做 `;`/`\t` 自动猜测；SJIS、Big5 等会按 GB18030 误判成功的概率存在（GBK 超集覆盖广），
  需要在契约里约定来源编码。
- 单次上传上限 8MB（约 10 万行量级）；不做流式分片。
- 按日发布要求每份文件只含一个站点、一天；混合站点/混合日期的文件会被拒绝，需上游按站、按天拆分。
  站点清单固定为契约中的 6 站；新增站点需同步修改 `contract.SITES`。
- 按日发布的「最新成功上传胜出」：若人为把某站旧内容再次上传，会产生一个新版本（旧数据可从历史版本查到），
  不会静默拒绝；如需禁止回退到旧内容，应在业务侧另加版本白名单。
- 明细与批次不提供删除接口；旧口径重灌靠 `force_new_batch` + `superseded_by` 追溯，
  按日发布靠版本号追溯。
