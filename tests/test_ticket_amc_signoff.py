"""AMC-covered tickets and customer sign-off (v1.29.0): the job card without a database, then the endpoints end to end."""
import asyncio
import base64
import os
import re
import time
from datetime import date, datetime
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.main import app
from app.utils.pdf import build_ticket_pdf
from tests.helpers import first_login, raw_connect

OWNER = {"phone": "9000000001", "password": "ownerpass1"}
ADMIN = {"phone": "9000000000", "password": "adminpass1"}
STORE = {"store_name": "Test Store", "address": "1 Main Rd", "phone": "99999"}
TICKET = {
    "ticket_no": "TKT/2026-27/0001", "service_type": "AMC", "status": "ATTENDED", "title": "Camera 3 offline",
    "site_address": "12 MG Road", "customer_name": "Asha", "created_at": datetime(2026, 10, 1, 10, 0),
    "amc_contract_no": "AMC/2026-27/0001",
}


def signature_png(size=(300, 120)) -> bytes:
    img = Image.new("RGBA", size, (255, 255, 255, 0))
    ImageDraw.Draw(img).line([(10, 100), (80, 20), (150, 90), (240, 30)], fill=(10, 10, 120, 255), width=6)
    out = BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode()


def images_in(pdf: bytes) -> int:
    return len(re.findall(rb"/Subtype /Image", pdf))


# ----------------------------------------------------------------------------- job card (no database)
def test_job_card_prints_the_customer_signoff():
    signed = {"obtained": True, "customer_name": "Asha Rao", "signed_at": datetime(2026, 10, 2), "acknowledged": False}
    blank = build_ticket_pdf(STORE, TICKET, [], False)
    assert blank.startswith(b"%PDF") and images_in(blank) == 0

    with_signature = build_ticket_pdf(STORE, TICKET, [], False, None, signed, signature_png())
    # the signature is drawn in the customer's box (a transparent PNG is an image plus its mask)
    assert with_signature.startswith(b"%PDF") and images_in(with_signature) >= 1

    ack_only = build_ticket_pdf(STORE, TICKET, [], False, None, {**signed, "acknowledged": True}, None)
    assert ack_only.startswith(b"%PDF") and images_in(ack_only) == 0

    declined = {"obtained": False, "not_obtained_reason": "Customer not at site " * 10}
    assert build_ticket_pdf(STORE, TICKET, [], False, None, declined, None).startswith(b"%PDF")


def test_job_card_survives_a_bad_signature_image_and_shows_the_amc_contract():
    signed = {"obtained": True, "customer_name": "A", "signed_at": datetime(2026, 10, 2)}
    assert build_ticket_pdf(STORE, TICKET, [], True, None, signed, b"garbage").startswith(b"%PDF")
    assert build_ticket_pdf(STORE, {**TICKET, "amc_contract_no": None}, [], True).startswith(b"%PDF")
    # an AMC ticket has no charge section even for the owner
    assert build_ticket_pdf(STORE, {**TICKET, "charge_amount": 1500, "gst_rate": 18}, [], True).startswith(b"%PDF")


# ----------------------------------------------------------------------------- end to end (needs PostgreSQL, see README 2m)
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        c.headers["X-Setup-Token"] = os.environ["SETUP_TOKEN"]
        yield c


def H(token):
    return {"Authorization": f"Bearer {token}"}


def ok(resp, code=200):
    assert resp.status_code == code, f"{resp.status_code}: {resp.text}"
    return resp.json()


def photo():
    buf = BytesIO()
    Image.new("RGB", (160, 120), (20, 120, 60)).save(buf, "JPEG")
    return ("images", ("done.jpg", buf.getvalue(), "image/jpeg"))


