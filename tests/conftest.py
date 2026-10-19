"""Test configuration — isolate the DB before any app import."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Point the app at a throwaway SQLite file BEFORE importing anything from `app`
# (db/base.py builds the engine from settings at import time).
_TMP_DB = Path(tempfile.gettempdir()) / "sellerbot_test.db"
for suffix in ("", "-wal", "-shm"):
    p = Path(str(_TMP_DB) + suffix)
    if p.exists():
        p.unlink()

os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP_DB.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin"
os.environ["BOT_TOKEN"] = ""  # keep bot polling disabled during tests
os.environ["SELLERBOT_TESTING"] = "1"  # use NullPool so per-test loops don't clash

import asyncio  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db.base import init_db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _prepare_db():
    """Create tables once before any test (commerce tests don't use `client`)."""
    asyncio.run(init_db())


@pytest.fixture(scope="session")
def client():
    # `with` runs the app lifespan once (init_db + seed) on a single event loop.
    with TestClient(app) as c:
        yield c
