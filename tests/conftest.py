"""Fixtures partagees par les tests d'API.

`test_bugfix_regression.py` definit les siennes en local (elles ont la
priorite): ce fichier sert les fichiers de test ecrits ensuite, sans avoir
a redeclarer une base en memoire dans chacun.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.api import rate_limit
from src.api.main import app, get_db, reset_stats_cache
from src.database.models import Base


@pytest.fixture
def session():
    """Session SQLAlchemy sur une base SQLite en memoire (jamais thaimonth.db)."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


@pytest.fixture
def client(session):
    """TestClient dont get_db pointe sur la base en memoire.

    Sans gestionnaire de contexte: le hook de demarrage (init_db + ALTER
    TABLE) ne doit pas s'executer sur la vraie base.
    """
    app.dependency_overrides[get_db] = lambda: session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Compteurs de debit et cache de /stats remis a zero entre deux tests.

    Ils vivent dans des dicts de module (deploiement a un seul worker): sans
    cette purge, les requetes d'un test consomment le budget du suivant, ou
    lui servent les compteurs d'une base qui n'existe plus, et l'ordre
    d'execution decide qui echoue.
    """
    rate_limit.reset()
    reset_stats_cache()
    yield
    rate_limit.reset()
    reset_stats_cache()