@pytest.fixture(scope="module")
def world(client):
    if ok(client.get("/api/v1/auth/bootstrap-status"))["needs_admin_bootstrap"]:
        ok(client.post("/api/v1/auth/bootstrap-admin", json={"name": "Admin", **ADMIN}), 201)
    if ok(client.get("/api/v1/auth/bootstrap-status"))["needs_owner_bootstrap"]:
        ok(client.post("/api/v1/auth/bootstrap-owner", json={"name": "Owner", **OWNER}), 201)
    ot = ok(client.post("/api/v1/auth/login", json=OWNER))["token"]
    sfx = str(int(time.time() * 1000))[-8:]
    tech = ok(client.post("/api/v1/auth/users", headers=H(ot), json={"name": "Sign Tech", "phone": f"93{sfx}", "role": "TECHNICIAN"}), 201)
    other = ok(client.post("/api/v1/auth/users", headers=H(ot), json={"name": "Sign Other", "phone": f"92{sfx}", "role": "STAFF"}), 201)
    cust = ok(client.post("/api/v1/customers", headers=H(ot), json={"name": "AMC Ticket Cust", "phone": f"91{sfx}", "address": "7 Hill Rd"}), 201)
    rival = ok(client.post("/api/v1/customers", headers=H(ot), json={"name": "Rival Cust", "phone": f"90{sfx}"}), 201)
    today = date.today()
    contract = ok(client.post("/api/v1/amc", headers=H(ot), json={
        "customer_id": cust["id"], "site_address": "Warehouse 9", "start_date": f"{today.year - 1}-01-01",
        "end_date": f"{today.year + 1}-12-31", "amount": 6000}), 201)
    return {"ot": ot, "tech": tech, "t1": first_login(client, tech), "t2": first_login(client, other),
            "cust": cust, "rival": rival, "amc": contract}


def new_amc_ticket(client, w, **extra):
    body = {"customer_id": w["cust"]["id"], "service_type": "AMC", "amc_id": w["amc"]["id"], "title": "Camera 3 offline",
            "assigned_to": w["tech"]["id"], **extra}
    return ok(client.post("/api/v1/service-tickets", headers=H(w["ot"]), json=body), 201)


# ---- 1. AMC link ----
def test_raise_a_ticket_under_an_amc_contract(client, world):
    w, ot = world, world["ot"]
    t = new_amc_ticket(client, w)
    assert t["service_type"] == "AMC" and t["amc_id"] == w["amc"]["id"] and t["amc_contract_no"] == w["amc"]["contract_no"]
    assert t["charge_amount"] is None and t["site_address"] == "Warehouse 9"  # the contract's site, not the customer's address
    detail = ok(client.get(f"/api/v1/service-tickets/{t['id']}", headers=H(ot)))
    assert w["amc"]["contract_no"] in detail["events"][0]["note"] and detail["customer_signoff"] is None

    # the contract screen sees it, and so does the ticket list filter
    contract = ok(client.get(f"/api/v1/amc/{w['amc']['id']}", headers=H(ot)))
    assert contract["service_ticket_count"] >= 1 and t["id"] in [x["id"] for x in contract["service_tickets"]]
    assert contract["service_tickets"][0]["amc_contract_no"] == w["amc"]["contract_no"]
    listed = ok(client.get("/api/v1/service-tickets", headers=H(ot), params={"amc_id": w["amc"]["id"]}))
    assert t["id"] in [x["id"] for x in listed]
    assert all(x["service_type"] == "AMC" for x in ok(client.get("/api/v1/service-tickets", headers=H(ot), params={"service_type": "AMC"})))
    assert client.get("/api/v1/service-tickets", headers=H(ot), params={"amc_id": "nope"}).status_code == 400
    assert client.get("/api/v1/service-tickets", headers=H(ot), params={"service_type": "FREE"}).status_code == 400

    # the technician sees which contract it is under (never a price), and the report has an AMC bucket
    seen = ok(client.get(f"/api/v1/service-tickets/{t['id']}", headers=H(w["t1"])))
    assert seen["amc_contract_no"] == w["amc"]["contract_no"] and "charge_amount" not in seen
    today = date.today().isoformat()
    assert ok(client.get("/api/v1/reports/service-tickets", headers=H(ot), params={"from": today, "to": today}))["by_type"]["AMC"] >= 1
    assert client.get(f"/api/v1/service-tickets/{t['id']}/pdf", headers=H(ot)).content.startswith(b"%PDF")


