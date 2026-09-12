#!/usr/bin/env python3
"""
Migration idempotente : cree la table `users` (comptes email + mot de passe
et Google) sur une base existante. Voir src/database/models.py::User.

`create_all` ne touche jamais une table deja presente, mais cree sans
probleme celles qui manquent : sur une base neuve, `--init-db-only` suffit
et ce script n'a rien a faire. Fonctionne sur SQLite comme sur Postgres.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import inspect
from src.database.models import User
from src.database.session import engine


def main() -> None:
    if "users" in inspect(engine).get_table_names():
        print("Table 'users' deja presente, skip.")
    else:
        User.__table__.create(engine)
        print("Table 'users' creee.")
    print("Migration terminee.")


if __name__ == "__main__":
    main()
