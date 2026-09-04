"""
Parse la page individuelle d'une annonce RentHub.
Extrait: listing_no, description, amenities, room_types, images, contacts.
"""
from __future__ import annotations

import re
import json
import logging
import itertools
from typing import Optional
from datetime import datetime
from urllib.parse import unquote
from zoneinfo import ZoneInfo

from scrapling.parser import Selector

from src.models.schemas import ListingDetail, RoomTypeSchema
from src.normalizers.price import parse_price_range
from src.normalizers.amenities import derive_amenities

log = logging.getLogger(__name__)
BANGKOK_TZ = ZoneInfo("Asia/Bangkok")
CDN_BASE = "https://bcdn.renthub.in.th"

# Nombre maximum d'occurrences "lat"/"lng" appariees dans le repli regex de
# _extract_coordinates. Au-dela, on paie un produit cartesien pour rien.
_MAX_COORD_MATCHES = 200


def parse_detail_page(html: str, url: str) -> Optional[ListingDetail]:
    """Parse la page détail d'une annonce."""
    try:
        soup = Selector(html)

        source_id = _extract_listing_no(soup)
        description = _extract_description(soup)
        amenities = _extract_amenities_from_icons(soup)
        if amenities is None:
            amenities = derive_amenities(description)
        room_types = _extract_room_types(soup)
        images = _extract_images(soup)
        phone, line_id, whatsapp, email = _extract_contacts(soup)
        deposit, advance, electric, water, service = _extract_fees(soup)
        latitude, longitude = _extract_coordinates(soup, html)

        return ListingDetail(
            source_id=source_id,
            description=description,
            amenities=amenities,
            room_types=room_types,
            images=images,
            phone=phone,
            line_id=line_id,
            whatsapp=whatsapp,
            email=email,
            deposit=deposit,
            advance_payment=advance,
            electric_price=electric,
            water_price=water,
            service_fee=service,
            latitude=latitude,
            longitude=longitude,
            has_structured_data=_has_listing_payload(_extract_next_data(soup)),
        )
    except Exception as e:
        log.warning(f"Erreur parsing détail {url}: {e}")
        return None


def _extract_listing_no(soup: Selector) -> Optional[str]:
    """Extrait 'Listing no : 9560'."""
    m = re.search(r"Listing\s+no\s*:\s*(\d+)", soup.get_all_text())
    if m:
        return m.group(1)
    return None


_CSS_RULE_RE = re.compile(r"\.css-[\w-]+\s*\{[^}]*\}")
_LONE_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def _clean_description_text(text: str) -> Optional[str]:
    """
    Nettoie le texte de description scrapé:
    - retire les règles CSS scoped (emotion/styled-components) qui fuient
      parfois dans le texte au lieu de rester dans leur <style>
    - retire les caractères de remplacement / demi-paires de substituts
      issus d'un mauvais décodage d'emoji dans le HTML source
    """
    if not text:
        return text
    text = _CSS_RULE_RE.sub("", text)
    text = text.replace("�", "")
    text = _LONE_SURROGATE_RE.sub("", text)
    text = text.strip()
    return text or None


def _extract_description(soup: Selector) -> Optional[str]:
    """Extrait la description principale."""
    # Chercher la section "Detail" ou "Information about..."
    h2_detail = next(
        (
            h2
            for h2 in soup.css("h2")
            if h2.text and re.search(r"Detail|Information about", h2.text, re.I)
        ),
        None,
    )
    if h2_detail:
        # Prendre le contenu jusqu'au prochain h2/h3.
        # get_all_text() ignore déjà <script>/<style> par défaut, donc
        # pas besoin de les retirer manuellement au préalable.
        parts = []
        sib = h2_detail
        while True:
            sib = sib.next
            if sib is None or getattr(sib, "tag", None) in ("h2", "h3"):
                break
            text = sib.get_all_text(separator="\n", strip=True)
            if text:
                parts.append(text)
        if parts:
            return _clean_description_text("\n".join(parts))

    # Fallback: chercher un bloc <p> de description substantiel
    for p in soup.css("p"):
        text = p.get_all_text(separator=" ", strip=True)
        if len(text) > 50:
            return _clean_description_text(text)

    return None


_ROW_DIV_OPEN_RE = re.compile(r'<div class="css-([\w-]+)">')
_ROW_STYLE_RE = re.compile(r'<style data-emotion="css ([\w-]+)">\.css-\1\{([^}]*)\}')
_ROW_LABEL_RE = re.compile(r'<span class="css-[\w-]+">([^<]+)</span>')


