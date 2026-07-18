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
| [docsearch-dataset-generator](../docsearch-dataset-generator) | Génération de jeux de test |

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
| GET/POST/DELETE | `/saved-searches` | Recherches enregistrées par utilisateur |
| PATCH | `/saved-searches/{id}/alert` | Active/désactive l'alerte d'une recherche enregistrée (fréquence quotidienne/hebdomadaire) |
| GET  | `/alerts` | Notifications in-app de l'utilisateur (nouveaux résultats détectés par `alert_worker.py`) |
| POST | `/alerts/{id}/seen`, `/alerts/mark-all-seen` | Marque une/toutes les notifications comme lues |
| GET  | `/searchable-sources` | Sources cherchables, pour la présélection avant recherche |
| GET/POST/DELETE | `/collections` | Collections de documents personnelles ("📋 Mes collections") |
| POST | `/collections/{id}/rename`, `/collections/{id}/documents`, `/collections/{id}/documents/{doc_id}` | Gestion du contenu d'une collection |
| POST | `/ask` | Assistant conversationnel (RAG), voir `chat.html` |
| GET  | `/ui-config` | Bascules d'interface publique (lien Assistant IA, pied de page, export...) |
| GET  | `/is-admin` | Indique si l'utilisateur courant a accès au panneau d'administration |
| GET  | `/engagement-config` | Bascules de mesure de satisfaction (pouce, NPS, suggestions) |
| POST | `/feedback`, `/click`, `/nps`, `/suggestions` | Signaux de mesure de satisfaction (voir "Mesure de satisfaction" dans l'admin) |

**Recherche exacte** : entourer la requête de guillemets (`"terme exact"`)
force une correspondance de phrase exacte (ordre et adjacence des mots
respectés, sans tolérance aux fautes de frappe), au lieu de la
recherche floue par défaut (`fuzziness: "AUTO"`, qui tolère les
variantes et fautes de frappe).

**Recherche restreinte à un champ** : `search_in` (`"all"` par défaut,
`"title"`, `"author"` ou `"filepath"`) limite la recherche en texte
libre à un seul champ plutôt que tous — `"all"` interroge `content`,
`title`, `filename` et `author.text`. `author` et `filepath`
interrogent leurs sous-champs analysés respectifs (`author.text`,
`filepath.text`) plutôt que les champs racine, qui sont en `keyword`
(non tokenisés — nécessaires pour le filtre exact des facettes et
`purge_path`/`is_path_allowed`, mais incompatibles avec une recherche
partielle en texte libre). ⚠️ Ces sous-champs ne sont peuplés que pour
les documents indexés après l'ajout de ce mapping — une réindexation
est nécessaire pour que les documents déjà présents deviennent
cherchables par ce biais.

## Alertes sur recherches sauvegardées

Une recherche enregistrée (`saved_searches.py`) peut être marquée
"alerte" (`PATCH /saved-searches/{id}/alert`, fréquence quotidienne ou
hebdomadaire). Un worker séparé, `alert_worker.py` — conteneur
`alert-worker` dans `docsearch-infra/docker-compose.yml`, même image que
`api` mais aucune route HTTP exposée — rejoue périodiquement les
critères de chaque recherche marquée, restreints aux documents dont
`indexed_at` (date d'entrée dans l'index, pas `date_modified`) est
postérieure à la dernière vérification. S'il trouve de nouveaux
résultats, une notification est déposée dans Redis
(`alert_notifications.py`) et lue par l'interface via `GET /alerts`.

**In-app uniquement, pas d'email** : DocSearch n'a aujourd'hui aucune
brique SMTP, et un email ferait sortir des titres de documents
potentiellement confidentiels (filtrés par ACL à l'intérieur de l'app)
hors du périmètre d'accès contrôlé. Suspendable globalement depuis
l'admin (`ui_config.alerts_enabled`), comme les collections et les
mots-clés personnalisés — désactivé, toutes les routes `/alerts*` et
`PATCH /saved-searches/{id}/alert` renvoient 403.

`search_query.py` reconstruit volontairement sa propre version (must +
filtres ACL/facettes) de la requête ES de `/search`, plutôt que
d'importer `search_api.py` dans le worker — ce dernier charge FastAPI,
Kafka et LDAP au chargement du module, inutilement lourd pour un simple
worker de fond. ⚠️ Cette duplication doit rester en cohérence avec la
construction de requête de `/search` : toute évolution de la logique de
filtrage faite dans `search_api.py` doit être répercutée dans
`search_query.py`, sinon une alerte pourrait signaler des documents
qu'une recherche manuelle ne trouverait pas (ou l'inverse).

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
| `GET/POST/DELETE /admin/file-sources[/{name}]`, `.../label`, `.../description`, `.../ocr` | Sources fichiers : CRUD, libellé, description, activation de l'OCR par source |
| `GET/POST/DELETE /admin/sql-sources[/{name}]`, `.../label`, `.../description` | Sources SQL (PostgreSQL/MySQL) |
| `GET/POST/DELETE /admin/sql-dsns[/{name}]` | DSN chiffrés (Fernet) utilisables par les sources SQL |
| `GET/POST/DELETE /admin/web-sources[/{name}]`, `.../label`, `.../description`, `.../pause` | Sources web (Elastic Open Web Crawler) |
| `GET /admin/all-sources`, `POST .../searchable`, `.../collectable` | Vue unifiée fichier/SQL/web — bascules "Recherche"/"Collections", par source |
| `GET/POST /admin/filetypes`, `POST .../reset` | Types de fichiers indexés (activation, taille max), par source |
| `GET/POST /admin/config`, `POST .../reset` | Paramètres opérationnels (limites d'archives, cadences, OCR) |
| `GET/POST /admin/path-filters`, `.../exclude`, `.../include`, `.../remove` | Inclusion/exclusion de sous-dossiers |
| `POST /admin/purge-path` | Purger l'index existant selon un motif (dry-run par défaut) |
| `POST /admin/ui-config` | Bascules d'interface (liens Assistant IA/Administration, export, collections...) — voir `GET /ui-config` public |
| `POST /admin/engagement-config` | Bascules de mesure de satisfaction (pouce, NPS, suggestions) — voir `GET /engagement-config` public |
| `GET /admin/nps-summary`, `.../suggestions`, `POST .../suggestions/{id}/status` | Résultats NPS et suggestions utilisateurs |
| `GET /admin/search-logs[...]`, `.../summary`, `.../zero-results`, `.../export`, `GET /admin/audit-log` | Journaux de recherche et d'audit — alimentent `stats.html` |
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
