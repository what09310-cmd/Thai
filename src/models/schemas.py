from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel


class RoomTypeSchema(BaseModel):
    name: str
    room_type: Optional[str] = None
    size_sqm: Optional[float] = None
    monthly_min_thb: Optional[int] = None
    monthly_max_thb: Optional[int] = None
    contract_1_month_thb: Optional[int] = None
    contract_3_month_thb: Optional[int] = None
    contract_6_month_thb: Optional[int] = None
    daily_thb: Optional[int] = None
    status: Optional[str] = None


class ListingRaw(BaseModel):
    """Données brutes extraites de la page de liste (une carte)."""
    name: str
    url: str
    slug: str

    # Localisation
    address: Optional[str] = None
    subdistrict: Optional[str] = None
    district: Optional[str] = None
    province: Optional[str] = None

    # Prix affiché en entête de carte
    price_monthly_raw: Optional[str] = None
    price_monthly_min: Optional[int] = None
    price_monthly_max: Optional[int] = None
    daily_price_raw: Optional[str] = None
    daily_price_min: Optional[int] = None
    daily_price_max: Optional[int] = None

    # Contrats structurés
    contract_monthly_raw: Optional[str] = None
    contract_monthly_min: Optional[int] = None
    contract_monthly_max: Optional[int] = None
    contract_3_month_raw: Optional[str] = None
    contract_3_month_min: Optional[int] = None
    contract_3_month_max: Optional[int] = None
    contract_6_month_raw: Optional[str] = None
    contract_6_month_min: Optional[int] = None
    contract_6_month_max: Optional[int] = None

    has_monthly_contract: Literal["true", "false", "unknown"] = "unknown"

    # True quand la source de cette carte porte vraiment le bloc de contrats
    # court terme (1/3/6 mois). Les pages "par lieu"
    # (/en/short-term-rental/<slug>, option --include-locations) donnent bien
    # `price.monthly` mais pas `shortTerm.*.shortContract`: sans ce drapeau,
    # elles écrasaient par None les contrats déjà connus d'une annonce vue
    # auparavant sur /browse/short-term-monthly. Voir
    # change_detector._update_listing_fields.
    from_structured_list: bool = False

    # Métadonnées
    source_updated_at: Optional[datetime] = None
    thumbnail_url: Optional[str] = None
    is_verified: bool = False
    has_promotion: bool = False


class ListingDetail(BaseModel):
    """Données enrichies depuis la page individuelle."""
    source_id: Optional[str] = None  # Listing no
    description: Optional[str] = None
    amenities: list[str] = []
    room_types: list[RoomTypeSchema] = []
    phone: Optional[str] = None
    line_id: Optional[str] = None
    whatsapp: Optional[str] = None
    email: Optional[str] = None
    deposit: Optional[str] = None
    advance_payment: Optional[str] = None
    electric_price: Optional[str] = None
    water_price: Optional[str] = None
    service_fee: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    images: list[str] = []
    # True quand le JSON __NEXT_DATA__ de la page a pu etre lu: contacts
    # et charges viennent alors directement de la source et font autorite,
    # y compris quand ils sont vides (voir apply_detail_fields).
    has_structured_data: bool = False
    # True quand la page detail n'existe plus (404, redirection hors site):
    # ce n'est pas un echec de scrape mais une information -- l'annonce sera
    # retiree par la page de liste au bout de N scans. Marquer
    # detail_scraped_at evite de redemander cette page a chaque scan.
    page_gone: bool = False


class ListingFull(ListingRaw, ListingDetail):
    """Données complètes fusionnées."""
    pass
