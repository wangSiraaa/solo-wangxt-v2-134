"""Database session dependency. Engine/schema live in models.py."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session

from .models import SessionLocal, init_db


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def bootstrap() -> None:
    init_db(seed=True)
