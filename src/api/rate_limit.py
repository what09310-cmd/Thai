"""Compteurs a fenetre glissante par adresse IP: plafond de requetes sur les
routes de donnees, et (via src/api/auth.py) echecs de connexion sur /login.

Un dict en memoire de processus, suffisant pour ce deploiement a un seul
worker et sans dependance supplementaire. Un backend partage (Redis) ne
deviendrait necessaire qu'avec plusieurs workers, ou chaque processus
tiendrait son propre compte et multiplierait le plafond d'autant.

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


class SlidingWindowCounter:
    """Horodatages par cle, oublies au-dela de `window_seconds`.

    Le compteur d'une cle n'est purge que lorsqu'elle revient: en faisant
    tourner l'adresse source, on ferait croitre le dict sans limite, d'ou
    la purge globale au-dela de `tracked_max` cles.
    """

    def __init__(self, window_seconds: float, tracked_max: int = 10_000) -> None:
        self.window_seconds = window_seconds
        self.tracked_max = tracked_max
        self._events: dict[str, list[float]] = {}

    def recent(self, key: str, now: float | None = None) -> list[float]:
        """Horodatages encore dans la fenetre (et purge de la cle)."""
        now = time.time() if now is None else now
        cutoff = now - self.window_seconds
        events = [t for t in self._events.get(key, []) if t >= cutoff]
        if events:
            self._events[key] = events
        else:
            self._events.pop(key, None)
        return events

    def add(self, key: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        events = self.recent(key, now)
        events.append(now)
        self._events[key] = events
        if len(self._events) > self.tracked_max:
            cutoff = now - self.window_seconds
            for stale in [k for k, v in self._events.items() if not v or v[-1] < cutoff]:
                self._events.pop(stale, None)

    def count(self, key: str) -> int:
        return len(self.recent(key))

    def forget(self, key: str) -> None:
        self._events.pop(key, None)

    def clear(self) -> None:
        self._events.clear()


_hits = SlidingWindowCounter(WINDOW_SECONDS)


def register_hit(client_key: str, max_per_minute: int) -> int | None:
    """Enregistre une requete. Rend le delai d'attente si le plafond est atteint.

    Rend `None` quand la requete passe. La requete refusee n'est pas
    comptee: sinon un client qui insiste repousse indefiniment sa propre
    fenetre et reste bloque bien au-dela d'une minute.
    """
    now = time.time()
    recent = _hits.recent(client_key, now)
    if len(recent) >= max_per_minute:
        # Arrondi a la seconde superieure: un `Retry-After: 0` invite a
        # revenir immediatement, pour un nouveau 429.
        return max(1, int(recent[0] + WINDOW_SECONDS - now) + 1)
    _hits.add(client_key, now)
    return None


def reset() -> None:
    """Vide les compteurs (tests)."""
    _hits.clear()
