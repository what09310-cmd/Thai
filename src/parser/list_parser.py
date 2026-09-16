"""
Parse les pages /en/browse/short-term-monthly.
Structure réelle: <li class="css-18heyf0"> contenant chaque annonce.
"""
from __future__ import annotations

import json
import re
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from scrapling.parser import Selector

from src.models.schemas import ListingRaw
from src.normalizers.price import parse_price_range
from src.filters.contract import has_monthly_contract

log = logging.getLogger(__name__)
BANGKOK_TZ = ZoneInfo("Asia/Bangkok")
BASE_URL = "https://www.renthub.in.th"
CDN_URL = "https://bcdn.renthub.in.th"

# Base de fingerprints adaptatifs Scrapling (locale, pas versionnée).
# Un seul identifiant partagé entre toutes les pages: le gabarit de
# carte "Contract monthly" est le même sur tout le site.
_ADAPTIVE_DB_PATH = Path(__file__).resolve().parents[2] / ".scrapling" / "elements_storage.db"
_ADAPTIVE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
_ADAPTIVE_DB_PATH = str(_ADAPTIVE_DB_PATH)
_CARD_IDENTIFIER = "listing-card"

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)


def extract_next_data(html: str) -> Optional[dict]:
    """
    Extrait le JSON __NEXT_DATA__ embarqué (Next.js) dans la page.
    Contient les données structurées de chaque annonce (prix,
    shortContract par durée 1/3/6 mois, province/district/subdistrict
    exacts) — bien plus fiable que le grattage du texte affiché.
    """
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _fmt_price(min_v, max_v, suffix: str) -> Optional[str]:
    if min_v is None:
        return None
    if min_v == 0 and (max_v or 0) == 0:
        return None
    if max_v is None or min_v == max_v:
        return f"{min_v:,} {suffix}"
    return f"{min_v:,} - {max_v:,} {suffix}"


