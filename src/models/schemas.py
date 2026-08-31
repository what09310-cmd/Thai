from __future__ import annotations
from typing import Optional, Literal
from datetime import datetime
from pydantic import BaseModel, HttpUrl, field_validator


class RoomTypeSchema(BaseModel):
    name: str
    room_type: Optional[str] = None
    size_sqm: Optional[float] = None
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
    published_at: Optional[datetime] = None
    # True quand le JSON __NEXT_DATA__ de la page a pu etre lu: contacts
    # et charges viennent alors directement de la source et font autorite,
    # y compris quand ils sont vides (voir apply_detail_fields).
    has_structured_data: bool = False


class ListingFull(ListingRaw, ListingDetail):
    """Données complètes fusionnées."""
    pass
