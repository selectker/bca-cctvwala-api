import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

# Reuse the same .env-driven config the API itself uses, so DB credentials
# only ever live in one place (app/config.py), not duplicated in alembic.ini.
from app.config import settings
from app.db import build_ssl_context

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No ORM models in this project (raw asyncpg + hand-written SQL everywhere),
# so there's nothing to autogenerate against. Every migration here is written
# by hand with op.execute(...).
target_metadata = None


def _database_url() -> str:
    return (
        f"postgresql+asyncpg://{settings.db_user}:{settings.db_password}"
        f"@{settings.db_host}:{settings.db_port}/{settings.db_name}"
    )


def _connect_args() -> dict:
    # Same verified TLS (CA file + host name check) as the API itself, see app/db.py.
    context = build_ssl_context()
    return {"ssl": context} if context is not None else {}


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it (`alembic upgrade head --sql`)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = create_async_engine(_database_url(), connect_args=_connect_args())
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
