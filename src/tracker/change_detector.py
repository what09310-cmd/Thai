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

from sqlalchemy.orm import Session, selectinload

from src.config import settings
from src.database.models import Listing, ListingHistory, ListingImage
from src.models.schemas import ListingFull
from src.normalizers.contact_description import build_contact_description

log = logging.getLogger(__name__)


# Champs utilisés pour calculer le content_hash (changements détectables).
# Le hash est calculé sur la ligne en base APRÈS application des champs, et
# non sur le ListingFull scrapé: `description` et `amenities` ne sont
# renseignés que quand la page détail a été lue dans ce run. Hasher l'objet
# scrapé faisait donc basculer le hash à chaque cycle de péremption détail
# (detail_refresh_days), produisant un UPDATED fantôme par annonce et par
# bascule. Voir compute_content_hash.
#
# `deposit` et `electric_price` plutot que `description`: la description est
# regeneree a partir de ces deux champs et de la climatisation (deja dans
# `amenities`), donc la hasher revenait a hasher ceux-ci -- sauf qu'un
# simple changement de format de build_contact_description declenchait un
# UPDATED sur tout le catalogue. Changer cette liste impose de recalculer
# content_hash sur les lignes existantes (scripts/migrate.py le fait), sans
# quoi le scan suivant ecrit un UPDATED fantome par annonce.
HASH_FIELDS = [
    "name",
    "price_monthly_min", "price_monthly_max",
    "contract_monthly_raw", "contract_3_month_raw", "contract_6_month_raw",
    "deposit", "electric_price",
    "amenities",
]

# Taille des clauses IN (...) envoyees en base: SQLite d'avant 3.32 plafonne
# a 999 parametres, et une clause de plusieurs milliers d'entrees n'apporte
# rien de plus qu'une serie de 500.
_IN_CHUNK = 500


def _chunks(values: list, size: int = _IN_CHUNK):
    for i in range(0, len(values), size):
        yield values[i:i + size]

# Fraction minimale du catalogue actif qu'un scan doit avoir vue pour que
# la détection des suppressions soit considérée fiable.
MIN_SCAN_COVERAGE = 0.5

# Champs renseignés par la page détail uniquement (jamais par la page de
# liste), reportés en base par apply_detail_fields.
DETAIL_FIELDS = (
    "phone", "line_id", "whatsapp", "email",
    "deposit", "advance_payment", "electric_price", "water_price", "service_fee",
)

# Champs décrivant l'offre "court terme" (contrats 1/3/6 mois), reportés
# en base par apply_contract_fields.
CONTRACT_FIELDS = (
    "contract_monthly_raw", "contract_monthly_min", "contract_monthly_max",
    "contract_3_month_raw", "contract_3_month_min", "contract_3_month_max",
    "contract_6_month_raw", "contract_6_month_min", "contract_6_month_max",
)

# Champs recopies tels quels du ListingFull scrape vers une ligne neuve:
# toute colonne de `listings` portee sous le meme nom par ListingFull, sauf
# celles qui demandent une conversion (JSON, fuseau) ou une valeur imposee
# a la creation. Une colonne ajoutee des deux cotes est copiee d'office.
_NOT_COPIED = {"amenities", "room_types", "source_updated_at", "status", "missing_scan_count"}
COPIED_FIELDS = tuple(
    sorted((set(Listing.__table__.columns.keys()) & set(ListingFull.model_fields)) - _NOT_COPIED)
)

# Champs de prix spécifiquement trackés
PRICE_FIELDS = [
    "price_monthly_min", "price_monthly_max",
    "contract_monthly_min", "contract_monthly_max",
    "contract_3_month_min", "contract_3_month_max",
    "contract_6_month_min", "contract_6_month_max",
    "daily_price_min", "daily_price_max",
]


