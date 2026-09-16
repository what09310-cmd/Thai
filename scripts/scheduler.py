#!/usr/bin/env python3
"""
Lance scripts/run_scraper.py à intervalle régulier (SCAN_INTERVAL_MINUTES).

Utilisé par le service "scheduler" de docker-compose.yml. Exécute le scan
dans un sous-processus (et non dans ce process) pour réutiliser telle
quelle la logique de scripts/run_scraper.py (parsing CLI, asyncio.run,
sys.exit en cas d'échec de connexion DB) sans risquer de conflit avec la
boucle d'événements d'apscheduler.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from src.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent
RUN_SCRAPER = Path(__file__).parent / "run_scraper.py"


# Un scan complet dure quelques heures (REQUEST_DELAY entre chaque page).
# Sans plafond, un sous-processus bloque (socket qui ne rend jamais la main)
# fige le scheduler indefiniment: apscheduler garde max_instances=1 et se
# contente d'ignorer toutes les executions suivantes.
SCAN_TIMEOUT_SECONDS = 6 * 3600


def run_scan() -> None:
    log.info("Démarrage du scan planifié...")
    try:
        result = subprocess.run(
            [sys.executable, str(RUN_SCRAPER)],
            cwd=str(REPO_ROOT),
            timeout=SCAN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        log.error(
            "Scan planifié interrompu après %d h sans terminer",
            SCAN_TIMEOUT_SECONDS // 3600,
        )
        return
    if result.returncode != 0:
        log.error("Scan planifié terminé en erreur (code %s)", result.returncode)
    else:
        log.info("Scan planifié terminé avec succès")


def main() -> None:
    interval = settings.scan_interval_minutes
    log.info("Scheduler démarré: scan toutes les %d minutes", interval)

    scheduler = BlockingScheduler()
    # `next_run_time` explicite: sans lui, IntervalTrigger place le premier
    # passage a maintenant + `interval` (le commentaire precedent affirmait
    # l'inverse), et un conteneur fraichement demarre restait sans scan
    # pendant une demi-heure.
    scheduler.add_job(
        run_scan,
        IntervalTrigger(minutes=interval),
        next_run_time=datetime.now(timezone.utc),
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
