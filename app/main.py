"""FastAPI 入口：路由保持薄，业务判定都在 pipeline / 纯函数模块里。"""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import contract, dayrelease, pipeline, report, seed, settings, store

MAX_BYTES = 8 * 1024 * 1024

STATUS_HTTP = {
    "committed": 200,
    "duplicate_file": 200,
    "rejected": 422,
    "encoding_failed": 400,
    "rolled_back": 500,
    # 按日发布单元
    "published": 200,
    "staged": 202,
    "unchanged": 200,
}


def get_conn() -> sqlite3.Connection:
    return store.connect()


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = get_conn()
    try:
        store.init_schema(conn)
        info = seed.ensure_seed(conn)
        app.state.seed_info = info
    finally:
        conn.close()
    yield


app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)
BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


def base_context(request: Request, active: str) -> dict:
    conn = get_conn()
    try:
        stats = store.stats(conn)
    finally:
        conn.close()
    return {
        "request": request,
        "app_name": settings.APP_NAME,
        "app_short": settings.APP_SHORT,
        "active": active,
        "stats": stats,
        "samples": settings.SAMPLE_FILES,
    }


def _render(request: Request, name: str, context: dict, status_code: int = 200):
    ctx = base_context(request, context.pop("active", ""))
    ctx.update(context)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


# ---------------------------------------------------------------- 页面


@app.get("/health")
def health():
    conn = get_conn()
    try:
        stats = store.stats(conn)
        return {
            "status": "ok",
            "app": settings.APP_SHORT,
            "db": str(settings.DB_PATH),
            "fact_rows": stats["fact_rows"],
            "released_days": stats["released_days"],
            "released_rows": stats["released_rows"],
            "pending_days": stats["pending_days"],
            "seed_version": store.get_meta(conn, "seed_version"),
            "day_seed_version": store.get_meta(conn, "day_seed_version"),
        }
    finally:
        conn.close()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    conn = get_conn()
    try:
        recent = store.list_batches(conn, limit=5)
        attempts = store.list_attempts(conn, limit=5)
        current = dayrelease.current_days(conn)
        pending = dayrelease.pending_days(conn)
    finally:
        conn.close()
    return _render(request, "index.html", {
        "active": "",
        "page": "index",
        "recent_batches": recent,
        "recent_attempts": attempts,
        "current_days": current,
        "pending_days": pending,
        "contract_columns": contract.COLUMN_SPECS,
        "required": contract.REQUIRED,
    })


# ---------------------------------------------------------------- 按日发布页面


@app.post("/days/upload")
async def days_upload(
    request: Request,
    file: UploadFile = File(...),
    extra_policy: str = Form("reject"),
):
    raw = await _read_upload(file)
    conn = get_conn()
    try:
        result = dayrelease.ingest(conn, raw, file.filename or "upload.csv",
                                   extra_policy=extra_policy)
    finally:
        conn.close()
    if result.get("business_date") and result["status"] != "rejected":
        flag = {"published": "published", "staged": "staged",
                "unchanged": "unchanged", "rolled_back": "rolled_back"}.get(
                    result["status"], "rejected")
        return RedirectResponse(
            url=f"/days/{result['business_date']}?{flag}=1", status_code=303
        )
    conn = get_conn()
    try:
        current = dayrelease.current_days(conn)
        pending = dayrelease.pending_days(conn)
    finally:
        conn.close()
    return _render(request, "days.html", {
        "active": "days",
        "page": "days",
        "current_days": current,
        "pending_days": pending,
        "expected_sites": list(dayrelease.EXPECTED_SITES),
        "day_samples": settings.DAY_SAMPLE_FILES,
        "error": result.get("detail", "站点日文件未通过校验"),
        "result": result,
    }, status_code=STATUS_HTTP.get(result["status"], 400))


@app.get("/days", response_class=HTMLResponse)
def days_page(request: Request):
    conn = get_conn()
    try:
        current = dayrelease.current_days(conn)
        pending = dayrelease.pending_days(conn)
    finally:
        conn.close()
    return _render(request, "days.html", {
        "active": "days",
        "page": "days",
        "current_days": current,
        "pending_days": pending,
        "expected_sites": list(dayrelease.EXPECTED_SITES),
        "day_samples": settings.DAY_SAMPLE_FILES,
    })