def _parse_next_data_datetime(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def listing_from_json_item(item: dict) -> Optional[ListingRaw]:
    """Convertit un item `pageProps.listings[i]` du JSON en ListingRaw."""
    slug = item.get("slug")
    name = item.get("name")
    if not slug or not name:
        return None

    price = item.get("price") or {}
    monthly = price.get("monthly") or {}
    daily = price.get("daily") or {}
    short_term = price.get("shortTerm") or {}

    price_monthly_raw = price_monthly_min = price_monthly_max = None
    if monthly.get("type") == "AMOUNT":
        price_monthly_min = monthly.get("minPrice")
        price_monthly_max = monthly.get("maxPrice")
        price_monthly_raw = _fmt_price(price_monthly_min, price_monthly_max, "THB/month")

    daily_price_raw = daily_price_min = daily_price_max = None
    if daily.get("type") == "AMOUNT":
        daily_price_min = daily.get("minPrice")
        daily_price_max = daily.get("maxPrice")
        daily_price_raw = _fmt_price(daily_price_min, daily_price_max, "THB/day")

    def term(period: str):
        d = short_term.get(period) or {}
        if not d.get("shortContract"):
            return None, None, None
        mn, mx = d.get("minPrice"), d.get("maxPrice")
        return _fmt_price(mn, mx, "THB/month"), mn, mx

    contract_monthly_raw, contract_monthly_min, contract_monthly_max = term("oneMonth")
    contract_3_month_raw, contract_3_month_min, contract_3_month_max = term("threeMonth")
    contract_6_month_raw, contract_6_month_min, contract_6_month_max = term("sixMonth")

    # Présence du bloc `oneMonth`: seule preuve que cette page décrit vraiment
    # l'offre court terme de l'annonce. Absent (pages /short-term-rental/<slug>),
    # l'absence de contrat ne veut rien dire et ne doit rien écraser en base.
    # Présent avec shortContract=false, en revanche, l'information "pas de
    # contrat 1 mois" est réelle et fait autorité.
    structured = "oneMonth" in short_term
    if structured:
        monthly_status = "true" if contract_monthly_raw else "false"
    else:
        monthly_status = "unknown"

    cover = item.get("coverPicture")
    thumbnail_url = f"{CDN_URL}{cover}" if cover else None

    promotion = item.get("promotion") or {}
    address_doc = item.get("addressDocument") or {}

    address_parts = [
        item.get("street"),
        item.get("road"),
        item.get("subdistrict"),
        item.get("district"),
        item.get("province"),
    ]
    address = " ".join(p.strip() for p in address_parts if p and p.strip()) or None

    return ListingRaw(
        name=name,
        url=f"{BASE_URL}/en/{slug}",
        slug=slug,
        address=address,
        subdistrict=item.get("subdistrict"),
        district=item.get("district"),
        province=item.get("province"),
        price_monthly_raw=price_monthly_raw,
        price_monthly_min=price_monthly_min,
        price_monthly_max=price_monthly_max,
        daily_price_raw=daily_price_raw,
        daily_price_min=daily_price_min,
        daily_price_max=daily_price_max,
        contract_monthly_raw=contract_monthly_raw,
        contract_monthly_min=contract_monthly_min,
        contract_monthly_max=contract_monthly_max,
        contract_3_month_raw=contract_3_month_raw,
        contract_3_month_min=contract_3_month_min,
        contract_3_month_max=contract_3_month_max,
        contract_6_month_raw=contract_6_month_raw,
        contract_6_month_min=contract_6_month_min,
        contract_6_month_max=contract_6_month_max,
        has_monthly_contract=monthly_status,
        from_structured_list=structured,
        source_updated_at=_parse_next_data_datetime(
            item.get("updatedAt") or item.get("modifiedAt")
        ),
        thumbnail_url=thumbnail_url,
        is_verified=address_doc.get("reviewStatus") == "VERIFIED",
        has_promotion=promotion.get("type") not in (None, "NO_PROMOTION"),
    )


def parse_listing_page_json(html: str) -> Optional[list[ListingRaw]]:
    """
    Parse une page de listing via le JSON __NEXT_DATA__ embarqué.
    Retourne None si le JSON est absent/illisible (l'appelant doit
    alors se rabattre sur le parsing HTML).
    """
    data = extract_next_data(html)
    if not data:
        return None

    try:
        items = data["props"]["pageProps"]["listings"]
    except (KeyError, TypeError):
        return None

    if not isinstance(items, list):
        return None

    listings = []
    seen_slugs = set()
    for item in items:
        # Un seul item hors format (prix en chaine, `price` qui n'est pas un
        # objet...) faisait echouer la page entiere, et l'exception remontait
        # jusqu'a run_scraper: le scan complet tombait pour une carte. On
        # ignore la carte et on garde les autres.
        try:
            result = listing_from_json_item(item) if isinstance(item, dict) else None
        except Exception as e:
            log.warning(
                "Carte JSON ignoree (%s): %s",
                e,
                item.get("slug") if isinstance(item, dict) else item,
            )
            continue
        if result and result.slug not in seen_slugs:
            seen_slugs.add(result.slug)
            listings.append(result)
    return listings


def extract_last_page_json(html: str) -> Optional[int]:
    """Lit `pagination.totalPages` depuis le JSON __NEXT_DATA__.

    La valeur est validee comme un entier >= 1: telle quelle, une chaine
    ou un null seraient passes a `min()` et `range()` dans list_scraper, qui
    levent TypeError -- fin du scan pour une page d'entete inhabituelle.
    """
    data = extract_next_data(html)
    if not data:
        return None
    try:
        total = data["props"]["pageProps"]["pagination"]["totalPages"]
    except (KeyError, TypeError):
        return None
    if isinstance(total, bool):
        return None
    if isinstance(total, str) and total.isdigit():
        total = int(total)
    if not isinstance(total, int) or total < 1:
        return None
    return total


# Slugs de pages utilitaires (nav/footer) à ignorer dans la stratégie
# de repli, pour ne pas les confondre avec de vraies annonces.
_NON_LISTING_SLUGS = {
    "agreements", "contact", "privacy", "packages",
    "nearby-apartment", "nearby-short-term-rental",
}


def parse_listing_page(html: str) -> list[ListingRaw]:
    """
    Parse une page de listing.

    Essaie d'abord le JSON __NEXT_DATA__ embarqué (fiable: prix exacts,
    shortContract par durée, localisation exacte). Si absent/illisible,
    se rabat sur le grattage HTML (deux structures de carte coexistent:
    cartes "Contract monthly" structurées, et cartes "par lieu" avec
    juste un prix THB/month/day).
    """
    from_json = parse_listing_page_json(html)
    if from_json is not None:
        log.info(f"Annonces parsées (JSON): {len(from_json)}")
        return from_json

    page = Selector(
        html, adaptive=True, storage_args={"storage_file": _ADAPTIVE_DB_PATH, "url": ""}
    )
    listings: list[ListingRaw] = []
    seen_slugs: set[str] = set()
    seen_cards: set[int] = set()

    # Stratégie 1: <li> contenant "Contract monthly"
    for card in _find_contract_cards(page):
        # Identite du noeud lxml sous-jacent: Scrapling reconstruit un
        # wrapper Selector neuf a chaque requete, id(card) ne matcherait
        # jamais entre les deux strategies.
        seen_cards.add(id(card._root))
        result = _parse_card(card)
        if result and result.slug not in seen_slugs:
            seen_slugs.add(result.slug)
            listings.append(result)

    # Stratégie 2 (repli): <li> avec un lien annonce + un prix "THB",
    # sans label "Contract monthly" structuré (ex: pages par lieu/zone).
    for card in page.css("li"):
        if id(card._root) in seen_cards:
            continue
        text = card.get_all_text(separator=" ", strip=True)
        if "THB" not in text:
            continue

        link = None
        for a in card.css("a[href]"):
            href = a.attrib.get("href", "")
            if href.startswith("/en/") and href.count("/") == 2:
                slug_candidate = href.strip("/").split("/")[-1]
                if slug_candidate in _NON_LISTING_SLUGS:
                    continue
                link = a
                break
        if not link:
            continue

        result = _parse_card(card)
        if result and result.slug not in seen_slugs:
            seen_slugs.add(result.slug)
            listings.append(result)

    log.info(f"Annonces parsées: {len(listings)}")
    return listings


def _find_contract_cards(page: Selector) -> list[Selector]:
    """
    Localise les <li> contenant un label "Contract monthly", en remontant
    7 niveaux depuis le texte.

    Si la recherche textuelle ne trouve plus rien (renthub a changé le
    libellé ou la profondeur du DOM), on retombe sur le fingerprint de
    la dernière carte connue: on la relocalise sur la page courante par
    similarité structurelle, puis on récupère ses "voisines" du même
    gabarit via find_similar(). Sur un run réussi, on rafraîchit le
    fingerprint pour le prochain run.
    """
    labels = page.find_by_text("Contract monthly", first_match=False)
    cards: list[Selector] = []

    if labels:
        for label in labels:
            node = label
            card = None
            for _ in range(7):
                node = getattr(node, "parent", None)
                if node is None:
                    break
                if getattr(node, "tag", None) == "li":
                    card = node
                    break
            if card is not None:
                cards.append(card)

        if cards:
            page.save(cards[0], identifier=_CARD_IDENTIFIER)
        return cards

    log.warning("Aucun label 'Contract monthly' trouvé, tentative de relocalisation adaptative")
    saved = page.retrieve(_CARD_IDENTIFIER)
    if not saved:
        return []

    relocated = page.relocate(saved, selector_type=True)
    if not relocated:
        log.warning("Relocalisation adaptative: aucun candidat au-dessus du seuil")
        return []

    anchor = relocated[0]
    similar = anchor.find_similar()
    log.info(f"Relocalisation adaptative: {len(similar) + 1} cartes retrouvées")
    return [anchor, *similar]


def _parse_card(card: Selector) -> Optional[ListingRaw]:
    try:
        full_text = card.get_all_text(separator="\n", strip=True)
        lines = [l.strip() for l in full_text.split("\n") if l.strip()]

        # Nom et URL: premier lien avec /en/
        link = None
        for a in card.css("a[href]"):
            href = a.attrib.get("href", "")
            if href.startswith("/en/") and href.count("/") == 2:
                link = a
                break
        if not link:
            return None

        href = link.attrib.get("href", "")
        name = link.text.strip() if link.text else None
        if not name:
            # Parfois le nom est dans un élément sibling
            name = _find_name_in_card(card)
        if not name:
            return None

        url = BASE_URL + href
        slug = href.strip("/").split("/")[-1]

        # Adresse: chercher une ligne contenant une province connue
        address_raw = _find_address(lines, name)
        subdistrict, district, province = _parse_location(address_raw)

        # Prix principal (ex: "14,000 - 42,000 THB/month")
        price_monthly_raw = _extract_price(full_text, "THB/month")
        price_monthly_min, price_monthly_max = parse_price_range(price_monthly_raw)

        daily_price_raw = _extract_price(full_text, "THB/day")
        daily_price_min, daily_price_max = parse_price_range(daily_price_raw)

        # Contrats structurés: extraire la valeur qui suit chaque label
        contract_monthly_raw = _extract_contract_value(lines, "Contract monthly")
        contract_3_month_raw = _extract_contract_value(lines, "Contract 3 month")
        contract_6_month_raw = _extract_contract_value(lines, "Contract 6 month")

        contract_monthly_min, contract_monthly_max = parse_price_range(contract_monthly_raw)
        contract_3_month_min, contract_3_month_max = parse_price_range(contract_3_month_raw)
        contract_6_month_min, contract_6_month_max = parse_price_range(contract_6_month_raw)

        monthly_status = has_monthly_contract(contract_monthly_raw)

        # Date
        source_updated_at = _extract_date(full_text)

        # Image
        img = next(
            (i for i in card.css("img") if "bcdn.renthub" in (i.attrib.get("src") or "")),
            None,
        )
        thumbnail_url = img.attrib["src"].split("?")[0] if img else None

        return ListingRaw(
            name=name,
            url=url,
            slug=slug,
            address=address_raw,
            subdistrict=subdistrict,
            district=district,
            province=province,
            price_monthly_raw=price_monthly_raw,
            price_monthly_min=price_monthly_min,
            price_monthly_max=price_monthly_max,
            daily_price_raw=daily_price_raw,
            daily_price_min=daily_price_min,
            daily_price_max=daily_price_max,
            contract_monthly_raw=contract_monthly_raw,
            contract_monthly_min=contract_monthly_min,
            contract_monthly_max=contract_monthly_max,
            contract_3_month_raw=contract_3_month_raw,
            contract_3_month_min=contract_3_month_min,
            contract_3_month_max=contract_3_month_max,
            contract_6_month_raw=contract_6_month_raw,
            contract_6_month_min=contract_6_month_min,
            contract_6_month_max=contract_6_month_max,
            has_monthly_contract=monthly_status,
            # Le label "Contract monthly" est le pendant HTML du bloc
            # `shortTerm.oneMonth` du JSON: sans lui, la carte vient d'une
            # page par lieu et son absence de contrat n'apprend rien.
            from_structured_list=contract_monthly_raw is not None,
            source_updated_at=source_updated_at,
            thumbnail_url=thumbnail_url,
            is_verified=_is_verified(full_text),
            has_promotion="PROMOTION" in full_text,
        )
    except Exception as e:
        log.warning(f"Erreur parsing carte: {e}")
        return None


# RentHub affiche "This is not verified listing" sur les annonces dont
# l'adresse n'a PAS été vérifiée: chercher "verified" dans le texte de la
# carte y matche et rendait `is_verified` systématiquement vrai. On exige
# donc un marqueur positif, et on écarte d'abord toute forme négative.
_NOT_VERIFIED_RE = re.compile(r"\b(?:not|non|un)[\s-]*verified\b", re.I)
_VERIFIED_RE = re.compile(r"\bverified\b", re.I)


def _is_verified(full_text: str) -> bool:
    """Équivalent HTML de `addressDocument.reviewStatus == "VERIFIED"`."""
    if _NOT_VERIFIED_RE.search(full_text):
        return False
    return bool(_VERIFIED_RE.search(full_text))


def _extract_contract_value(lines: list[str], label: str) -> Optional[str]:
    """
    Cherche le label dans les lignes et retourne la ligne suivante.
    Ex: lines = [..., "Contract monthly", "14,000 - 36,000 THB/month", ...]

    Le prix et l'unité "THB/month" peuvent atterrir sur la même ligne ou
    sur deux lignes séparées selon la façon dont le parseur HTML découpe
    les nœuds texte autour des commentaires React vides du markup source
    (`<span>5,400 - 6,900<!-- --> <!-- -->THB/month</span>`) : on gère
    les deux cas pour ne pas dépendre de ce détail d'implémentation.
    """
    for i, line in enumerate(lines):
        if line.strip() == label:
            if i + 1 >= len(lines):
                return None
            val = lines[i + 1].strip()
            if val == "-":
                return val
            if not re.search(r"[\d,]+", val):
                return None
            if "THB" not in val.upper() and i + 2 < len(lines) and lines[i + 2].strip().upper().startswith("THB"):
                val = f"{val} {lines[i + 2].strip()}"
            return val
    return None


def _find_name_in_card(card: Selector) -> Optional[str]:
    """Cherche le nom de l'annonce dans la carte."""
    for tag in ["h2", "h3", "h4", "strong", "b"]:
        el = card.find(tag)
        if el:
            t = el.text.strip() if el.text else None
            if t and len(t) > 3:
                return t
    return None


def _extract_price(text: str, suffix: str) -> Optional[str]:
    m = re.search(r"([\d,]+(?:\s*-\s*[\d,]+)?)\s*" + re.escape(suffix), text)
    return m.group(0).strip() if m else None


def _extract_date(text: str) -> Optional[datetime]:
    m = re.search(r"(\d{2}/\d{2}/\d{4}\s+\d{1,2}:\d{2})", text)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1), "%d/%m/%Y %H:%M")
        return dt.replace(tzinfo=BANGKOK_TZ)
    except ValueError:
        return None


