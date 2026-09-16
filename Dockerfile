# Python 3.12: la meme version que runtime.txt (Render) et que le workflow
# GitHub Actions, pour que les trois environnements executent le meme
# interpreteur que celui ou les tests ont tourne.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Les dependances d'abord, dans leur propre couche: le cache de build n'est
# invalide que quand les requirements changent, pas a chaque edition du
# code. Une seule image sert l'API et le scraper (docker-compose): ni les
# outils ponctuels ni les tests n'y sont installes.
COPY requirements-api.txt requirements-scraper.txt ./
RUN pip install --no-cache-dir -r requirements-api.txt -r requirements-scraper.txt

COPY src/ ./src/
COPY scripts/ ./scripts/
COPY frontend/ ./frontend/

# Les scripts font `sys.path.insert(0, <racine du depot>)`; l'API est
# lancee en module (`uvicorn src.api.main:app`) depuis /app.
ENV PYTHONPATH=/app

# Ne pas tourner en root: le conteneur n'ecrit que dans /app/exports.
RUN useradd --create-home --uid 1000 renthub \
    && mkdir -p /app/exports \
    && chown -R renthub:renthub /app
USER renthub

EXPOSE 8000

CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
