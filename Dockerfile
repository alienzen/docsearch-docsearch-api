# ── docsearch-api — Image Python ──────────────────────────────
# API REST de recherche (FastAPI) avec filtrage ACL
# Python 3.12 · LibreOffice (conversion aperçu Office → PDF)

FROM python:3.12-slim

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

ARG DOCKER_UID=1000
RUN useradd -m -u ${DOCKER_UID} appuser 2>/dev/null || useradd -m appuser && \
    chown -R appuser /app
USER appuser

EXPOSE 8000
CMD ["uvicorn", "search_api:app", "--host", "0.0.0.0", "--port", "8000"]
