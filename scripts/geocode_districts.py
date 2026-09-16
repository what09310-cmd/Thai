"""
Géocode les combinaisons (subdistrict, district, province) présentes en
base via Nominatim (OpenStreetMap, gratuit, sans clé API) et écrit le
résultat dans frontend/district_coords.json.

Sert de repli "point GPS du quartier" pour les annonces qui n'ont pas de
latitude/longitude précise (la grande majorité) : le frontend utilise ce
point pour ouvrir Google Maps sur un marqueur réel au lieu d'une simple
recherche texte.

Respecte la politique d'usage de Nominatim (max 1 requête/seconde, User-Agent
identifiable). Ré-exécutable : les entrées déjà géocodées ne sont pas
requêtées à nouveau.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

# La console Windows par défaut (cp1252) ne peut pas encoder les noms de
# lieux thaïs translittérés avec certains caractères Unicode: on force
# l'UTF-8 en sortie pour ne pas planter le script en cours de route.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.config import settings

# Dérivé de DATABASE_URL plutôt qu'un nom en dur: sinon ce script continue
# silencieusement à lire un fichier obsolète dès que DATABASE_URL pointe
# ailleurs (ex. le Postgres de docker-compose).
if not settings.database_url.startswith("sqlite"):
    sys.exit(
        f"DATABASE_URL n'est pas SQLite ({settings.database_url!r}), "
        "ce script ne lit qu'une base SQLite locale."
    )
DB_PATH = Path(settings.database_url.split("///", 1)[1])
OUT_PATH = Path(__file__).parent.parent / "frontend" / "district_coords.json"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "thaimonth/1.0 (geocoding cache script)"
DELAY_SECONDS = 1.1


def make_key(subdistrict: str | None, district: str | None, province: str | None) -> str:
    return "|".join(p.strip().lower() for p in (subdistrict or "", district or "", province or ""))


def geocode(query: str) -> tuple[float, float] | None:
    url = f"{NOMINATIM_URL}?{urllib.parse.urlencode({'q': query, 'format': 'json', 'limit': 1})}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"  Erreur géocodage '{query}': {e}")
        return None
    if not data:
        return None
    return float(data[0]["lat"]), float(data[0]["lon"])


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT subdistrict, district, province FROM listings "
        "WHERE district IS NOT NULL AND district != ''"
    )
    combos = cur.fetchall()
    conn.close()
    print(f"{len(combos)} combinaisons quartier/district/province à géocoder")

    cache: dict = {}
    if OUT_PATH.exists():
        cache = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        print(f"{len(cache)} déjà en cache dans {OUT_PATH.name}")

    new_count = 0
    for i, (subdistrict, district, province) in enumerate(combos, 1):
        key = make_key(subdistrict, district, province)
        if key in cache:
            continue

        # Essaie d'abord avec le sous-district (le plus précis), puis se
        # rabat sur district+province si Nominatim ne trouve rien.
        result = None
        if subdistrict:
            result = geocode(f"{subdistrict}, {district}, {province}, Thailand")
            time.sleep(DELAY_SECONDS)
        if not result and district:
            result = geocode(f"{district}, {province}, Thailand")
            time.sleep(DELAY_SECONDS)
        if not result and province:
            result = geocode(f"{province}, Thailand")
            time.sleep(DELAY_SECONDS)

        if result:
            cache[key] = {"lat": result[0], "lon": result[1]}
            new_count += 1
            print(f"[{i}/{len(combos)}] {key} -> {result}")
        else:
            print(f"[{i}/{len(combos)}] {key} -> INTROUVABLE")

        # Sauvegarde incrémentale (permet d'interrompre/reprendre sans perte).
        if new_count % 10 == 0:
            OUT_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

    OUT_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Terminé: {len(cache)} points en cache ({new_count} nouveaux) -> {OUT_PATH}")


if __name__ == "__main__":
    main()
