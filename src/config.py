from pydantic_settings import BaseSettings
from typing import Optional

# Valeurs publiees dans le depot (.env.example). L'API refuse de demarrer
# tant qu'elles n'ont pas ete remplacees: la cle HMAC etant connue de
# quiconque lit le depot, un cookie de session valide se forge sans mot de
# passe. Voir src/api/main.py::_assert_secrets_configured.
DEFAULT_SITE_USERNAME = "admin"
DEFAULT_SITE_PASSWORD = "changeme"
DEFAULT_SECRET_KEY = "change-this-to-a-random-secret"


class Settings(BaseSettings):
    database_url: str = "postgresql://user:password@localhost:5432/renthub"
    request_delay: float = 2.0
    max_concurrent_requests: int = 2
    max_pages: Optional[int] = None
    download_images: bool = False
    removed_after_missing_scans: int = 3
    detail_refresh_days: int = 7
    tz: str = "Asia/Bangkok"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    scan_interval_minutes: int = 30
    site_username: str = DEFAULT_SITE_USERNAME
    site_password: str = DEFAULT_SITE_PASSWORD
    secret_key: str = DEFAULT_SECRET_KEY
    # A activer (COOKIE_SECURE=true) dès que l'app est servie en HTTPS:
    # empeche le cookie de session de transiter en clair. Desactive par
    # defaut pour ne pas casser le login en dev local (http://).
    cookie_secure: bool = False
    # Origines autorisees a appeler l'API depuis un autre site, separees par
    # des virgules. Vide par defaut: le frontend etant servi par cette meme
    # application, les appels sont same-origin et le CORS est inutile.
    cors_allow_origins: str = ""
    # OAuth Google (connexion "Continuer avec Google"). Les deux vides =>
    # le bouton n'apparait pas et /auth/google repond 404. URL de redirection
    # a declarer dans Google Cloud Console: <origine>/auth/google/callback.
    google_client_id: str = ""
    google_client_secret: str = ""
    anthropic_api_key: Optional[str] = None

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