def test_raise_from_the_contract_endpoint(client, world):
    w, ot = world, world["ot"]
    url = f"/api/v1/amc/{w['amc']['id']}/tickets"
    t = ok(client.post(url, headers=H(ot), json={"title": "Annual check extra visit", "assigned_to": w["tech"]["id"]}), 201)
    assert t["service_type"] == "AMC" and t["customer_id"] == w["cust"]["id"] and t["amc_id"] == w["amc"]["id"]
    assert t["site_address"] == "Warehouse 9" and t["charge_amount"] is None
    assert ok(client.post(url, headers=H(ot), json={"title": "Other site", "site_address": "Shed 2"}), 201)["site_address"] == "Shed 2"
    assert client.post(url, headers=H(ot), json={"title": ""}).status_code == 422
    assert client.post(url, headers=H(w["t1"]), json={"title": "x"}).status_code == 403
    assert client.post(url, json={"title": "x"}).status_code == 401
    assert client.post("/api/v1/amc/00000000-0000-0000-0000-000000000000/tickets", headers=H(ot), json={"title": "x"}).status_code == 404
    assert client.post("/api/v1/amc/not-a-uuid/tickets", headers=H(ot), json={"title": "x"}).status_code == 404
    # the audit log files it under the new ticket, not the contract
    logs = ok(client.get("/api/v1/audit-logs", headers=H(ot), params={"entity_type": "SERVICE_TICKET", "entity_id": t["id"]}))
    assert logs and logs[0]["action"] == "CREATE"


def test_amc_cover_rules(client, world):
    w, ot = world, world["ot"]
    base = {"customer_id": w["cust"]["id"], "title": "x"}
    post = lambda **kw: client.post("/api/v1/service-tickets", headers=H(ot), json={**base, **kw})  # noqa: E731
    assert post(service_type="AMC").status_code == 400                                              # no contract named
    assert post(service_type="PAID", amc_id=w["amc"]["id"]).status_code == 400                     # contract on a non-AMC ticket
    assert post(service_type="IN_WARRANTY", amc_id=w["amc"]["id"]).status_code == 400
    assert post(service_type="AMC", amc_id="not-a-uuid").status_code == 400
    assert post(service_type="AMC", amc_id="00000000-0000-0000-0000-000000000000").status_code == 404
    assert post(service_type="AMC", amc_id=w["amc"]["id"], customer_id=w["rival"]["id"]).status_code == 400  # someone else's contract

    today = date.today()
    expired = ok(client.post("/api/v1/amc", headers=H(ot), json={"customer_id": w["cust"]["id"], "start_date": "2020-01-01", "end_date": "2020-12-31", "amount": 1}), 201)
    future = ok(client.post("/api/v1/amc", headers=H(ot), json={"customer_id": w["cust"]["id"], "start_date": f"{today.year + 2}-01-01", "end_date": f"{today.year + 3}-01-01", "amount": 1}), 201)
    cancelled = ok(client.post("/api/v1/amc", headers=H(ot), json={"customer_id": w["cust"]["id"], "start_date": f"{today.year - 1}-01-01", "end_date": f"{today.year + 1}-01-01", "amount": 1}), 201)
    ok(client.put(f"/api/v1/amc/{cancelled['id']}/status", headers=H(ot), json={"status": "CANCELLED"}))
    for contract, word in ((expired, "ended"), (future, "starts"), (cancelled, "cancelled")):
        r = post(service_type="AMC", amc_id=contract["id"])
        assert r.status_code == 400 and word in r.json()["error"], r.text
        assert client.post(f"/api/v1/amc/{contract['id']}/tickets", headers=H(ot), json={"title": "x"}).status_code == 400


