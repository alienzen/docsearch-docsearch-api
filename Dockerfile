# ── docsearch-api — Image Python ──────────────────────────────
# API REST de recherche (FastAPI) avec filtrage ACL
# Python 3.12 · LibreOffice (conversion aperçu Office → PDF) · Kerberos
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
    # Bibliothèques Kerberos d'EXÉCUTION, pour `gssapi` (connexion
    # automatique SPNEGO, voir app/auth/kerberos.py) : elles restent dans
    # l'image. krb5-user fournit klist/kinit, indispensables pour
    # diagnostiquer un keytab depuis le conteneur ("klist -k") — le premier
    # geste quand le SSO refuse un ticket sans dire pourquoi.
    libgssapi-krb5-2 \
    krb5-user \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

# `gssapi` n'a pas de roue précompilée : elle se compile contre les
# en-têtes krb5. Les outils de compilation sont installés ET RETIRÉS dans
# la MÊME couche — une couche qui les purgerait plus loin les laisserait
# malgré tout dans l'image, un compilateur C embarqué en production pour
# rien.
#
# La construction reste une opération connectée : sur les serveurs isolés,
# l'image arrive déjà construite par "podman load"
# (HOWTO-deploiement-hors-ligne.md). Rien de nouveau n'est téléchargé à
# l'exécution — gssapi ne parle qu'au KDC du domaine, sur l'intranet.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        python3-dev \
        libkrb5-dev \
        krb5-config \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove gcc python3-dev libkrb5-dev krb5-config \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY app/ .
# Génération des clés RS256 et gestion des comptes de secours locaux —
# jamais des routes HTTP, voir leur en-tête.
COPY scripts/ ./scripts/

# ── Identité de la livraison ──────────────────────────────────
# Le fichier VERSION porte la version PRODUIT, la même dans les trois
# dépôts construits en images. Il est copié dans l'image pour servir de
# repli à app/version.py : une image construite sans les --build-arg
# ci-dessous affiche encore la bonne version produit, seule l'estampille
# de build manque.
COPY VERSION .

# Estampille de build, injectée par ./manage.sh build depuis git (voir
# docsearch-infra/manage.sh, build_one). Le dépôt .git n'est pas — et ne
# doit pas être — copié dans l'image : la machine de construction est le
# seul endroit qui connaisse le commit.
ARG DOCSEARCH_VERSION=inconnu
ARG DOCSEARCH_COMMIT=inconnu
ARG DOCSEARCH_BUILD_DATE=inconnu
ENV DOCSEARCH_VERSION=${DOCSEARCH_VERSION} \
    DOCSEARCH_COMMIT=${DOCSEARCH_COMMIT} \
    DOCSEARCH_BUILD_DATE=${DOCSEARCH_BUILD_DATE}
# Labels OCI en plus des variables d'environnement : ils se lisent par
# `podman inspect` SANS démarrer le conteneur, ce qui est le seul moyen
# d'identifier une archive fraîchement chargée par `podman load` sur un
# serveur isolé (voir HOWTO-deploiement-hors-ligne.md).
LABEL org.opencontainers.image.title="docsearch-api" \
      org.opencontainers.image.version=${DOCSEARCH_VERSION} \
      org.opencontainers.image.revision=${DOCSEARCH_COMMIT} \
      org.opencontainers.image.created=${DOCSEARCH_BUILD_DATE}

# UID de l'utilisateur du conteneur — doit correspondre au propriétaire
# des volumes montés depuis l'hôte. Renommé depuis DOCKER_UID avec le
# passage à podman ; se règle par ./manage.sh build (APP_UID=... ).
ARG APP_UID=1000
RUN useradd -m -u ${APP_UID} appuser 2>/dev/null || useradd -m appuser && \
    chown -R appuser /app
USER appuser

EXPOSE 8000
# --h11-max-incomplete-event-size : plafond, en octets, de la requête tant
# que ses en-têtes ne sont pas complets. Le défaut de h11 (16 Ko) est
# INSUFFISANT pour un ticket Kerberos d'Active Directory : le PAC y
# transporte tous les SID de groupes du compte, et l'en-tête
# "Authorization: Negotiate" dépasse couramment 8 Ko sur un utilisateur à
# nombreux groupes. Au-delà du plafond, uvicorn coupe la connexion SANS
# RIEN JOURNALISER — le piège le plus coûteux à diagnostiquer de tout le
# SPNEGO. Doit être relevé DE PAIR avec large_client_header_buffers côté
# Nginx (les deux étages plafonnent la même chose, le plus bas gagne) et
# répété dans l'unité Quadlet, qui redéfinit Exec=.
CMD ["uvicorn", "search_api:app", "--host", "0.0.0.0", "--port", "8000", \
     "--h11-max-incomplete-event-size", "65536"]
