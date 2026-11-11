"""Request-size limits and the generic storage error (v1.13.3). No database needed."""
import pytest
from fastapi.testclient import TestClient

from app.body_limit import limit_for
from app.config import settings
from app.main import app
from app.routers import admin as admin_router

MB = 1024 * 1024
ATTEND = "/api/v1/service-tickets/00000000-0000-4000-8000-000000000000/attend"


@pytest.fixture(scope="module")
def client():
    return TestClient(app)  # no lifespan: nothing here reaches the database


def test_limits_per_route():
    assert limit_for("/api/v1/auth/login") == settings.max_body_kb * 1024
    assert limit_for(ATTEND) == (6 * settings.max_upload_mb + 1) * MB
    assert limit_for(ATTEND.replace("/attend", "/addendum")) == limit_for(ATTEND)  # same photo allowance
    assert limit_for("/api/v1/admin/restore") == (settings.max_restore_mb + 1) * MB
    assert limit_for("/api/v1/admin/restore-not") == settings.max_body_kb * 1024


def test_oversized_declared_body_is_refused_without_being_read(client):
    r = client.post("/api/v1/auth/login", content=b"x" * (settings.max_body_kb * 1024 + 1),
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and "too large" in r.json()["error"]
    r = client.post(ATTEND, content=b"x" * (limit_for(ATTEND) + 1), headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 413


def test_oversized_chunked_body_is_cut_off(client):
    def chunks():  # no Content-Length: the bytes are counted as they arrive
        for _ in range(settings.max_body_kb // 64 + 2):
            yield b"x" * 65536

    r = client.post("/api/v1/auth/login", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and "error" in r.json()


def test_a_normal_request_is_not_touched(client):
    r = client.post(ATTEND, content=b"", headers={"Content-Type": "multipart/form-data; boundary=x"})
    assert r.status_code != 413
    assert client.get("/health").status_code == 200


def test_restore_limit_follows_the_setting(client, monkeypatch):
    monkeypatch.setattr(settings, "max_restore_mb", 1)
    r = client.post("/api/v1/admin/restore", content=b"x" * (3 * MB), headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 413


def test_storage_errors_do_not_leak_details():
    err = admin_router._storage_error(Exception("EndpointConnectionError: https://acct123456.r2.cloudflarestorage.com/bucket-x"))
    assert err.status_code == 502
    assert "acct123456" not in err.detail and "bucket-x" not in err.detail and "server log" in err.detail