def test_changing_the_type_of_a_ticket_keeps_the_contract_link_consistent(client, world):
    w, ot = world, world["ot"]
    paid = ok(client.post("/api/v1/service-tickets", headers=H(ot), json={
        "customer_id": w["cust"]["id"], "service_type": "PAID", "title": "Move a camera", "charge_amount": 500}), 201)
    url = f"/api/v1/service-tickets/{paid['id']}"
    assert client.put(url, headers=H(ot), json={"service_type": "AMC"}).status_code == 400                        # which contract?
    assert client.put(url, headers=H(ot), json={"amc_id": w["amc"]["id"]}).status_code == 400                      # still PAID
    covered = ok(client.put(url, headers=H(ot), json={"service_type": "AMC", "amc_id": w["amc"]["id"]}))
    assert covered["service_type"] == "AMC" and covered["amc_id"] == w["amc"]["id"] and covered["charge_amount"] is None
    assert "AMC covered" in ok(client.get(url, headers=H(ot)))["events"][-1]["note"]
    assert ok(client.put(url, headers=H(ot), json={"title": "Move two cameras"}))["amc_id"] == w["amc"]["id"]   # untouched
    back = ok(client.put(url, headers=H(ot), json={"service_type": "PAID", "charge_amount": 700}))
    assert back["service_type"] == "PAID" and back["amc_id"] is None and back["amc_contract_no"] is None


def test_an_amc_ticket_is_never_billed(client, world):
    w, ot = world, world["ot"]
    t = new_amc_ticket(client, w)
    ok(client.post(f"/api/v1/service-tickets/{t['id']}/attend", headers=H(w["t1"]), files=[photo()]))
    r = client.post("/api/v1/invoices", headers=H(ot), json={
        "customer_id": w["cust"]["id"], "invoice_type": "SERVICE", "service_ticket_ids": [t["id"]],
        "items": [{"description": "Visit", "quantity": 1, "unit_price": 100, "gst_rate": 18}]})
    assert r.status_code == 400 and "AMC" in r.json()["error"], r.text
    assert t["id"] not in [x["id"] for x in ok(client.get("/api/v1/service-tickets", headers=H(ot), params={"billable": "true"}))]
    hist = ok(client.get(f"/api/v1/customers/{w['cust']['id']}/history", headers=H(ot)))
    assert any(x["service_type"] == "AMC" and x["amc_contract_no"] == w["amc"]["contract_no"] for x in hist["service_tickets"])


def test_the_database_refuses_a_ticket_that_disagrees_with_its_contract(client, world):
    w = world

    async def attempt():
        conn = await raw_connect()
        try:
            for service_type, amc_id in (("AMC", None), ("PAID", w["amc"]["id"])):
                with pytest.raises(Exception, match="service_tickets_amc_link_check"):
                    await conn.execute(
                        "INSERT INTO service_tickets (ticket_no, customer_id, service_type, title, amc_id) VALUES ($1,$2,$3,'x',$4::uuid)",
                        f"TKT/BAD/{service_type}", w["cust"]["id"], service_type, amc_id)
        finally:
            await conn.close()

    asyncio.run(attempt())


# ---- 2. customer sign-off ----
def attend(client, token, ticket_id, **data):
    files = [photo()]
    return client.post(f"/api/v1/service-tickets/{ticket_id}/attend", headers=H(token), files=files, data=data)


