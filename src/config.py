from typing import Optional

from pydantic_settings import BaseSettings

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
    removed_after_missing_scans: int = 3
    detail_refresh_days: int = 7
    tz: str = "Asia/Bangkok"
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
    # Nombre de proxys inverses de confiance devant l'application (tunnel
    # Cloudflare, Render, nginx...). 0 = aucun: l'adresse du client est celle
    # de la connexion TCP. N > 0: l'adresse du client est la N-ieme en partant
    # de la fin de X-Forwarded-For, celle qu'a ecrite le proxy de confiance.
    # Sans ce reglage, derriere un proxy, tous les visiteurs partagent la
    # meme adresse (celle du proxy) et donc le meme budget de requetes et le
    # meme compteur d'echecs de connexion. Voir src/api/main.py::_client_key.
    trusted_proxy_hops: int = 0
    # OAuth Google (connexion "Continuer avec Google"). Les deux vides =>
    # le bouton n'apparait pas et /auth/google repond 404. URL de redirection
    # a declarer dans Google Cloud Console: <origine>/auth/google/callback.
    google_client_id: str = ""
    google_client_secret: str = ""
    anthropic_api_key: Optional[str] = None
    # Abonnements Stripe (src/api/billing_routes.py). Vides par defaut: le
    # bouton de paiement reste inerte (mailto: de secours) et /api/checkout/*
    # repond 404 tant qu'ils ne sont pas remplis, comme le bouton Google
    # ci-dessus.
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_flex: str = ""
    stripe_price_essentiel: str = ""
    stripe_price_serenite: str = ""
    # Envoi de l'email "mot de passe oublié" (src/api/auth_routes.py). Vide =
    # le lien de reinitialisation part dans les logs au lieu d'un email, comme
    # les reglages Google/Stripe ci-dessus quand ils sont vides.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = "no-reply@thaimonth.app"

    # extra="ignore": une cle inconnue dans .env (reglage retire, faute de
    # frappe, variable d'un autre outil) ne doit pas empecher le demarrage.
    # Sans cela, pydantic-settings refuse tout .env portant une cle qui n'est
    # plus un champ -- c'est arrive en retirant MAX_CONCURRENT_REQUESTS,
    # API_HOST et API_PORT, encore presents dans les .env deployes.
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
