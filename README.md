# ThaiMonth

Suivi des locations courte durée (1, 3, 6 mois) publiées sur renthub.in.th : un scraper
alimente une base, un tracker détecte nouveautés, changements de prix et retraits, une API
FastAPI sert les données et un frontend statique (catalogue, cartes, vitrine).

```
python -m venv venv && venv\Scripts\pip install -r requirements.txt
cp .env.example .env            # puis SECRET_KEY et SITE_PASSWORD (l'API refuse les valeurs d'exemple)
python scripts/run_scraper.py --max-pages 2     # un petit scan
venv\Scripts\python.exe -m uvicorn src.api.main:app --port 8000
pytest
```

Guide complet (commandes, architecture, règles par dossier) : [CLAUDE.md](CLAUDE.md) et `.claude/rules/`.
Audit et historique du nettoyage : [docs/audit-2026-09-16.md](docs/audit-2026-09-16.md).