def test_attend_with_the_customers_signature(client, world, fake_r2):
    w, ot = world, world["ot"]
    t = new_amc_ticket(client, w)
    png = signature_png()
    before = set(fake_r2.objects)
    done = ok(attend(client, w["t1"], t["id"], remarks="All cameras back", customer_name="Asha Rao", customer_signature=data_url(png)))
    so = done["customer_signoff"]
    assert done["status"] == "ATTENDED" and done["customer_signed_off"] is True
    assert so["obtained"] and so["has_signature"] and so["customer_name"] == "Asha Rao" and so["captured_by_name"] == "Sign Tech"
    assert "signature_key" not in so and "signature_key" not in str(done)

    new = [k for k in set(fake_r2.objects) - before if "/signoff/" in k]
    assert len(new) == 1 and new[0].endswith(".png") and fake_r2.objects[new[0]]["body"].startswith(b"\x89PNG")

    detail = ok(client.get(f"/api/v1/service-tickets/{t['id']}", headers=H(ot)))
    attended = [e for e in detail["events"] if e["event_type"] == "ATTENDED"][0]
    assert attended["signoff"]["id"] == so["id"] and detail["customer_signoff"]["id"] == so["id"]
    assert [e["signoff"] for e in detail["events"] if e["event_type"] == "CREATED"] == [None]

    # the image: owner and the technician see it; a login that can't see the ticket gets a 404
    sig_url = f"/api/v1/service-tickets/{t['id']}/signoffs/{so['id']}/signature"
    for token in (ot, w["t1"]):
        r = client.get(sig_url, headers=H(token))
        assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content.startswith(b"\x89PNG")
    assert client.get(sig_url, headers=H(w["t2"])).status_code == 404
    assert client.get(sig_url).status_code == 401
    assert client.get(f"/api/v1/service-tickets/{t['id']}/signoffs/{t['id']}/signature", headers=H(ot)).status_code == 404

    # the job card carries it; the list tells whether a ticket is signed
    assert images_in(client.get(f"/api/v1/service-tickets/{t['id']}/pdf", headers=H(ot)).content) >= 1
    row = [x for x in ok(client.get("/api/v1/service-tickets", headers=H(ot), params={"search": t["ticket_no"]}))][0]
    assert row["customer_signed_off"] is True
    # the audit log never holds the picture
    logs = client.get("/api/v1/audit-logs", headers=H(ot), params={"entity_type": "SERVICE_TICKET", "entity_id": t["id"]}).text
    assert "iVBOR" not in logs and "customer_signature" not in logs


def test_sign_off_is_optional_by_default_and_can_be_made_compulsory(client, world, monkeypatch):
    from app.config import settings

    w = world
    plain = new_amc_ticket(client, w)
    done = ok(attend(client, w["t1"], plain["id"]))
    assert done["status"] == "ATTENDED" and done["customer_signoff"] is None and done["customer_signed_off"] is False
    unsigned_card = client.get(f"/api/v1/service-tickets/{plain['id']}/pdf", headers=H(w["ot"])).content
    assert unsigned_card.startswith(b"%PDF") and images_in(unsigned_card) == 0

    monkeypatch.setattr(settings, "require_customer_signoff", True)
    t = new_amc_ticket(client, w)
    r = attend(client, w["t1"], t["id"])
    assert r.status_code == 400 and "sign" in r.json()["error"].lower()
    assert ok(client.get(f"/api/v1/service-tickets/{t['id']}", headers=H(w["ot"])))["status"] == "OPEN"  # nothing was recorded
    assert attend(client, w["t1"], t["id"], customer_name="Asha").status_code == 400                       # a name alone is not a sign-off
    declined = ok(attend(client, w["t1"], t["id"], no_signoff_reason="Customer not at site"))
    assert declined["customer_signoff"]["obtained"] is False and declined["customer_signoff"]["not_obtained_reason"] == "Customer not at site"
    assert declined["customer_signed_off"] is False

    t2 = new_amc_ticket(client, w)
    acked = ok(attend(client, w["t1"], t2["id"], customer_name="Asha Rao", customer_acknowledged="true"))
    assert acked["customer_signoff"]["obtained"] and acked["customer_signoff"]["acknowledged"] and not acked["customer_signoff"]["has_signature"]
    assert client.get(f"/api/v1/service-tickets/{t2['id']}/signoffs/{acked['customer_signoff']['id']}/signature", headers=H(w["ot"])).status_code == 404


