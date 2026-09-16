"""Dependances FastAPI partagees par les routeurs."""
from __future__ import annotations

from src.database.session import SessionLocal


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
