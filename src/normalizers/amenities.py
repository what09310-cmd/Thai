"""
Dérive la liste des équipements réels d'une annonce depuis son texte de
description libre.

Contexte: la page détail RentHub affiche une légende fixe de 28 catégories
d'équipements (icônes) qui est identique sur presque toutes les pages,
qu'un équipement soit effectivement présent ou non pour cette annonce
précise. Scraper cette légende telle quelle (comme le faisait l'ancien
parseur HTML) produit une liste bidon, quasi identique sur ~95% des
annonces. La description en texte libre, elle, est spécifique à chaque
annonce et reste le seul signal fiable pour ces 28 catégories.
"""
from __future__ import annotations

import re

# Nom de catégorie officiel -> (motifs positifs, motifs de négation).
# Une catégorie n'est retenue que si un motif positif matche ET qu'aucun
# motif de négation ne matche (ex: "No smoking" ne doit pas déclencher
# la catégorie "Smoking").
AMENITY_PATTERNS: dict[str, tuple[list[str], list[str]]] = {
    "Air Conditioner": (
        [r"air[\s-]*condition(?:er|ing)?", r"air[\s-]*con\b", r"\baircon\b", r"\bA/?C\b"],
        [
            r"no\s*air[\s-]*condition", r"no\s*aircon", r"no\s*air[\s-]*con\b", r"without\s*air[\s-]*con",
            # Format structuré généré par build_contact_description(): "Air Conditioner : NO"
            r"air[\s-]*condition(?:er|ing)?\s*:\s*no\b",
        ],
    ),
    "Furnished": (
        [r"furnished", r"fully\s*furnish"],
        [r"un-?furnished", r"not\s*furnished"],
    ),
    "Water Heater": (
        [r"water\s*heater"],
        [r"no\s*water\s*heater"],
    ),
    "Fan": (
        [r"\bfan\b"],
        [r"no\s*fan\b"],
    ),
    "Television": (
        [r"television", r"\btv\b"],
        [r"no\s*tv\b", r"no\s*television"],
    ),
    "Refrigerator": (
        [r"refrigerator", r"\bfridge\b"],
        [r"no\s*fridge", r"no\s*refrigerator"],
    ),
    "Sofa": (
        [r"\bsofa\b"],
        [],
    ),
    "Desk": (
        [r"\bdesk\b"],
        [],
    ),
    "Kitchen Stove": (
        [r"kitchen\s*stove", r"\bstove\b", r"induction\s*(?:cooker|stove)"],
        [],
    ),
    "Phone": (
        [r"land\s*line", r"telephone"],
        [],
    ),
    "In-room WIFI": (
        # "Internet" seul est exclu: RentHub affiche systématiquement une
        # ligne de contact générique "Internet : Please contact" en pied de
        # description, qui ne confirme rien sur la présence du wifi.
        [r"wi-?fi"],
        [r"no\s*wi-?fi"],
    ),
    "Cable TV": (
        [r"cable\s*tv"],
        [],
    ),
    "Pets": (
        [r"\bpets?\b", r"pet[\s-]*friendly"],
        [r"no\s*pets?\b", r"pets?\s*not\s*allowed"],
    ),
    "Smoking": (
        [r"\bsmoking\b", r"\bsmoker\b"],
        [r"no[\s-]*smoking", r"non[\s-]*smoking", r"smoking\s*(?:is\s*)?not\s*(?:allowed|permitted)", r"🚭"],
    ),
    "Parking": (
        [r"\bparking\b", r"car\s*park"],
        [r"no\s*parking"],
    ),
    "Bicycle Parking": (
        [r"bicycle\s*park", r"bike\s*park"],
        [],
    ),
    "Lift": (
        [r"\blift\b", r"\belevator\b"],
        [],
    ),
    "Pool": (
        [r"\bpool\b", r"swimming\s*pool"],
        [r"no\s*(?:swimming\s*)?pool", r"without\s*(?:a\s*)?pool"],
    ),
    "Fitness": (
        [r"fitness", r"\bgym\b"],
        [],
    ),
    "Security keycard": (
        [r"key\s*card", r"keycard"],
        [],
    ),
    "Security finger print": (
        [r"finger\s*print", r"fingerprint"],
        [],
    ),
    "CCTV": (
        [r"\bcctv\b", r"security\s*camera"],
        [],
    ),
    "Security": (
        [r"\bsecurity\b", r"security\s*guard"],
        [],
    ),
    "Restaurant/Food Shop": (
        [r"restaurant", r"food\s*shop"],
        [],
    ),
    "Convenient Store": (
        [r"convenient\s*store", r"convenience\s*store", r"mini\s*mart", r"7-?eleven"],
        [],
    ),
    "Laundry": (
        [r"laundry", r"washing\s*machine"],
        [],
    ),
    "Beauty Salon in Building": (
        [r"beauty\s*salon"],
        [],
    ),
    "EV Charger": (
        [r"ev\s*charger", r"electric\s*vehicle\s*charg"],
        [],
    ),
}


def derive_amenities(description: str | None) -> list[str]:
    """
    Détecte, parmi les 28 catégories connues, celles réellement mentionnées
    (positivement) dans la description libre d'une annonce.

    Retourne une liste vide si la description est absente: on préfère ne
    rien affirmer plutôt que de recopier une légende générique.
    """
    if not description:
        return []

    text = description.lower()
    result = []
    for name, (positives, negatives) in AMENITY_PATTERNS.items():
        if any(re.search(neg, text, re.I) for neg in negatives):
            continue
        if any(re.search(pos, text, re.I) for pos in positives):
            result.append(name)
    return result
