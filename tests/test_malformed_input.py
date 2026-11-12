"""v1.17.4: input the database can't take (bad IDs, huge offsets, NUL bytes...) is a 4xx, never a 500 + stack trace.

Same setup as test_login_protection.py: a real PostgreSQL; the admin / owner are bootstrapped when missing.
"""
import os
import re

import pytest
from fastapi.testclient import TestClient

from app.main import app

ADMIN = {"phone": "9000000000", "password": "adminpass1"}
OWNER = {"phone": "9000000001", "password": "ownerpass1"}
NIL = "00000000-0000-0000-0000-000000000000"


@pytest.fixture(scope="module")
def client():
    with TestClient(app, raise_server_exceptions=False) as c:  # a 500 must show up as a 500, not as a raised error
        c.headers["X-Setup-Token"] = os.environ["SETUP_TOKEN"]
        yield c


def H(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def tokens(client):
    state = client.get("/api/v1/auth/bootstrap-status").json()
    if state["needs_admin_bootstrap"]:
        assert client.post("/api/v1/auth/bootstrap-admin", json={"name": "Admin", **ADMIN}).status_code == 201
    if client.get("/api/v1/auth/bootstrap-status").json()["needs_owner_bootstrap"]:
        assert client.post("/api/v1/auth/bootstrap-owner", json={"name": "Owner", **OWNER}).status_code == 201
    owner = client.post("/api/v1/auth/login", json=OWNER).json()["token"]
    admin = client.post("/api/v1/auth/login", json=ADMIN).json()["token"]
    return {"owner": owner, "admin": admin}


def _get_routes_with_a_path_id():
    paths = []
    spec = app.openapi()["paths"]
    for path, ops in spec.items():
        if path.startswith("/api/v1/") and "{" in path and "get" in ops and not path.startswith("/api/v1/admin"):
            paths.append(path)
    return sorted(paths)


@pytest.mark.parametrize("bad_id", ["not-a-uuid", "1", "%00", "99999999999999999999", "'; DROP TABLE users;--"])
def test_an_id_that_is_not_a_uuid_is_a_404_on_every_get_route(client, tokens, bad_id):
    for path in _get_routes_with_a_path_id():
        if "images" in path or "lookup" in path:  # ticket photo / barcode lookup take other kinds of value
            continue
        url = re.sub(r"\{[^}]+\}", bad_id, path)
        r = client.get(url, headers=H(tokens["owner"]))
        assert r.status_code < 500, f"{path} with {bad_id!r}: {r.status_code} {r.text}"
        assert r.status_code in (400, 404, 405, 422), f"{path} with {bad_id!r}: {r.status_code}"


def test_bad_id_on_writes_is_a_4xx_too(client, tokens):
    for method, path in [("PUT", "/customers/{}"), ("DELETE", "/customers/{}"), ("DELETE", "/products/{}"),
                         ("POST", "/invoices/{}/cancel"), ("POST", "/warranties/{}/claim"), ("POST", "/auth/users/{}/reset-pin")]:
        r = client.request(method, "/api/v1" + path.format("not-a-uuid"), headers=H(tokens["owner"]), json={})
        assert 400 <= r.status_code < 500, f"{method} {path}: {r.status_code} {r.text}"


def test_the_error_does_not_reveal_database_details(client, tokens):
    r = client.get("/api/v1/customers/not-a-uuid", headers=H(tokens["owner"]))
    assert r.status_code == 404 and r.json() == {"error": "Not found"}


@pytest.mark.parametrize("params", [
    {"offset": "99999999999999999999"}, {"offset": "100000001"}, {"offset": "-1"}, {"limit": "0"}, {"limit": "501"},
    {"search": "\x00"}, {"search": "a\x00b"},
])
def test_list_paging_and_search_values_are_validated(client, tokens, params):
    for path in ("/customers", "/invoices", "/products", "/purchases", "/quotations", "/suppliers", "/warranties",
                 "/service-tickets", "/amc", "/audit-logs"):
        r = client.get("/api/v1" + path, headers=H(tokens["owner"]), params=params)
        assert r.status_code == 422, f"{path} {params}: {r.status_code} {r.text}"


def test_top_products_limit_is_bounded(client, tokens):
    for limit in ("-1", "0", "101", "abc"):
        assert client.get("/api/v1/reports/top-products", headers=H(tokens["owner"]), params={"limit": limit}).status_code == 422
    assert client.get("/api/v1/reports/top-products", headers=H(tokens["owner"]), params={"limit": "100"}).status_code == 200


def test_a_nul_byte_in_other_query_filters_is_a_4xx(client, tokens):
    for path, params in [("/audit-logs", {"action": "\x00"}), ("/audit-logs", {"entity_type": "\x00"}),
                         ("/invoices", {"payment_status": "\x00"}), ("/service-tickets", {"status": "\x00"}),
                         ("/warranties", {"status": "\x00"}), ("/amc", {"status": "\x00"})]:
        r = client.get("/api/v1" + path, headers=H(tokens["owner"]), params=params)
        assert r.status_code < 500, f"{path} {params}: {r.status_code} {r.text}"


@pytest.mark.parametrize("value", [None, "", "1234", "12345678"])
def test_product_hsn_code_null_and_blank_are_accepted(client, tokens, value):
    # null / "" used to be a TypeError inside pydantic (a 500): the length check ran after the blank became None
    body = {"name": f"HSN {value!r}", "unit": "PCS", "gst_rate": 18, "selling_price": 100, "hsn_code": value}
    r = client.post("/api/v1/products", headers=H(tokens["owner"]), json=body)
    assert r.status_code == 201, r.text
    assert r.json()["hsn_code"] in (None, value)
    pid = r.json()["id"]
    put = client.put(f"/api/v1/products/{pid}", headers=H(tokens["owner"]), json={**body, "hsn_code": value, "reorder_level": 5})
    assert put.status_code == 200, put.text


@pytest.mark.parametrize("value", ["12", "abcd", "123456789", "1" * 11])
def test_product_hsn_code_still_rejects_bad_values(client, tokens, value):
    body = {"name": "HSN bad", "unit": "PCS", "gst_rate": 18, "selling_price": 100, "hsn_code": value}
    r = client.post("/api/v1/products", headers=H(tokens["owner"]), json=body)
    assert r.status_code == (201 if value == "12" else 422), r.text
