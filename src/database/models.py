from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Listing(Base):
    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(50), default="renthub", nullable=False)
    source_id: Mapped[Optional[str]] = mapped_column(String(100))  # Listing no
    slug: Mapped[str] = mapped_column(String(500), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    url: Mapped[str] = mapped_column(String(1000), nullable=False)

    # Localisation
    address: Mapped[Optional[str]] = mapped_column(String(500))
    subdistrict: Mapped[Optional[str]] = mapped_column(String(200))
    district: Mapped[Optional[str]] = mapped_column(String(200))
    province: Mapped[Optional[str]] = mapped_column(String(200))
    latitude: Mapped[Optional[float]] = mapped_column(Float)
    longitude: Mapped[Optional[float]] = mapped_column(Float)

    # Prix affichés (entête carte)
    price_monthly_raw: Mapped[Optional[str]] = mapped_column(String(100))
    price_monthly_min: Mapped[Optional[int]] = mapped_column(Integer)
    price_monthly_max: Mapped[Optional[int]] = mapped_column(Integer)
    daily_price_raw: Mapped[Optional[str]] = mapped_column(String(100))
    daily_price_min: Mapped[Optional[int]] = mapped_column(Integer)
    daily_price_max: Mapped[Optional[int]] = mapped_column(Integer)

    # Contrats structurés
    contract_monthly_raw: Mapped[Optional[str]] = mapped_column(String(100))
    contract_monthly_min: Mapped[Optional[int]] = mapped_column(Integer)
    contract_monthly_max: Mapped[Optional[int]] = mapped_column(Integer)
    contract_3_month_raw: Mapped[Optional[str]] = mapped_column(String(100))
    contract_3_month_min: Mapped[Optional[int]] = mapped_column(Integer)
    contract_3_month_max: Mapped[Optional[int]] = mapped_column(Integer)
    contract_6_month_raw: Mapped[Optional[str]] = mapped_column(String(100))
    contract_6_month_min: Mapped[Optional[int]] = mapped_column(Integer)
    contract_6_month_max: Mapped[Optional[int]] = mapped_column(Integer)

    has_monthly_contract: Mapped[str] = mapped_column(
        String(10), default="unknown"
    )  # "true" | "false" | "unknown"

    # Détails
    description: Mapped[Optional[str]] = mapped_column(Text)
    amenities: Mapped[Optional[str]] = mapped_column(Text)       # JSON list
    room_types: Mapped[Optional[str]] = mapped_column(Text)      # JSON list
    deposit: Mapped[Optional[str]] = mapped_column(String(200))
    advance_payment: Mapped[Optional[str]] = mapped_column(String(200))
    electric_price: Mapped[Optional[str]] = mapped_column(String(100))
    water_price: Mapped[Optional[str]] = mapped_column(String(100))
    service_fee: Mapped[Optional[str]] = mapped_column(String(200))
    phone: Mapped[Optional[str]] = mapped_column(String(100))
    line_id: Mapped[Optional[str]] = mapped_column(String(100))
    # None: jamais verifie par scripts/verify_line_ids.py. True/False: un
    # compte/page LINE existe (ou non) derriere ce line_id au moment du
    # dernier passage -- voir _clean_line_id et check_line_id. Le lien
    # LINE affiche cote frontend s'appuie dessus pour eviter un 404 sur
    # un identifiant deja connu comme invalide.
    line_verified: Mapped[Optional[bool]] = mapped_column(Boolean)
    whatsapp: Mapped[Optional[str]] = mapped_column(String(100))
    email: Mapped[Optional[str]] = mapped_column(String(200))

    # Flags
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    has_promotion: Mapped[bool] = mapped_column(Boolean, default=False)

    # Tracking
    status: Mapped[str] = mapped_column(String(20), default="active")
    # "active" | "removed"
    missing_scan_count: Mapped[int] = mapped_column(Integer, default=0)
    content_hash: Mapped[Optional[str]] = mapped_column(String(64))

    # Timestamps
    source_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_scraped_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Dernier scrape de la *page detail* (last_scraped_at, lui, avance a
    # chaque scan, meme quand seule la page de liste a ete lue).
    detail_scraped_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relations
    images: Mapped[list["ListingImage"]] = relationship(
        "ListingImage", back_populates="listing", cascade="all, delete-orphan"
    )
    history: Mapped[list["ListingHistory"]] = relationship(
        "ListingHistory", back_populates="listing", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_listings_province", "province"),
        Index("ix_listings_status", "status"),
        Index("ix_listings_has_monthly", "has_monthly_contract"),
        Index("ix_listings_price_monthly_min", "price_monthly_min"),
        # Tri par defaut de GET /listings. Sans lui, chaque appel faisait un
        # SCAN de la table suivi d'un USE TEMP B-TREE FOR ORDER BY.
        Index("ix_listings_source_updated_at", "source_updated_at"),
        # Compteur "nouvelles aujourd'hui" de /stats.
        Index("ix_listings_first_seen_at", "first_seen_at"),
    )


class ListingImage(Base):
    __tablename__ = "listing_images"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("listings.id", ondelete="CASCADE"), nullable=False
    )
    image_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0)
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    exclusion_reason: Mapped[Optional[str]] = mapped_column(String(200))
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    listing: Mapped["Listing"] = relationship("Listing", back_populates="images")

    __table_args__ = (
        # Cle etrangere sans index: les routes de liste balayaient les
        # ~60 000 lignes de la table a chaque requete pour n'en tirer qu'une
        # vignette par annonce (SCAN listing_images).
        Index("ix_listing_images_listing_id", "listing_id"),
        # Une URL par annonce: l'ancien calcul de position en avait inscrit
        # certaines deux fois (scripts/migrate.py dedoublonne l'existant).
        UniqueConstraint("listing_id", "image_url", name="uq_listing_images_listing_url"),
    )


