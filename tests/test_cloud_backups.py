"""Cloud backups: stored in R2, listed, restorable by name; encrypted only when BACKUP_ENCRYPTION_KEY is set (v1.14.0).
Needs pg_dump / pg_restore on the machine. Runs on an empty database or after the other modules."""
import os

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.limiter import limiter
from app.main import app
from app.utils import storage

ADMIN = {"phone": "9000000000", "password": "adminpass1"}
OWNER = {"phone": "9000000001", "password": "ownerpass1"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        c.headers["X-Setup-Token"] = os.environ["SETUP_TOKEN"]
        limiter.reset()
        yield c


def H(token):
    return {"Authorization": f"Bearer {token}"}


def ok(resp, code=200):
    assert resp.status_code == code, f"{resp.status_code}: {resp.text}"
    return resp.json()


@pytest.fixture()
def admin_token(client):
    state = ok(client.get("/api/v1/auth/bootstrap-status"))
    if state["needs_admin_bootstrap"]:
        ok(client.post("/api/v1/auth/bootstrap-admin", json={"name": "Admin", **ADMIN}), 201)
    if ok(client.get("/api/v1/auth/bootstrap-status"))["needs_owner_bootstrap"]:
        ok(client.post("/api/v1/auth/bootstrap-owner", json={"name": "Owner", **OWNER}), 201)
    return ok(client.post("/api/v1/auth/login", json=ADMIN))["token"]


def test_cloud_backup_with_a_key_is_encrypted_listed_and_restorable(client, admin_token, tmp_path):
    made = ok(client.post("/api/v1/admin/backup/cloud", headers=H(admin_token)))
    assert made["name"].endswith(".dump.enc") and made["key"] == "backups/" + made["name"]
    assert made["name"] in [b["name"] for b in ok(client.get("/api/v1/admin/backups", headers=H(admin_token)))]

    stored = str(tmp_path / "stored")
    storage.download_file(made["key"], stored)
    head = open(stored, "rb").read(64)
    assert head.startswith(b"BCAENC1\n") and b"PGDMP" not in head  # ciphertext, not a readable pg_dump archive

    token = ok(client.post("/api/v1/auth/login", json=ADMIN))["token"]
    restored = ok(client.post(f"/api/v1/admin/backups/{made['name']}/restore?confirm=RESTORE", headers=H(token)))
    assert restored["snapshot"].startswith("pre-restore-") and restored["snapshot"].endswith(".dump.enc")
    token = ok(client.post("/api/v1/auth/login", json=ADMIN))["token"]  # a restore ends every sign-in
    snaps = ok(client.get("/api/v1/admin/snapshots", headers=H(token)))
    assert snaps and snaps[0]["name"].endswith(".dump.enc")  # safety snapshots are encrypted too


def test_without_a_key_backups_are_plain_pg_dump_files(client, admin_token, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "backup_encryption_key", "")
    made = ok(client.post("/api/v1/admin/backup/cloud", headers=H(admin_token)))
    assert made["name"].endswith(".dump") and not made["name"].endswith(".enc")
    stored = str(tmp_path / "stored")
    storage.download_file(made["key"], stored)
    assert open(stored, "rb").read(5) == b"PGDMP"
    restored = ok(client.post(f"/api/v1/admin/backups/{made['name']}/restore?confirm=RESTORE", headers=H(admin_token)))
    assert restored["snapshot"].endswith(".dump")


def test_old_style_backups_are_not_listed_or_restorable(client, admin_token, tmp_path):
    old = tmp_path / "old.bca.gz"
    old.write_bytes(b"\x1f\x8b old json backup")
    name = "bcacctvwala-backup-20200101-000000.bca.gz"
    storage.put_file("backups/" + name, str(old))
    assert name not in [b["name"] for b in ok(client.get("/api/v1/admin/backups", headers=H(admin_token)))]
    assert client.post(f"/api/v1/admin/backups/{name}/restore?confirm=RESTORE", headers=H(admin_token)).status_code == 404
