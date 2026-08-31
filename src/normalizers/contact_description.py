"""Génère le texte de description standardisé (charges) à
afficher pour une annonce, à partir de ses champs structurés."""
from __future__ import annotations

import json

from src.database.models import Listing


def build_contact_description(listing: Listing) -> str:
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
