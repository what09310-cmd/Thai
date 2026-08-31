#!/usr/bin/env python3
"""
Version 100% locale (sans API payante) de la detection de photos parasites.
Complementaire de scripts/filter_problematic_images.py (IA vision Claude) :
meme schema (`excluded`, `exclusion_reason`, `reviewed_at` sur ListingImage),
meme filtrage cote API (src/api/main.py) donc les deux scripts sont
interchangeables/complementaires.

Detections :
- QR code -> OpenCV (cv2.QRCodeDetector), gratuit, deterministe.
- Texte/chiffres/symboles incrustes -> OCR local (pytesseract + Tesseract-OCR).
  NECESSITE d'installer le moteur Tesseract-OCR separement (pas juste pip) :
  https://github.com/UB-Mannheim/tesseract/wiki (installeur Windows officiel).
- Logo/filigrane etranger -> pas de reconnaissance de contenu possible sans IA
  vision. A la place : heuristique par empreinte perceptuelle (imagehash) des
  4 coins de chaque photo, comparee entre les photos d'une meme annonce. Un
  motif graphique quasi identique qui revient au meme coin sur plusieurs
  photos de la meme annonce est presque toujours un filigrane/logo appose par
  l'agent -> exclu (raison "repeated_watermark").

Ne supprime rien : marque `excluded=True` + `exclusion_reason`, la ligne
reste en base. Reprise possible (seules les lignes `reviewed_at IS NULL`
sont traitees).

Usage:
    python scripts/filter_problematic_images_local.py --limit-listings 5
    python scripts/filter_problematic_images_local.py --listing-id 461
    python scripts/filter_problematic_images_local.py
"""
import argparse
import io
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

import cv2
import httpx
import imagehash
import numpy as np
import pytesseract
from PIL import Image
from sqlalchemy import select

from src.database.session import get_session
from src.database.models import ListingImage

# Sur Windows, Tesseract n'est generalement pas sur le PATH : on essaie
# l'emplacement d'installation par defaut si `pytesseract` ne le trouve pas.
_DEFAULT_TESSERACT_WIN = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
if Path(_DEFAULT_TESSERACT_WIN).exists():
    pytesseract.pytesseract.tesseract_cmd = _DEFAULT_TESSERACT_WIN

CORNER_FRACTION = 0.22  # taille des coins analyses, en fraction de largeur/hauteur
CORNER_UPSCALE = 3  # facteur d'agrandissement des coins avant OCR (watermarks = texte petit)
OCR_CONFIG = "--psm 11"  # "sparse text" : mieux adapte a un watermark isole qu'un bloc de page
HASH_DISTANCE_THRESHOLD = 6  # distance de Hamming max pour considerer 2 coins "identiques"
MIN_CLUSTER_SIZE = 3  # nb min de photos partageant le meme coin pour parler de filigrane
# Une photo reelle contient beaucoup de texture (grain du bois, carrelage...)
# que l'OCR brut confond facilement avec du texte. On ne retient que les
# "mots" a haute confiance Tesseract pour eviter de tout exclure par erreur.
OCR_CONFIDENCE_THRESHOLD = 60
MIN_WORD_LENGTH = 4  # 3 caracteres laisse trop de bruit OCR passer (ex: "ba)")
MIN_CONFIDENT_WORDS_FULL_IMAGE = 2
MIN_CONFIDENT_WORDS_CORNER = 1
DOWNLOAD_TIMEOUT = 20.0
DEFAULT_WORKERS = 8


def _detect_qr(img_bgr: "np.ndarray") -> bool:
    detector = cv2.QRCodeDetector()
    try:
        ok, _ = detector.detectMulti(img_bgr)[:2]
        return bool(ok)
    except Exception:
        try:
            data, _, _ = detector.detectAndDecode(img_bgr)
            return bool(data)
        except Exception:
            return False


