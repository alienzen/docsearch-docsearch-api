# docsearch-api

API REST de recherche pour **DocSearch** — FastAPI, filtrage par ACL,
aperçu de documents. Fait partie de l'écosystème DocSearch :

| Dépôt | Rôle |
|---|---|
| [docsearch-ingestion](../docsearch-ingestion) | Extraction, ACL, indexation |
| **docsearch-api** (ce dépôt) | API de recherche |
| [docsearch-ui](../docsearch-ui) | Interface web statique |
| [docsearch-infra](../docsearch-infra) | Orchestration Docker Compose |
| [docsearch-docs](../docsearch-docs) | Documents commerciaux |

Ce dépôt ne dépend d'aucun autre : il lit uniquement un index Elasticsearch
déjà peuplé (par `docsearch-ingestion`). Aucun couplage de code.

## Endpoints

| Méthode | Route | Description |
|---|---|---|
| GET  | `/health` | Santé du service + version ES |
| POST | `/search` | Recherche full-text filtrée par ACL |
| GET  | `/document/{id}` | Détail d'un document (vérifie l'ACL) |
| GET  | `/document/{id}/similar` | Documents similaires (More Like This) |
| GET  | `/api/preview/{id}` | Aperçu PDF (conversion LibreOffice si besoin) |
| GET  | `/metrics` | Statistiques d'indexation |

## Authentification / ACL

L'identité de l'utilisateur est lue depuis le header `X-User`, injecté par
Nginx après validation SSO (AgentConnect, Keycloak…). En développement,
`DEV_USER` dans `.env` simule un utilisateur sans SSO.

Chaque requête de recherche est filtrée automatiquement :

```python
acl_filter = {
    "bool": {
        "should": [
            {"term":  {"acl.public": True}},
            {"term":  {"acl.owner":  username}},
            {"term":  {"acl.users":  username}},
            {"terms": {"acl.groups": user_groups}},  # POSIX + LDAP/AD
        ],
        "minimum_should_match": 1
    }
}
```

## Lancer en local (nécessite un ES déjà peuplé)

```bash
cp .env.example .env
docker build -t docsearch-api .
docker run -p 8000:8000 --env-file .env \
  --network docsearch-infra_docsearch-net \
  docsearch-api

curl http://localhost:8000/health
open http://localhost:8000/docs   # Swagger UI
```

## Activer LDAP/Active Directory

```bash
# Dans .env
LDAP_ENABLED=true
LDAP_HOST=ldap://votre-dc.domaine.gouv.fr
LDAP_BASE=dc=domaine,dc=gouv,dc=fr
LDAP_BINDDN=cn=svc-docsearch,ou=services,dc=domaine,dc=gouv,dc=fr
LDAP_PASS=...
```

`ldap3` est une implémentation Python pure — aucune dépendance système
(pas besoin de `libldap-dev`).