@app.get("/days/{business_date}", response_class=HTMLResponse)
def day_page(request: Request, business_date: str,
             published: int = 0, staged: int = 0, unchanged: int = 0,
             rejected: int = 0, rolled_back: int = 0):
    conn = get_conn()
    try:
        summary = dayrelease.day_summary(conn, business_date)
        pending = next((p for p in dayrelease.pending_days(conn)
                        if p["business_date"] == business_date), None)
        versions = dayrelease.list_versions(conn, business_date)
        attempts = [a for a in store.list_attempts(conn, limit=100)
                    if a.get("detail") and business_date in a["detail"]]
    finally:
        conn.close()
    if not summary and not pending:
        raise HTTPException(status_code=404, detail=f"日期 {business_date} 没有任何上传记录")
    for attempt in attempts:
        attempt.pop("errors_json", None)
    notice = ""
    if published:
        notice = "站点齐全：当天订单明细与站点汇总已在一个事务里切换到新版本。"
    elif staged:
        notice = "该站点文件已校验并暂存；站点未齐，当前结果不变，等待补齐。"
    elif unchanged:
        notice = "同一份内容已在当前快照/当前版本中：未新增快照、未新增版本。"
    elif rejected:
        notice = "本次上传未通过校验：未写入快照，上一版继续生效。"
    elif rolled_back:
        notice = "发布过程失败已整体回滚：当天保持上一版，未发生数据丢失。"
    return _render(request, "day.html", {
        "active": "days",
        "page": "days",
        "business_date": business_date,
        "summary": summary,
        "pending": pending,
        "versions": versions,
        "attempts": attempts,
        "expected_sites": list(dayrelease.EXPECTED_SITES),
        "notice": notice,
    })


@app.get("/days/{business_date}/v/{version_no}", response_class=HTMLResponse)
def day_version_page(request: Request, business_date: str, version_no: int):
    conn = get_conn()
    try:
        version = dayrelease.get_version(conn, business_date, version_no)
        current = dayrelease.day_summary(conn, business_date)
        versions = dayrelease.list_versions(conn, business_date)
    finally:
        conn.close()
    if not version:
        raise HTTPException(status_code=404,
                             detail=f"{business_date} 没有版本 v{version_no}")
    return _render(request, "day_version.html", {
        "active": "days",
        "page": "days",
        "business_date": business_date,
        "version": version,
        "summary": current,
        "versions": versions,
        "expected_sites": list(dayrelease.EXPECTED_SITES),
    })


@app.get("/workbench", response_class=HTMLResponse)
def workbench(request: Request):
    return _render(request, "workbench.html", {
        "active": "workbench",
        "page": "workbench",
        "policies": settings.EXTRA_POLICIES,
        "column_contract": [
            {"name": n, "required": s["required"], "type": s["type"],
             "label": s["label"], "max_length": s.get("max_length"),
             "enum": s.get("enum")}
            for n, s in contract.COLUMN_SPECS.items()
        ],
        "sites": contract.SITES,
        "categories": contract.CATEGORIES,
    })


async def _read_upload(upload: UploadFile) -> bytes:
    raw = await upload.read()
    if len(raw) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="文件超过 8MB 上限，请分片后分批入库。")
    return raw


def _render_result(request: Request, result: dict, notice: str = "", status_code: int = 200):
    conn = get_conn()
    try:
        attempts = store.list_attempts(conn, limit=10)
        batch = store.get_batch(conn, result["batch_id"]) if result.get("batch_id") else None
        rows_before = result.get("rows_before")
        rows_after = result.get("rows_after")
        for attempt in attempts:
            attempt.pop("errors_json", None)
    finally:
        conn.close()
    return _render(request, "result.html", {
        "active": "result",
        "page": "result",
        "result": result,
        "batch": batch,
        "attempts": attempts,
        "notice": notice,
        "rows_before": rows_before,
        "rows_after": rows_after,
    }, status_code=status_code)


