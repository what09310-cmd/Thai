"""
Détecte les changements entre un scan et le précédent.
Gère les statuts: NEW, UPDATED, PRICE_CHANGED, REMOVED.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from src.database.models import Listing, ListingHistory, ListingImage, Province
from src.models.schemas import ListingFull
from src.normalizers.contact_description import build_contact_description
from src.config import settings

log = logging.getLogger(__name__)


# Champs utilisés pour calculer le content_hash (changements détectables)
HASH_FIELDS = [
    "name",
    "price_monthly_min", "price_monthly_max",
    "contract_monthly_raw", "contract_3_month_raw", "contract_6_month_raw",
    "description",
    "amenities",
]

# Fraction minimale du catalogue actif qu'un scan doit avoir vue pour que
# la détection des suppressions soit considérée fiable.
MIN_SCAN_COVERAGE = 0.5

# Champs renseignés par la page détail uniquement (jamais par la page de
# liste), reportés en base par apply_detail_fields.
DETAIL_FIELDS = (
    "phone", "line_id", "whatsapp", "email",
    "deposit", "advance_payment", "electric_price", "water_price", "service_fee",
)

# Champs de prix spécifiquement trackés
PRICE_FIELDS = [
    "price_monthly_min", "price_monthly_max",
    "contract_monthly_min", "contract_monthly_max",
    "contract_3_month_min", "contract_3_month_max",
    "contract_6_month_min", "contract_6_month_max",
    "daily_price_min", "daily_price_max",
]


def apply_detail_fields(db_listing: Listing, detail) -> None:
    """Reporte sur la ligne en base les champs issus de la page détail.

    Quand le JSON __NEXT_DATA__ a été lu (`has_structured_data`), contacts
    et charges viennent de la source et font autorité: ils sont écrasés
    même par None. Sans cela les valeurs parasites laissées par l'ancien
    parseur regex (dépôt "This is not verified listing...", line_id
    "Unavailable") restaient en base indéfiniment, aucune valeur non vide
    ne venant jamais les remplacer.

    Sinon — scan de liste seul, ou page détail sans JSON exploitable — on
    ne remplace que par une valeur non vide, pour ne rien perdre.
    """
    authoritative = getattr(detail, "has_structured_data", False)
    for field in DETAIL_FIELDS:
        value = getattr(detail, field, None)
        if value or authoritative:
            setattr(db_listing, field, value)


def compute_content_hash(listing: ListingFull) -> str:
    """Hash stable pour détecter les changements de contenu."""
    data = {}
    for field in HASH_FIELDS:
        val = getattr(listing, field, None)
        data[field] = val
    serialized = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode()).hexdigest()


def upsert_listing(
    session: Session,
    listing: ListingFull,
    scan_time: datetime,
) -> tuple[str, Optional[Listing]]:
    """
    Insère ou met à jour une annonce dans la base.

    Retourne: (change_type, db_listing)
    change_type: "NEW" | "UPDATED" | "PRICE_CHANGED" | "UNCHANGED"
    """
    now = scan_time
    content_hash = compute_content_hash(listing)

    # Chercher l'annonce existante par slug
    db_listing = session.query(Listing).filter_by(slug=listing.slug).first()

    if db_listing is None:
        # Nouvelle annonce
        db_listing = _create_listing(session, listing, content_hash, now)
        _add_history(session, db_listing, "NEW", now)
        log.info(f"[NEW] {listing.name[:50]}")
        return "NEW", db_listing

    # Annonce existante: vérifier les changements
    change_types = []

    # 1. Changement de prix
    price_changed = False
    for field in PRICE_FIELDS:
        old_val = getattr(db_listing, field)
        new_val = getattr(listing, field, None)
        if old_val != new_val:
            price_changed = True
            _add_history(
                session, db_listing, "PRICE_CHANGED", now,
                field_name=field,
                old_value=str(old_val),
                new_value=str(new_val),
            )
    if price_changed:
        change_types.append("PRICE_CHANGED")

    # 2. Changement de contenu
    if db_listing.content_hash != content_hash and not price_changed:
        change_types.append("UPDATED")
        _add_history(
            session, db_listing, "UPDATED", now,
            field_name="content_hash",
            old_value=db_listing.content_hash,
            new_value=content_hash,
        )

    # 3. Réactivation si l'annonce était marquée REMOVED
    if db_listing.status == "removed":
        db_listing.status = "active"
        db_listing.missing_scan_count = 0
        _add_history(session, db_listing, "REACTIVATED", now)
        change_types.append("REACTIVATED")

    # Mettre à jour les champs
    _update_listing_fields(db_listing, listing, content_hash, now)

    if not change_types:
        return "UNCHANGED", db_listing

    # Retourner le changement le plus significatif
    priority = ["PRICE_CHANGED", "UPDATED", "REACTIVATED"]
    for p in priority:
        if p in change_types:
            return p, db_listing

    return "UNCHANGED", db_listing


def mark_removed_listings(
    session: Session,
    seen_slugs: set[str],
    scan_time: datetime,
) -> int:
    """
    Marque comme REMOVED les annonces non vues dans ce scan.

    Stratégie progressive:
    - missing_scan_count += 1
    - Si missing_scan_count >= REMOVED_AFTER_MISSING_SCANS -> status = "removed"
    """
    threshold = settings.removed_after_missing_scans
    removed_count = 0

    active_listings = (
        session.query(Listing)
        .filter(Listing.status == "active")
        .all()
    )

    # Filet de sécurité: un scan interrompu (panne réseau, --max-pages) ne
    # voit qu'une fraction du catalogue. Incrémenter missing_scan_count sur
    # tout le reste finirait par marquer l'ensemble des annonces "removed".
    if active_listings and len(seen_slugs) < len(active_listings) * MIN_SCAN_COVERAGE:
        log.warning(
            "Scan partiel (%d annonces vues pour %d actives): "
            "détection des suppressions ignorée",
            len(seen_slugs),
            len(active_listings),
        )
        return 0

    for db_listing in active_listings:
        if db_listing.slug not in seen_slugs:
            db_listing.missing_scan_count += 1

            if db_listing.missing_scan_count >= threshold:
                if db_listing.status != "removed":
                    db_listing.status = "removed"
                    _add_history(
                        session, db_listing, "REMOVED", scan_time,
                        field_name="missing_scan_count",
                        old_value="active",
                        new_value=str(db_listing.missing_scan_count),
                    )
                    removed_count += 1
                    log.info(f"[REMOVED] {db_listing.name[:50]}")
        else:
            # Annonce vue: réinitialiser le compteur
            db_listing.missing_scan_count = 0

    return removed_count


def upsert_province(session: Session, province_data: dict) -> None:
    """Insère ou met à jour une province."""
    db_prov = session.query(Province).filter_by(slug=province_data["slug"]).first()
    if db_prov is None:
        db_prov = Province(
            name=province_data["name"],
            slug=province_data["slug"],
            renthub_url=province_data.get("url"),
            listing_count=province_data.get("count"),
            active=True,
        )
        session.add(db_prov)
    else:
        db_prov.listing_count = province_data.get("count", db_prov.listing_count)
        db_prov.renthub_url = province_data.get("url", db_prov.renthub_url)


# --- Helpers ---

def _create_listing(
    session: Session,
    listing: ListingFull,
    content_hash: str,
    now: datetime,
) -> Listing:
    db_listing = Listing(
        source="renthub",
        source_id=listing.source_id,
        slug=listing.slug,
        name=listing.name,
        url=listing.url,
        address=listing.address,
        subdistrict=listing.subdistrict,
        district=listing.district,
        province=listing.province,
        latitude=listing.latitude,
        longitude=listing.longitude,
        price_monthly_raw=listing.price_monthly_raw,
        price_monthly_min=listing.price_monthly_min,
        price_monthly_max=listing.price_monthly_max,
        daily_price_raw=listing.daily_price_raw,
        daily_price_min=listing.daily_price_min,
        daily_price_max=listing.daily_price_max,
        contract_monthly_raw=listing.contract_monthly_raw,
        contract_monthly_min=listing.contract_monthly_min,
        contract_monthly_max=listing.contract_monthly_max,
        contract_3_month_raw=listing.contract_3_month_raw,
        contract_3_month_min=listing.contract_3_month_min,
        contract_3_month_max=listing.contract_3_month_max,
        contract_6_month_raw=listing.contract_6_month_raw,
        contract_6_month_min=listing.contract_6_month_min,
        contract_6_month_max=listing.contract_6_month_max,
        has_monthly_contract=listing.has_monthly_contract,
        amenities=json.dumps(listing.amenities) if listing.amenities else None,
        room_types=json.dumps(
            [r.model_dump() for r in listing.room_types]
        ) if listing.room_types else None,
        deposit=listing.deposit,
        advance_payment=listing.advance_payment,
        electric_price=listing.electric_price,
        water_price=listing.water_price,
        service_fee=listing.service_fee,
        phone=listing.phone,
        line_id=listing.line_id,
        whatsapp=listing.whatsapp,
        email=listing.email,
        is_verified=listing.is_verified,
        has_promotion=listing.has_promotion,
        status="active",
        missing_scan_count=0,
        content_hash=content_hash,
        source_updated_at=listing.source_updated_at,
        published_at=listing.published_at,
        first_seen_at=now,
        last_seen_at=now,
        last_scraped_at=now,
        # source_id n'est renseigné que par le parseur de page détail:
        # sa présence signale que la page individuelle a bien été lue.
        detail_scraped_at=now if listing.source_id else None,
    )
    db_listing.description = build_contact_description(db_listing)
    session.add(db_listing)
    session.flush()

    # Images
    for i, img_url in enumerate(listing.images or []):
        img = ListingImage(
            listing_id=db_listing.id,
            image_url=img_url,
            position=i,
        )
        session.add(img)
    # Thumbnail si pas d'autres images
    if not listing.images and listing.thumbnail_url:
        img = ListingImage(
            listing_id=db_listing.id,
            image_url=listing.thumbnail_url,
            position=0,
        )
        session.add(img)

    return db_listing


def _update_listing_fields(
    db_listing: Listing,
    listing: ListingFull,
    content_hash: str,
    now: datetime,
) -> None:
    """Met à jour tous les champs d'une annonce existante."""
    db_listing.source_id = listing.source_id or db_listing.source_id
    db_listing.name = listing.name
    db_listing.address = listing.address or db_listing.address
    db_listing.subdistrict = listing.subdistrict or db_listing.subdistrict
    db_listing.district = listing.district or db_listing.district
    db_listing.province = listing.province or db_listing.province
    db_listing.latitude = listing.latitude or db_listing.latitude
    db_listing.longitude = listing.longitude or db_listing.longitude
    db_listing.price_monthly_raw = listing.price_monthly_raw
    db_listing.price_monthly_min = listing.price_monthly_min
    db_listing.price_monthly_max = listing.price_monthly_max
    db_listing.daily_price_raw = listing.daily_price_raw
    db_listing.daily_price_min = listing.daily_price_min
    db_listing.daily_price_max = listing.daily_price_max
    db_listing.contract_monthly_raw = listing.contract_monthly_raw
    db_listing.contract_monthly_min = listing.contract_monthly_min
    db_listing.contract_monthly_max = listing.contract_monthly_max
    db_listing.contract_3_month_raw = listing.contract_3_month_raw
    db_listing.contract_3_month_min = listing.contract_3_month_min
    db_listing.contract_3_month_max = listing.contract_3_month_max
    db_listing.contract_6_month_raw = listing.contract_6_month_raw
    db_listing.contract_6_month_min = listing.contract_6_month_min
    db_listing.contract_6_month_max = listing.contract_6_month_max
    db_listing.has_monthly_contract = listing.has_monthly_contract
    db_listing.is_verified = listing.is_verified
    db_listing.has_promotion = listing.has_promotion
    db_listing.content_hash = content_hash
    db_listing.source_updated_at = listing.source_updated_at
    db_listing.last_seen_at = now
    db_listing.last_scraped_at = now

    # Enrichissements depuis la page détail
    if listing.amenities:
        db_listing.amenities = json.dumps(listing.amenities)
    if listing.room_types:
        db_listing.room_types = json.dumps([r.model_dump() for r in listing.room_types])
    apply_detail_fields(db_listing, listing)
    if listing.source_id:
        db_listing.detail_scraped_at = now

    db_listing.description = build_contact_description(db_listing)

    # Mise à jour des images (si nouvelles trouvées).
    # Les positions repartent du maximum existant et s'incrémentent une par
    # une: indexer sur la liste complète des URLs scrapées (dont la plupart
    # sont déjà connues) produisait des positions dupliquées, et c'est
    # l'ordre des positions qui décide de la vignette affichée.
    if listing.images:
        existing_urls = {img.image_url for img in db_listing.images}
        next_position = max((img.position for img in db_listing.images), default=-1) + 1
        for img_url in listing.images:
            if img_url in existing_urls:
                continue
            db_listing.images.append(
                ListingImage(
                    listing_id=db_listing.id,
                    image_url=img_url,
                    position=next_position,
                )
            )
            existing_urls.add(img_url)
            next_position += 1


def _add_history(
    session: Session,
    db_listing: Listing,
    change_type: str,
    changed_at: datetime,
    field_name: Optional[str] = None,
    old_value: Optional[str] = None,
    new_value: Optional[str] = None,
) -> None:
    entry = ListingHistory(
        listing_id=db_listing.id,
        changed_at=changed_at,
        change_type=change_type,
        field_name=field_name,
        old_value=old_value,
        new_value=new_value,
    )
    session.add(entry)
