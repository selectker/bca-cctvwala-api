"""v1.17.5: nosniff / no-store / CSP... on every response. No database needed."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.security_headers import SecurityHeadersMiddleware

client = TestClient(app)  # no lifespan: nothing here reaches the database

EXPECTED = {
    "x-content-type-options": "nosniff",
    "cache-control": "no-store",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
}


def assert_headers(resp):
    for name, value in EXPECTED.items():
        assert resp.headers.get(name) == value, f"{name}: {resp.headers.get(name)!r} on {resp.status_code}"
    assert "strict-transport-security" not in resp.headers  # off unless HSTS_MAX_AGE is set


def test_success_and_error_responses_all_carry_them():
    assert_headers(client.get("/health"))                               # 200
    assert_headers(client.get("/api/v1/version"))                       # 200, versioned
    assert_headers(client.get("/api/v1/customers"))                     # 401, no token
    assert_headers(client.get("/api/v1/does-not-exist"))                # 404
    assert_headers(client.post("/api/v1/auth/login", json={}))          # 422 validation
    assert_headers(client.delete("/health"))                            # 405


def test_a_body_too_large_answer_has_them_too():
    # the 413 comes from BodyLimitMiddleware, which sits inside this one
    big = client.post("/api/v1/auth/login", content=b"x" * 3_000_000, headers={"Content-Type": "application/json"})
    assert big.status_code == 413
    assert_headers(big)


def run_middleware(app_response_headers, *, path="/x", hsts=0):
    """The headers a bare ASGI app's response ends up with after the middleware."""
    seen = {}

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": app_response_headers})
        await send({"type": "http.response.body", "body": b""})

    async def send(message):
        if message["type"] == "http.response.start":
            seen["headers"] = [(k.decode(), v.decode()) for k, v in message["headers"]]

    asyncio.run(SecurityHeadersMiddleware(inner, hsts_max_age=hsts)({"type": "http", "path": path, "headers": []}, None, send))
    return seen["headers"]


def test_a_route_that_chose_its_own_cache_control_keeps_it():
    headers = run_middleware([(b"cache-control", b"private, max-age=31536000, immutable"), (b"x-content-type-options", b"nosniff")])
    names = [k for k, _ in headers]
    assert dict(headers)["cache-control"] == "private, max-age=31536000, immutable"
    assert names.count("cache-control") == 1 and names.count("x-content-type-options") == 1  # no duplicates


def test_docs_pages_get_no_csp_but_the_rest():
    headers = dict(run_middleware([], path="/docs"))
    assert "content-security-policy" not in headers and headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" in dict(run_middleware([], path="/api/v1/customers"))


def test_hsts_only_when_configured():
    assert "strict-transport-security" not in dict(run_middleware([]))
    assert dict(run_middleware([], hsts=31536000))["strict-transport-security"] == "max-age=31536000"


def test_non_http_scopes_pass_through():
    called = {}

    async def inner(scope, receive, send):
        called["scope"] = scope["type"]

    asyncio.run(SecurityHeadersMiddleware(inner)({"type": "lifespan"}, None, None))
    assert called["scope"] == "lifespan"


def test_hsts_setting_is_validated():
    from pydantic import ValidationError
    from tests.test_client_ip import _settings

    assert _settings(hsts_max_age=31536000).hsts_max_age == 31536000
    assert _settings().hsts_max_age == 0
    with pytest.raises(ValidationError):
        _settings(hsts_max_age=-1)
