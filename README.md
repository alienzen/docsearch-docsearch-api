# docsearch-api

API REST de recherche pour **DocSearch** — FastAPI, filtrage par ACL,
aperçu de documents. Fait partie de l'écosystème DocSearch :

| Dépôt | Rôle |
|---|---|
| [docsearch-ingestion](../docsearch-ingestion) | Extraction, ACL, indexation |
| **docsearch-api** (ce dépôt) | API de recherche |
| [docsearch-ui-vue](../docsearch-ui-vue) | Interface web (Vue 3 + DSFR) |
| [docsearch-infra](../docsearch-infra) | Orchestration podman + systemd (Quadlet) |
| [docsearch-docs](../docsearch-docs) | Documents commerciaux |
| `docsearch-dataset-generator` | Génération de jeux de test (cloné à la demande) |

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
`docsearch-alert-worker` (unité Quadlet de `docsearch-infra`), même image que
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

## Authentification

**Tout vit dans [`app/auth/`](app/auth/)** — architecture reprise de
`charlie/app-api-auth` ; les écarts et leur justification sont dans
`docsearch-infra/PLAN-AUTH-SSO.md`.

L'identité vient d'un **jeton RS256 signé par cette application**, posé en
cookie `httpOnly` à la connexion et vérifié à chaque requête
(`app/auth/deps.py::current_user`). Elle ne vient plus de l'en-tête
`X-User` : celui-ci était censé être injecté par Nginx après validation
SSO, mais le SSO n'a jamais été branché et l'API publiant son port,
`curl -H "X-User: alice.admin" …/admin/status` répondait `200`. `X-User`
subsiste comme harnais de développement, sous `TRUST_X_USER_HEADER`, et
l'API **refuse de démarrer** s'il est armé avec `API_ENV=production`.

| Route | Rôle |
|---|---|
| `POST /auth/login` | `{identifiant, mot_de_passe}` → cookies de session. Le **serveur** choisit le fournisseur : l'existence d'un compte de secours local est le discriminant, et il n'y a aucun repli de l'un vers l'autre |
| `GET /auth/login/kerberos` | Connexion automatique par ticket SPNEGO (voir plus bas) |
| `POST /auth/refresh` | Renouvelle le jeton d'accès. Le jeton de rafraîchissement ne sert **qu'une fois** |
| `POST /auth/logout` | Révoque la session côté Redis — sans quoi « se déconnecter » n'effacerait qu'un cookie recollable |
| `GET /auth/me` | Identité, groupes effectifs, `is_admin` |
| `GET /auth/check-access`, `/auth/check-admin` | Cibles internes du `auth_request` de Nginx, qui garde chaque page |
| `GET /auth/.well-known/jwks.json` | Clé publique (RFC 7517) |

Régimes d'erreur, constants : `401` identifiants refusés (message
générique unique, jamais de variation qui dirait lequel des deux est en
cause), `403` authentifié mais hors du groupe requis, `429` trop de
tentatives, `501` SSO désactivé, `503` annuaire / Redis / keytab / clés
indisponibles. **Un 503 n'est jamais présenté comme un 401** : une panne
déguisée en mot de passe faux envoie chercher au mauvais endroit.

