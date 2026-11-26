"""后端测试夹具：每个用例都从示例数据重新起一份内存仓库和修订台账。"""
from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.seed import SEED_ROWS
from app.services.result_revision import ledger
from app.store import store


@pytest.fixture()
def client() -> TestClient:
    store._tables = {name: [copy.deepcopy(row) for row in rows] for name, rows in SEED_ROWS.items()}
    ledger.reset()
    ledger.before_commit_guard = None
    return TestClient(app)


@pytest.fixture()
def revision_ledger():
    return ledger
