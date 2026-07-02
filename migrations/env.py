"""Alembic environment — targets the app's configured database.

The connection comes from ``riftbound_engine.db`` (which reads ``DATABASE_URL``),
so ``alembic upgrade head`` migrates whatever the app uses — Postgres on the
server, or the local SQLite default — with no per-environment config here.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context

# Make the engine package importable when alembic runs from any cwd.
_ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

from riftbound_engine.db import Base, database_url, get_engine, load_dotenv  # noqa: E402
import riftbound_engine.db_models  # noqa: E402,F401  (registers tables on Base.metadata)

load_dotenv()  # honor the engine's .env so `alembic upgrade head` targets the app DB

config = context.config
if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:
        pass

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout (``alembic upgrade head --sql``) without a DB."""
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection from the app's engine."""
    connectable = get_engine()
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # so SQLite can ALTER via table-copy
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
