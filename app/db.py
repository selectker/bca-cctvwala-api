import logging
import os
import ssl

import asyncpg
from app.config import settings

logger = logging.getLogger("app")

_pool: asyncpg.Pool | None = None


def build_ssl_context() -> ssl.SSLContext | None:
    """TLS settings for the database connection (shared with alembic/env.py).

    DB_SSL=true gives the equivalent of libpq's sslmode=verify-full: the server certificate must chain to the CA
    file in DB_SSL_CA_FILE (and only to it, the system trust store is not used) and must be valid for DB_HOST.
    asyncpg's plain ssl="require" encrypts the connection but never checks who is on the other end.

    Fails at start-up (rather than quietly connecting unverified) when the CA file is missing or unreadable.
    Returns None only when DB_SSL=false.
    """
    if not settings.db_ssl:
        logger.warning("DB_SSL=false: the database connection is NOT encrypted. Use this for local development only.")
        return None
    ca_file = settings.db_ssl_ca_file.strip()
    if not ca_file:
        raise RuntimeError(
            "DB_SSL=true needs DB_SSL_CA_FILE: the path to the RDS CA bundle "
            "(download https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem). "
            "Set DB_SSL=false only for a local development database."
        )
    if not os.path.isfile(ca_file):
        raise RuntimeError(f"DB_SSL_CA_FILE ({ca_file}) does not exist or is not a file")
    try:
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=ca_file)
    except (ssl.SSLError, OSError) as exc:
        raise RuntimeError(f"DB_SSL_CA_FILE ({ca_file}) could not be loaded as a CA bundle: {exc}") from exc
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


async def connect_db() -> None:
    global _pool
    _pool = await asyncpg.create_pool(
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        ssl=build_ssl_context(),
        min_size=2,
        max_size=15,
        # Every connection runs in the store's time zone (IST by default). Without this RDS hands out UTC sessions, so
        # CURRENT_DATE / now()::date roll over at 05:30 IST and yesterday's sales show as "today" until then.
        server_settings={"timezone": settings.app_timezone},
    )
    # v1.13.2 keeps the sign-in lock-out counters in login_failures. Without the table every login would fail with a
    # 500, so refuse to start with a message that says what to do instead.
    async with _pool.acquire() as conn:
        present = await conn.fetchval("SELECT to_regclass('public.login_failures')") is not None
    if not present:  # (closed only after the connection above went back to the pool, or close() would wait forever)
        await _pool.close()
        _pool = None
        raise RuntimeError(
            "The login_failures table is missing. Apply db/migrations/013_login_failures.sql "
            "(or run `alembic upgrade head`) and start the API again."
        )


async def close_db() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialized — did the app start up correctly?")
    return _pool
