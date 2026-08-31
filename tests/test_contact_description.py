"""Tests pour build_contact_description (format standardisé de la
description: Deposit / Electric price / Air Conditioner)."""
from __future__ import annotations

import json

from src.database.models import Listing
from src.normalizers.contact_description import build_contact_description


def _make_listing(
    *,
    deposit: str | None = None,
    electric_price: str | None = None,
    amenities: list[str] | None = None,
) -> Listing:
    return Listing(
        slug="test-listing",
        name="Test Listing",
        url="https://www.renthub.in.th/en/test-listing",
        deposit=deposit,
        electric_price=electric_price,
        amenities=json.dumps(amenities) if amenities is not None else None,
    )


def test_full_listing_has_all_three_lines():
    listing = _make_listing(
        deposit="7500 Baht",
        electric_price="9 THB/unit",
        amenities=["Air Conditioner"],
    )
    assert build_contact_description(listing) == (
        "Deposit: 7500 Baht\n"
        "Electric price: 9 THB/unit\n"
        "Air Conditioner : YES"
    )


def test_no_air_conditioner_amenity_shows_no():
    listing = _make_listing(
        deposit="1 month",
        electric_price="8 THB/unit",
        amenities=["WIFI", "Fan"],
    )
    assert build_contact_description(listing) == (
        "Deposit: 1 month\n"
        "Electric price: 8 THB/unit\n"
        "Air Conditioner : NO"
    )


def test_missing_amenities_shows_no():
    listing = _make_listing(deposit="1 month", electric_price="8 THB/unit", amenities=None)
    assert build_contact_description(listing) == (
        "Deposit: 1 month\n"
        "Electric price: 8 THB/unit\n"
        "Air Conditioner : NO"
    )


def test_missing_deposit_leaves_label_blank():
    listing = _make_listing(deposit=None, electric_price="8 THB/unit", amenities=[])
    assert build_contact_description(listing) == (
        "Deposit:\n"
        "Electric price: 8 THB/unit\n"
        "Air Conditioner : NO"
    )


def test_missing_electric_price_omits_the_line():
    listing = _make_listing(deposit="2500 Baht", electric_price=None, amenities=["Air Conditioner"])
    assert build_contact_description(listing) == (
        "Deposit: 2500 Baht\n"
        "Air Conditioner : YES"
    )


def test_please_contact_electric_price_omits_the_line():
    """"Please contact" n'est pas un vrai tarif: la ligne doit disparaitre,
    pas juste s'afficher vide (cf. Deposit qui, lui, garde 'Please contact')."""
    listing = _make_listing(
        deposit="Please contact",
        electric_price="Please contact",
        amenities=["Air Conditioner"],
    )
    assert build_contact_description(listing) == (
        "Deposit: Please contact\n"
        "Air Conditioner : YES"
    )


def test_please_contact_deposit_is_kept_alongside_real_electric_price():
    """"Please contact" ne doit disparaitre que pour Electric price:
    sur Deposit, la valeur reste affichée telle quelle, meme quand
    Electric price a un vrai tarif a cote."""
    listing = _make_listing(
        deposit="Please contact",
        electric_price="8 THB/unit",
        amenities=["Air Conditioner"],
    )
    assert build_contact_description(listing) == (
        "Deposit: Please contact\n"
        "Electric price: 8 THB/unit\n"
        "Air Conditioner : YES"
    )


def test_description_never_contains_phone_or_line():
    """Régression: le contact (phone/LINE) a été retiré de la description
    — il reste dans ses propres colonnes (listing.phone/line_id/whatsapp)
    affichées séparément côté frontend."""
    listing = _make_listing(deposit="1 month", electric_price="8 THB/unit", amenities=[])
    listing.phone = "0817322385"
    listing.line_id = "@274bmlpb"

    result = build_contact_description(listing)
    assert "0817322385" not in result
    assert "274bmlpb" not in result
    assert "LINE" not in result
    assert "📞" not in result
