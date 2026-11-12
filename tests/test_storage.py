"""Storage layer tests against the in-memory fake of the R2 (S3) client: no network, no database."""
import pytest

from app.config import Settings, settings
from app.utils import storage
from tests.fakes3 import FakeS3


@pytest.fixture
def r2(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(settings, "r2_bucket", "bkt")
    monkeypatch.setattr(settings, "r2_backup_bucket", "bkt-private")
    monkeypatch.setattr(storage, "_client", lambda: fake)
    return fake


def test_photo_roundtrip_and_delete(r2):
    storage.put_bytes("service_tickets/t/1.jpg", b"jpegdata", "image/jpeg")
    assert r2.objects["service_tickets/t/1.jpg"]["type"] == "image/jpeg"
    assert storage.get_bytes("service_tickets/t/1.jpg") == b"jpegdata"
    storage.delete_keys(["service_tickets/t/1.jpg", "never/existed.jpg"])  # best effort, never raises
    with pytest.raises(storage.ObjectMissing):
        storage.get_bytes("service_tickets/t/1.jpg")


def test_backup_listing_and_prune(r2, tmp_path):
    f = tmp_path / "b.bca.gz"
    f.write_bytes(b"data")
    for i in range(5):
        storage.put_file(f"backups/bcacctvwala-backup-20260101-00000{i}.bca.gz", str(f))
    storage.put_file("backups/sub/ignored.bca.gz", str(f))
    items = storage.list_objects("backups/")
    assert items[0]["name"].endswith("4.bca.gz")  # newest first
    assert len(items) == 5  # nested keys are not listed
    storage.prune("backups/", 2)
    assert len(storage.list_objects("backups/")) == 2
    out = tmp_path / "dl"
    storage.download_file("backups/" + items[0]["name"], str(out))
    assert out.read_bytes() == b"data"
    with pytest.raises(storage.ObjectMissing):
        storage.download_file("backups/nope.bca.gz", str(tmp_path / "x"))


def test_backups_use_private_bucket(r2, tmp_path):
    f = tmp_path / "b"
    f.write_bytes(b"1")
    storage.put_file("backups/a.gz", str(f))
    storage.put_bytes("service_tickets/t/1.jpg", b"j", "image/jpeg")
    assert r2.buckets == {"bkt-private": {"backups/a.gz"}, "bkt": {"service_tickets/t/1.jpg"}}


def test_config_validation(monkeypatch):
    monkeypatch.delenv("R2_BUCKET", raising=False)  # conftest exports one; the check below needs it truly absent
    base = dict(db_host="h", db_name="n", db_user="u", db_password="p", jwt_secret="x" * 32, r2_account_id="acc",
                r2_access_key_id="k", r2_secret_access_key="s", r2_bucket="b")
    Settings(_env_file=None, **base)
    with pytest.raises(ValueError):  # R2 is mandatory
        Settings(_env_file=None, **{k: v for k, v in base.items() if k != "r2_bucket"})
    assert not hasattr(settings, "r2_public_base_url")  # photos are never public