PROVINCES_LIST = [
    "Bangkok", "Chiang Mai", "Phuket", "Chonburi", "Pathumthani",
    "Nonthaburi", "Samut Prakarn", "Rayong", "Khon Kaen",
    "Nakhon Ratchasima", "Nakhon Pathom", "Prachaubkirikhan",
    "Songkhla", "Krabi", "Phra Nakhon Sri Ayutthaya", "Samut Sakhon",
    "Samut Songkram", "Chachoengsao", "Prachinburi", "Saraburi",
    "Lopburi", "Suphanburi", "Nakhon Nayok", "Ratchburi",
    "Petchburi", "Kanchanaburi", "Udon Thani", "Ubon Ratchathani",
    "Chaiyaphum", "Nakhon Sri Thammarat", "Yala", "Trang",
    "Pattani", "Ranong", "Chiang Rai", "Lamphang", "Phitsanulok",
    "Maha Sarakham", "Si Sa Ket", "Tak", "Phetchabun",
    "Singburi", "Chainat", "Uthai Thani", "Buri Ram",
    "Mukdahan", "Amnat Charoen",
]


def _find_address(lines: list[str], name: str) -> Optional[str]:
    for line in lines:
        if line == name:
            continue
        for prov in PROVINCES_LIST:
            if prov.lower() in line.lower() and len(line) < 150:
                if not re.search(r"THB|month|day|Contract|Filter|Sort|Browse", line, re.I):
                    return line
    return None


def _parse_location(address: Optional[str]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    if not address:
        return None, None, None
    for prov in PROVINCES_LIST:
        if prov.lower() in address.lower():
            idx = address.lower().index(prov.lower())
            before = address[:idx].strip()
            parts = before.split()
            district = parts[-1] if parts else None
            subdistrict = " ".join(parts[:-1]) if len(parts) > 1 else None
            return subdistrict, district, prov
    return None, None, None


def extract_last_page(html: str, path_marker: str = "short-term-monthly") -> int:
    """
    Détecte la dernière page depuis les liens de pagination
    <path_marker>/<N>. path_marker doit inclure le slug de lieu pour
    les pages /en/short-term-rental/<slug> (ex: "short-term-rental/bangkok"),
    afin de ne pas confondre avec les liens de filtre par prix
    (ex: /short-term-rental/ko-samui/600-1200-baht).
    """
    from_json = extract_last_page_json(html)
    if from_json is not None:
        return from_json

    page = Selector(html)
    max_page = 1
    pattern = re.compile(re.escape(path_marker) + r"/(\d+)$")
    for link in page.css("a[href]"):
        m = pattern.search(link.attrib.get("href", ""))
        if m:
            n = int(m.group(1))
            if n > max_page:
                max_page = n
    return max_page