def test_sign_off_input_is_checked(client, world, fake_r2):
    w = world
    t = new_amc_ticket(client, w)
    before = set(fake_r2.objects)
    sig = data_url(signature_png())
    bad = [
        {"customer_signature": sig},                                                                   # who signed?
        {"customer_signature": sig, "customer_name": "  "},
        {"customer_acknowledged": "true"},
        {"customer_signature": sig, "customer_name": "A", "no_signoff_reason": "also this"},          # both
        {"customer_signature": "!!!not base64!!!", "customer_name": "A"},
        {"customer_signature": base64.b64encode(b"just text").decode(), "customer_name": "A"},        # not an image
        {"customer_signature": "A" * 700_001, "customer_name": "A"},                                  # over the field cap
        {"customer_name": "A" * 101, "customer_acknowledged": "true"},
    ]
    for data in bad:
        r = attend(client, w["t1"], t["id"], **data)
        assert r.status_code in (400, 422), (list(data), r.status_code, r.text[:200])
    assert set(fake_r2.objects) == before                                                              # nothing was uploaded
    assert ok(client.get(f"/api/v1/service-tickets/{t['id']}", headers=H(w["ot"])))["status"] == "OPEN"
    # a technician who can't see the ticket, and the owner, can't attend it
    assert attend(client, w["t2"], t["id"], customer_name="A", customer_acknowledged="true").status_code == 404
    assert attend(client, w["ot"], t["id"], customer_name="A", customer_acknowledged="true").status_code == 403
    # a ticket that is already attended is refused before anything is stored
    ok(attend(client, w["t1"], t["id"], customer_name="A", customer_signature=sig))
    mark = set(fake_r2.objects)
    assert attend(client, w["t1"], t["id"], customer_name="B", customer_signature=sig).status_code == 409
    assert set(fake_r2.objects) == mark


def test_sign_off_added_later_and_after_a_reopen(client, world):
    w, ot = world, world["ot"]
    t = new_amc_ticket(client, w)
    ok(attend(client, w["t1"], t["id"], no_signoff_reason="Customer on the phone only"))
    add = f"/api/v1/service-tickets/{t['id']}/addendum"
    assert client.post(add, headers=H(w["t1"]), data={}).status_code == 400                                       # nothing to add
    sig = data_url(signature_png())
    later = ok(client.post(add, headers=H(w["t1"]), data={"customer_name": "Asha Rao", "customer_signature": sig}))
    assert later["customer_signoff"]["obtained"] and later["customer_signed_off"] is True and later["status"] == "ATTENDED"
    again = client.post(add, headers=H(w["t1"]), data={"customer_name": "Asha Rao", "customer_signature": sig})
    assert again.status_code == 409                                                                               # already signed
    assert ok(client.post(add, headers=H(w["t1"]), data={"remarks": "Left a spare cable"}))["customer_signed_off"] is True
    detail = ok(client.get(f"/api/v1/service-tickets/{t['id']}", headers=H(ot)))
    assert [bool(e["signoff"]) for e in detail["events"] if e["event_type"] in ("ATTENDED", "ADDENDUM")] == [True, True, False]

    # the owner sends it back: the new visit needs its own sign-off; the earlier one stays on the timeline
    ok(client.post(f"/api/v1/service-tickets/{t['id']}/reopen", headers=H(ot), json={"reason": "Camera 3 still offline"}))
    second = ok(attend(client, w["t1"], t["id"], remarks="Replaced the camera"))
    assert second["customer_signed_off"] is False and second["customer_signoff"] is None
    detail = ok(client.get(f"/api/v1/service-tickets/{t['id']}", headers=H(ot)))
    assert sum(1 for e in detail["events"] if e["signoff"]) == 2 and detail["customer_signoff"] is None
    signed_again = ok(client.post(add, headers=H(w["t1"]), data={"customer_name": "Asha Rao", "customer_acknowledged": "true"}))
    assert signed_again["customer_signed_off"] is True
    ok(client.post(f"/api/v1/service-tickets/{t['id']}/close", headers=H(ot), json={}))
