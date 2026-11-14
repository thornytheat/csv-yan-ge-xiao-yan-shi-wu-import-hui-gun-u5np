"""FastAPI 入口：路由保持薄，业务判定都在 pipeline / 纯函数模块里。"""
from __future__ import annotations

import io
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import contract, pipeline, report, seed, settings, store

MAX_BYTES = 8 * 1024 * 1024

STATUS_HTTP = {
    "committed": 200,
    "duplicate_file": 200,
    "rejected": 422,
    "encoding_failed": 400,
    "rolled_back": 500,
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
        return {
            "status": "ok",
            "app": settings.APP_SHORT,
            "db": str(settings.DB_PATH),
            "fact_rows": store.count_rows(conn),
            "seed_version": store.get_meta(conn, "seed_version"),
        }
    finally:
        conn.close()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    conn = get_conn()
    try:
        recent = store.list_batches(conn, limit=5)
        attempts = store.list_attempts(conn, limit=5)
    finally:
        conn.close()
    return _render(request, "index.html", {
        "active": "",
        "page": "index",
        "recent_batches": recent,
        "recent_attempts": attempts,
        "contract_columns": contract.COLUMN_SPECS,
        "required": contract.REQUIRED,
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
    conn = get_conn()
    try:
        result = pipeline.run(raw, file.filename or "upload.csv",
                              extra_policy=extra_policy, force_new_batch=forced, conn=conn)
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
):
    raw = await _read_upload(file)
    forced = force_new_batch.lower() in ("1", "true", "on", "yes")
    conn = get_conn()
    try:
        result = pipeline.run(raw, file.filename or "upload.csv",
                              extra_policy=extra_policy, force_new_batch=forced, conn=conn)
    finally:
        conn.close()
    payload = _public_result(result)
    return JSONResponse(payload, status_code=STATUS_HTTP.get(result["status"], 400))


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


@app.get("/api/samples/{name}")
def api_sample(name: str):
    allowed = {s["name"] for s in settings.SAMPLE_FILES}
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