def _extract_amenities_from_icons(soup: Selector) -> Optional[list[str]]:
    """
    Extrait les équipements réellement présents depuis la section
    "Amenities" de la page détail (grille d'icônes), en distinguant les
    catégories actives (texte noir) des catégories absentes (texte gris
    barré, `text-decoration-line:line-through`).

    Contrairement à `derive_amenities` (texte libre de la description, qui
    n'énumère pas cette grille), cette grille couvre les 28 catégories
    connues de façon fiable et par annonce. Retourne None si la section est
    introuvable (structure de page différente) pour permettre un repli sur
    `derive_amenities`.

    Chaque libellé est rattaché au div de ligne le plus proche qui le
    précède, plutôt qu'à un div trouvé en cherchant un `<svg>` en avant: cette
    dernière approche laissait le premier équipement (toujours Air
    Conditioner) se faire rattacher au div englobant `css-wxy4k`, qui n'est
    jamais barré — Air Conditioner ressortait donc toujours "présent".
    """
    h2 = next(
        (h for h in soup.css("h2") if h.text and re.search(r"Amenities", h.text, re.I)),
        None,
    )
    if h2 is None or h2.parent is None:
        return None

    row_container = next(
        (c for c in h2.parent.children if getattr(c, "tag", None) == "div"),
        None,
    )
    if row_container is None:
        return None

    content = row_container.html_content
    if not content:
        return None

    style_by_class = dict(_ROW_STYLE_RE.findall(content))
    div_opens = [(m.start(), m.group(1)) for m in _ROW_DIV_OPEN_RE.finditer(content)]
    if not div_opens:
        return None

    present = []
    for label_m in _ROW_LABEL_RE.finditer(content):
        item_cls = next(
            (cls for pos, cls in reversed(div_opens) if pos <= label_m.start()),
            None,
        )
        if item_cls is None:
            continue
        if "line-through" not in style_by_class.get(item_cls, ""):
            present.append(label_m.group(1))

    return present


def _extract_room_types_from_next_data(next_data: Optional[dict]) -> Optional[list[RoomTypeSchema]]:
    """Room types depuis `listing.rooms` du JSON __NEXT_DATA__.

    C'est la source qui alimente le tableau "Room Type" affiché sur la page
    (rendu côté client depuis ce JSON, pas depuis un <table> HTML statique) :
    nom réel de la chambre, taille, prix par durée de contrat et disponibilité
    y sont exacts, contrairement au fallback texte ci-dessous qui ne peut
    produire que des chambres nommées "Unknown" avec un prix approximatif.
    """
    try:
        rooms = next_data["props"]["pageProps"]["listing"]["rooms"]
    except (KeyError, TypeError):
        return None
    if not isinstance(rooms, list) or not rooms:
        return None

    result = []
    for room in rooms:
        if not isinstance(room, dict):
            continue
        short_term = (room.get("price") or {}).get("shortTerm") or {}
        daily = (room.get("price") or {}).get("daily") or {}
        monthly = (room.get("price") or {}).get("monthly") or {}
        min_size = room.get("minSize")
        max_size = room.get("maxSize")
        sizes = [s for s in (min_size, max_size) if s is not None]
        size_sqm = sum(sizes) / len(sizes) if sizes else None
        availability = room.get("availability")
        status = None if availability is None else ("Available" if availability else "Not Available")

        result.append(RoomTypeSchema(
            name=room.get("roomName") or "Unknown",
            room_type=room.get("roomType"),
            size_sqm=size_sqm,
            monthly_min_thb=monthly.get("minPrice"),
            monthly_max_thb=monthly.get("maxPrice"),
            contract_1_month_thb=short_term.get("oneMonth"),
            contract_3_month_thb=short_term.get("threeMonth"),
            contract_6_month_thb=short_term.get("sixMonth"),
            daily_thb=daily.get("minPrice"),
            status=status,
        ))
    return result