def _to_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Ramene une date aware en UTC avant stockage.

    Le type DATETIME de SQLite ecarte le fuseau a l'ecriture et garde
    l'heure murale: une date en Asia/Bangkok (repli HTML de
    list_parser._extract_date) etait donc stockee avec 7 h d'avance sur
    les dates UTC du chemin JSON, et relue comme de l'UTC par l'API. Une
    date naive est laissee telle quelle (convention: deja UTC).
    """
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc)


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


def apply_contract_fields(db_listing: Listing, listing: ListingFull) -> None:
    """Reporte les contrats 1/3/6 mois, sans effacer ce qu'on sait déjà.

    Seule une carte issue d'une page qui décrit vraiment l'offre court terme
    (`from_structured_list`) fait autorité: elle écrase les contrats même
    par None, car "plus de contrat 1 mois" est une information réelle.

    Les pages "par lieu" (/en/short-term-rental/<slug>, --include-locations)
    portent `price.monthly` mais pas `shortTerm.*.shortContract`: leurs
    contrats sont vides par construction. Sans cette garde, un scan
    --include-locations effaçait les contrats déjà connus d'une annonce vue
    auparavant sur /browse/short-term-monthly, et le scan suivant les
    restaurait — d'où des allers-retours valeur/None dans l'historique.
    """
    if listing.from_structured_list:
        for field in CONTRACT_FIELDS:
            setattr(db_listing, field, getattr(listing, field, None))
        db_listing.has_monthly_contract = listing.has_monthly_contract
        return

    for field in CONTRACT_FIELDS:
        value = getattr(listing, field, None)
        if value is not None:
            setattr(db_listing, field, value)
    if listing.has_monthly_contract != "unknown":
        db_listing.has_monthly_contract = listing.has_monthly_contract


def compute_content_hash(db_listing: Listing) -> str:
    """Hash stable pour détecter les changements de contenu.

    Prend la ligne *en base*, après application des champs du scan: c'est le
    seul état qui ne dépende pas de ce que ce run précis a scrapé. Sur le
    ListingFull scrapé, `description` et `amenities` sont vides dès que la
    page détail a été sautée (péremption à detail_refresh_days), et le hash
    basculait d'un run à l'autre sans qu'aucune donnée n'ait changé.
    """
    data = {field: getattr(db_listing, field, None) for field in HASH_FIELDS}
    serialized = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode()).hexdigest()


def load_existing(session: Session, slugs: list[str]) -> dict[str, Listing]:
    """Charge en quelques requetes les annonces deja connues, images comprises.

    Sans cette table, upsert_listing faisait un SELECT par slug puis
    chargeait les ~45 images de chaque annonce a la demande: ~4 000 aller-
    retours par scan, soit plusieurs minutes vers un Postgres distant.
    """
    found: dict[str, Listing] = {}
    for chunk in _chunks(slugs):
        for db_listing in (
            session.query(Listing)
            .options(selectinload(Listing.images))
            .filter(Listing.slug.in_(chunk))
        ):
            found[db_listing.slug] = db_listing
    return found


def upsert_listing(
    session: Session,
    listing: ListingFull,
    scan_time: datetime,
    existing: Optional[dict[str, Listing]] = None,
) -> tuple[str, Optional[Listing]]:
    """
    Insère ou met à jour une annonce dans la base.

    `existing` (voir load_existing) evite un SELECT par annonce; sans lui,
    l'annonce est cherchee par slug.

    Retourne: (change_type, db_listing)
    change_type: "NEW" | "UPDATED" | "PRICE_CHANGED" | "UNCHANGED"
    """
    now = scan_time

    if existing is not None:
        db_listing = existing.get(listing.slug)
    else:
        db_listing = session.query(Listing).filter_by(slug=listing.slug).first()

    if db_listing is None:
        # Nouvelle annonce
        db_listing = _create_listing(session, listing, now)
        _add_history(session, db_listing, "NEW", now)
        log.info(f"[NEW] {listing.name[:50]}")
        return "NEW", db_listing

    # Annonce existante: vérifier les changements.
    #
    # Les prix sont comparés avant/après application des champs, et non
    # contre le ListingFull scrapé: apply_contract_fields peut décider de
    # NE PAS écrire une valeur (source non autoritaire). Comparer à
    # l'objet scrapé journalisait alors un PRICE_CHANGED pour une écriture
    # qui n'a jamais eu lieu.
    change_types = []
    before = {field: getattr(db_listing, field) for field in PRICE_FIELDS}

    _update_listing_fields(db_listing, listing, now)

    # 1. Changement de prix
    price_changed = False
    for field in PRICE_FIELDS:
        old_val = before[field]
        new_val = getattr(db_listing, field)
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
    #
    # Indépendant de `price_changed`: la condition portait auparavant un
    # `and not price_changed`, alors que `content_hash` était écrasé dans
    # tous les cas juste en dessous. Un scan qui changeait à la fois le prix
    # et le contenu (titre, description, équipements) n'écrivait donc aucune
    # ligne UPDATED, et le scan suivant comparait au hash déjà mis à jour:
    # le changement de contenu était perdu pour de bon.
    #
    # L'invariant « un scan sans changement n'écrit pas d'historique » tient
    # toujours: la condition reste l'inégalité des hash.
    content_hash = compute_content_hash(db_listing)
    if db_listing.content_hash != content_hash:
        change_types.append("UPDATED")
        _add_history(
            session, db_listing, "UPDATED", now,
            field_name="content_hash",
            old_value=db_listing.content_hash,
            new_value=content_hash,
        )
    db_listing.content_hash = content_hash

    # 3. Réactivation si l'annonce était marquée REMOVED
    if db_listing.status == "removed":
        db_listing.status = "active"
        db_listing.missing_scan_count = 0
        _add_history(session, db_listing, "REACTIVATED", now)
        change_types.append("REACTIVATED")

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

    # Quatre colonnes, pas des objets ORM complets: cette fonction ne fait
    # que des mises a jour en masse, elle n'a pas besoin de charger 1 200
    # annonces avec leurs 50 colonnes.
    active = (
        session.query(Listing.id, Listing.slug, Listing.missing_scan_count, Listing.name)
        .filter(Listing.status == "active")
        .all()
    )

    # Filet de sécurité: un scan interrompu (panne réseau, --max-pages) ne
    # voit qu'une fraction du catalogue. Incrémenter missing_scan_count sur
    # tout le reste finirait par marquer l'ensemble des annonces "removed".
    if active and len(seen_slugs) < len(active) * MIN_SCAN_COVERAGE:
        log.warning(
            "Scan partiel (%d annonces vues pour %d actives): "
            "détection des suppressions ignorée",
            len(seen_slugs),
            len(active),
        )
        return 0

    seen_to_reset = [row.id for row in active if row.slug in seen_slugs and row.missing_scan_count]
    missing = [row for row in active if row.slug not in seen_slugs]
    to_remove = [row for row in missing if row.missing_scan_count + 1 >= threshold]

    for chunk in _chunks(seen_to_reset):
        session.query(Listing).filter(Listing.id.in_(chunk)).update({Listing.missing_scan_count: 0})
    for chunk in _chunks([row.id for row in missing]):
        session.query(Listing).filter(Listing.id.in_(chunk)).update(
            {Listing.missing_scan_count: Listing.missing_scan_count + 1}
        )
    for chunk in _chunks([row.id for row in to_remove]):
        session.query(Listing).filter(Listing.id.in_(chunk)).update({Listing.status: "removed"})
    for row in to_remove:
        _add_history(
            session, row.id, "REMOVED", scan_time,
            field_name="missing_scan_count",
            old_value="active",
            new_value=str(row.missing_scan_count + 1),
        )
        log.info(f"[REMOVED] {row.name[:50]}")

    return len(to_remove)


# --- Helpers ---

def _create_listing(
    session: Session,
    listing: ListingFull,
    now: datetime,
) -> Listing:
    db_listing = Listing(
        source="renthub",
        **{field: getattr(listing, field) for field in COPIED_FIELDS},
        amenities=json.dumps(listing.amenities) if listing.amenities else None,
        room_types=json.dumps(
            [r.model_dump() for r in listing.room_types]
        ) if listing.room_types else None,
        status="active",
        missing_scan_count=0,
        source_updated_at=_to_utc(listing.source_updated_at),
        first_seen_at=now,
        last_seen_at=now,
        last_scraped_at=now,
        # source_id n'est renseigné que par le parseur de page détail:
        # sa présence signale que la page individuelle a bien été lue.
        detail_scraped_at=now if (listing.source_id or listing.page_gone) else None,
    )
    db_listing.description = build_contact_description(db_listing)
    # Après build_contact_description: `description` est un champ de hash.
    db_listing.content_hash = compute_content_hash(db_listing)
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
    now: datetime,
) -> None:
    """Met à jour tous les champs d'une annonce existante.

    N'écrit pas `content_hash`: il se calcule sur la ligne résultante, une
    fois tous les champs posés (voir upsert_listing).
    """
    db_listing.source_id = listing.source_id or db_listing.source_id
    db_listing.name = listing.name
    db_listing.address = listing.address or db_listing.address
    db_listing.subdistrict = listing.subdistrict or db_listing.subdistrict
    db_listing.district = listing.district or db_listing.district
    db_listing.province = listing.province or db_listing.province
    if listing.latitude is not None and listing.longitude is not None:
        db_listing.latitude, db_listing.longitude = listing.latitude, listing.longitude
    db_listing.price_monthly_raw = listing.price_monthly_raw
    db_listing.price_monthly_min = listing.price_monthly_min
    db_listing.price_monthly_max = listing.price_monthly_max
    db_listing.daily_price_raw = listing.daily_price_raw
    db_listing.daily_price_min = listing.daily_price_min
    db_listing.daily_price_max = listing.daily_price_max
    apply_contract_fields(db_listing, listing)
    db_listing.is_verified = listing.is_verified
    db_listing.has_promotion = listing.has_promotion
    db_listing.source_updated_at = _to_utc(listing.source_updated_at)
    db_listing.last_seen_at = now
    db_listing.last_scraped_at = now

    # Enrichissements depuis la page détail
    if listing.amenities:
        db_listing.amenities = json.dumps(listing.amenities)
    if listing.room_types:
        db_listing.room_types = json.dumps([r.model_dump() for r in listing.room_types])
    apply_detail_fields(db_listing, listing)
    # source_id n'est renseigne que par un scrape detail reussi; une page
    # disparue (page_gone) compte aussi comme lue, sinon elle serait
    # redemandee a chaque scan jusqu'a son retrait.
    if listing.source_id or listing.page_gone:
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
    listing: Listing | int,
    change_type: str,
    changed_at: datetime,
    field_name: Optional[str] = None,
    old_value: Optional[str] = None,
    new_value: Optional[str] = None,
) -> None:
    entry = ListingHistory(
        listing_id=listing if isinstance(listing, int) else listing.id,
        changed_at=changed_at,
        change_type=change_type,
        field_name=field_name,
        old_value=old_value,
        new_value=new_value,
    )
    session.add(entry)
