"""Database connectivity check (riftbound_engine.db). Verifies the app can tell
whether it's connected — degrading gracefully when SQLAlchemy/driver is missing
or the server is unreachable, so /health never crashes.
"""

import importlib
import unittest

from riftbound_engine import db

_HAS_SQLALCHEMY = importlib.util.find_spec("sqlalchemy") is not None


class DatabaseUrlTests(unittest.TestCase):
    def test_defaults_to_local_sqlite(self):
        import os

        old = os.environ.pop("DATABASE_URL", None)
        try:
            self.assertTrue(db.database_url().startswith("sqlite:///"))
        finally:
            if old is not None:
                os.environ["DATABASE_URL"] = old

    def test_bare_postgres_url_routes_to_installed_driver(self):
        # A bare postgresql:// URL is normalized to psycopg v3 when psycopg2
        # isn't installed (so the user's plain connection string just works).
        norm = db._normalize_url("postgresql://u:p@localhost:5432/riftbound")
        self.assertIn(norm, {
            "postgresql://u:p@localhost:5432/riftbound",          # psycopg2 present
            "postgresql+psycopg://u:p@localhost:5432/riftbound",  # psycopg v3 path
        })

    def test_explicit_driver_untouched(self):
        url = "postgresql+psycopg2://u:p@h/db"
        self.assertEqual(db._normalize_url(url), url)


class CheckConnectionTests(unittest.TestCase):
    def test_reports_shape_and_never_raises(self):
        # Whatever the environment, it returns the documented dict and doesn't throw.
        result = db.check_connection()
        self.assertIn("configured", result)
        self.assertIn("backend", result)
        self.assertIn("connected", result)
        if not result["connected"]:
            self.assertIn("error", result)

    @unittest.skipUnless(_HAS_SQLALCHEMY, "SQLAlchemy not installed")
    def test_connects_to_sqlite(self):
        import os

        old = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
        db.get_engine.cache_clear()
        try:
            result = db.check_connection()
            self.assertTrue(result["connected"], result)
            self.assertEqual(result["backend"], "sqlite")
        finally:
            db.get_engine.cache_clear()
            if old is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = old


if __name__ == "__main__":
    unittest.main()