def _extract_room_types(soup: Selector) -> list[RoomTypeSchema]:
    """
    Extrait le tableau des types de chambre.

    Structure HTML: tableau avec colonnes:
    Room Type | Size | Monthly Rental | Daily Rental | Short Contract | Status
    + sous-tableau: Contract 1 month / Contract 3 month / Contract 6 month
    """
    from_next_data = _extract_room_types_from_next_data(_extract_next_data(soup))
    if from_next_data is not None:
        return from_next_data

    rooms = []

    # Chercher la section "Room Type"
    section = soup.find_by_regex(r"^Room Type$", case_sensitive=False)
    if not section:
        section = soup.find_by_regex(r"Room Type", case_sensitive=False)

    if not section:
        return rooms

    # Chercher le tableau parent
    container = section.parent
    for _ in range(5):
        if container is None:
            break
        if container.tag == "table":
            break
        container = container.parent

    if container is None or getattr(container, "tag", None) != "table":
        # Essayer de trouver un tableau plus haut
        container = soup.find("table")

    if not container:
        # Fallback: extraire depuis texte structuré
        return _extract_room_types_from_text(soup)

    rows = container.css("tr")
    current_room: Optional[dict] = None

    for row in rows:
        cells = row.css("td, th")
        if not cells:
            continue

        cell_texts = [c.text.strip() if c.text else "" for c in cells]

        # Détecter une ligne de nom de chambre (ex: "STANDARD ROOM", "Studio", ...)
        if len(cell_texts) >= 3 and cell_texts[0] and not re.search(
            r"Contract|Room Type|Size|Monthly|Daily|Status", cell_texts[0], re.I
        ):
            if current_room:
                rooms.append(_dict_to_room_schema(current_room))

            price_min, price_max = parse_price_range(
                cell_texts[2] if len(cell_texts) > 2 else None
            )
            current_room = {
                "name": cell_texts[0],
                "room_type": cell_texts[1] if len(cell_texts) > 1 else None,
                "size_sqm": _parse_sqm(cell_texts[1] if len(cell_texts) > 1 else None),
                "monthly_min": price_min,
                "monthly_max": price_max,
                "daily_thb": None,
                "contract_1_month": None,
                "contract_3_month": None,
                "contract_6_month": None,
                "status": cell_texts[-1] if cell_texts else None,
            }

        # Ligne "Contract 1 month / Contract 3 month / Contract 6 month"
        elif current_room and re.search(r"Contract\s+1\s+month", cell_texts[0], re.I):
            # Récupérer les 3 valeurs de contrat en cherchant les textes suivants
            contract_vals = _extract_contract_row_values(row)
            if contract_vals:
                current_room["contract_1_month"] = contract_vals.get("1")
                current_room["contract_3_month"] = contract_vals.get("3")
                current_room["contract_6_month"] = contract_vals.get("6")

    if current_room:
        rooms.append(_dict_to_room_schema(current_room))

    return rooms


# "Contract 3 month" / "3 month": le numero de mois doit etre retire du
# texte avant d'en extraire un prix, sinon parse_price_range() le capture
# comme premier nombre et le stocke comme montant du contrat.
_CONTRACT_LABEL_RE = re.compile(r"(?:contract\s*)?\b\d+\s*months?\b", re.I)


def _extract_contract_row_values(row: Selector) -> dict:
    """
    Extrait les valeurs de contrat depuis une ligne avec
    "Contract 1 month / Contract 3 month / Contract 6 month"
    """
    result = {}
    # Chercher les labels et leurs valeurs dans les lignes suivantes
    parent_table = row.find_ancestor(lambda e: e.tag == "table")
    if not parent_table:
        return result

    rows = parent_table.css("tr")
    idx = next((i for i, r in enumerate(rows) if id(r._root) == id(row._root)), -1)
    if idx < 0:
        return result

    # Les 3 lignes suivantes contiennent les valeurs
    for next_row in rows[idx + 1: idx + 4]:
        # `.text` ne rend que le texte direct du noeud: sur un <tr>, dont
        # tout le contenu vit dans des <td>, il rend systematiquement "".
        text = next_row.get_all_text(separator=" ", strip=True)
        for month_num, pattern in [("1", r"1\s*month"), ("3", r"3\s*month"), ("6", r"6\s*month")]:
            if re.search(pattern, text, re.I):
                mn, mx = parse_price_range(_CONTRACT_LABEL_RE.sub(" ", text))
                if mn:
                    result[month_num] = mn

    return result


