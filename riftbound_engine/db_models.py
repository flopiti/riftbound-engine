"""SQLAlchemy ORM models — the database schema Alembic manages.

Kept separate from the game engine so migrations only pull in the DB layer.
Importing this registers the tables on ``db.Base.metadata`` (what Alembic's
autogenerate/upgrade reads)."""

from __future__ import annotations

from sqlalchemy import Float, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class SavedGameRow(Base):
    """One saved game — the DB form of an entry in ``saved_games.json``.

    ``setup`` (fake-fill config + shuffle seed) and ``moves`` (the branch path)
    are stored as JSON: portable across SQLite (TEXT-backed JSON) and Postgres
    (jsonb-capable) with no schema churn as those payloads evolve. ``id`` is the
    same human slug used today (e.g. ``autosave``), so the rolling autosave stays
    one upserted row."""

    __tablename__ = "saved_games"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    setup: Mapped[dict] = mapped_column(JSON, default=dict)
    moves: Mapped[list] = mapped_column(JSON, default=list)

    def to_dict(self) -> dict:
        """The same shape the JSON store / API returns."""
        out = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "created_at": self.created_at,
            "setup": self.setup or {},
            "moves": self.moves or [],
        }
        if self.updated_at is not None:
            out["updated_at"] = self.updated_at
        return out