def _confident_word_count(pil_img: Image.Image) -> int:
    try:
        data = pytesseract.image_to_data(
            pil_img, config=OCR_CONFIG, output_type=pytesseract.Output.DICT
        )
    except pytesseract.TesseractNotFoundError:
        raise
    except Exception:
        return 0

    count = 0
    for text, conf in zip(data.get("text", []), data.get("conf", [])):
        text = text.strip()
        if len(text) < MIN_WORD_LENGTH or not any(c.isalnum() for c in text):
            continue
        try:
            conf_val = float(conf)
        except (TypeError, ValueError):
            continue
        if conf_val >= OCR_CONFIDENCE_THRESHOLD:
            count += 1
    return count


def _corner_crops(pil_img: Image.Image) -> dict:
    w, h = pil_img.size
    cw, ch = int(w * CORNER_FRACTION), int(h * CORNER_FRACTION)
    if cw < 8 or ch < 8:
        return {}
    boxes = {
        "top_left": (0, 0, cw, ch),
        "top_right": (w - cw, 0, w, ch),
        "bottom_left": (0, h - ch, cw, h),
        "bottom_right": (w - cw, h - ch, w, h),
    }
    return {name: pil_img.crop(box) for name, box in boxes.items()}


def _detect_text(pil_img: Image.Image, corners: dict) -> bool:
    # Watermarks/badges sont souvent petits et dans un coin : l'OCR sur
    # l'image entiere les rate facilement. On teste l'image complete ET
    # chaque coin agrandi (meilleure resolution effective pour l'OCR).
    if _confident_word_count(pil_img) >= MIN_CONFIDENT_WORDS_FULL_IMAGE:
        return True
    for crop in corners.values():
        upscaled = crop.resize(
            (crop.width * CORNER_UPSCALE, crop.height * CORNER_UPSCALE), Image.LANCZOS
        )
        if _confident_word_count(upscaled) >= MIN_CONFIDENT_WORDS_CORNER:
            return True
    return False


def _corner_hashes(corners: dict) -> dict:
    return {name: imagehash.phash(crop) for name, crop in corners.items()}


def _fetch_image(http_client: httpx.Client, url: str) -> Optional[Image.Image]:
    try:
        resp = http_client.get(url, timeout=DOWNLOAD_TIMEOUT)
        resp.raise_for_status()
        return Image.open(io.BytesIO(resp.content)).convert("RGB")
    except Exception:
        return None


def _analyze_one(http_client: httpx.Client, image_id: int, url: str) -> tuple:
    """Retourne (image_id, pil_img_or_None, has_qr, has_text, corner_hashes, fetch_error)."""
    pil_img = _fetch_image(http_client, url)
    if pil_img is None:
        return image_id, None, False, False, {}, True

    bgr = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    corners = _corner_crops(pil_img)
    has_qr = _detect_qr(bgr)
    has_text = _detect_text(pil_img, corners)
    hashes = _corner_hashes(corners)
    return image_id, pil_img, has_qr, has_text, hashes, False


def _cluster_watermarks(rows: list) -> set:
    """
    rows: liste de (image_id, corner_hashes dict).
    Retourne l'ensemble des image_id ayant un coin partage par >= MIN_CLUSTER_SIZE photos.
    """
    buckets: dict = defaultdict(list)  # (corner_name, hash_repr) approx -> [image_id]
    flagged: set = set()

    per_corner: dict = defaultdict(list)  # corner_name -> [(image_id, hash)]
    for image_id, hashes in rows:
        for corner, h in hashes.items():
            per_corner[corner].append((image_id, h))

    for corner, items in per_corner.items():
        n = len(items)
        for i in range(n):
            id_i, hash_i = items[i]
            if id_i in flagged:
                continue
            cluster = [id_i]
            for j in range(n):
                if i == j:
                    continue
                id_j, hash_j = items[j]
                if hash_i - hash_j <= HASH_DISTANCE_THRESHOLD:
                    cluster.append(id_j)
            if len(cluster) >= MIN_CLUSTER_SIZE:
                flagged.update(cluster)

    return flagged


