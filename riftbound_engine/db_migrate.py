"""One-time data import: saved_games.json → the ``saved_games`` table.

Idempotent — re-running upserts by id, so it's safe to run again (and won't
duplicate the autosave). Run AFTER the schema exists (``alembic upgrade head``):

    python -m riftbound_engine.db_migrate            # import saved games
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def import_saved_games(json_path: str | Path | None = None) -> int:
    """Load saved games from the JSON store into the DB, upserting by id.
    Returns the number of games imported. A missing/empty file imports nothing."""
    from . import saved_games as _sg
    from .db import get_sessionmaker
    from .db_models import SavedGameRow

    path = Path(json_path) if json_path is not None else _sg.SAVED_GAMES_PATH
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    games = data.get("games", []) if isinstance(data, dict) else []

    Session = get_sessionmaker()
    count = 0
    with Session() as session:
        for g in games:
            gid = (g.get("id") or "").strip()
            if not gid:
                continue
            updated = g.get("updated_at")
            session.merge(
                SavedGameRow(
                    id=gid,
                    name=g.get("name", ""),
                    description=g.get("description", ""),
                    created_at=float(g.get("created_at") or 0.0),
                    updated_at=float(updated) if updated is not None else None,
                    setup=g.get("setup") or {},
                    moves=g.get("moves") or [],
                )
            )
            count += 1
        session.commit()
    return count


def main() -> None:
    from .db import load_dotenv

    load_dotenv()  # honor the engine's .env so it targets the app DB
    n = import_saved_games()
    print(f"imported {n} saved game(s) into the database", file=sys.stderr)


if __name__ == "__main__":
    main()
