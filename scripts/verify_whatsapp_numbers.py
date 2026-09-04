#!/usr/bin/env python3
"""
Verifie si les numeros stockes dans `Listing.whatsapp` correspondent a de
vrais comptes WhatsApp actifs, via une session WhatsApp Web pilotee par
Playwright (le lien `wa.me`/`api.whatsapp.com` ne valide que le *format* du
numero cote client, pas l'existence d'un compte -- voir la conversation qui
a motive ce script).

Premier lancement : un navigateur Chromium s'ouvre sur web.whatsapp.com et
attend un scan de QR code. Utilise de preference un numero WhatsApp dedie a
ces verifications, pas le compte principal/business : WhatsApp peut limiter
ou bannir un compte qui interroge beaucoup de numeros de facon automatisee.
La session est ensuite reutilisee via le profil persistant dans
`.whatsapp-profile/` (a la racine du repo, ignore par git) -- pas besoin de
rescanner au lancement suivant.

Par defaut le script tourne en dry-run (affiche juste le resultat). Avec
--apply, il efface le `whatsapp` des annonces jugees invalides -- a lancer
seulement apres backup de la base (renthub.db.bak-*), comme pour les autres
scripts de correction en masse de ce dossier.

A utiliser par petits lots (--limit) avec un delai genereux (--delay,
8s par defaut) entre deux numeros : c'est exactement le pattern que WhatsApp
cherche a detecter et limiter cote anti-abus.

Detection : le DOM de WhatsApp Web n'a pas d'identifiants stables (classes
generees, textes localises selon la langue du compte lie). Le script attend
soit le panneau de conversation (`#main`), soit une boite de dialogue
(role="dialog") qui s'affiche quand le numero n'est pas joignable. Teste ce
script sur un numero connu valide et un connu invalide avant de faire
confiance a --apply sur un vrai lot.
"""
import argparse
import asyncio
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.async_api import Page, async_playwright

from src.database.models import Listing
from src.database.session import get_session

PROFILE_DIR = Path(__file__).parent.parent / ".whatsapp-profile"


async def wait_for_login(page: Page, timeout: float) -> None:
    print(f"Si un QR code s'affiche : scanne-le avec le WhatsApp dedie a ces verifications (jusqu'a {int(timeout)}s).")
    await page.wait_for_selector("#pane-side", timeout=timeout * 1000)
    print("Session WhatsApp Web active.")


# Sous-chaines (normalisees, minuscules, multi-langues) qui apparaissent
# specifiquement dans la boite de dialogue "numero invalide / pas sur
# WhatsApp" de WhatsApp Web. Toute autre boite de dialogue (notifications
# navigateur, rappel de mise a jour, annonce de fonctionnalite...) peut
# aussi porter role="dialog" sans rapport avec la validite du numero --
# d'ou ce filtrage par texte plutot que la simple presence d'un dialog.
# NB: on evite les sous-chaines avec apostrophe ("n'est pas valide") car
# WhatsApp Web utilise l'apostrophe typographique (') qui ne matche pas
# l'apostrophe droite (') -- voir _normalize.
INVALID_NUMBER_KEYWORDS = [
    "invalid",
    "pas valide",
    "non valide",
    "not on whatsapp",
    "pas sur whatsapp",
]

# WhatsApp propose de confirmer avant de demarrer une conversation avec un
# numero qui n'est pas deja dans les contacts (boite a deux boutons, ex.
# "Lancement de la discussion" / "Annuler") : il ne le fait que si le
# numero existe reellement, donc on confirme pour verifier plutot que de
# deviner a partir du texte (localise).
CANCEL_KEYWORDS = ["annuler", "cancel", "no"]


def _normalize(text: str) -> str:
    return text.replace("’", "'").replace("‘", "'").strip().replace("\n", " ").lower()


