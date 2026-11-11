"""CORS allowlist, minimal /health, and length caps on free text / search. No database needed."""
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.main import app
from app.schemas.store import UpdateStoreProfileRequest
from app.schemas.tickets import CreateTicketRequest
from app.schemas.warranty import ClaimWarrantyRequest

BASE = dict(db_host="h", db_name="n", db_user="u", db_password="p", jwt_secret="s" * 32,
            r2_account_id="a", r2_access_key_id="k", r2_secret_access_key="s", r2_bucket="b")


def test_health_reports_no_version():
    body = TestClient(app).get("/health").json()
    assert set(body) == {"status", "time"} and body["status"] == "ok"


def test_cors_is_closed_by_default_and_refuses_wildcards():
    assert Settings(_env_file=None, **{**BASE, "cors_allowed_origins": ""}).cors_origins == []
    for bad in ("*", "app.example.com", "https://a.com,*"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **{**BASE, "cors_allowed_origins": bad})
    ok = Settings(_env_file=None, **{**BASE, "cors_allowed_origins": "https://a.com/, http://localhost:3000"})
    assert ok.cors_origins == ["https://a.com", "http://localhost:3000"]


def test_no_cors_headers_for_a_foreign_origin():
    r = TestClient(app).get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_free_text_is_capped():
    with pytest.raises(ValidationError):
        UpdateStoreProfileRequest(store_name="x", address="a" * 501)
    with pytest.raises(ValidationError):
        CreateTicketRequest(customer_id="c", service_type="PAID", title="t", description="d" * 2001)
    with pytest.raises(ValidationError):
        CreateTicketRequest(customer_id="c", service_type="PAID", title="t", site_address="a" * 501)
    with pytest.raises(ValidationError):
        CreateTicketRequest(customer_id="c" * 65, service_type="PAID", title="t")
    with pytest.raises(ValidationError):
        ClaimWarrantyRequest(notes="n" * 2001)
    CreateTicketRequest(customer_id="c", service_type="PAID", title="t", description="d" * 2000, site_address="a" * 500)


def test_search_params_are_capped():
    paths = app.openapi()["paths"]
    checked = 0
    for path, ops in paths.items():
        for op in ops.values():
            for p in op.get("parameters", []):
                if p["name"] == "search":
                    assert p["schema"]["anyOf"][0]["maxLength"] == 100, path
                    checked += 1
    assert checked >= 8
