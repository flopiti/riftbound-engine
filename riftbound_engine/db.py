"""Database connectivity — the seam for moving persistent stores off JSON files.

For now this only establishes (and health-checks) a connection. The engine URL
comes from the ``DATABASE_URL`` env var; with none set it defaults to a local
SQLite file, so nothing external is required to run. Point ``DATABASE_URL`` at
Postgres/MySQL on the server (e.g. ``postgresql://riftbound:pw@localhost:5432/
riftbound``) to use that instead — no code change, just the env var.

Everything here degrades gracefully: if SQLAlchemy (or a DB driver) isn't
installed, or the server is unreachable, ``check_connection`` reports it rather
than raising, so the app keeps running on the file-based stores.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

_ENGINE_ROOT = Path(__file__).resolve().parent.parent


def _normalize_url(url: str) -> str:
    """Make a bare ``postgresql://`` URL use whichever Postgres driver is
    actually installed. SQLAlchemy defaults bare ``postgresql://`` to psycopg2;
    the ``db`` extra ships psycopg (v3), so if psycopg2 is missing we point the
    URL at psycopg v3 (``postgresql+psycopg://``). A URL that already names a
    driver (``postgresql+psycopg2://`` etc.) is left untouched."""
    if url.startswith("postgresql://"):
        try:
            import psycopg2  # noqa: F401
        except ImportError:
            return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def database_url() -> str:
    """The configured connection string — ``DATABASE_URL`` if set, else a local
    SQLite file next to the engine (``riftbound.db``)."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return _normalize_url(url)
    return f"sqlite:///{_ENGINE_ROOT / 'riftbound.db'}"


def _url_scheme(url: str) -> str:
    return url.split(":", 1)[0].split("+", 1)[0] if ":" in url else url


@lru_cache(maxsize=1)
def get_engine():
    """The lazily-created SQLAlchemy Engine (one per process). Raises
    ImportError if SQLAlchemy isn't installed — callers that want to stay
    optional should use :func:`check_connection` instead."""
    from sqlalchemy import create_engine

    # pool_pre_ping avoids handing out a dead connection after the DB restarts.
    return create_engine(database_url(), pool_pre_ping=True, future=True)


def check_connection() -> dict:
    """Try to actually reach the database (runs ``SELECT 1``) and report the
    result. Never raises — returns a dict the /health endpoint can surface:

        {"configured": bool,   # was DATABASE_URL explicitly set?
         "backend": "sqlite|postgresql|mysql|…",
         "connected": bool,
         "error": "<message>"}  # only when not connected
    """
    url = database_url()
    result: dict = {
        "configured": bool(os.environ.get("DATABASE_URL", "").strip()),
        "backend": _url_scheme(url),
        "connected": False,
    }
    try:
        from sqlalchemy import text

        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        result["connected"] = True
    except ImportError as exc:
        result["error"] = f"SQLAlchemy/driver not installed: {exc}"
    except Exception as exc:  # unreachable server, bad credentials, etc.
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result
