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

## Panneau d'administration (/admin)

Routes protégées par appartenance à un groupe LDAP/AD (`ADMIN_GROUP`,
nécessite `LDAP_ENABLED=true`) — voir `admin_auth.py`. Interface web
correspondante : `docsearch-ui/public/admin.html`.

| Route | Rôle |
|---|---|
| `GET /admin/status` | État de tous les composants (ES, Redis, Tika, Kafka, workers actifs, progression de l'indexation, battement du watcher) |
| `GET /metrics` | Métriques d'indexation (documents indexés, taille de l'index, répartition par extension) — route publique existante, réutilisée par le panneau admin |
| `GET/POST /admin/filetypes` | Types de fichiers indexés (activation, taille max) |
| `GET/POST /admin/config` | Paramètres opérationnels (limites d'archives, cadences) |
| `GET/POST /admin/path-filters` | Inclusion/exclusion de sous-dossiers |
| `POST /admin/purge-path` | Purger l'index existant selon un motif (dry-run par défaut) |
| `POST /admin/scan` | Déclencher un scan d'indexation (en arrière-plan) |

**Aucune de ces routes n'a besoin d'un accès Docker** : l'état est
vérifié via le réseau applicatif normal (HTTP, Redis, Kafka — comme
un client classique), et le déclenchement de scan publie simplement
sur Kafka (les workers déjà actifs font le travail). Piloter le nombre
de workers ou démarrer/arrêter des conteneurs reste réservé à
`manage.sh` en CLI (`docsearch-infra`).

### Tester sans authentification

`ADMIN_AUTH_DISABLED=true` contourne tout contrôle d'accès sur
`/admin/*` (y compris la vérification du header `X-User`) — utile pour
tester le panneau localement sans SSO/LDAP configurés.

⚠️ **Jamais en production** : n'importe qui peut alors modifier la
configuration, purger l'index ou déclencher des scans sans la moindre
vérification. Le contournement est volontairement bruyant (bannière
au démarrage + log à chaque requête `/admin/*`) pour qu'un oubli soit
impossible à manquer dans les logs.

**Modules dupliqués depuis `docsearch-ingestion`** (architecture
multi-dépôts : impossible d'importer le code d'un autre dépôt au
build) — `filetype_config.py`, `runtime_config.py`, `path_filter.py`
doivent rester identiques entre les deux dépôts. Redis reste la seule
source de vérité partagée, donc pas de risque de désynchronisation des
*données* — seul le *code* doit être maintenu en parallèle.

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
