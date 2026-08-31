#!/usr/bin/env python3
"""
Script pour vérifier les villes uniques dans la base RentHub
et afficher les statistiques par province
"""

import sqlite3
from pathlib import Path

try:
    from tabulate import tabulate
except ImportError:
    print("Installation de tabulate...")
    import subprocess
    subprocess.check_call(["pip", "install", "tabulate"])
    from tabulate import tabulate

# Chemin vers la base SQLite
DB_PATH = Path("renthub.db")

if not DB_PATH.exists():
    print(f"❌ Erreur : {DB_PATH} n'existe pas !")
    exit(1)

# Connexion
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

# Requête 1 : Provinces uniques et compte
print("\n" + "="*60)
print("📊 STATISTIQUES PAR PROVINCE")
print("="*60 + "\n")

cursor.execute("""
    SELECT province, COUNT(*) as count
    FROM listings
    WHERE province IS NOT NULL AND province != ''
    GROUP BY province
    ORDER BY count DESC
""")

provinces = cursor.fetchall()
print(tabulate(provinces, headers=["Province", "Annonces"], tablefmt="grid"))

total = sum(p[1] for p in provinces)
print(f"\n✅ Total provinces : {len(provinces)}")
print(f"✅ Total annonces : {total}")

# Requête 2 : Provinces NULL (si en existe)
print("\n" + "="*60)
cursor.execute("SELECT COUNT(*) FROM listings WHERE province IS NULL OR province = ''")
null_count = cursor.fetchone()[0]
if null_count > 0:
    print(f"⚠️  {null_count} annonces SANS province (province NULL/vide)")
print("="*60 + "\n")

# Requête 3 : Districts par province (top 5)
print("="*60)
print("📍 TOP DISTRICTS PAR PROVINCE")
print("="*60 + "\n")

cursor.execute("""
    SELECT province, district, COUNT(*) as count
    FROM listings
    WHERE province IS NOT NULL AND province != '' 
          AND district IS NOT NULL AND district != ''
    GROUP BY province, district
    ORDER BY province, count DESC
""")

districts = cursor.fetchall()
for province_name in [p[0] for p in provinces]:
    province_districts = [d for d in districts if d[0] == province_name][:5]
    if province_districts:
        print(f"\n🏙️  {province_name}:")
        for _, district, count in province_districts:
            print(f"   → {district}: {count}")

conn.close()
print("\n✅ Recherche terminée !")
