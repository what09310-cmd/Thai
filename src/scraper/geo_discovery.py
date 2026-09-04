import sqlite3
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urldefrag
from collections import deque
import time

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from src.config import settings


def _sqlite_path() -> str:
    # Dérivé de DATABASE_URL plutôt qu'un nom en dur: sinon ce script
    # continue silencieusement à lire un fichier obsolète dès que
    # DATABASE_URL pointe ailleurs (ex. le Postgres de docker-compose).
    if not settings.database_url.startswith("sqlite"):
        raise SystemExit(
            f"DATABASE_URL n'est pas SQLite ({settings.database_url!r}), "
            "ce script ne lit qu'une base SQLite locale."
        )
    return settings.database_url.split("///", 1)[1]


BASE_URL = "https://www.renthub.in.th"

START_URLS = [
    "https://www.renthub.in.th/en/browse/provinces",
    "https://www.renthub.in.th/en/browse/zones",
    "https://www.renthub.in.th/en/short-term-rental",
]


def init_db():

    conn = sqlite3.connect(_sqlite_path())

    conn.execute("""
        CREATE TABLE IF NOT EXISTS locations (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            name TEXT,

            url TEXT UNIQUE NOT NULL,

            location_type TEXT DEFAULT 'unknown',

            parent_name TEXT,

            discovered_from TEXT,

            status TEXT DEFAULT 'pending',

            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()

    return conn


def normalize_url(url):

    url, _ = urldefrag(url)

    parsed = urlparse(url)

    clean_url = (
        parsed.scheme
        + "://"
        + parsed.netloc
        + parsed.path
    )

    return clean_url.rstrip("/")


def is_valid_renthub_url(url):

    parsed = urlparse(url)

    return parsed.netloc in [
        "renthub.in.th",
        "www.renthub.in.th"
    ]


def is_location_url(url):

    path = urlparse(url).path

    allowed_prefixes = [

        "/en/short-term-rental/",

        # Ajoute ici d'autres formats
        # de pages géographiques
    ]

    return any(
        path.startswith(prefix)
        for prefix in allowed_prefixes
    )


def get_page(url):

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "Chrome/120 Safari/537.36"
        )
    }

    response = requests.get(
        url,
        headers=headers,
        timeout=30
    )

    response.raise_for_status()

    return response.text


def extract_links(page_url, html):

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    links = []

    for tag in soup.find_all(
        "a",
        href=True
    ):

        href = tag["href"]

        full_url = urljoin(
            BASE_URL,
            href
        )

        full_url = normalize_url(
            full_url
        )

        if is_valid_renthub_url(
            full_url
        ):

            links.append(
                full_url
            )

    return set(links)


def save_location(
    conn,
    url,
    discovered_from
):

    name = (
        url.rstrip("/")
        .split("/")[-1]
    )

    conn.execute(
        """
        INSERT OR IGNORE INTO locations (

            name,
            url,
            discovered_from

        )

        VALUES (?, ?, ?)
        """,
        (
            name,
            url,
            discovered_from
        )
    )

    conn.commit()


def crawl():

    conn = init_db()

    queue = deque(
        START_URLS
    )

    visited = set()

    all_locations = set()

    while queue:

        current_url = queue.popleft()

        current_url = normalize_url(
            current_url
        )

        if current_url in visited:
            continue

        visited.add(
            current_url
        )

        print(
            f"\nSCRAPING : {current_url}"
        )

        try:

            html = get_page(
                current_url
            )

        except Exception as e:

            print(
                f"ERREUR : {e}"
            )

            continue

        links = extract_links(
            current_url,
            html
        )

        print(
            f"Liens trouvés : {len(links)}"
        )

        for url in links:

            # URL géographique
            if is_location_url(url):

                all_locations.add(
                    url
                )

                save_location(
                    conn,
                    url,
                    current_url
                )

                # Explorer aussi
                # cette page
                if url not in visited:

                    queue.append(
                        url
                    )

        time.sleep(1)

    print(
        "\n========================"
    )

    print(
        f"TOTAL LOCATIONS : "
        f"{len(all_locations)}"
    )

    print(
        "========================"
    )

    conn.close()


if __name__ == "__main__":

    crawl()