def _extract_room_types_from_text(soup: Selector) -> list[RoomTypeSchema]:
    """Fallback: extraire les types de chambre depuis le texte brut."""
    rooms = []
    text = soup.get_all_text(separator="\n")

    # Chercher les blocs "Contract 1 month / Contract 3 month / Contract 6 month"
    # qui précèdent des montants
    pattern = re.compile(
        r"Contract 1 month\s+Contract 3 month\s+Contract 6 month\s+"
        r"([\d,]+(?:\s*-\s*[\d,]+)?\s*THB/Month|-)\s+"
        r"([\d,]+(?:\s*-\s*[\d,]+)?\s*THB/Month|-)\s+"
        r"([\d,]+(?:\s*-\s*[\d,]+)?\s*THB/Month|-)",
        re.I | re.S
    )

    for m in pattern.finditer(text):
        room = RoomTypeSchema(
            name="Unknown",
            contract_1_month_thb=parse_price_range(m.group(1))[0],
            contract_3_month_thb=parse_price_range(m.group(2))[0],
            contract_6_month_thb=parse_price_range(m.group(3))[0],
        )
        rooms.append(room)

    return rooms


def _dict_to_room_schema(d: dict) -> RoomTypeSchema:
    return RoomTypeSchema(
        name=d.get("name", ""),
        room_type=d.get("room_type"),
        size_sqm=d.get("size_sqm"),
        monthly_min_thb=d.get("monthly_min"),
        monthly_max_thb=d.get("monthly_max"),
        contract_1_month_thb=d.get("contract_1_month"),
        contract_3_month_thb=d.get("contract_3_month"),
        contract_6_month_thb=d.get("contract_6_month"),
        daily_thb=d.get("daily_thb"),
        status=d.get("status"),
    )


def _parse_sqm(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    m = re.search(r"([\d.]+)\s*sq\.?\s*m", text, re.I)
    if m:
        return float(m.group(1))
    return None


def _extract_images(soup: Selector) -> list[str]:
    """Extrait toutes les URLs d'images de la galerie."""
    images = []
    seen = set()

    # Chercher les images depuis le CDN bcdn.renthub.in.th
    for img in soup.css("img[src]"):
        src = img.attrib.get("src", "")
        if "bcdn.renthub.in.th/listing_picture" in src:
            # Normaliser: supprimer le paramètre ?class=thumbnail
            clean_url = src.split("?")[0]
            if clean_url not in seen:
                seen.add(clean_url)
                images.append(clean_url)

    # Aussi chercher dans les attributs data-src (lazy loading)
    for img in soup.css("img[data-src], div[data-src]"):
        src = img.attrib.get("data-src", "")
        if "bcdn.renthub.in.th/listing_picture" in src:
            clean_url = src.split("?")[0]
            if clean_url not in seen:
                seen.add(clean_url)
                images.append(clean_url)

    return images


def _extract_next_data(soup: Selector) -> Optional[dict]:
    """Extrait le JSON embarqué par Next.js (<script id="__NEXT_DATA__">).

    RentHub y met les données brutes de l'annonce (dont le téléphone en
    clair) même quand l'affichage visible le masque en "xxxx" côté client."""
    tag = next(iter(soup.css("script#__NEXT_DATA__")), None)
    if tag is None or not tag.text:
        return None
    try:
        return json.loads(tag.text)
    except (json.JSONDecodeError, TypeError):
        return None


# Ce que RentHub affiche à la place d'un LINE ID quand l'annonce n'en a
# pas, et ce que certains bailleurs saisissent pour dire la même chose.
# Aucune de ces valeurs n'est un contact: la case doit rester vide.
_LINE_ID_PLACEHOLDERS = {
    "unavailable", "n/a", "na", "none", "null", "no line", "nolineid",
    "-", "--", "---", ".", "x", "xx", "xxx", "ไม่มี",
}


# Libellé recopié dans le champ par le bailleur ("Line ID : xxx",
# "Phone0971538717"): on ne garde que ce qui suit.
_LINE_ID_LABEL_RE = re.compile(
    r"^(?:line\s*id|line|phone|tel|โทร)\s*[:：]\s*|^(?:phone|tel|โทร)\s*(?=\d)",
    re.I,
)
# Un identifiant LINE contient au moins une lettre. Les comptes officiels
# commencent par "@", les comptes personnels non ("secretpurse"), mais
# aucun n'est un numéro de téléphone.
_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)


