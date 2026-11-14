"""测试共用：把 SQLite 指到临时目录，每个用例用干净库 + 启动 seed。"""
from __future__ import annotations

import os
import pathlib
import tempfile

import pytest

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="csvgate-tests-"))
os.environ["CSVGATE_DATA_DIR"] = str(_TMP)

from app import settings  # noqa: E402


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app

    db = _TMP / "csvgate.sqlite3"
    for suffix in ("", "-wal", "-shm"):
        candidate = pathlib.Path(str(db) + suffix)
        if candidate.exists():
            candidate.unlink()
    settings.DB_PATH = db

    with TestClient(fastapi_app) as test_client:
        yield test_client


@pytest.fixture()
def sample_dir() -> pathlib.Path:
    return settings.SAMPLE_DIR
