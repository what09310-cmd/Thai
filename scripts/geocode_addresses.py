"""
Géocode chaque adresse d'annonce unique (rue/route + quartier + district +
province, quand RentHub la publie) via Nominatim, et écrit le résultat
dans frontend/address_coords.json.

Plus précis que geocode_districts.py (qui ne géocode que le centre du
quartier) pour les annonces dont RentHub publie un nom de rue/route: le
point tombe alors près du bâtiment réel plutôt qu'au centre du quartier.
Pour les annonces sans rue/route, l'adresse est identique au texte
quartier/district/province et le résultat est donc équivalent au geocode
district-level.

Le frontend combine les deux caches: adresse précise en priorité, quartier
en repli — voir mapPoint() dans frontend/index.html.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

# Cf. geocode_districts.py: force l'UTF-8 en sortie pour éviter un crash
# cp1252 sur les caractères translittérés du thaï.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB_PATH = Path(__file__).parent.parent / "renthub.db"
OUT_PATH = Path(__file__).parent.parent / "frontend" / "address_coords.json"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "renthub-tracker/1.0 (geocoding cache script)"
DELAY_SECONDS = 1.1


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
        "SELECT DISTINCT address, district, province FROM listings "
        "WHERE address IS NOT NULL AND address != ''"
    )
    rows = cur.fetchall()
    conn.close()
    print(f"{len(rows)} adresses uniques à géocoder")

    cache: dict = {}
    if OUT_PATH.exists():
        cache = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        print(f"{len(cache)} déjà en cache dans {OUT_PATH.name}")

    new_count = 0
    for i, (address, district, province) in enumerate(rows, 1):
        key = address.strip().lower()
        if key in cache:
            continue

        result = geocode(f"{address}, Thailand")
        time.sleep(DELAY_SECONDS)
        if not result and district:
            # Repli: le nom de rue masqué/mal formé empêche parfois
            # Nominatim de résoudre l'adresse complète.
            result = geocode(f"{district}, {province}, Thailand")
            time.sleep(DELAY_SECONDS)

        if result:
            cache[key] = {"lat": result[0], "lon": result[1]}
            new_count += 1
            print(f"[{i}/{len(rows)}] {key[:60]} -> {result}")
        else:
            print(f"[{i}/{len(rows)}] {key[:60]} -> INTROUVABLE")

        if new_count % 10 == 0:
            OUT_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

    OUT_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Terminé: {len(cache)} adresses en cache ({new_count} nouvelles) -> {OUT_PATH}")


if __name__ == "__main__":
    main()