@app.post("/workbench", response_class=HTMLResponse)
async def workbench_submit(
    request: Request,
    file: UploadFile = File(...),
    extra_policy: str = Form("reject"),
    force_new_batch: str = Form(""),
):
    raw = await _read_upload(file)
    forced = force_new_batch.lower() in ("1", "true", "on", "yes")
    filename = file.filename or "upload.csv"
    conn = get_conn()
    try:
        prepared = pipeline.prepare(raw, filename, extra_policy)
        if dayrelease.is_release_unit(prepared):
            result = dayrelease.ingest(conn, raw, filename,
                                       extra_policy=extra_policy, prepared=prepared)
            status = result["status"]
            flag = {"published": "published=1", "staged": "staged=1",
                    "unchanged": "unchanged=1"}.get(status, "rejected=1")
            return RedirectResponse(
                url=f"/days/{result['business_date']}?{flag}", status_code=303
            )
        result = pipeline.run(raw, filename, extra_policy=extra_policy,
                              force_new_batch=forced, conn=conn)
    finally:
        conn.close()

    status = result["status"]
    if status in ("committed", "duplicate_file") and result.get("batch_id"):
        flag = "force_new_batch=1" if forced else ""
        if status == "duplicate_file":
            flag = "duplicate_file=1"
        return RedirectResponse(url=f"/result/{result['batch_id']}?{flag}", status_code=303)
    if status == "rejected":
        return _render(request, "workbench.html", {
            "active": "workbench",
            "page": "workbench",
            "policies": settings.EXTRA_POLICIES,
            "column_contract": _column_contract(),
            "sites": contract.SITES,
            "categories": contract.CATEGORIES,
            "result": result,
            "error": result.get("detail", "校验未通过"),
            "filename": result.get("filename"),
        }, status_code=422)
    return _render(request, "workbench.html", {
        "active": "workbench",
        "page": "workbench",
        "policies": settings.EXTRA_POLICIES,
        "column_contract": _column_contract(),
        "sites": contract.SITES,
        "categories": contract.CATEGORIES,
        "result": result,
        "error": result.get("detail", "未能入库"),
        "filename": result.get("filename"),
    }, status_code=STATUS_HTTP.get(status, 400))


def _column_contract() -> list[dict]:
    return [
        {"name": n, "required": s["required"], "type": s["type"], "label": s["label"],
         "max_length": s.get("max_length"), "enum": s.get("enum")}
        for n, s in contract.COLUMN_SPECS.items()
    ]


@app.get("/result/{batch_id}", response_class=HTMLResponse)
def result_page(request: Request, batch_id: str,
                new: int = 0, duplicate_file: int = 0, force_new_batch: int = 0):
    conn = get_conn()
    try:
        batch = store.get_batch(conn, batch_id)
        attempts = [a for a in store.list_attempts(conn, limit=50)
                    if a.get("batch_id") == batch_id]
        for attempt in attempts:
            attempt.pop("errors_json", None)
        total_batches = store.stats(conn)
    finally:
        conn.close()
    if not batch:
        raise HTTPException(status_code=404, detail=f"批次 {batch_id} 不存在")

    notice = ""
    if duplicate_file:
        notice = "该文件的 sha256 已在库中：按幂等策略未重复写入，下面展示的是既有批次。"
    elif force_new_batch:
        notice = "已按 force_new_batch 覆盖策略新建批次；旧批次保留并标记 superseded_by。"
    elif new:
        notice = "新批次已在一个事务内提交完成。"

    return _render(request, "result.html", {
        "active": "result",
        "page": "result",
        "result": {"status": "committed", "sha256": batch["sha256"],
                   "filename": batch["filename"], "encoding": {"encoding": batch["encoding"],
                                                               "bom": batch["bom"]},
                   "extra_policy": "reject"},
        "batch": batch,
        "attempts": attempts,
        "stats": total_batches,
        "notice": notice,
        "rows_before": None,
        "rows_after": None,
    })


@app.get("/result/attempt/{attempt_id}", response_class=HTMLResponse)
def attempt_page(request: Request, attempt_id: int):
    conn = get_conn()
    try:
        attempt = store.get_attempt(conn, attempt_id)
    finally:
        conn.close()
    if not attempt:
        raise HTTPException(status_code=404, detail=f"记录 {attempt_id} 不存在")
    rep = report.build_report(attempt.get("errors") or [])
    return _render(request, "attempt.html", {
        "active": "review",
        "page": "review",
        "attempt": attempt,
        "report": rep,
        "result": {
            "status": attempt["status"],
            "filename": attempt["filename"],
            "sha256": attempt["sha256"],
            "encoding": {"encoding": attempt.get("encoding") or "-", "bom": False},
            "detail": attempt.get("detail") or "",
            "attempt_id": attempt["attempt_id"],
        },
        "batch": None,
        "attempts": [],
    })


