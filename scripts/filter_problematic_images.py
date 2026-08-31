#!/usr/bin/env python3
"""
Detecte et exclut les photos parasites (QR code, texte/numeros/symboles
incrustes, logo d'une autre agence) parmi les photos scrapees dans
`listing_images`, via l'API vision Claude. Le seul logo autorise est
ThaiMonth (le logo de l'utilisateur) - toute autre marque visible declenche
l'exclusion.

Ne supprime rien : marque `excluded=True` + `exclusion_reason`, la ligne
reste en base pour audit. `src/api/main.py` filtre ensuite les images
excluees de ce qui est renvoye au frontend.

Reprise possible : seules les lignes `reviewed_at IS NULL` sont traitees,
donc un run interrompu peut etre relance sans retraiter ce qui l'a deja ete.

Usage:
    python scripts/filter_problematic_images.py --limit 20
    python scripts/filter_problematic_images.py --listing-id 461
    python scripts/filter_problematic_images.py
"""
import argparse
import asyncio
import base64
import io
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from anthropic import AsyncAnthropic
from PIL import Image
from sqlalchemy import select

from src.config import settings
from src.database.session import get_session
from src.database.models import ListingImage

MODEL = "claude-haiku-4-5-20251001"
MAX_DIMENSION = 768
JPEG_QUALITY = 80
BATCH_SIZE = 50
DEFAULT_CONCURRENCY = 8

PROMPT = """Tu analyses une photo d'annonce de location immobiliere. Reponds UNIQUEMENT avec un objet JSON, sans texte autour, au format exact :
{"has_qr_code": bool, "has_text_or_numbers_or_symbols": bool, "has_foreign_logo": bool}

Regles :
- has_qr_code: vrai si un QR code est visible sur la photo, peu importe sa taille.
- has_text_or_numbers_or_symbols: vrai si du texte, des chiffres ou des symboles sont incrustes/surimposes sur la photo (ex: numero de telephone, watermark texte, prix affiche en overlay). Ignore le texte qui fait naturellement partie de la scene photographiee (ex: texte sur un livre, une boite de cereales, un panneau dans la rue) - seul le texte ajoute en overlay sur l'image compte.
- has_foreign_logo: vrai si un logo ou une marque visible sur la photo n'est PAS "ThaiMonth". Le logo "ThaiMonth" est autorise et ne doit jamais declencher ce champ. Tout autre logo (autre agence, autre marque, autre site web) declenche ce champ.

Reponds seulement avec le JSON, rien d'autre."""


def _resize_to_jpeg_b64(raw: bytes) -> Optional[str]:
    try:
        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGB")
        img.thumbnail((MAX_DIMENSION, MAX_DIMENSION))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY)
        return base64.standard_b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


def _parse_classification(text: str) -> Optional[dict]:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not all(k in data for k in ("has_qr_code", "has_text_or_numbers_or_symbols", "has_foreign_logo")):
        return None
    return data


async def _classify_image(
    http_client: httpx.AsyncClient,
    anthropic_client: AsyncAnthropic,
    image_id: int,
    image_url: str,
) -> tuple[int, bool, str]:
    """Retourne (image_id, excluded, exclusion_reason)."""
    try:
        resp = await http_client.get(image_url, timeout=30.0)
        resp.raise_for_status()
    except Exception as e:
        return image_id, False, f"fetch_error:{type(e).__name__}"

    b64 = _resize_to_jpeg_b64(resp.content)
    if b64 is None:
        return image_id, False, "fetch_error:decode_failed"

    try:
        message = await anthropic_client.messages.create(
            model=MODEL,
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                    {"type": "text", "text": PROMPT},
                ],
            }],
        )
    except Exception as e:
        return image_id, False, f"api_error:{type(e).__name__}"

    text = "".join(block.text for block in message.content if block.type == "text")
    data = _parse_classification(text)
    if data is None:
        return image_id, False, "api_error:unparseable_response"

    reasons = [k for k, v in {
        "qr_code": data["has_qr_code"],
        "text_overlay": data["has_text_or_numbers_or_symbols"],
        "foreign_logo": data["has_foreign_logo"],
    }.items() if v]
    return image_id, bool(reasons), ",".join(reasons)


async def _process_batch(
    http_client: httpx.AsyncClient,
    anthropic_client: AsyncAnthropic,
    rows: list,
    semaphore: asyncio.Semaphore,
) -> list[tuple[int, bool, str]]:
    async def _bound(image_id: int, image_url: str):
        async with semaphore:
            return await _classify_image(http_client, anthropic_client, image_id, image_url)

    return await asyncio.gather(*(_bound(i, u) for i, u in rows))


def _fetch_pending(limit: Optional[int], listing_id: Optional[int]) -> list:
    with get_session() as session:
        query = select(ListingImage.id, ListingImage.image_url).where(
            ListingImage.reviewed_at.is_(None)
        )
        if listing_id is not None:
            query = query.where(ListingImage.listing_id == listing_id)
        query = query.order_by(ListingImage.id)
        if limit is not None:
            query = query.limit(limit)
        return list(session.execute(query).all())


def _save_results(results: list[tuple[int, bool, str]]) -> None:
    now = datetime.now(timezone.utc)
    with get_session() as session:
        for image_id, excluded, reason in results:
            image = session.get(ListingImage, image_id)
            if image is None:
                continue
            image.excluded = excluded
            image.exclusion_reason = reason or None
            image.reviewed_at = now


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="Nombre max d'images a traiter")
    parser.add_argument("--listing-id", type=int, default=None, help="Ne traiter qu'une annonce")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    args = parser.parse_args()

    if not settings.anthropic_api_key:
        print("ANTHROPIC_API_KEY manquante dans .env, abandon.")
        return

    pending = _fetch_pending(args.limit, args.listing_id)
    total = len(pending)
    if total == 0:
        print("Aucune image en attente de revue.")
        return
    print(f"{total} images a traiter.")

    semaphore = asyncio.Semaphore(args.concurrency)
    anthropic_client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    processed = 0
    excluded_count = 0
    error_count = 0

    async with httpx.AsyncClient(follow_redirects=True) as http_client:
        for start in range(0, total, BATCH_SIZE):
            batch = pending[start:start + BATCH_SIZE]
            results = await _process_batch(http_client, anthropic_client, batch, semaphore)
            _save_results(results)

            processed += len(results)
            excluded_count += sum(1 for _, excluded, _ in results if excluded)
            error_count += sum(
                1 for _, _, reason in results
                if reason.startswith(("fetch_error", "api_error"))
            )

            print(f"[{processed}/{total}] {excluded_count} exclues, {error_count} erreurs")

    print(f"Termine : {processed} traitees, {excluded_count} exclues, {error_count} erreurs.")


if __name__ == "__main__":
    asyncio.run(main())