def _clean_line_id(value: Optional[str]) -> Optional[str]:
    """Normalise un LINE ID, ou None si l'annonce n'en donne pas.

    Trois nettoyages, dans cet ordre:

    - le lien `line.me/ti/p/<id>` porte l'identifiant percent-encodé
      ("%40zimple_asset" pour "@zimple_asset"), il est décodé ici;
    - les libellés que RentHub (ou le bailleur) met à la place d'un
      identifiant sont vidés, cf. _LINE_ID_PLACEHOLDERS;
    - un numéro de téléphone n'est pas un LINE ID: le champ doit rester
      vide plutôt que d'afficher un numéro dans la case LINE.
    """
    if not value:
        return None
    cleaned = _LINE_ID_LABEL_RE.sub("", unquote(value).strip()).strip()
    if not cleaned or cleaned.lower() in _LINE_ID_PLACEHOLDERS:
        return None
    if not _LETTER_RE.search(cleaned):
        return None
    return cleaned


def _has_listing_payload(next_data: Optional[dict]) -> bool:
    """True si le JSON de la page porte bien la fiche d'une annonce.

    Une annonce retirée de RentHub ne renvoie ni 404 ni page vide: le site
    sert la page d'accueil, dont le __NEXT_DATA__ est parfaitement valide
    mais sans clé `listing`. Sans cette vérification ce JSON passerait pour
    une source faisant autorité et effacerait le contact et les charges
    déjà en base (cf. change_detector.apply_detail_fields).
    """
    try:
        return isinstance(next_data["props"]["pageProps"]["listing"], dict)
    except (KeyError, TypeError):
        return False


def _extract_contacts_from_next_data(
    next_data: Optional[dict],
) -> Optional[tuple[Optional[str], Optional[str], Optional[str], Optional[str]]]:
    if not next_data:
        return None
    try:
        contacts = next_data["props"]["pageProps"]["listing"]["contactInformation"]
        contact = contacts[0] if contacts else None
    except (KeyError, TypeError, IndexError):
        return None
    if not contact:
        return None

    phones = contact.get("phone") or []
    phone = phones[0].get("phoneNumber") if phones else None
    line_id = _clean_line_id(contact.get("lineId"))
    whatsapp = contact.get("whatsApp") or None
    email = contact.get("email") or None

    if not any((phone, line_id, whatsapp, email)):
        return None
    return phone, line_id, whatsapp, email


def _page_has_whatsapp_link(soup: Selector) -> bool:
    """True si la page affiche vraiment un bouton/lien WhatsApp (wa.me).

    `contactInformation[0].whatsApp` du JSON est souvent renseigné côté site
    sans qu'aucun bouton WhatsApp ne soit pour autant affiché sur la page
    (le champ semble parfois dupliqué depuis le téléphone par RentHub lui-
    même) : sur un échantillon d'annonces actives, ~60% avaient ce champ
    rempli sans le moindre lien wa.me sur la page. On ne retient donc le
    champ que si RentHub l'affiche lui-même comme un contact WhatsApp.
    """
    return any("wa.me/" in a.attrib.get("href", "") for a in soup.css("a[href]"))