@app.get("/review", response_class=HTMLResponse)
def review(request: Request):
    conn = get_conn()
    try:
        batches = store.list_batches(conn, limit=30)
        attempts = store.list_attempts(conn, limit=30)
        stats = store.stats(conn)
    finally:
        conn.close()
    for attempt in attempts:
        attempt.pop("errors_json", None)
    return _render(request, "review.html", {
        "active": "review",
        "page": "review",
        "batches": batches,
        "attempts": attempts,
        "stats": stats,
    })


# ---------------------------------------------------------------- API


@app.post("/api/import")
async def api_import(
    file: UploadFile = File(...),
    extra_policy: str = Form("reject"),
    force_new_batch: str = Form(""),
    mode: str = Form("auto"),
):
    """单份文件导入。

    - `mode=auto`（默认）：文件只含一个站点、一天 → 按日发布（暂存/发布）；
      否则退回旧口径「一份文件 = 一个批次」。
    - `mode=daily`：强制按日发布，混合站点/日期会被拒绝。
    - `mode=legacy`：强制旧口径，兼容既有调用方。
    """
    raw = await _read_upload(file)
    forced = force_new_batch.lower() in ("1", "true", "on", "yes")
    filename = file.filename or "upload.csv"
    conn = get_conn()
    try:
        if mode in ("daily", "auto"):
            prepared = pipeline.prepare(raw, filename, extra_policy)
            if mode == "daily" or dayrelease.is_release_unit(prepared):
                result = dayrelease.ingest(conn, raw, filename,
                                           extra_policy=extra_policy, prepared=prepared)
                return JSONResponse(_day_result(result),
                                    status_code=STATUS_HTTP.get(result["status"], 400))
            # auto 且文件不是单站单日（或校验失败）：退回旧口径，结论与历史一致。
            result = pipeline.run(raw, filename, extra_policy=extra_policy,
                                  force_new_batch=forced, conn=conn)
        else:
            result = pipeline.run(raw, filename, extra_policy=extra_policy,
                                  force_new_batch=forced, conn=conn)
    finally:
        conn.close()
    return JSONResponse(_public_result(result),
                        status_code=STATUS_HTTP.get(result["status"], 400))


def _public_result(result: dict) -> dict:
    """返回完整结论；import 内的批次号/写入行数也保留，便于 curl 断言。"""
    return dict(result)


@app.post("/api/contract-check")
async def api_contract_check(request: Request):
    """工作台「表头自检」：粘贴一行表头立即看契约结论（模块 2 的独立入口）。"""
    body = await request.json()
    header_line = (body.get("header") or "").strip()
    policy = body.get("extra_policy") or "reject"
    if not header_line:
        return JSONResponse({"detail": "请粘贴表头行"}, status_code=400)
    cells = [c for c in header_line.replace("\t", ",").split(",")]
    result = contract.check(cells, policy)
    result.pop("column_contract", None)
    return result


@app.post("/api/replay-check")
async def api_replay_check(request: Request):
    """复核页「可重复导入验证」：判断某 sha256 现在再次上传会是什么结论。"""
    body = await request.json()
    sha = (body.get("sha256") or "").strip().lower()
    if len(sha) != 64:
        return JSONResponse({"detail": "sha256 需为 64 位十六进制"}, status_code=400)
    conn = get_conn()
    try:
        existing = store.find_batch_by_sha(conn, sha)
        count = store.count_rows(conn)
    finally:
        conn.close()
    if existing:
        return {
            "sha256": sha,
            "verdict": "duplicate_file",
            "batch_id": existing["batch_id"],
            "message": (f"同文件已入库为 {existing['batch_id']}（{existing['filename']}，"
                        f"{existing['row_count']} 行）。再次上传不会重复写入；"
                        "需要重灌请显式勾选 force_new_batch。"),
            "fact_rows": count,
        }
    return {
        "sha256": sha,
        "verdict": "new_file",
        "batch_id": None,
        "message": "库中没有该 sha256，上传会作为新批次正常入库。",
        "fact_rows": count,
    }


