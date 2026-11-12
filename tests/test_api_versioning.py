"""API versioning (v1.17.0): everything lives under /api/v1; the old /api paths are a hidden alias. No database needed."""
import pytest
from fastapi.testclient import TestClient

from app.body_limit import limit_for
from app.config import APP_VERSION, settings
from app.main import app
from app.utils.audit import _classify


@pytest.fixture(scope="module")
def client():
    return TestClient(app)  # no lifespan: nothing here reaches the database


def _all_routes():
    """(full path, methods) of every route. Newer FastAPI keeps an included router as one nested object with a
    prefix; older versions copy each route into app.routes. Both are handled."""
    for route in app.routes:
        ctx = getattr(route, "include_context", None)
        if ctx is not None:
            for sub in ctx.included_router.routes:
                yield ctx.prefix + sub.path, getattr(sub, "methods", None) or ()
        elif getattr(route, "path", None):
            yield route.path, getattr(route, "methods", None) or ()


def _paths(prefix: str) -> set[tuple[str, str]]:
    out = set()
    for path, methods in _all_routes():
        if path.startswith(prefix + "/"):
            for method in methods:
                out.add((method, path[len(prefix):]))
    return out


def test_version_endpoint_under_v1(client):
    r = client.get("/api/v1/version")
    assert r.status_code == 200
    assert r.json()["version"] == APP_VERSION and r.json()["api_version"] == "v1"


def test_every_route_is_under_v1():
    v1 = _paths("/api/v1")
    assert ("POST", "/auth/login") in v1 and ("GET", "/invoices") in v1 and ("GET", "/version") in v1
    assert len(v1) > 50


def test_legacy_alias_matches_v1_when_enabled(client):
    assert settings.legacy_api_enabled  # switched on for the tests in conftest.py (the real default is off)
    legacy = {(m, p) for m, p in _paths("/api") if not p.startswith("/v1/")}
    assert len(legacy) > 50
    assert legacy == _paths("/api/v1")
    assert client.get("/api/version").status_code == 200


def test_openapi_lists_only_v1_paths():
    paths = [p for p in app.openapi()["paths"] if p != "/health"]
    assert paths and all(p.startswith("/api/v1/") for p in paths)


def test_protected_route_still_needs_login_on_v1(client):
    assert client.get("/api/v1/customers").status_code in (401, 403)


def test_body_limits_apply_to_both_prefixes():
    for prefix in ("/api", "/api/v1"):
        assert limit_for(f"{prefix}/admin/restore") == (settings.max_restore_mb + 1) * 1024 * 1024
        assert limit_for(f"{prefix}/store-profile/logo") == (settings.max_logo_mb + 1) * 1024 * 1024
        assert limit_for(f"{prefix}/auth/login") == settings.max_body_kb * 1024


@pytest.mark.parametrize("prefix", ["/api", "/api/v1"])
def test_audit_classifies_both_prefixes(prefix):
    assert _classify("POST", f"{prefix}/invoices/{{invoice_id}}/cancel") == ("INVOICE", "CANCEL")
    assert _classify("PUT", f"{prefix}/store-profile/signature") == ("STORE_PROFILE", "UPDATE_SIGNATURE")
    assert _classify("POST", f"{prefix}/customers") == ("CUSTOMER", "CREATE")
