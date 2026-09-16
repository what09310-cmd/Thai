#!/usr/bin/env python3
"""
Verifie si les identifiants stockes dans `Listing.line_id` correspondent a
une vraie page/compte LINE, en interrogeant `https://page.line.me/<id>`
(sans le `@` de tete) : LINE redirige vers la page officielle si elle
existe, sinon vers le lien "ajouter en ami" (`line.me/R/ti/p/@<id>`), qui
renvoie 404 si l'identifiant n'existe pas du tout. Contrairement a
verify_whatsapp_numbers.py, c'est une simple page web publique -- pas
besoin de session/navigateur, une requete HTTP suffit.

Par defaut le script tourne en dry-run (affiche juste le resultat). Avec
--apply, il enregistre le resultat dans `Listing.line_verified` (True/False)
sans toucher a `line_id` lui-meme : le frontend garde tous les LINE ID
affiches et cliquables (y compris les invalides), mais bascule le lien vers
`line.me/ti/p/~<id>` pour ceux verifies invalides afin d'eviter un 404 --
voir frontend/index.html. A lancer seulement apres backup de la base
(thaimonth.db.bak-*), comme pour les autres scripts de correction en masse de
ce dossier.
"""
import argparse
import asyncio
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

from src.database.models import Listing
from src.database.session import get_session

CONCURRENCY = 5


async def check_line_id(client: httpx.AsyncClient, line_id: str) -> str:
    """Retourne 'valid', 'invalid' ou 'unknown' pour `line_id`."""
    slug = line_id.lstrip("@")
    try:
        resp = await client.get(f"https://page.line.me/{slug}", follow_redirects=True, timeout=10)
    except httpx.HTTPError:
        return "unknown"
    if resp.status_code == 200:
        return "valid"
    if resp.status_code == 404:
        return "invalid"
    return "unknown"


async def run(limit: int | None, apply_changes: bool) -> None:
    with get_session() as session:
        listings = (
            session.query(Listing)
            .filter(Listing.status == "active", Listing.line_id.isnot(None))
            .all()
        )
        if not listings:
            print("Aucune annonce active avec line_id.")
            return

        by_id: dict[str, list[Listing]] = defaultdict(list)
        for listing in listings:
            by_id[listing.line_id].append(listing)
        ids = list(by_id.keys())
        if limit:
            ids = ids[:limit]

        print(f"Annonces avec line_id : {len(listings)}  IDs uniques a verifier : {len(ids)}")

        counts = {"valid": 0, "invalid": 0, "unknown": 0}
        semaphore = asyncio.Semaphore(CONCURRENCY)
        results: dict[str, str] = {}

        async def worker(line_id: str) -> None:
            async with semaphore:
                results[line_id] = await check_line_id(client, line_id)

        async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}) as client:
            await asyncio.gather(*(worker(i) for i in ids))

        for i, line_id in enumerate(ids, 1):
            group = by_id[line_id]
            result = results[line_id]
            counts[result] += len(group)
            ids_str = ", ".join(l.source_id or str(l.id) for l in group)
            shared = f" (partage par {len(group)} annonces: {ids_str})" if len(group) > 1 else f" (annonce {ids_str})"
            print(f"  [{i}/{len(ids)}] {line_id}: {result}{shared}")

            if apply_changes and result != "unknown":
                for listing in group:
                    listing.line_verified = result == "valid"

        print(
            f"Valides: {counts['valid']}  Invalides: {counts['invalid']}  "
            f"Indetermines: {counts['unknown']}  (en annonces, ids dedupliques a la verification)"
            + (" -- base mise a jour (line_verified)" if apply_changes else " -- dry-run, base non modifiee")
        )


def main() -> None:
    # Certains line_id en base portent des caracteres invisibles (espace de
    # largeur nulle...) que la console Windows (cp1252) ne sait pas encoder.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=None, help="Nombre max d'IDs uniques a verifier")
    parser.add_argument("--apply", action="store_true", help="Enregistre le resultat dans line_verified (sinon dry-run)")
    args = parser.parse_args()
    asyncio.run(run(args.limit, args.apply))


if __name__ == "__main__":
    main()