class ListingHistory(Base):
    __tablename__ = "listing_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("listings.id", ondelete="CASCADE"), nullable=False
    )
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    change_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # "NEW" | "UPDATED" | "PRICE_CHANGED" | "REMOVED" | "REACTIVATED"
    field_name: Mapped[Optional[str]] = mapped_column(String(100))
    old_value: Mapped[Optional[str]] = mapped_column(Text)
    new_value: Mapped[Optional[str]] = mapped_column(Text)

    listing: Mapped["Listing"] = relationship("Listing", back_populates="history")

    __table_args__ = (
        Index("ix_history_listing_id", "listing_id"),
        Index("ix_history_changed_at", "changed_at"),
        # Compteurs "du jour" de /stats: WHERE change_type = ? AND changed_at >= ?.
        # Un index sur change_type seul (5 valeurs) ne servait a rien.
        Index("ix_history_type_changed_at", "change_type", "changed_at"),
    )


class ScanLog(Base):
    __tablename__ = "scan_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    pages_scanned: Mapped[int] = mapped_column(Integer, default=0)
    listings_found: Mapped[int] = mapped_column(Integer, default=0)
    new_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, default=0)
    price_changed_count: Mapped[int] = mapped_column(Integer, default=0)
    removed_count: Mapped[int] = mapped_column(Integer, default=0)
    monthly_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="running")
    # "running" | "completed" | "failed"
    error_message: Mapped[Optional[str]] = mapped_column(Text)


class User(Base):
    """Compte utilisateur (email + mot de passe, ou Google).

    `password_hash` est None pour un compte cree via Google et jamais dote
    d'un mot de passe; `google_sub` est l'identifiant stable fourni par
    Google (l'email peut changer, pas le `sub`). `is_premium` decide l'acces
    aux pages et champs payants (src/api/main.py::PROTECTED_PATHS).
    """
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    password_hash: Mapped[Optional[str]] = mapped_column(String(100))
    google_sub: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    is_premium: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