def _extract_contacts(soup: Selector) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Extrait phone, line_id, whatsapp, email."""
    from_next_data = _extract_contacts_from_next_data(_extract_next_data(soup))
    if from_next_data:
        phone, line_id, whatsapp, email = from_next_data
        if whatsapp and not _page_has_whatsapp_link(soup):
            whatsapp = None
        return phone, line_id, whatsapp, email

    # Fallback : anciennes heuristiques texte/liens, pour les pages sans
    # __NEXT_DATA__ exploitable.
    text = soup.get_all_text(separator="\n")

    # Phone (format thaïlandais, souvent masqué)
    phone = None
    m = re.search(r"(\d{2,3}-?\d{3,4}-?\d{3,4}xxx?|\d{9,10})", text)
    if m:
        phone = m.group(1)

    # Line ID
    line_id = None
    m = re.search(r"Line\s+ID\s*:\s*(@[\w.]+|\w[\w.]+)", text, re.I)
    if m:
        line_id = m.group(1)
    # Aussi dans les liens
    line_tag = next(
        (a for a in soup.css("a[href]") if re.search(r"line\.me/ti/p/", a.attrib.get("href", ""))),
        None,
    )
    if line_tag:
        href = line_tag.attrib.get("href", "")
        m = re.search(r"line\.me/ti/p/(.+)$", href)
        if m:
            line_id = m.group(1)
    line_id = _clean_line_id(line_id)

    # WhatsApp
    whatsapp = None
    wa_tag = next(
        (a for a in soup.css("a[href]") if re.search(r"wa\.me/", a.attrib.get("href", ""))),
        None,
    )
    if wa_tag:
        href = wa_tag.attrib.get("href", "")
        m = re.search(r"wa\.me/(\d+)", href)
        if m:
            whatsapp = m.group(1)

    # Email
    email = None
    email_tag = next(
        (a for a in soup.css("a[href]") if a.attrib.get("href", "").startswith("mailto:")),
        None,
    )
    if email_tag:
        href = email_tag.attrib.get("href", "")
        email = href.replace("mailto:", "").split("?")[0]

    return phone, line_id, whatsapp, email


def _format_fee(
    value: Optional[dict],
    *,
    unit_suffix: str = "",
    meter_label: Optional[str] = None,
) -> Optional[str]:
    """Formate un sous-objet fee (deposit/advancePayment/electric/water/service)
    du JSON __NEXT_DATA__ en texte lisible. Retourne None si le `type` n'est
    pas reconnu, pour laisser le repli regex prendre le relais."""
    if not value:
        return None
    fee_type = value.get("type")
    if fee_type == "CALL":
        return "Please contact"
    if fee_type == "INCLUDED":
        return "Included"
    if fee_type == "MONTH":
        month = value.get("month")
        if month is None:
            return None
        return f"{month} month" if month == 1 else f"{month} months"
    if fee_type == "AMOUNT":
        amount = value.get("unitPrice")
        if amount is None:
            amount = value.get("amount")
        if amount is None:
            return None
        return f"{amount} THB{unit_suffix}"
    if fee_type == "PER_PERSON":
        price = value.get("perPersonPrice")
        if price is None:
            return None
        return f"{price} THB/person"
    if fee_type == "RANGE":
        return value.get("range") or None
    if meter_label and fee_type and fee_type.endswith("_METER"):
        return meter_label
    return None


def _format_deposit(value: Optional[dict]) -> Optional[str]:
    """Formate le dépôt en se limitant à : "Please contact" / "N month" /
    "N Baht" / "No deposit". Retourne None pour tout le reste (pas de texte
    scrapé au hasard)."""
    if not value:
        return None
    fee_type = value.get("type")
    if fee_type == "CALL":
        return "Please contact"
    if fee_type == "NO_DEPOSIT_PAYMENT":
        return "No deposit"
    if fee_type == "MONTH":
        month = value.get("month")
        return f"{month} month" if month is not None else None
    if fee_type in ("AMOUNT", "RANGE"):
        amount = value.get("amount")
        if amount is not None:
            return f"{amount} Baht"
        rng = value.get("range")
        if isinstance(rng, (int, float)):
            return f"{rng} Baht"
        if isinstance(rng, dict):
            lo, hi = rng.get("min"), rng.get("max")
            if lo is not None and hi is not None:
                return f"{lo}-{hi} Baht"
            if lo is not None or hi is not None:
                return f"{lo if lo is not None else hi} Baht"
    return None


def _extract_fees_from_next_data(
    next_data: Optional[dict],
) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]:
    fee = None
    if next_data:
        try:
            fee = next_data["props"]["pageProps"]["listing"]["fee"]
        except (KeyError, TypeError):
            fee = None
    if not fee:
        return None, None, None, None, None

    deposit = _format_deposit(fee.get("deposit"))
    advance = _format_fee(fee.get("advancePayment"))
    electric = _format_fee(
        fee.get("electric"), unit_suffix="/unit", meter_label="as charged by Electricity Authority"
    )
    water = _format_fee(
        fee.get("water"), unit_suffix="/unit", meter_label="as charged by Waterworks Authority"
    )
    service = _format_fee(fee.get("service"))
    return deposit, advance, electric, water, service


def _extract_fees(soup: Selector) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Extrait Deposit, Advance payment, Electric price, Water price, Service fee."""
    json_deposit, json_advance, json_electric, json_water, json_service = (
        _extract_fees_from_next_data(_extract_next_data(soup))
    )

    def _find_value(label_pattern: str) -> Optional[str]:
        # find_by_regex retourne déjà l'élément conteneur du texte
        # (équivalent au `.parent` d'un NavigableString bs4).
        parent = soup.find_by_regex(label_pattern, case_sensitive=False)
        if parent is None:
            return None
        # Chercher le texte suivant
        next_el = parent.next
        if next_el is not None:
            val = next_el.get_all_text(separator="", strip=True)
            if val:
                return val
        # Chercher dans le parent commun
        grandparent = parent.parent
        if grandparent:
            text = grandparent.get_all_text(separator="\n", strip=True)
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            for i, line in enumerate(lines):
                if re.search(label_pattern, line, re.I):
                    if i + 1 < len(lines):
                        return lines[i + 1]
        return None

    deposit = json_deposit
    advance = json_advance or _find_value(r"Advance\s+payment")
    electric = json_electric or _find_value(r"Electric\s+price")
    water = json_water or _find_value(r"Water\s+price")
    service = json_service or _find_value(r"Service\s+fee")

    return deposit, advance, electric, water, service


