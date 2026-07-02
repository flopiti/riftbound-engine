"""Schema + saved_games import. Verifies the model builds the table and that
import_saved_games() loads the JSON idempotently (re-running upserts, so the
rolling autosave never duplicates).
"""

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path

_HAS_SQLALCHEMY = importlib.util.find_spec("sqlalchemy") is not None


@unittest.skipUnless(_HAS_SQLALCHEMY, "SQLAlchemy not installed")
class SavedGamesImportTests(unittest.TestCase):
    def setUp(self):
        from riftbound_engine import db

        self._tmp = tempfile.TemporaryDirectory()
        self._old_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = f"sqlite:///{Path(self._tmp.name) / 'test.db'}"
        db.get_engine.cache_clear()
        db.get_sessionmaker.cache_clear()
        # Build the schema straight from the models (same table the migration makes).
        import riftbound_engine.db_models  # noqa: F401
        db.Base.metadata.create_all(db.get_engine())

    def tearDown(self):
        from riftbound_engine import db

        db.get_engine.cache_clear()
        db.get_sessionmaker.cache_clear()
        if self._old_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self._old_url
        self._tmp.cleanup()

    def _json(self, games):
        p = Path(self._tmp.name) / "saved_games.json"
        p.write_text(json.dumps({"games": games}), encoding="utf-8")
        return p

    def _rows(self):
        from riftbound_engine.db import get_sessionmaker
        from riftbound_engine.db_models import SavedGameRow

        with get_sessionmaker()() as s:
            return {r.id: r for r in s.query(SavedGameRow).all()}

    def test_imports_games(self):
        from riftbound_engine.db_migrate import import_saved_games

        path = self._json([
            {"id": "autosave", "name": "Autosave", "created_at": 1.0,
             "setup": {"player_1_deck": "x"}, "moves": [{"a": 1}]},
            {"id": "my-game", "name": "My Game", "created_at": 2.0, "setup": {}, "moves": []},
        ])
        self.assertEqual(import_saved_games(path), 2)
        rows = self._rows()
        self.assertEqual(set(rows), {"autosave", "my-game"})
        self.assertEqual(rows["autosave"].setup, {"player_1_deck": "x"})
        self.assertEqual(rows["autosave"].moves, [{"a": 1}])

    def test_reimport_is_idempotent_and_updates(self):
        from riftbound_engine.db_migrate import import_saved_games

        self._json([{"id": "autosave", "name": "Autosave", "created_at": 1.0,
                     "setup": {}, "moves": [1, 2]}])
        import_saved_games(Path(self._tmp.name) / "saved_games.json")
        # Re-run with a newer autosave payload → same row, updated, no duplicate.
        self._json([{"id": "autosave", "name": "Autosave", "created_at": 1.0,
                     "setup": {}, "moves": [1, 2, 3, 4]}])
        n = import_saved_games(Path(self._tmp.name) / "saved_games.json")
        self.assertEqual(n, 1)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows["autosave"].moves, [1, 2, 3, 4])

    def test_missing_file_imports_nothing(self):
        from riftbound_engine.db_migrate import import_saved_games

        self.assertEqual(import_saved_games(Path(self._tmp.name) / "nope.json"), 0)


if __name__ == "__main__":
    unittest.main()