def _fetch_pending_listing_ids(limit_listings: Optional[int], listing_id: Optional[int]) -> list:
    with get_session() as session:
        query = select(ListingImage.listing_id).where(ListingImage.reviewed_at.is_(None)).distinct()
        if listing_id is not None:
            query = query.where(ListingImage.listing_id == listing_id)
        query = query.order_by(ListingImage.listing_id)
        if limit_listings is not None:
            query = query.limit(limit_listings)
        return [row[0] for row in session.execute(query).all()]


def _fetch_pending_images_for_listing(listing_id: int) -> list:
    with get_session() as session:
        query = (
            select(ListingImage.id, ListingImage.image_url)
            .where(ListingImage.listing_id == listing_id, ListingImage.reviewed_at.is_(None))
            .order_by(ListingImage.id)
        )
        return list(session.execute(query).all())


def _save_results(results: list) -> None:
    now = datetime.now(timezone.utc)
    with get_session() as session:
        for image_id, excluded, reason in results:
            image = session.get(ListingImage, image_id)
            if image is None:
                continue
            image.excluded = excluded
            image.exclusion_reason = reason or None
            image.reviewed_at = now


def _process_listing(http_client: httpx.Client, listing_id: int, workers: int) -> list:
    rows = _fetch_pending_images_for_listing(listing_id)
    if not rows:
        return []

    analyzed = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_analyze_one, http_client, image_id, url): image_id
            for image_id, url in rows
        }
        for future in as_completed(futures):
            image_id, pil_img, has_qr, has_text, hashes, fetch_error = future.result()
            analyzed[image_id] = (has_qr, has_text, hashes, fetch_error)

    watermarked = _cluster_watermarks(
        [(image_id, data[2]) for image_id, data in analyzed.items() if data[2]]
    )

    results = []
    for image_id, (has_qr, has_text, hashes, fetch_error) in analyzed.items():
        if fetch_error:
            results.append((image_id, False, "fetch_error"))
            continue
        reasons = []
        if has_qr:
            reasons.append("qr_code")
        if has_text:
            reasons.append("text_overlay")
        if image_id in watermarked:
            reasons.append("repeated_watermark")
        results.append((image_id, bool(reasons), ",".join(reasons)))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit-listings", type=int, default=None, help="Nombre max d'annonces a traiter")
    parser.add_argument("--listing-id", type=int, default=None, help="Ne traiter qu'une annonce")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = parser.parse_args()

    try:
        pytesseract.get_tesseract_version()
    except Exception:
        print(
            "Tesseract-OCR introuvable. Installe-le depuis "
            "https://github.com/UB-Mannheim/tesseract/wiki puis relance ce script "
            "(ou renseigne pytesseract.pytesseract.tesseract_cmd si installe ailleurs "
            f"que {_DEFAULT_TESSERACT_WIN})."
        )
        return

    listing_ids = _fetch_pending_listing_ids(args.limit_listings, args.listing_id)
    total_listings = len(listing_ids)
    if total_listings == 0:
        print("Aucune annonce avec des images en attente de revue.")
        return
    print(f"{total_listings} annonces a traiter.")

    processed_images = 0
    excluded_images = 0
    error_images = 0

    with httpx.Client(follow_redirects=True) as http_client:
        for i, listing_id in enumerate(listing_ids, 1):
            results = _process_listing(http_client, listing_id, args.workers)
            if results:
                _save_results(results)
                processed_images += len(results)
                excluded_images += sum(1 for _, excluded, _ in results if excluded)
                error_images += sum(1 for _, _, reason in results if reason == "fetch_error")

            if i % 20 == 0 or i == total_listings:
                print(
                    f"[{i}/{total_listings} annonces] {processed_images} photos traitees, "
                    f"{excluded_images} exclues, {error_images} erreurs"
                )

    print(
        f"Termine : {processed_images} photos traitees, "
        f"{excluded_images} exclues, {error_images} erreurs."
    )


if __name__ == "__main__":
    main()