def _extract_coordinates(
    soup: Selector,
    html: str
) -> tuple[Optional[float], Optional[float]]:
    """
    Extrait latitude/longitude depuis une page RentHub.

    Recherche dans :
    0. __NEXT_DATA__ (`listing.location`), la source reelle du site
    1. JSON-LD
    2. attributs HTML
    3. JavaScript
    4. URLs Google Maps
    5. URLs avec coordonnées
    """

    def valid(lat, lon):
        try:
            lat = float(str(lat).replace(",", "."))
            lon = float(str(lon).replace(",", "."))

            # Thaïlande : permet d'éviter de récupérer de faux nombres
            if 5 <= lat <= 21 and 97 <= lon <= 106:
                return lat, lon

        except (TypeError, ValueError):
            pass

        return None

    # =========================================================
    # 0. __NEXT_DATA__ — la source, plutot que son reflet dans le HTML
    # =========================================================
    #
    # C'est de `listing.location` que le site tire lui-meme le marqueur de
    # la carte. Les etapes 1 a 5 ci-dessous balayent le HTML entier a coups
    # d'expressions regulieres, dont un produit cartesien entre toutes les
    # occurrences de "lat" et toutes celles de "lng": sur une page portant
    # un gros __NEXT_DATA__, cela fait beaucoup de travail pour retrouver
    # une valeur directement lisible ici. Elles restent en repli pour les
    # pages sans ce JSON.

    try:
        location = _extract_next_data(soup)["props"]["pageProps"]["listing"]["location"]
    except (KeyError, TypeError):
        location = None

    if isinstance(location, dict):
        result = valid(location.get("lat"), location.get("lng"))
        if result:
            log.debug("Coordonnees trouvees dans __NEXT_DATA__: %s", result)
            return result

    # =========================================================
    # 1. JSON-LD
    # =========================================================

    for script in soup.css('script[type="application/ld+json"]'):
        content = script.text

        if not content:
            continue

        try:
            data = json.loads(content)

            def find_geo(obj):
                if isinstance(obj, dict):

                    # geo: { latitude, longitude }
                    geo = obj.get("geo")

                    if isinstance(geo, dict):
                        result = valid(
                            geo.get("latitude"),
                            geo.get("longitude")
                        )

                        if result:
                            return result

                    # latitude / longitude directement
                    for lat_key in ("latitude", "lat"):
                        for lon_key in ("longitude", "lng", "lon"):
                            if lat_key in obj and lon_key in obj:
                                result = valid(
                                    obj.get(lat_key),
                                    obj.get(lon_key)
                                )

                                if result:
                                    return result

                    for value in obj.values():
                        result = find_geo(value)

                        if result:
                            return result

                elif isinstance(obj, list):
                    for item in obj:
                        result = find_geo(item)

                        if result:
                            return result

                return None

            result = find_geo(data)

            if result:
                log.info("Coordonnées trouvées dans JSON-LD: %s", result)
                return result

        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    # =========================================================
    # 2. ATTRIBUTS HTML
    # =========================================================

    for tag in soup.css(
        "[data-latitude], [data-lat], [latitude],"
        " [data-longitude], [data-lng], [data-lon], [longitude]"
    ):

        attrs = tag.attrib

        lat = (
            attrs.get("data-latitude")
            or attrs.get("data-lat")
            or attrs.get("latitude")
        )

        lon = (
            attrs.get("data-longitude")
            or attrs.get("data-lng")
            or attrs.get("data-lon")
            or attrs.get("longitude")
        )

        result = valid(lat, lon)

        if result:
            log.info("Coordonnées trouvées dans HTML: %s", result)
            return result

    # =========================================================
    # 3. GOOGLE MAPS / GOOGLE MAPS EMBED
    # =========================================================

    # Exemple :
    # https://www.google.com/maps?q=13.7563,100.5018
    # https://maps.google.com/?q=13.7563,100.5018

    google_patterns = [

        r'[?&]q=(-?\d+\.\d+),\s*(-?\d+\.\d+)',

        r'@(-?\d+\.\d+),\s*(-?\d+\.\d+)',

        r'!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)',

        r'query=(-?\d+\.\d+),\s*(-?\d+\.\d+)',

    ]

    for pattern in google_patterns:

        for match in re.finditer(pattern, html, re.I):

            result = valid(
                match.group(1),
                match.group(2)
            )

            if result:
                log.info(
                    "Coordonnées trouvées dans Google Maps: %s",
                    result
                )
                return result

    # =========================================================
    # 4. JAVASCRIPT
    # =========================================================

    patterns = [

        # latitude: 13.123, longitude: 100.123
        (
            r'["\']?(?:latitude)["\']?\s*[:=]\s*["\']?'
            r'(-?\d+(?:\.\d+)?)',
            r'["\']?(?:longitude)["\']?\s*[:=]\s*["\']?'
            r'(-?\d+(?:\.\d+)?)'
        ),

        # lat: 13.123, lng: 100.123
        (
            r'["\']?(?:lat)["\']?\s*[:=]\s*["\']?'
            r'(-?\d+(?:\.\d+)?)',
            r'["\']?(?:lng|lon)["\']?\s*[:=]\s*["\']?'
            r'(-?\d+(?:\.\d+)?)'
        ),

        # latitude / longitude avec espaces
        (
            r'latitude\s*[:=]\s*["\']?(-?\d+(?:\.\d+)?)',
            r'longitude\s*[:=]\s*["\']?(-?\d+(?:\.\d+)?)'
        ),

    ]

    for lat_pattern, lon_pattern in patterns:

        # Plafond: ces motifs matchent chaque "lat"/"lng" du HTML, JSON
        # embarque compris. Sans borne, le produit cartesien ci-dessous
        # explose sur une page qui en contient des milliers, pour un gain
        # nul — la bonne paire est toujours parmi les premieres.
        lat_matches = list(
            itertools.islice(re.finditer(lat_pattern, html, re.I), _MAX_COORD_MATCHES)
        )

        lon_matches = list(
            itertools.islice(re.finditer(lon_pattern, html, re.I), _MAX_COORD_MATCHES)
        )

        for lat_match in lat_matches:

            for lon_match in lon_matches:

                # Évite d'associer deux coordonnées trop éloignées
                distance = abs(
                    lat_match.start() - lon_match.start()
                )

                if distance > 10000:
                    continue

                result = valid(
                    lat_match.group(1),
                    lon_match.group(1)
                )

                if result:
                    log.info(
                        "Coordonnées trouvées dans JavaScript: %s",
                        result
                    )
                    return result

    # =========================================================
    # 5. TABLEAUX / OBJETS JAVASCRIPT
    # =========================================================

    pair_patterns = [

        # [13.7563, 100.5018]
        r'\[\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\s*\]',

        # (13.7563, 100.5018)
        r'\(\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\s*\)',

        # "13.7563,100.5018"
        r'["\'](-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)["\']',

    ]

    for pattern in pair_patterns:

        for match in re.finditer(pattern, html):

            result = valid(
                match.group(1),
                match.group(2)
            )

            if result:
                log.info(
                    "Coordonnées trouvées dans une paire: %s",
                    result
                )
                return result

    # =========================================================
    # 6. RECHERCHE GLOBALE
    # =========================================================

    lat_values = re.findall(
        r'(?:latitude|lat)\s*[:=]\s*["\']?'
        r'(-?\d+(?:\.\d+)?)',
        html,
        re.I
    )[:_MAX_COORD_MATCHES]

    lon_values = re.findall(
        r'(?:longitude|lng|lon)\s*[:=]\s*["\']?'
        r'(-?\d+(?:\.\d+)?)',
        html,
        re.I
    )[:_MAX_COORD_MATCHES]

    for lat in lat_values:

        for lon in lon_values:

            result = valid(lat, lon)

            if result:
                log.info(
                    "Coordonnées trouvées globalement: %s",
                    result
                )
                return result

    # =========================================================
    # AUCUNE COORDONNÉE
    # =========================================================

    log.debug("Aucune coordonnée trouvée sur la page RentHub.")

    return None, None