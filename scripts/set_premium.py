#!/usr/bin/env python3
"""
Passe un compte en premium (ou l'en retire) sans SQL a la main.

    python scripts/set_premium.py --email alice@example.com
    python scripts/set_premium.py --email alice@example.com --off
    python scripts/set_premium.py --list

Le cookie de session porte le role au moment de la connexion
(src/api/auth.py): l'utilisateur doit se reconnecter pour que le changement
prenne effet. Une seule ligne touchee, pas de sauvegarde necessaire.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.auth import get_user_by_email
from src.database.models import User
from src.database.session import get_session


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", help="Email du compte a modifier")
    parser.add_argument("--off", action="store_true", help="Retirer le premium au lieu de l'accorder")
    parser.add_argument("--list", action="store_true", help="Lister les comptes et leur niveau")
    args = parser.parse_args()

    if not args.email and not args.list:
        parser.error("--email ou --list requis")

    with get_session() as db:
        if args.list:
            for u in db.query(User).order_by(User.created_at).all():
                level = "premium" if u.is_premium else "gratuit"
                via = "google" if u.google_sub and not u.password_hash else "email"
                print(f"{u.id:>4}  {level:<8} {via:<7} {u.email}")
            return 0

        user = get_user_by_email(db, args.email)
        if user is None:
            print(f"Aucun compte avec l'email {args.email!r}.")
            return 1
        wanted = not args.off
        if user.is_premium == wanted:
            print(f"{user.email} est deja {'premium' if wanted else 'gratuit'}, rien a faire.")
            return 0
        user.is_premium = wanted
        print(f"{user.email} -> {'premium' if wanted else 'gratuit'}. "
              "L'utilisateur doit se reconnecter pour que la session le reflete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
