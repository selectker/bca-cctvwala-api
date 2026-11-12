"""Shared rate limiter. Lives here (not in main.py) so routers can import it without a circular import."""
import math
import re

from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import settings

_LIMIT = re.compile(r"^\s*(\d+)\s*/\s*(.+?)\s*$")


def per_worker(limit: str) -> str:
    """"N/minute" -> the share of it one worker may hand out, so that API_WORKERS workers together allow about N.

    The counters live in each worker's own memory (there is no shared store), so without this a limit of 10/minute
    with 2 workers really allowed up to 20. Rounded up, and never below 1.
    """
    workers = settings.api_workers
    match = _LIMIT.match(limit)
    if workers <= 1 or not match:
        return limit
    return f"{max(1, math.ceil(int(match.group(1)) / workers))}/{match.group(2)}"


def _default_limit() -> str:
    return per_worker("500/15minutes")


def _login_limit() -> str:
    return per_worker(settings.login_rate_limit)  # read per request, so it can be changed without re-importing


# 500 requests / 15 min / IP for everything; credential endpoints get the much tighter shared limit below.
# get_remote_address reads request.client.host, which app/client_ip.py has already set to the real client address.
limiter = Limiter(key_func=get_remote_address, default_limits=[_default_limit])

# One bucket per client IP across ALL credential-checking endpoints, so spreading guesses across
# /auth/login and /auth/login-pin doesn't multiply the allowance.
credential_limit = limiter.shared_limit(_login_limit, scope="credentials")

# The unauthenticated first-run status check: its own bucket (the app calls it once at start-up), same ceiling.
public_limit = limiter.limit(_login_limit)
