"""movie catalog (BookMyShow event codes)

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-14

Seeded from the movies listed on https://in.bookmyshow.com/explore/movies-pune, read in a normal
browser on 2026-11-14. Event codes (ET…) are BookMyShow's own and the same in every city. The list
goes stale as films change; Apna also learns codes from BookMyShow links users send.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SEED = [
    ("ET00521921", "aadi-anant", "Aadi Anant"),
    ("ET00516631", "scene", "Scene"),
    ("ET00525702", "john-doe", "John Doe"),
    ("ET00451914", "ramayana", "Ramayana"),
    ("ET00477911", "drishyam-the-conclusion", "Drishyam The Conclusion"),
    ("ET00429213", "rajini-the-jailer-2", "Rajini: The Jailer 2"),
    ("ET00507738", "hanuman-ansh", "Hanuman Ansh"),
    ("ET00465382", "gondhal", "Gondhal"),
    ("ET00502504", "prahaar", "Prahaar"),
    ("ET00519811", "pyre", "Pyre"),
    ("ET00495822", "udta-teer", "Udta Teer"),
    ("ET00516584", "wildwood", "Wildwood"),
    ("ET00507337", "doraemon-castle-of-the-undersea-devil", "Doraemon: Castle of the Undersea Devil"),
    ("ET00508520", "i-play-rocky", "I Play Rocky"),
    ("ET00451881", "the-cat-in-the-hat", "The Cat in the Hat"),
    ("ET00518333", "ghost-soldier", "Ghost Soldier"),
]


def upgrade() -> None:
    table = op.create_table(
        "movie_catalog",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("et_code", sa.String(length=16), nullable=False),
        sa.Column("slug", sa.String(length=160), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("et_code"),
    )
    op.create_index("ix_movie_catalog_slug", "movie_catalog", ["slug"])
    now = datetime.now(UTC)
    op.bulk_insert(table, [
        {"id": uuid.uuid4(), "et_code": et, "slug": slug, "title": title, "source": "bms-listing-2026-11-14",
         "created_at": now, "updated_at": now}
        for et, slug, title in _SEED
    ])


def downgrade() -> None:
    op.drop_index("ix_movie_catalog_slug", table_name="movie_catalog")
    op.drop_table("movie_catalog")