async def check_number(page: Page, number: str) -> tuple[str, str]:
    """Retourne (verdict, detail) avec verdict in {'valid', 'invalid', 'unknown'}.

    `detail` porte le texte de la boite de dialogue rencontree, pour pouvoir
    diagnostiquer les faux positifs/negatifs sans avoir a rejouer le test.
    """
    await page.goto(f"https://web.whatsapp.com/send?phone={number}", wait_until="domcontentloaded")

    chat = page.locator("#main")
    dialog = page.locator("div[role='dialog']")
    try:
        await page.wait_for_selector("#main, div[role='dialog']", timeout=20_000)
    except Exception:
        return "unknown", ""

    if await chat.count() > 0:
        return "valid", ""
    if await dialog.count() == 0:
        return "unknown", ""

    text = _normalize(await dialog.first.inner_text())
    if any(kw in text for kw in INVALID_NUMBER_KEYWORDS):
        return "invalid", text

    buttons = dialog.first.locator("button")
    button_count = await buttons.count()
    if button_count < 2:
        # Boite a un seul bouton (ex. "OK") mais texte non reconnu :
        # probablement sans rapport avec le numero (notif, annonce...).
        return "unknown", text

    for j in range(button_count):
        btn_text = _normalize(await buttons.nth(j).inner_text())
        # Ignore les boutons sans texte (icone "fermer" X) et les boutons
        # d'annulation ; on veut le bouton de confirmation ("Lancement de
        # la discussion" ou equivalent localise).
        if not btn_text or any(kw in btn_text for kw in CANCEL_KEYWORDS):
            continue
        await buttons.nth(j).click()
        break
    else:
        return "unknown", text

    try:
        await page.wait_for_selector("#main, div[role='dialog']", timeout=15_000)
    except Exception:
        return "unknown", text

    if await chat.count() > 0:
        return "valid", text
    if await dialog.count() > 0:
        text2 = _normalize(await dialog.first.inner_text())
        if any(kw in text2 for kw in INVALID_NUMBER_KEYWORDS):
            return "invalid", text2
        return "unknown", text2
    return "unknown", text


async def run(delay: float, limit: int | None, apply_changes: bool, headless: bool, login_timeout: float) -> None:
    with get_session() as session:
        listings = (
            session.query(Listing)
            .filter(Listing.status == "active", Listing.whatsapp.isnot(None))
            .all()
        )
        if not listings:
            print("Aucune annonce active avec whatsapp.")
            return

        # Plusieurs annonces (ex. une meme agence) partagent souvent le
        # meme numero : on ne teste chaque numero qu'une fois, puis on
        # applique le verdict a toutes les annonces qui le partagent.
        by_number: dict[str, list[Listing]] = defaultdict(list)
        for listing in listings:
            by_number[listing.whatsapp].append(listing)
        numbers = list(by_number.keys())
        if limit:
            numbers = numbers[:limit]

        print(
            f"Annonces avec whatsapp : {len(listings)}  "
            f"Numeros uniques a verifier : {len(numbers)}"
        )

        PROFILE_DIR.mkdir(exist_ok=True)
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                str(PROFILE_DIR), headless=headless
            )
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://web.whatsapp.com")
            await wait_for_login(page, login_timeout)

            counts = {"valid": 0, "invalid": 0, "unknown": 0}
            for i, number in enumerate(numbers, 1):
                group = by_number[number]
                ids = ", ".join(l.source_id or str(l.id) for l in group)
                try:
                    result, detail = await check_number(page, number)
                except Exception as exc:
                    print(f"  [erreur] {number}: {exc}")
                    result, detail = "unknown", ""

                counts[result] += len(group)
                suffix = f" -- {detail[:80]!r}" if detail else ""
                shared = f" (partage par {len(group)} annonces: {ids})" if len(group) > 1 else f" (annonce {ids})"
                print(f"  [{i}/{len(numbers)}] {number}: {result}{suffix}{shared}")

                if result == "invalid" and apply_changes:
                    for listing in group:
                        listing.whatsapp = None

                await asyncio.sleep(delay + random.uniform(0, delay * 0.5))

            await context.close()

        print(
            f"Valides: {counts['valid']}  Invalides: {counts['invalid']}  "
            f"Indetermines: {counts['unknown']}  (en annonces, numeros dedupliques a la verification)"
            + (" -- base mise a jour" if apply_changes else " -- dry-run, base non modifiee")
        )


def main() -> None:
    # Le texte des boites de dialogue WhatsApp peut contenir des caracteres
    # que la console Windows (cp1252) ne sait pas encoder.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--delay", type=float, default=8.0, help="Delai minimum en secondes entre deux verifications (defaut: 8)")
    parser.add_argument("--limit", type=int, default=None, help="Nombre max de numeros UNIQUES a verifier (recommande pour un premier lot)")
    parser.add_argument("--apply", action="store_true", help="Efface le whatsapp des annonces invalides en base (sinon dry-run)")
    parser.add_argument("--headless", action="store_true", help="Lance Chromium sans interface (a utiliser une fois la session deja liee)")
    parser.add_argument("--login-timeout", type=float, default=300.0, help="Delai max en secondes pour scanner le QR code (defaut: 300)")
    args = parser.parse_args()
    asyncio.run(run(args.delay, args.limit, args.apply, args.headless, args.login_timeout))


if __name__ == "__main__":
    main()
