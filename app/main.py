import logging
import os

import asyncpg
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.config import APP_NAME, APP_VERSION
from app.db import close_db, connect_db
from app.scheduler import start_scheduler, stop_scheduler
from app.utils.dates import now_local
from app.limiter import limiter
from app.utils.gst import AmountTooLarge
from app.routers import admin, amc, audit, auth, credit_notes, customers, debit_notes, expenses, invoices, products, profile, purchases, quotations, reports, store, suppliers, tickets, warranties

logger = logging.getLogger("app")


def _warn_about_proxy_settings() -> None:
    # uvicorn reads FORWARDED_ALLOW_IPS from the environment when --forwarded-allow-ips is not given. '*' makes it
    # believe an X-Forwarded-For header from ANY address, so a client could pick the address it appears to come from.
    if os.environ.get("FORWARDED_ALLOW_IPS", "").strip() == "*":
        logger.warning(
            "FORWARDED_ALLOW_IPS=* lets any client choose its own IP address for rate limits and lock-outs. "
            "Start uvicorn with --no-proxy-headers and list your proxy in TRUSTED_PROXIES instead."
        )
    if settings.api_workers > 1:
        logger.info("Per-IP request limits are split across %s workers (API_WORKERS).", settings.api_workers)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _warn_about_proxy_settings()
    await connect_db()
    scheduler = start_scheduler()
    try:
        yield
    finally:
        await stop_scheduler(scheduler)
        await close_db()


from app.body_limit import BodyLimitMiddleware  # noqa: E402
from app.client_ip import ClientIPMiddleware  # noqa: E402
from app.security_headers import SecurityHeadersMiddleware  # noqa: E402
from app.config import settings  # noqa: E402

_docs = {} if settings.enable_docs else {"docs_url": None, "redoc_url": None, "openapi_url": None}
app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan, **_docs)

# The Android app is not a browser and needs no CORS. Browsers may call the API only from the origins listed in
# CORS_ALLOWED_ORIGINS (comma-separated, e.g. https://app.example.com); empty = no browser origin is allowed.
if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Setup-Token"],
        expose_headers=["X-Total-Count"],
    )

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(BodyLimitMiddleware)  # refuses oversized bodies before anything reads them
app.add_middleware(ClientIPMiddleware)  # the real client address is known to everything below
app.add_middleware(SecurityHeadersMiddleware)  # added last = outermost: even 413 / 429 answers carry the headers


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    # Mirror the Node backend's { "error": "..." } shape instead of FastAPI's default { "detail": "..." }.
    # Keep any headers the error carries (e.g. Retry-After on a 429 lockout).
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail}, headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # Same { "error": "..." } shape, with a readable first-problem message for the app to show.
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(p) for p in first.get("loc", []) if p not in ("body", "query", "path"))
    message = first.get("msg", "Invalid request")
    return JSONResponse(status_code=422, content={"error": f"{field}: {message}" if field else message})


@app.exception_handler(AmountTooLarge)
async def amount_too_large_handler(request: Request, exc: AmountTooLarge):
    return JSONResponse(status_code=400, content={"error": str(exc)})


@app.exception_handler(asyncpg.exceptions.DataError)
async def bad_data_handler(request: Request, exc: asyncpg.exceptions.DataError):
    """Input PostgreSQL / asyncpg can't take (an ID that isn't a UUID, a number out of range, a NUL byte, a negative
    LIMIT...). That is the caller's mistake: 4xx, one log line, no traceback (it used to be a 500 plus a stack trace
    per request, which a script could use to flood the log). The database's own message is never sent to the client."""
    logger.warning("Rejected malformed input on %r %r (%s)", request.method, request.url.path, type(exc).__name__)
    if request.scope.get("path_params") and "invalid UUID" in str(exc):
        return JSONResponse(status_code=404, content={"error": "Not found"})  # such an ID can't exist
    return JSONResponse(status_code=400, content={"error": "Invalid value in the request"})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Never send str(exc) to the client: it can contain SQL, table/column names or file paths.
    # The full traceback goes to the server log instead. (%r: the path is client text, so a newline in it can't forge a log line.)
    logger.error("Unhandled error on %r %r", request.method, request.url.path, exc_info=exc)
    return JSONResponse(status_code=500, content={"error": "Internal server error"})


@app.get("/health")
async def health():
    # Unauthenticated, so no name / version: those would tell a scanner which release (and which known bugs) to try.
    return {"status": "ok", "time": now_local().isoformat()}  # store time (IST), with its +05:30 offset


API_V1_PREFIX = "/api/v1"
LEGACY_API_PREFIX = "/api"  # pre-1.17.0 paths; kept so installed apps keep working (LEGACY_API_ENABLED)


async def version():
    return {"name": APP_NAME, "version": APP_VERSION, "api_version": "v1"}


app.add_api_route(f"{API_V1_PREFIX}/version", version, methods=["GET"])
if settings.legacy_api_enabled:
    app.add_api_route(f"{LEGACY_API_PREFIX}/version", version, methods=["GET"], include_in_schema=False)

_ROUTERS = (auth, profile, customers, suppliers, products, purchases, quotations, invoices, credit_notes, debit_notes, expenses, amc, tickets,
            warranties, reports, store, audit, admin)

for _module in _ROUTERS:
    app.include_router(_module.router, prefix=API_V1_PREFIX)

# The same routes under the old /api prefix, hidden from the OpenAPI docs. Turn off with LEGACY_API_ENABLED=false
# once every app and web client calls /api/v1.
if settings.legacy_api_enabled:
    for _module in _ROUTERS:
        app.include_router(_module.router, prefix=LEGACY_API_PREFIX, include_in_schema=False)
