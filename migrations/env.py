"""Alembic environment.

Two things worth knowing:

* The URL comes from DATABASE_URL, never from alembic.ini. Nothing is
  configured to point at a database by default, so a migration cannot be run
  against the wrong environment by forgetting a flag.
* The async driver is swapped for a sync one here. Alembic runs migrations
  synchronously, and asyncpg cannot serve that; the application keeps its async
  engine and the migration tool gets psycopg. Same database, same URL, one
  string substitution.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env")

from luma.store.models import Base  # noqa: E402

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit(
            "DATABASE_URL is not set.\n"
            "  export DATABASE_URL=postgresql+asyncpg://luma:luma@127.0.0.1:5432/luma"
        )
    # asyncpg drives the app; Alembic needs a synchronous driver.
    return url.replace("+asyncpg", "").replace("+aiosqlite", "")


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it.

    `alembic upgrade head --sql` is how a change gets reviewed, or handed to a
    DBA who will not run a Python tool against production.
    """
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = database_url()
    engine = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Without these, autogenerate silently ignores a changed column type
            # or a changed server default -- the diff looks clean and the
            # migration is wrong.
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
