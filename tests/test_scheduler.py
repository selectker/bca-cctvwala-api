"""v1.34.0: daily backup scheduler and IST time. No database needed."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.scheduler import next_run_after
from app.utils import backup as bk
from app.utils.dates import now_local

IST = ZoneInfo("Asia/Kolkata")
BASE = dict(db_host="h", db_name="n", db_user="u", db_password="p", jwt_secret="s" * 32,
            r2_account_id="a", r2_access_key_id="k", r2_secret_access_key="s", r2_bucket="b")


def test_next_run_is_the_coming_midnight_ist():
    now = datetime(2026, 11, 8, 18, 30, tzinfo=IST)
    assert next_run_after(now, 0, 0) == datetime(2026, 11, 9, 0, 0, tzinfo=IST)


def test_next_run_later_today_when_the_time_has_not_come():
    now = datetime(2026, 11, 8, 1, 0, tzinfo=IST)
    assert next_run_after(now, 2, 30) == datetime(2026, 11, 8, 2, 30, tzinfo=IST)


def test_next_run_is_strictly_in_the_future():
    exactly = datetime(2026, 11, 9, 0, 0, tzinfo=IST)
    assert next_run_after(exactly, 0, 0) == datetime(2026, 11, 10, 0, 0, tzinfo=IST)


def test_next_run_follows_ist_even_when_now_is_given_in_utc():
    utc_now = datetime(2026, 11, 8, 18, 30, tzinfo=timezone.utc)  # 00:00 IST on the 9th, exactly
    nxt = next_run_after(utc_now.astimezone(IST), 0, 0)
    assert nxt == datetime(2026, 11, 10, 0, 0, tzinfo=IST)
    assert nxt - utc_now.astimezone(IST) == timedelta(days=1)


def test_now_local_is_ist():
    assert now_local().utcoffset() == timedelta(hours=5, minutes=30)


def test_backup_names_use_store_time():
    name = bk.new_backup_name(False)
    assert bk.BACKUP_NAME_RE.match(name)
    stamp = datetime.strptime(name.split("-", 2)[2].split(".")[0], "%Y%m%d-%H%M%S").replace(tzinfo=IST)
    assert abs((now_local() - stamp).total_seconds()) < 5
    assert bk.SNAPSHOT_RE.match(bk.new_snapshot_name(True))


def test_schedule_settings(monkeypatch):
    monkeypatch.delenv("BACKUP_SCHEDULE_ENABLED", raising=False)  # conftest turns it off for the test run
    s = Settings(_env_file=None, **BASE)
    assert s.backup_schedule_enabled and s.backup_schedule_hm == (0, 0) and s.app_timezone == "Asia/Kolkata"
    assert Settings(_env_file=None, backup_schedule_time="02:15", **BASE).backup_schedule_hm == (2, 15)
    for bad in ("24:00", "0:00", "midnight", "12:60", ""):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, backup_schedule_time=bad, **BASE)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_timezone="Mars/Olympus", **BASE)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, backup_schedule_retries=-1, **BASE)
