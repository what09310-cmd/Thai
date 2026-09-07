"""Plafond de requetes par adresse IP sur les routes de donnees.

Meme approche que le compteur de `/login` (src/api/auth.py): un dict en
memoire de processus, suffisant pour ce deploiement a un seul worker et
sans dependance supplementaire. Un backend partage (Redis) ne deviendrait
necessaire qu'avec plusieurs workers, ou chaque processus tiendrait son
propre compte et multiplierait le plafond d'autant.

Ce qu'il protege: `/listings*` et `/stats` sont publics, donc bornes mais
lisibles sans compte. Les bornes limitent ce qu'une requete rend, pas
combien de requetes on enchaine -- sans plafond de debit, l'echantillon
public se reconstitue simplement en bouclant.
"""
from __future__ import annotations

import time

WINDOW_SECONDS = 60
ANONYMOUS_MAX_PER_MINUTE = 60
# Le frontend authentifie (index.html, vip.html, carte*.html) pagine tout
# le catalogue par pages de 500 au chargement, et plusieurs pages peuvent
# etre ouvertes de front: lui appliquer le budget anonyme casserait
# l'affichage des cartes.
AUTHENTICATED_MAX_PER_MINUTE = 300

# Meme raison que dans auth.py: le compteur d'une IP n'etait purge que
# lorsqu'elle revenait. En faisant tourner l'adresse source, on faisait
# croitre ce dictionnaire sans limite.
_TRACKED_CLIENTS_MAX = 10_000

_hits: dict[str, list[float]] = {}


def _prune_all(now: float) -> None:
    cutoff = now - WINDOW_SECONDS
    for key in [k for k, v in _hits.items() if not v or v[-1] < cutoff]:
        _hits.pop(key, None)


def register_hit(client_key: str, max_per_minute: int) -> int | None:
    """Enregistre une requete. Rend le delai d'attente si le plafond est atteint.

    Rend `None` quand la requete passe. La requete refusee n'est pas
    comptee: sinon un client qui insiste repousse indefiniment sa propre
    fenetre et reste bloque bien au-dela d'une minute.
    """
    now = time.time()
    cutoff = now - WINDOW_SECONDS
    attempts = [t for t in _hits.get(client_key, []) if t >= cutoff]

    if len(attempts) >= max_per_minute:
        _hits[client_key] = attempts
        # Arrondi a la seconde superieure: un `Retry-After: 0` invite a
        # revenir immediatement, pour un nouveau 429.
        return max(1, int(attempts[0] + WINDOW_SECONDS - now) + 1)

    attempts.append(now)
    _hits[client_key] = attempts
    if len(_hits) > _TRACKED_CLIENTS_MAX:
        _prune_all(now)
    return None


def reset() -> None:
    """Vide les compteurs (tests)."""
    _hits.clear()
