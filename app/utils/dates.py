import calendar
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import settings


def add_months(start: date, months: int) -> date:
    """Add a whole number of months to a date, clamping the day to the target month's length
    (e.g. 31 Jan + 1 month -> 28/29 Feb instead of overflowing into March)."""
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def parse_date(value: str | None) -> date | None:
    """Query-string YYYY-MM-DD -> date (asyncpg refuses plain strings for DATE parameters)."""
    if value is None or value == "":
        return None
    from fastapi import HTTPException, status

    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Dates must be YYYY-MM-DD")


def app_tz():
    """The store's time zone (APP_TIMEZONE, Asia/Kolkata by default); UTC if the name is unknown."""
    try:
        return ZoneInfo(settings.app_timezone)
    except ZoneInfoNotFoundError:
        return timezone.utc


def now_local() -> datetime:
    """The current moment as an aware datetime in the store's time zone (IST by default)."""
    return datetime.now(app_tz())


def today_local() -> date:
    """Today's date in the store's time zone (APP_TIMEZONE), not the server's -- a payment entered at
    00:30 IST must not be rejected as in the future just because the server runs on UTC."""
    try:
        tz = ZoneInfo(settings.app_timezone)
    except ZoneInfoNotFoundError:
        tz = timezone.utc
    return datetime.now(tz).date()