Prérequis, une fois : `scripts/generer-cles.py` (les clés vivent hors du
dépôt et hors de l'image). Le dossier étant monté **en lecture seule**
dans le service, la génération passe par un conteneur jetable :

```bash
sudo install -d -o 1000 -g 1000 -m 700 /etc/docsearch/jwt
sudo podman run --rm -v /etc/docsearch/jwt:/etc/docsearch/jwt:Z \\
     localhost/docsearch/api:latest python scripts/generer-cles.py
```

### Comptes de secours locaux

`scripts/gerer-comptes-locaux.py`, jamais une route HTTP. Ce **n'est pas**
une gestion d'utilisateurs : sans annuaire, `require_access` refuse tout
le monde, administration comprise — ces comptes sont la porte de secours,
et ils **portent leurs propres groupes**, sans quoi ils se feraient
refuser par le contrôle qu'ils sont censés contourner.

### Connexion automatique Kerberos / SPNEGO

`app/auth/kerberos.py`, transposé de `charlie/app-api-auth`. Désactivé par
défaut (réglage à chaud `sso_kerberos_enabled`, panneau
d'administration) : sans interrupteur, une installation sans keytab
répondrait un défi que personne ne peut relever, à chaque chargement de
page.

Ce qui décide du succès n'est pas le code : un FQDN (le navigateur dérive
le SPN du nom d'hôte — il ne tente **rien** contre une IP littérale), un
SPN `HTTP/<fqdn>`, un keytab, un certificat au même nom, et une stratégie
de parc autorisant les navigateurs à envoyer un ticket. Voir
`PLAN-AUTH-SSO.md` §2.5.

## ACL

Chaque requête de recherche est filtrée automatiquement, à partir des
**groupes effectifs** (annuaire ∪ compte de secours,
`app/auth/directory.py::get_effective_groups` — point unique de vérité) :

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
correspondante : `docsearch-ui-vue/admin.html` (+ `src/pages/admin/`).

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

**Aucune de ces routes n'a besoin d'accéder au moteur de conteneurs** : l'état est
vérifié via le réseau applicatif normal (HTTP, Redis, Kafka — comme
un client classique), et le déclenchement de scan publie simplement
sur Kafka (les workers déjà actifs font le travail). Piloter le nombre
de workers ou démarrer/arrêter des conteneurs reste réservé à
`manage.sh` en CLI (`docsearch-infra`).

### Tester sans annuaire

`ADMIN_AUTH_DISABLED=true` contourne le contrôle de **groupe** sur
`/admin/*`. Il ne dispense plus d'être authentifié — c'est la différence
avec son comportement précédent, où il ouvrait aussi le panneau à un
anonyme complet.

⚠️ **Jamais en production**, et ce n'est plus une simple recommandation :
avec `API_ENV=production`, l'API **refuse de démarrer** si ce drapeau (ou
l'un des quatre autres harnais) est armé, plutôt que de l'ignorer — voir
`app/auth/guardrails.py` et `docsearch-infra/HOWTO-simuler-utilisateur.md`.
Hors production, un encadré s'affiche au démarrage et chaque usage laisse
une ligne de log.

**Modules dupliqués depuis `docsearch-ingestion`** (architecture
multi-dépôts : impossible d'importer le code d'un autre dépôt au
build) — `filetype_config.py`, `runtime_config.py`, `path_filter.py`
doivent rester identiques entre les deux dépôts. Redis reste la seule
source de vérité partagée, donc pas de risque de désynchronisation des
*données* — seul le *code* doit être maintenu en parallèle.

## Statistiques par groupe d'utilisateurs

Les journaux enregistrent, **au moment de l'événement**, les groupes
LDAP de l'utilisateur dans un champ `groups` (`keyword`) :

| Index | Écrit par | Remarque |
|---|---|---|
| `search_logs` | `search_log.log_search()` | Écrit dès la recherche, **pas** à l'avis : `POST /feedback` est une mise à jour partielle du même document, y attacher les groupes n'en aurait couvert que les recherches notées |
| `nps_logs` | `nps_log.log_nps()` | |
| `suggestions` | `suggestion_log.log_suggestion()` | **Uniquement si un `username` est présent** — une suggestion déposée anonymement ne reçoit pas de groupe, sans quoi l'anonymat choisi par son auteur serait percé |

Le mapping est ajouté par `put_mapping`, qui **fusionne sans écraser** :
les documents déjà indexés restent en place, simplement dépourvus du
champ. Ils tombent alors dans un lot `__sans_groupe__`, rendu « Non
renseigné » à l'écran — jamais ignoré silencieusement, un total par
groupe qui ne retombe pas sur le total global ferait douter de tout le
tableau.

**Restitution** — les endpoints existants sont étendus, aucun nouveau :
`GET /admin/search-logs/summary` (clés `searches_by_group` et
`by_group`, celle-ci portant avis positifs/négatifs par groupe),
`.../zero-results`, `GET /admin/nps-summary` (score **recalculé** par
groupe : `%promoteurs − %détracteurs`, jamais moyenné depuis le score
global) et `GET /admin/suggestions`.

`GET /admin/search-logs/export` porte une colonne **« Groupes »**,
juste après « Utilisateur ».

Deux propriétés à connaître avant de lire ces chiffres, rappelées sur
`stats.html` :

- un utilisateur appartenant à plusieurs groupes compte dans chacun —
  **la somme des lignes dépasse le total**, c'est correct ;
- **aucun seuil d'anonymat** n'est appliqué : dans un groupe très
  restreint, « ce groupe a mis 0 » désigne quelqu'un. Les consultations
  d'administration sont tracées dans le journal d'audit.

### Rétro-remplir l'historique (opération exceptionnelle)

`backfill_groups.py` complète les documents antérieurs à l'ajout du
champ, sans quoi le lot « Non renseigné » écrase tous les autres
pendant des mois :

```bash
sudo podman exec docsearch-api python3 backfill_groups.py          # simulation
sudo podman exec docsearch-api python3 backfill_groups.py --apply  # écriture
```

⚠️ **Sémantique inverse de la capture normale** : le script applique
l'appartenance LDAP **d'aujourd'hui** à des événements passés. Un agent
ayant changé de service voit ses anciennes recherches recomptées dans
son service actuel. Acceptable une fois pour amorcer les statistiques,
à ne pas transformer en tâche récurrente — rejoué régulièrement, il
réécrirait l'histoire à chaque mouvement de personnel.

Garde-fous : simulation par défaut ; ne touche **que** les documents
dépourvus de `groups` (une valeur capturée à l'écriture fait foi et
n'est jamais écrasée) ; laisse intactes les suggestions anonymes ; et
n'écrit rien pour un utilisateur introuvable dans LDAP — une liste vide
masquerait le fait qu'on n'a rien trouvé.

⚠️ Sur les instances déjà en service, l'index `suggestions` peut porter
un `username` de type `text` : il précède la déclaration en `keyword`
de `suggestion_log.py`, et Elasticsearch ne change jamais le type d'un
champ existant. Toute agrégation dessus échoue (« Fielddata is
disabled ») ; seule une réindexation corrigerait. Le script détecte le
cas et bascule sur le sous-champ `username.keyword`.

## Lancer en local (nécessite un ES déjà peuplé)

```bash
podman build -t localhost/docsearch/api:latest .
podman run -p 8000:8000 --env-file /etc/docsearch/docsearch.env \
  --network docsearch-net \
  localhost/docsearch/api:latest

curl http://localhost:8000/health
open http://localhost:8000/docs   # Swagger UI
```

## Activer LDAP/Active Directory

```bash
# Dans .env
LDAP_ENABLED=true
LDAP_HOST=ldaps://votre-dc.domaine.gouv.fr
LDAP_BASE=dc=domaine,dc=gouv,dc=fr
LDAP_BINDDN=cn=svc-docsearch,ou=services,dc=domaine,dc=gouv,dc=fr
LDAP_PASS=...
```

**`ldaps://` et non `ldap://`** : le bind en clair est désormais refusé,
sauf dérogation explicite `LDAP_ALLOW_PLAINTEXT_INSECURE=true`, qui reste
possible (beaucoup d'annuaires internes n'exposent pas LDAPS, et en faire
une erreur fatale couperait l'application au lieu de la sécuriser) mais
journalise un `WARNING` à chaque connexion. **Une installation existante
qui bindait en clair doit poser ce drapeau au moment de la mise à jour**,
sinon plus personne ne se connecte.

`ldap3` est une implémentation Python pure — aucune dépendance système
(pas besoin de `libldap-dev`).

## Linter

```bash
.venv/bin/pip install -r requirements-dev.txt   # une fois
.venv/bin/ruff check .                          # signale
.venv/bin/ruff check --fix .                    # corrige ce qui est sûr
```

`ruff` tournait déjà ici : `.github/workflows/ci.yml` lance `ruff check app/`
depuis toujours. Ce qui a changé le 2026-08-12, c'est qu'il a désormais un
`ruff.toml` — jusque-là il tournait sur ses seules règles par défaut (E4, E7,
E9, F), sans que ce choix soit écrit nulle part. Le fichier le rend explicite,
ajoute `B`, `C4` et `SIM`, étend l'analyse à `tests/`, et argumente règle par
règle ce qui reste écarté :

- **`E402`** — les constantes d'environnement se lisent délibérément ENTRE les
  imports (`ES_HOST`, `REDIS_HOST`, `LDAP_ENABLED` en tête de `app/search_api.py`),
  et `auth/config.py` lit l'environnement à l'import, ce dont `tests/conftest.py`
  dépend. 39 signalements pour un parti pris assumé.
- **`E701`** — les tables de correspondance alignées de `app/admin_scan.py`.
- **`B904`** — `raise HTTPException(...)` sans `from err` : 80 occurrences, un
  lot à traiter pour lui-même.
- **`E501`** — 383 signalements pour un 95e centile réel à 84 caractères. Aucun
  formateur n'est en place, et le dépôt n'en a jamais eu.

La version est **épinglée à l'identique dans `requirements-dev.txt` et dans la
CI** : un linter dont la version flotte finit par échouer en CI sur une règle
que personne n'a choisie.

## Tests

```bash
python -m pytest
```

Les tests LDAP tapent le **vrai** annuaire de dev de la VM
(`~/ldap-test-stack`) et se sautent proprement s'il est arrêté — ou si
ses deux mots de passe ne sont pas dans l'environnement, car ils ne sont
délibérément pas écrits dans le dépôt :

```bash
export DOCSEARCH_TEST_LDAP_BIND_PASSWORD=...   # cn=admin, voir son docker-compose.yml
export DOCSEARCH_TEST_LDAP_USER_PASSWORD=...   # alice.admin / bob.user, voir 03-users.ldif
```

Sans eux, 91 tests passent et 9 se sautent (`requires_ldap`) ; ceux de session tapent le **vrai** Redis
(`requires_redis`), sous le préfixe `docsearch:auth:` uniquement, nettoyé
avant et après chaque test. `requires_kerberos` marque le seul chemin
qu'aucune machine de ce projet ne peut exercer : l'acceptation d'un ticket
authentique, qui attend un KDC.
