"""
Tests de non-regression pour la migration BeautifulSoup -> Scrapling.

Les fixtures HTML sont des pages reelles de renthub.in.th, capturees une
fois. Les fichiers .golden.json contiennent la sortie du parser BS4
d'origine: si la migration Scrapling change le resultat, ces tests
echouent.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import src.parser.list_parser as list_parser
from scrapling.parser import Selector
from src.parser.detail_parser import parse_detail_page
from src.parser.list_parser import parse_listing_page

FIXTURES = Path(__file__).parent / "fixtures"
DETAIL_URL = "https://www.renthub.in.th/en/krongthongmansion-bangkapi"


def _load_golden(name: str):
    with open(FIXTURES / name, encoding="utf-8") as f:
        return json.load(f)


def _to_json(model) -> dict:
    return json.loads(model.model_dump_json())


def test_list_page_json_path_matches_golden():
    """Page avec __NEXT_DATA__: chemin JSON, inchange par la migration."""
    html = (FIXTURES / "list_page.html").read_text(encoding="utf-8")
    listings = parse_listing_page(html)
    golden = _load_golden("list_page.golden.json")

    assert len(listings) == len(golden)
    assert [_to_json(l) for l in listings] == golden


def test_list_page_fallback_path_matches_golden():
    """Page sans __NEXT_DATA__: chemin de repli, migre vers Scrapling."""
    html = (FIXTURES / "list_page_no_json.html").read_text(encoding="utf-8")
    listings = parse_listing_page(html)
    golden = _load_golden("list_page_no_json.golden.json")

    assert len(listings) == len(golden)
    assert [_to_json(l) for l in listings] == golden


def test_detail_page_matches_golden():
    html = (FIXTURES / "detail_page.html").read_text(encoding="utf-8")
    detail = parse_detail_page(html, DETAIL_URL)
    golden = _load_golden("detail_page.golden.json")

    assert detail is not None
    assert _to_json(detail) == golden


def test_detail_page_amenities_first_item_absent_matches_golden():
    """
    Régression: quand le tout premier équipement de la grille (Air
    Conditioner, toujours en haut à gauche) est barré/absent sur la page,
    `_extract_amenities_from_icons` le rattachait à tort au div englobant
    de la grille (jamais barré) et le rapportait "présent" malgré tout.
    """
    html = (FIXTURES / "detail_page_ac_absent.html").read_text(encoding="utf-8")
    detail = parse_detail_page(html, "https://www.renthub.in.th/en/baanploy7")
    golden = _load_golden("detail_page_ac_absent.golden.json")

    assert detail is not None
    assert "Air Conditioner" not in detail.amenities
    assert _to_json(detail) == golden


def test_adaptive_relocation_survives_label_rename(tmp_path, monkeypatch):
    """
    Simule un redesign renthub (le libellé "Contract monthly" est
    renommé) et verifie que _find_contract_cards retrouve quand meme
    les cartes via le fingerprint adaptatif sauvegardé lors d'un run
    precedent, sans passer par la strategie 2 (repli THB/lien) qui
    masquerait sinon toute regression du mecanisme adaptatif.
    """
    db_path = tmp_path / "elements_storage.db"
    monkeypatch.setattr(list_parser, "_ADAPTIVE_DB_PATH", str(db_path))

    def make_page(html: str) -> Selector:
        return Selector(
            html, adaptive=True, storage_args={"storage_file": str(db_path), "url": ""}
        )

    original_html = (FIXTURES / "list_page_no_json.html").read_text(encoding="utf-8")
    renamed_html = (FIXTURES / "list_page_relabeled.html").read_text(encoding="utf-8")

    baseline_cards = list_parser._find_contract_cards(make_page(original_html))
    assert len(baseline_cards) == 40

    relocated_cards = list_parser._find_contract_cards(make_page(renamed_html))
    assert len(relocated_cards) == 40
