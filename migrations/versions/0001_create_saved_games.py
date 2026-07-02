"""create saved_games

Revision ID: 0001_saved_games
Revises:
Create Date: 2026-07-01

The first migration: the ``saved_games`` table that replaces saved_games.json.
``setup`` and ``moves`` are JSON columns (portable SQLite/Postgres).
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0001_saved_games"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "saved_games",
        sa.Column("id", sa.String(length=255), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=True),
        sa.Column("setup", sa.JSON(), nullable=True),
        sa.Column("moves", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("saved_games")
