import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

ROOT = Path(__file__).resolve().parents[1]
KEY_A, KEY_B = "test-key-tenant-a-0123456789", "test-key-tenant-b-0123456789"


def _sha(k: str) -> str:
    return hashlib.sha256(k.encode()).hexdigest()


def make_settings(**overrides) -> Settings:
    base = dict(
        _env_file=None, log_level="WARNING", rate_limit_per_minute=1000,
        api_keys_sha256={_sha(KEY_A): "tenant-a", _sha(KEY_B): "tenant-b"},
    )
    return Settings(**{**base, **overrides})


@pytest.fixture(scope="session")
def ingest_body() -> dict:
    docs = []
    for path in sorted((ROOT / "data" / "sample_docs").glob("*.md")):
        text = path.read_text()
        docs.append({"doc_id": path.stem, "title": text.splitlines()[0].lstrip("# "), "text": text})
    return {"documents": docs}


@pytest.fixture(scope="module")
def client(ingest_body):
    with TestClient(create_app(make_settings())) as c:
        r = c.post("/v1/ingest", json=ingest_body, headers={"X-API-Key": KEY_A})
        assert r.status_code == 201, r.text
        yield c


@pytest.fixture
def auth_a() -> dict:
    return {"X-API-Key": KEY_A}


@pytest.fixture
def auth_b() -> dict:
    return {"X-API-Key": KEY_B}


def load_queries() -> list[dict]:
    return json.loads((ROOT / "data" / "sample_requests" / "queries.json").read_text())
