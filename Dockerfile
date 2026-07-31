# ── docsearch-api — Image Python ──────────────────────────────
# API REST de recherche (FastAPI) avec filtrage ACL
# Python 3.12 · LibreOffice (conversion aperçu Office → PDF)
#
# Image de base pleinement qualifiée (docker.io/library/...) : podman
# n'a pas de registre implicite, un nom court dépend de la liste
# unqualified-search-registries de la machine — ambigu, et carrément
# bloquant sur les serveurs isolés du réseau où les images arrivent par
# "podman load" (voir HOWTO-deploiement-hors-ligne.md).

FROM docker.io/library/python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    # Conversion Office → PDF pour l'aperçu des documents
    libreoffice \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

# UID de l'utilisateur du conteneur — doit correspondre au propriétaire
# des volumes montés depuis l'hôte. Renommé depuis DOCKER_UID avec le
# passage à podman ; se règle par ./manage.sh build (APP_UID=... ).
ARG APP_UID=1000
RUN useradd -m -u ${APP_UID} appuser 2>/dev/null || useradd -m appuser && \
    chown -R appuser /app
USER appuser

EXPOSE 8000
CMD ["uvicorn", "search_api:app", "--host", "0.0.0.0", "--port", "8000"]
