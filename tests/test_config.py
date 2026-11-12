import pytest

from app.config import MIN_JWT_SECRET_LENGTH, Settings

BASE = dict(db_host="h", db_name="n", db_user="u", db_password="p", r2_account_id="acc", r2_access_key_id="k",
            r2_secret_access_key="s", r2_bucket="b")


def test_jwt_secret_must_be_at_least_32_characters():
    assert MIN_JWT_SECRET_LENGTH == 32
    Settings(_env_file=None, jwt_secret="s" * 32, **BASE)
    for short in ("", "x", "change-me", "s" * 31):
        with pytest.raises(ValueError, match="at least 32"):
            Settings(_env_file=None, jwt_secret=short, **BASE)


def test_token_lifetime_must_be_positive():
    with pytest.raises(ValueError):
        Settings(_env_file=None, jwt_secret="s" * 32, jwt_expires_minutes=0, **BASE)
<<<<<<< HEAD
=======


def test_db_pool_uses_store_timezone():
    """v1.38.5: CURRENT_DATE must follow APP_TIMEZONE (IST), not the RDS session default (UTC)."""
    import inspect

    from app import db

    assert 'server_settings={"timezone": settings.app_timezone}' in inspect.getsource(db.connect_db)
>>>>>>> dc2fcd2 (Release v1.38.5)
