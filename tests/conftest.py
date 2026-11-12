import os

import pytest

# Must be set before app.config is imported (settings are read once at import time).
os.environ.setdefault("SETUP_TOKEN", "test-setup-token-0123456789")
# The database settings are required by app.config. On a machine without them (a clean checkout / CI without a .env)
# use placeholders so the tests that don't touch a database can still run; the end-to-end tests need a real, empty
# PostgreSQL (see README 2m) and fail with a clear connection error against these. Environment variables win over .env
# in pydantic-settings, so a developer's own .env values are only replaced when they are really absent.
from dotenv import dotenv_values  # noqa: E402

_dotenv = dotenv_values(".env")
for _name, _placeholder in (("DB_HOST", "localhost"), ("DB_NAME", "test"), ("DB_USER", "test"), ("DB_PASSWORD", "test")):
    if _name not in _dotenv:
        os.environ.setdefault(_name, _placeholder)
# Most tests sign in dozens of times from one IP; the real limit is exercised in test_login_protection.py.
os.environ.setdefault("LOGIN_RATE_LIMIT", "100000/minute")
# The daily backup scheduler (v1.34.0) only wakes at midnight; keep it out of the test run. test_scheduler.py tests it directly.
os.environ.setdefault("BACKUP_SCHEDULE_ENABLED", "false")
# The old /api/... paths are off by default since 1.17.1; test_api_versioning.py checks that they still match /api/v1.
os.environ.setdefault("LEGACY_API_ENABLED", "true")
# The app refuses a JWT_SECRET shorter than 32 characters (a short one left over from an old setup is replaced here).
if len(os.environ.get("JWT_SECRET", "")) < 32:
    os.environ["JWT_SECRET"] = "test-only-secret-" + "x7Qp2LmZ" * 4
# R2 is required by the app; the tests never reach the network (see the fake client below).
os.environ.setdefault("R2_ACCOUNT_ID", "test-account")
os.environ.setdefault("R2_ACCESS_KEY_ID", "test-key")
os.environ.setdefault("R2_SECRET_ACCESS_KEY", "test-secret")
os.environ.setdefault("R2_BUCKET", "test-photos")
# Backups are encrypted with this key (at least 32 characters); the real one lives only in the server's .env.
os.environ.setdefault("BACKUP_ENCRYPTION_KEY", "test-only-backup-key-" + "k9Zr4TpW" * 4)


@pytest.fixture(autouse=True, scope="session")
def fake_r2():
    """Every test talks to an in-memory R2 instead of Cloudflare."""
    from app.utils import storage
    from tests.fakes3 import FakeS3

    fake = FakeS3()
    original = storage._client
    storage._client = lambda: fake
    yield fake
    storage._client = original
