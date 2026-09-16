"""Génère le texte de description standardisé (charges) à
afficher pour une annonce, à partir de ses champs structurés."""
from __future__ import annotations

import json
from typing import Protocol


class _HasFees(Protocol):
    """Ce que build_contact_description lit: une ligne `Listing`, ou tout
    objet portant ces trois attributs (le normaliseur ne dépend pas de
    l'ORM)."""

    deposit: str | None
    electric_price: str | None
    amenities: str | None  # liste JSON


def build_contact_description(listing: _HasFees) -> str:
    """Construit la description au format dépôt/électricité/AC."""
    amenities = json.loads(listing.amenities) if listing.amenities else []
    has_ac = "Air Conditioner" in amenities

    electric_price = listing.electric_price
    if electric_price == "Please contact":
        electric_price = None

    lines: list[str] = []
    lines.append(f"Deposit: {listing.deposit}" if listing.deposit else "Deposit:")
    if electric_price:
        lines.append(f"Electric price: {electric_price}")
    lines.append(f"Air Conditioner : {'YES' if has_ac else 'NO'}")

    return "\n".join(lines)