@app.get("/api/batches")
def api_batches():
    conn = get_conn()
    try:
        return {"batches": store.list_batches(conn, limit=50), "stats": store.stats(conn)}
    finally:
        conn.close()


# ---------------------------------------------------------------- 按日发布单元 API


def _day_result(result: dict) -> dict:
    """按日发布的公开结论：附带当前版本 / 缺失站点 / 是否影响当前结果。"""
    return {
        "status": result["status"],
        "stage": result.get("stage"),
        "business_date": result.get("business_date"),
        "site": result.get("site"),
        "filename": result.get("filename"),
        "sha256": result.get("sha256"),
        "encoding": result.get("encoding"),
        "detail": result.get("detail", ""),
        "attempt_id": result.get("attempt_id"),
        "snapshot_created": result.get("snapshot_created", False),
        "version_created": result.get("version_created", False),
        "version_no": result.get("version_no"),
        "previous_version": result.get("previous_version"),
        "current_version": result.get("current_version"),
        "staged_sites": result.get("staged_sites", []),
        "missing_sites": result.get("missing_sites", []),
        "expected_sites": result.get("expected_sites", []),
        # 本次上传是否让该日期进入了当前结果（unchanged 不会重复计入）
        "visible_in_current": bool(result.get("version_created")),
        "day": result.get("day"),
        "aggregate": result.get("aggregate"),
        "report": result.get("report"),
        "contract": result.get("contract"),
        "row_validation": result.get("row_validation"),
        "suggestion": result.get("suggestion"),
        "release_key": result.get("release_key"),
    }


@app.post("/api/day-import")
async def api_day_import(
    file: UploadFile = File(...),
    extra_policy: str = Form("reject"),
):
    """按日发布：一份「单站 + 单日」CSV。站点齐全才切换当日明细与汇总。"""
    raw = await _read_upload(file)
    conn = get_conn()
    try:
        result = dayrelease.ingest(conn, raw, file.filename or "upload.csv",
                                   extra_policy=extra_policy)
    finally:
        conn.close()
    return JSONResponse(_day_result(result), status_code=STATUS_HTTP.get(result["status"], 400))


@app.get("/api/days")
def api_days():
    conn = get_conn()
    try:
        current = dayrelease.current_days(conn)
        pending = dayrelease.pending_days(conn)
    finally:
        conn.close()
    return {
        "current": current,
        "pending": pending,
        "expected_sites": list(dayrelease.EXPECTED_SITES),
        "current_count": len(current),
        "pending_count": len(pending),
    }


@app.get("/api/days/{business_date}")
def api_day(business_date: str):
    conn = get_conn()
    try:
        summary = dayrelease.day_summary(conn, business_date)
        pending = next((p for p in dayrelease.pending_days(conn)
                        if p["business_date"] == business_date), None)
        versions = dayrelease.list_versions(conn, business_date)
    finally:
        conn.close()
    if not summary and not pending:
        raise HTTPException(status_code=404, detail=f"日期 {business_date} 没有任何上传记录")
    return {
        "business_date": business_date,
        "published": bool(summary),
        "summary": summary,
        "pending": pending,
        "versions": versions,
    }


@app.get("/api/days/{business_date}/versions/{version_no}")
def api_day_version(business_date: str, version_no: int):
    conn = get_conn()
    try:
        version = dayrelease.get_version(conn, business_date, version_no)
    finally:
        conn.close()
    if not version:
        raise HTTPException(status_code=404,
                             detail=f"{business_date} 没有版本 v{version_no}")
    return version


@app.get("/api/samples/{name}")
def api_sample(name: str):
    allowed = {s["name"] for s in settings.ALL_SAMPLE_FILES}
    if name not in allowed:
        raise HTTPException(status_code=404, detail="样例文件不存在")
    path = settings.SAMPLE_DIR / name
    if not path.exists():
        raise HTTPException(status_code=404, detail="样例文件未随镜像分发")
    media = "text/csv; charset=utf-8"
    if name.endswith(".json"):
        media = "application/json; charset=utf-8"
    elif name == "gbk_120.csv":
        media = "text/csv; charset=gb18030"
    return FileResponse(path, media_type=media, filename=name)
