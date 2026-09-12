# Python 3.12 et non 3.13+/3.14: les versions epinglees dans
# requirements.txt (pandas 2.2.2, lxml 5.2.2, psycopg2-binary 2.9.9)
# publient des roues manylinux pour cp312, pas au-dela — sans elles la
# construction bascule sur une compilation depuis les sources.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Les dependances d'abord, dans leur propre couche: le cache de build n'est
# invalide que quand requirements.txt change, pas a chaque edition du code.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

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
