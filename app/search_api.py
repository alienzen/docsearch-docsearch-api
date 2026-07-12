# search_api.py — API de recherche avec filtrage ACL
# Mis à jour le 08/07/2026 — ES 9.4.2 · Tika 3.3.1.0 · ACL · multi-source

import os
ES_HOST = os.getenv("ES_HOST", "http://localhost:9200")
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
DEV_USER = os.getenv("DEV_USER", "")
LDAP_ENABLED = os.getenv("LDAP_ENABLED", "false").lower() == "true"

import subprocess
import tempfile
import logging
import io
import re
import json
from pathlib import Path
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Header, Depends, Request, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from elasticsearch import Elasticsearch
from elasticsearch.helpers import scan as es_scan
from ldap_resolver import get_user_groups
from admin_auth import require_admin, is_admin
import cluster_status
import admin_scan
import filetype_config
import runtime_config
import path_filter
import search_log
import nps_log
import suggestion_log
import engagement_config
import ui_config
import saved_searches
import saved_collections
import custom_keywords
import audit_log
import file_sources_config
from file_sources_config import ES_SEARCH_ALIAS, DEFAULT_SOURCE_NAME
import sql_sources_config
import sql_dsn_registry
import web_sources_config

logger = logging.getLogger(__name__)

app = FastAPI(title="DocSearch API", version="2.1.0")

ES_HOST = ES_HOST
es = Elasticsearch(ES_HOST, retry_on_timeout=True, max_retries=3, request_timeout=60)

# Utilisateur anonyme de secours (dev uniquement — désactiver en prod)
DEV_USER = DEV_USER


@app.middleware("http")
async def audit_log_middleware(request: Request, call_next):
    """
    Journal d'audit générique des actions d'administration — voir
    audit_log.py. Volontairement un middleware plutôt qu'un appel
    explicite dans chaque route /admin/* : une nouvelle route de mutation
    est ainsi auditée automatiquement dès sa création, sans modification
    de ce fichier ni oubli possible.

    Ne journalise que les mutations (POST/DELETE/PUT) sous /admin/* dont
    la réponse est un succès (< 400) — un échec (validation, 404, Redis/ES
    injoignable...) ne représente aucun changement réel, l'enregistrer
    serait trompeur. Le corps de la requête est lu ICI, avant call_next :
    Starlette met en cache les octets déjà lus (request._body), la route
    elle-même peut donc ensuite reconstruire son modèle Pydantic à partir
    du corps sans rien perdre.
    """
    is_mutation = (
        request.method in ("POST", "DELETE", "PUT")
        and request.url.path.startswith("/admin/")
    )
    body_bytes = await request.body() if is_mutation else b""

    response = await call_next(request)

    if is_mutation and response.status_code < 400:
        # scope["route"] n'est renseigné qu'après résolution du routage,
        # qui a lieu à l'intérieur de call_next — d'où sa lecture ici,
        # après coup. .path est le PATRON de route (ex:
        # "/admin/file-sources/{name}/label"), request.path_params les valeurs
        # résolues (ex: {"name": "finance"}) — les deux ensemble
        # permettent de reconstituer une action lisible côté UI sans
        # dépendre d'une regex sur l'URL brute.
        route = request.scope.get("route")
        path_template = getattr(route, "path", request.url.path)
        try:
            body = json.loads(body_bytes) if body_bytes else {}
        except json.JSONDecodeError:
            body = {}
        audit_log.log_action(
            es,
            username=resolve_user(request.headers.get("x-user")),
            method=request.method,
            path=path_template,
            path_params=dict(request.path_params),
            body=body if isinstance(body, dict) else {},
            status_code=response.status_code,
        )
    return response


# ── Santé ────────────────────────────────────────────────────
@app.get("/health")
def health():
    try:
        info = es.info()
        return {
            "status":       "ok",
            "es_version":   info["version"]["number"],
            "cluster":      info["cluster_name"],
            "acl_enabled":  True,
            "ldap_enabled": str(LDAP_ENABLED),
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


# ── Modèle de requête ────────────────────────────────────────
class SearchQuery(BaseModel):
    query:           str
    size:            int = 10
    from_:           int = Field(0, alias="from")
    sort:            str = "_score"
    extension:       str | list[str] | None = None
    has_attachments: bool | None = None
    date_from:       str | None = None   # filtre sur date_modified (voir build de la requête)
    date_to:         str | None = None   # idem
    author:          str | list[str] | None = None
    folder:          str | list[str] | None = None   # sélection cumulative, comme extension/author/source
    keywords:        str | list[str] | None = None   # sélection cumulative, comme extension/author/folder/source
    source:          str | list[str] | None = None   # nom(s) de source (file_sources_config.py) — absent = recherche fédérée sur toutes
    search_in:       str = "all"   # "all" | "title" | "author" | "keywords" | "filepath" — restreint le champ interrogé

    model_config = {"populate_by_name": True}


class SavedSearchCreate(BaseModel):
    # Reflète directement l'état de l'UI (voir `state` dans index.html),
    # pas les valeurs résolues envoyées à /search (ex: "ext" est la ou
    # les clés de chip sélectionnées — "word" — pas la liste
    # d'extensions qu'elles recouvrent — [docx, doc]) : ça permet de
    # restaurer l'interface (chips actifs, champs) directement depuis
    # l'enregistrement, sans avoir à inverser une résolution.
    name:      str
    query:     str
    search_in: str = "all"
    ext:       str | list[str] = "all"
    author:    str | list[str] | None = None
    folder:    str | list[str] | None = None
    keywords:  str | list[str] | None = None
    source:    str | list[str] | None = None
    date_from: str | None = None
    date_to:   str | None = None
    sort:      str = "_score"


class SavedCollectionCreate(BaseModel):
    name: str


class SavedCollectionRename(BaseModel):
    name: str


class SavedCollectionDocumentAdd(BaseModel):
    doc_id: str


# ── Filtre ACL ───────────────────────────────────────────────
def build_acl_filter(username: str) -> dict:
    """
    Filtre Elasticsearch garantissant qu'un utilisateur
    ne voit que les documents auxquels il a accès :
      - documents publics (acl.public = true)
      - documents dont il est propriétaire
      - documents partagés explicitement avec lui
      - documents partagés avec un de ses groupes (POSIX ou AD)
    """
    user_groups = get_user_groups(username)

    return {
        "bool": {
            "should": [
                {"term":  {"acl.public": True}},
                {"term":  {"acl.owner":  username}},
                {"term":  {"acl.users":  username}},
                {"terms": {"acl.groups": user_groups}} if user_groups
                else {"term": {"acl.groups": "__never__"}},
            ],
            "minimum_should_match": 1,
        }
    }


def _folder_filter(folder: str | list[str] | None) -> dict | None:
    """
    Filtre ES pour la facette "Dossier" — sélection cumulative (comme
    extension/author/source) : matche tout document sous N'IMPORTE LEQUEL
    des dossiers demandés, exact OU sous-dossier (ex: folder="Finance"
    matche "Finance" et "Finance/Rapports"). Chaque dossier ajoute sa
    propre paire term/prefix au should, combinées en OR.
    """
    if not folder:
        return None
    folders = folder if isinstance(folder, list) else [folder]
    should = []
    for f in folders:
        should.append({"term": {"folder": f}})
        should.append({"prefix": {"folder": f.rstrip("/") + "/"}})
    return {"bool": {"should": should, "minimum_should_match": 1}}


def resolve_user(x_user: str | None) -> str:
    """
    Résout l'identité de l'utilisateur.
    En production : header X-User injecté par Nginx après validation SSO.
    En développement : variable DEV_USER ou 'anonymous'.
    """
    if x_user:
        return x_user.lower()
    if DEV_USER:
        return DEV_USER.lower()
    return "anonymous"


def get_client_ip(request: Request) -> str | None:
    """
    Résout l'IP réelle du client. En production, Nginx est devant l'API
    (voir nginx.conf) et transmet X-Forwarded-For / X-Real-IP — sans ça,
    request.client.host ne serait que l'IP interne de Nginx, pas celle
    de l'utilisateur. X-Forwarded-For peut contenir une chaîne de proxies
    ("client, proxy1, proxy2") : le premier maillon est le client d'origine.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip
    return request.client.host if request.client else None


def _ensure_index_exists():
    """
    Vérifie que l'alias fédéré (ES_SEARCH_ALIAS) existe avant toute
    requête qui le suppose — sans ça, une installation fraîche (avant le
    tout premier ./manage.sh init) remonte une exception ES non gérée,
    traduite par FastAPI en 500 générique ('Internal Server Error') sans
    aucune indication utile. L'alias est créé par create_index() (voir
    docsearch-ingestion/indexer.py) dès la première source indexée —
    toutes les sources y contribuent, jamais un index nommé en dur.
    """
    if not es.indices.exists_alias(name=ES_SEARCH_ALIAS):
        raise HTTPException(
            status_code=503,
            detail=(
                f"Aucune source n'a encore été indexée (alias "
                f"'{ES_SEARCH_ALIAS}' introuvable). Exécutez "
                f"'./manage.sh init' depuis docsearch-infra pour indexer "
                f"la source par défaut."
            ),
        )


def _validate_source_names(source_names: str | list[str] | None) -> list[str]:
    """
    Vérifie que chaque nom de source demandé existe bien dans l'UN des
    trois registres (fichiers, SQL, web) — évite qu'un nom mal
    orthographié matche silencieusement zéro document plutôt que de
    signaler l'erreur. Retourne la liste normalisée (vide si rien demandé).
    """
    if not source_names:
        return []
    names = source_names if isinstance(source_names, list) else [source_names]
    for name in names:
        try:
            file_sources_config.get_source(name)
            continue
        except KeyError:
            pass
        try:
            sql_sources_config.get_source(name)
            continue
        except KeyError:
            pass
        try:
            web_sources_config.get_source(name)
            continue
        except KeyError:
            pass
        raise HTTPException(
            status_code=400,
            detail=f"Source inconnue : '{name}' (fichier, SQL et web confondus).",
        )
    return names


def _searchable_source_names() -> list[str]:
    """
    Noms de TOUTES les sources actuellement cherchables, tous types
    confondus (fichier/SQL/web) — une source peut continuer d'être
    indexée normalement (watcher/sql-worker/web-worker) tout en étant
    exclue de /search via son flag "searchable" (voir set_searchable()
    dans chaque registre). Utilisé pour restreindre CHAQUE recherche,
    fédérée ou non : une source désactivée reste invisible même si elle
    est explicitement demandée via `source`.
    """
    names = []
    for name, s in file_sources_config.get_sources().items():
        if s.searchable:
            names.append(name)
    for name, s in sql_sources_config.get_sources().items():
        if s.searchable:
            names.append(name)
    for name, s in web_sources_config.get_sources().items():
        if s.searchable:
            names.append(name)
    return names


def _collectable_source_names() -> set[str]:
    """
    Noms de TOUTES les sources dont les documents peuvent actuellement
    être ajoutés à une collection ("Mes collections"), tous types
    confondus — indépendant de "searchable" : une source peut rester
    cherchable normalement tout en étant exclue des collections (voir
    set_collectable() dans chaque registre). Utilisé par
    add_collection_document() ; un set (pas une liste) car seul le test
    d'appartenance importe ici, pas l'ordre.
    """
    names = set()
    for name, s in file_sources_config.get_sources().items():
        if s.collectable:
            names.add(name)
    for name, s in sql_sources_config.get_sources().items():
        if s.collectable:
            names.add(name)
    for name, s in web_sources_config.get_sources().items():
        if s.collectable:
            names.add(name)
    return names


@app.get("/searchable-sources")
def get_searchable_sources():
    """
    Public (pas d'auth) — liste des sources actuellement cherchables,
    pour la présélection de sources AVANT de lancer une recherche (voir
    index.html) — complète la facette "Source" existante, qui n'apparaît
    qu'APRÈS une recherche (dérivée des résultats, avec leur compte).
    Contrairement à /admin/all-sources, pas de nombre de documents ni de
    taille d'index (réservé à l'admin) : juste de quoi peupler une liste
    de cases à cocher. `collectable` est inclus pour la même raison que
    `label`/`type` : index.html s'en sert pour masquer la case "ajouter à
    une collection" sur les résultats d'une source qui l'interdit (voir
    sourceCollectable() côté UI), sans appel séparé.
    """
    result = []
    for name, s in file_sources_config.get_sources().items():
        if s.searchable:
            result.append({"name": name, "label": s.label or name, "type": "file", "collectable": s.collectable})
    for name, s in sql_sources_config.get_sources().items():
        if s.searchable:
            result.append({"name": name, "label": s.label or name, "type": "sql", "collectable": s.collectable})
    for name, s in web_sources_config.get_sources().items():
        if s.searchable:
            result.append({"name": name, "label": s.label or name, "type": "web", "collectable": s.collectable})
    return sorted(result, key=lambda s: s["label"].lower())


def _resolve_doc_index(doc_id: str) -> str:
    """
    Un doc_id seul ne dit pas dans quel index il vit (recherche
    fédérée) — on le retrouve via une requête `ids` sur l'alias, dont le
    hit renvoie `_index`. Lève 404 si absent de toutes les sources.
    """
    res = es.search(index=ES_SEARCH_ALIAS, query={"ids": {"values": [doc_id]}}, size=1)
    hits = res["hits"]["hits"]
    if not hits:
        raise HTTPException(status_code=404, detail="Document introuvable")
    return hits[0]["_index"]


def _resolve_doc_source(doc_id: str) -> str | None:
    """
    Retourne le nom de la source (champ "source") d'un document, ou None
    s'il est introuvable — utilisé pour vérifier "collectable" avant
    l'ajout à une collection (voir add_collection_document). Ne lève
    jamais d'erreur ici : un doc_id déjà invalide/inaccessible est un cas
    déjà toléré ailleurs par saved_collections.py (une liste peut
    contenir des doc_ids devenus obsolètes).
    """
    try:
        res = es.search(index=ES_SEARCH_ALIAS, query={"ids": {"values": [doc_id]}}, size=1, source=["source"])
        hits = res["hits"]["hits"]
        return hits[0]["_source"].get("source") if hits else None
    except Exception:
        return None


# ── Recherche ────────────────────────────────────────────────
@app.post("/search")
def search(
    req: SearchQuery,
    request: Request,
    x_user: str | None = Header(default=None),
):
    _ensure_index_exists()
    username   = resolve_user(x_user)
    acl_filter = build_acl_filter(username)

    # search_in restreint la recherche à un seul champ plutôt que tous
    # ("Tout" par défaut). "author" utilise le sous-champ analysé
    # author.text (pas le "author" brut, en keyword — non tokenisé,
    # une recherche en texte libre dessus ne matcherait jamais un nom
    # partiel comme "Dupont" contre "Martin Dupont").
    FIELD_SETS = {
        "all":      ["content", "title^4", "filename^6", "author.text", "keywords.text^2"],
        "title":    ["title"],
        "author":   ["author.text"],
        "keywords": ["keywords.text"],
        "filepath": ["filepath.text"],
    }
    fields = FIELD_SETS.get(req.search_in, FIELD_SETS["all"])

    # Convention habituelle des moteurs de recherche : entourer les
    # termes de guillemets ("terme exact") force une correspondance
    # exacte (type "phrase" — ordre et adjacence des mots respectés,
    # sans tolérance aux fautes de frappe), plutôt que la recherche
    # floue par défaut (fuzziness "AUTO", qui tolère les variantes).
    query_text = req.query.strip()
    is_exact_phrase = len(query_text) >= 2 and query_text.startswith('"') and query_text.endswith('"')

    if not query_text:
        # Champ de recherche vide mais des filtres actifs (ex: syntaxe
        # avancée "auteur:...", "type:...", "source:...", "dossier:..."
        # utilisée seule, sans texte libre — voir index.html,
        # parseAdvancedQuery()) : matche tous les documents, les filtres
        # ci-dessous restent seuls responsables de la restriction.
        must = [{"match_all": {}}]
    elif is_exact_phrase:
        phrase = query_text[1:-1].strip()
        must = [{
            "multi_match": {
                "query":  phrase,
                "fields": fields,
                "type":   "phrase",
            }
        }]
    else:
        must = [{
            "multi_match": {
                "query":     query_text,
                "fields":    fields,
                "fuzziness": "AUTO",
            }
        }]

    # Filtres "de base" : toujours appliqués, jamais concernés par
    # l'exclusion décrite ci-dessous (ACL, pièces jointes, période,
    # sources désactivées pour la recherche). Une source "searchable:
    # false" (voir set_searchable()) est retirée ICI, en amont de tout —
    # donc invisible même si explicitement demandée via `source` : la
    # désactivation est absolue, pas seulement "absente par défaut".
    base_filters = [
        acl_filter,   # ACL en premier — mis en cache par ES
        {"terms": {"source": _searchable_source_names()}},
    ]
    if req.has_attachments:
        base_filters.append({"term": {"has_attachments": True}})
    if req.date_from or req.date_to:
        r = {}
        if req.date_from: r["gte"] = req.date_from
        if req.date_to:   r["lte"] = req.date_to
        base_filters.append({"range": {"date_modified": r}})

    # Filtres "de facette" : chacun correspond à une agrégation affichée
    # dans la barre latérale (extension/auteur/dossier/source), à
    # sélection cumulative. Construits à part des base_filters pour
    # pouvoir, plus bas, calculer le compte de chaque facette en
    # EXCLUANT le filtre de cette facette elle-même — sinon, sélectionner
    # un premier auteur ferait disparaître tous les autres de la liste
    # (impossible d'en cocher un second), pareil pour source/dossier.
    # Motif standard de "faceted navigation" avec post_filter + filter
    # aggregations : https://www.elastic.co/guide/en/elasticsearch/reference/current/search-aggregations-bucket-terms-aggregation.html
    extension_filter = None
    if req.extension:
        # Valeur(s) brutes du champ ES, point compris (".pdf", ".docx"...)
        # — même format que les clés retournées par facets.extensions,
        # pas de transformation ici (même principe que author/source :
        # le client envoie exactement ce que la facette lui a donné).
        exts = req.extension if isinstance(req.extension, list) else [req.extension]
        extension_filter = {"terms": {"extension": exts}}

    author_filter = None
    if req.author:
        authors = req.author if isinstance(req.author, list) else [req.author]
        author_filter = {"terms": {"author": authors}}

    keywords_filter = None
    if req.keywords:
        keywords = req.keywords if isinstance(req.keywords, list) else [req.keywords]
        keywords_filter = {"terms": {"keywords": keywords}}

    folder_filter = _folder_filter(req.folder)

    source_names  = _validate_source_names(req.source)
    source_filter = {"terms": {"source": source_names}} if source_names else None

    facet_filters = {
        "extension": extension_filter,
        "author":    author_filter,
        "keywords":  keywords_filter,
        "folder":    folder_filter,
        "source":    source_filter,
    }
    active_facet_filters = [f for f in facet_filters.values() if f]

    def facet_agg(field: str, size: int, exclude: str) -> dict:
        """Agrégation de facette qui exclut son propre filtre (voir plus
        haut) mais applique tous les AUTRES filtres de facette actifs —
        cocher un auteur ne doit réduire que les dossiers/sources/
        extensions affichés, jamais la liste des autres auteurs."""
        others = [f for name, f in facet_filters.items() if f and name != exclude]
        return {
            "filter": {"bool": {"filter": others}} if others else {"match_all": {}},
            "aggs":   {"values": {"terms": {"field": field, "size": size}}},
        }

    sort_clause = (
        [{"_score": "desc"}]
        if req.sort == "_score"
        # "missing": "_last" explicite plutôt que de compter sur le
        # comportement par défaut d'ES — utile ici car les emails PST
        # n'ont pas de champ "size" (pst_extractor.py ne l'indexe pas),
        # donc un tri par taille doit gérer ces valeurs absentes.
        else [{req.sort: {"order": "desc", "missing": "_last"}}, {"_score": "desc"}]
    )

    try:
        res = es.search(
            index=ES_SEARCH_ALIAS,
            query={"bool": {"must": must, "filter": base_filters}},
            # Les filtres de facette s'appliquent aux résultats ICI (via
            # post_filter, évalué après les agrégations) plutôt que dans
            # `query` — c'est ce qui permet à chaque facet_agg() ci-dessus
            # de les exclure sélectivement sans que les résultats
            # eux-mêmes cessent de respecter TOUS les filtres actifs.
            post_filter={"bool": {"filter": active_facet_filters}},
            highlight={
                "fields": {
                    # max_analyzed_offset : sans lui, dès qu'un document du
                    # lot (ex: gros PST/PDF) dépasse index.highlight.max_analyzed_offset
                    # (1 000 000 caractères), ES fait échouer TOUS les shards
                    # portant ce document, et le highlighting renvoie alors
                    # hits.hits=[] pour la requête entière (total correct,
                    # mais aucun résultat) — d'où des recherches qui
                    # semblaient soudain ne plus rien retourner. On tronque
                    # explicitement l'analyse du surlignage à cette limite.
                    "content": {
                        "fragment_size": 200,
                        "number_of_fragments": 2,
                        "max_analyzed_offset": 1000000,
                    }
                },
                # Sans ceci, ES utilise ses balises par défaut (<em>...</em>),
                # qui ne correspondent à AUCUNE règle CSS du frontend — les
                # termes trouvés n'étaient donc jamais visuellement surlignés,
                # juste en italique. On lui fait directement émettre la classe
                # CSS attendue.
                "pre_tags":  ['<mark class="highlight">'],
                "post_tags": ["</mark>"],
            },
            sort=sort_clause,
            # Nécessaire pour que le tri secondaire par _score (utilisé
            # comme départage quand le tri principal n'est pas _score)
            # soit réellement calculé — sans ça, ES ne calcule pas les
            # scores du tout en dehors d'un tri _score primaire.
            track_scores=True,
            from_=req.from_,
            size=req.size,
            source=["filename", "filepath", "extension", "title", "author", "keywords",
                    "size", "date_created", "date_modified", "indexed_at", "has_attachments", "folder",
                    "source", "acl.owner", "acl.groups", "acl.public"],
            aggs={
                "by_extension": facet_agg("extension",  10, "extension"),
                "by_author":    facet_agg("author",     10, "author"),
                "by_keywords":  facet_agg("keywords",   20, "keywords"),
                "by_folder":    facet_agg("folder_top", 10, "folder"),
                "by_source":    facet_agg("source",     20, "source"),
            }
        )
    except Exception as e:
        # Remonte le vrai message ES plutôt qu'un 500 générique opaque
        # ("Internal Server Error") — indispensable pour diagnostiquer
        # un problème de tri/requête sans avoir à fouiller les logs.
        logger.error(f"[search] Erreur ES pour la requête '{req.query}' (sort={req.sort}) : {e}")
        raise HTTPException(status_code=400, detail=f"Erreur de recherche : {e}")

    hits  = res["hits"]["hits"]
    total = res["hits"]["total"]["value"]

    search_id = search_log.log_search(
        es,
        username=username,
        ip=get_client_ip(request),
        query=req.query,
        search_in=req.search_in,
        source=req.source,
        total_results=total,
        result_files=[h["_source"].get("filename", "") for h in hits],
        extension=req.extension,
        author=req.author,
        folder=req.folder,
        keywords=req.keywords,
        date_from=req.date_from,
        date_to=req.date_to,
    )

    return {
        "total":     total,
        "username":  username,
        "search_id": search_id,
        "results": [
            {
                "id":        h["_id"],
                **h["_source"],
                "score":     round(h["_score"], 4),
                "highlight": h.get("highlight", {}).get("content", []),
            }
            for h in hits
        ],
        "facets": {
            "extensions": res["aggregations"]["by_extension"]["values"]["buckets"],
            "authors":    res["aggregations"]["by_author"]["values"]["buckets"],
            "keywords":   res["aggregations"]["by_keywords"]["values"]["buckets"],
            "folders":    res["aggregations"]["by_folder"]["values"]["buckets"],
            "sources":    res["aggregations"]["by_source"]["values"]["buckets"],
        }
    }


# ── Export des résultats de recherche (XLSX / DOCX) ─────────────
# Même critères que POST /search (SearchQuery), mais TOUS les résultats
# correspondants (jusqu'à SEARCH_EXPORT_MAX_ROWS) plutôt que la seule
# page affichée — d'où une requête ES séparée, sans les agrégations de
# facettes (inutiles ici, pas d'UI à peupler).
SEARCH_EXPORT_MAX_ROWS = 500


class SearchExportQuery(SearchQuery):
    format: str = "xlsx"   # "xlsx" | "docx"


def _build_search_query(req: SearchQuery, username: str) -> dict:
    """
    Construit la requête ES (must + filtres) pour une recherche —
    factorisé entre POST /search et POST /search/export. Ne couvre PAS
    les agrégations de facettes ni le post_filter associé (spécifiques
    à /search, sans objet pour un export).
    """
    acl_filter = build_acl_filter(username)
    FIELD_SETS = {
        "all":      ["content", "title^4", "filename^6", "author.text", "keywords.text^2"],
        "title":    ["title"],
        "author":   ["author.text"],
        "keywords": ["keywords.text"],
        "filepath": ["filepath.text"],
    }
    fields = FIELD_SETS.get(req.search_in, FIELD_SETS["all"])

    query_text = req.query.strip()
    is_exact_phrase = len(query_text) >= 2 and query_text.startswith('"') and query_text.endswith('"')
    if not query_text:
        # Voir le commentaire équivalent dans /search — champ vide + filtres
        # actifs (syntaxe avancée seule) doit matcher tout, pas rien.
        must = [{"match_all": {}}]
    elif is_exact_phrase:
        phrase = query_text[1:-1].strip()
        must = [{"multi_match": {"query": phrase, "fields": fields, "type": "phrase"}}]
    else:
        must = [{"multi_match": {"query": query_text, "fields": fields, "fuzziness": "AUTO"}}]

    filters = [acl_filter, {"terms": {"source": _searchable_source_names()}}]
    if req.has_attachments:
        filters.append({"term": {"has_attachments": True}})
    if req.date_from or req.date_to:
        r = {}
        if req.date_from: r["gte"] = req.date_from
        if req.date_to:   r["lte"] = req.date_to
        filters.append({"range": {"date_modified": r}})
    if req.extension:
        exts = req.extension if isinstance(req.extension, list) else [req.extension]
        filters.append({"terms": {"extension": exts}})
    if req.author:
        authors = req.author if isinstance(req.author, list) else [req.author]
        filters.append({"terms": {"author": authors}})
    if req.keywords:
        keywords = req.keywords if isinstance(req.keywords, list) else [req.keywords]
        filters.append({"terms": {"keywords": keywords}})
    folder_filter = _folder_filter(req.folder)
    if folder_filter:
        filters.append(folder_filter)
    source_names = _validate_source_names(req.source)
    if source_names:
        filters.append({"terms": {"source": source_names}})

    return {"bool": {"must": must, "filter": filters}}


def _slugify_query(text: str) -> str:
    """Nom de fichier sûr à partir de la requête — alphanumérique et
    tirets seulement, tronqué pour rester raisonnable."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return (slug or "recherche")[:60]


def _export_results_xlsx(query_text: str, hits: list) -> StreamingResponse:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Résultats de recherche"
    ws.append(["Nom", "Extension", "Auteur", "Mots-clés", "Source", "Dossier",
               "Date de modification", "Taille (o)", "Chemin", "Extrait"])
    for h in hits:
        s = h["_source"]
        snippet = " … ".join(h.get("highlight", {}).get("content", []))
        ws.append([
            s.get("filename", ""),
            s.get("extension", ""),
            s.get("author", ""),
            ", ".join(s.get("keywords") or []),
            s.get("source", ""),
            s.get("folder", ""),
            s.get("date_modified", ""),
            s.get("size", 0),
            s.get("filepath", ""),
            snippet,
        ])
    for col_idx, width in enumerate([32, 10, 18, 24, 14, 24, 18, 12, 50, 60], start=1):
        ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = width

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    filename = f"resultats-{_slugify_query(query_text)}.xlsx"
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _export_results_docx(query_text: str, hits: list) -> StreamingResponse:
    from docx import Document

    doc = Document()
    doc.add_heading(f'Résultats de recherche — « {query_text} »', level=1)
    doc.add_paragraph(f"{len(hits)} document(s)")
    for h in hits:
        s = h["_source"]
        doc.add_heading(s.get("filename") or "(sans nom)", level=2)
        meta = []
        if s.get("author"):        meta.append(f"Auteur : {s['author']}")
        if s.get("keywords"):      meta.append(f"Mots-clés : {', '.join(s['keywords'])}")
        if s.get("source"):        meta.append(f"Source : {s['source']}")
        if s.get("folder"):        meta.append(f"Dossier : {s['folder']}")
        if s.get("date_modified"): meta.append(f"Modifié le : {s['date_modified'][:10]}")
        if meta:
            doc.add_paragraph(" · ".join(meta))
        if s.get("filepath"):
            doc.add_paragraph(s["filepath"], style="Intense Quote")

    buffer = io.BytesIO()
    doc.save(buffer)
    buffer.seek(0)
    filename = f"resultats-{_slugify_query(query_text)}.docx"
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/search/export")
def export_search_results(req: SearchExportQuery, x_user: str | None = Header(default=None)):
    """
    Export XLSX ou DOCX des résultats d'une recherche — mêmes critères
    que POST /search (même corps de requête, avec juste "format" en
    plus), mais jusqu'à SEARCH_EXPORT_MAX_ROWS résultats plutôt que la
    seule page affichée à l'écran.
    """
    if not ui_config.get_config().get("export_enabled", True):
        raise HTTPException(status_code=403, detail="L'export des résultats est désactivé.")
    _ensure_index_exists()
    username = resolve_user(x_user)
    query = _build_search_query(req, username)

    sort_clause = (
        [{"_score": "desc"}]
        if req.sort == "_score"
        else [{req.sort: {"order": "desc", "missing": "_last"}}, {"_score": "desc"}]
    )

    try:
        res = es.search(
            index=ES_SEARCH_ALIAS,
            query=query,
            sort=sort_clause,
            track_scores=True,
            size=SEARCH_EXPORT_MAX_ROWS,
            source=["filename", "filepath", "extension", "title", "author", "keywords",
                    "size", "date_modified", "folder", "source"],
            highlight={
                # max_analyzed_offset : voir le commentaire équivalent dans
                # /search — sans lui, un seul document trop long dans les
                # 500 lignes de l'export fait échouer le highlighting sur
                # tous les shards qui le portent, et hits.hits revient
                # vide (total correct, mais 0 ligne exportée).
                "fields": {"content": {
                    "fragment_size": 200,
                    "number_of_fragments": 2,
                    "max_analyzed_offset": 1000000,
                }},
                # Pas de balises de surlignage ici (texte brut pour un export,
                # contrairement à /search qui les affiche en HTML).
                "pre_tags": [""], "post_tags": [""],
            },
        )
    except Exception as e:
        logger.error(f"[search/export] Erreur ES pour la requête '{req.query}' : {e}")
        raise HTTPException(status_code=400, detail=f"Erreur de recherche : {e}")

    hits = res["hits"]["hits"]
    if req.format == "docx":
        return _export_results_docx(req.query, hits)
    return _export_results_xlsx(req.query, hits)


# ── Recherches sauvegardées ─────────────────────────────────────
@app.get("/saved-searches")
def list_saved_searches(x_user: str | None = Header(default=None)):
    username = resolve_user(x_user)
    return saved_searches.list_saved(username)


@app.post("/saved-searches")
def create_saved_search(body: SavedSearchCreate, x_user: str | None = Header(default=None)):
    username = resolve_user(x_user)
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="Le nom de la recherche ne peut pas être vide")
    try:
        return saved_searches.save_search(username, body.name, body.model_dump(exclude={"name"}))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.delete("/saved-searches/{search_id}")
def remove_saved_search(search_id: str, x_user: str | None = Header(default=None)):
    username = resolve_user(x_user)
    try:
        return saved_searches.delete_saved(username, search_id)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


# ── Collections de documents ──────────────────────────────────────────
# Strictement personnel (voir saved_collections.py) — entièrement
# suspendable depuis l'admin (ui_config.collections_enabled) : désactivé,
# toutes les routes ci-dessous renvoient 403, y compris la simple
# consultation, plutôt que de ne bloquer que la création (cohérent avec
# l'intention d'un flag "fonctionnalité désactivée" plutôt que "création
# désactivée").
def _require_collections_enabled() -> None:
    if not ui_config.get_config().get("collections_enabled", True):
        raise HTTPException(status_code=403, detail="Les collections de documents sont désactivées.")


@app.get("/collections")
def get_collections(x_user: str | None = Header(default=None)):
    _require_collections_enabled()
    username = resolve_user(x_user)
    return saved_collections.list_collections(es, username)


@app.post("/collections")
def create_collection(body: SavedCollectionCreate, x_user: str | None = Header(default=None)):
    _require_collections_enabled()
    username = resolve_user(x_user)
    try:
        return saved_collections.create_collection(es, username, body.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.delete("/collections/{collection_id}")
def remove_collection(collection_id: str, x_user: str | None = Header(default=None)):
    _require_collections_enabled()
    username = resolve_user(x_user)
    try:
        return saved_collections.delete_collection(es, username, collection_id)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.post("/collections/{collection_id}/rename")
def rename_collection(collection_id: str, body: SavedCollectionRename, x_user: str | None = Header(default=None)):
    _require_collections_enabled()
    username = resolve_user(x_user)
    try:
        return saved_collections.rename_collection(es, username, collection_id, body.name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.post("/collections/{collection_id}/documents")
def add_collection_document(collection_id: str, body: SavedCollectionDocumentAdd, x_user: str | None = Header(default=None)):
    _require_collections_enabled()
    username = resolve_user(x_user)
    source_name = _resolve_doc_source(body.doc_id)
    if source_name is not None and source_name not in _collectable_source_names():
        raise HTTPException(
            status_code=403,
            detail=f"Les documents de la source '{source_name}' ne peuvent pas être ajoutés à une collection.",
        )
    try:
        return saved_collections.add_document(es, username, collection_id, body.doc_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.delete("/collections/{collection_id}/documents/{doc_id}")
def remove_collection_document(collection_id: str, doc_id: str, x_user: str | None = Header(default=None)):
    _require_collections_enabled()
    username = resolve_user(x_user)
    try:
        return saved_collections.remove_document(es, username, collection_id, doc_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


def _check_doc_access(doc: dict, username: str) -> bool:
    """Même règle ACL que build_acl_filter() (public/propriétaire/partagé
    utilisateur ou groupe), mais évaluée sur un document déjà récupéré
    plutôt qu'en filtre de requête ES — utilisée partout où un document
    précis est accédé par id (GET /document, édition des mots-clés
    personnalisés...)."""
    acl         = doc.get("acl", {})
    user_groups = get_user_groups(username)
    return (
        acl.get("public", False)
        or acl.get("owner")  == username
        or username in acl.get("users",  [])
        or any(g in acl.get("groups", []) for g in user_groups)
    )


# ── Détail document ──────────────────────────────────────────
@app.get("/document/{doc_id}")
def get_document(
    doc_id: str,
    x_user: str | None = Header(default=None),
):
    username = resolve_user(x_user)

    doc_index = _resolve_doc_index(doc_id)
    try:
        res = es.get(index=doc_index, id=doc_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Document introuvable")

    doc = res["_source"]

    if not _check_doc_access(doc, username):
        raise HTTPException(status_code=403, detail="Accès refusé")

    return doc


# ── Mots-clés personnalisés ────────────────────────────────────
# Activable/désactivable depuis l'admin (ui_config.custom_keywords_enabled)
# — désactivé, ces deux routes renvoient 403 ; les surcharges déjà
# enregistrées restent dans leur index ES (custom_keywords.py), simplement
# plus modifiables tant que le flag est désactivé (même principe que
# collections_enabled).
#
# Réservé aux documents de TYPE FICHIER ("document"/"archive_member") —
# email PST, page web, ligne SQL n'ont pas de notion de "mots-clés Office/
# PDF" à compléter.
class DocumentKeywordBody(BaseModel):
    keyword: str


def _require_custom_keywords_enabled() -> None:
    if not ui_config.get_config().get("custom_keywords_enabled", True):
        raise HTTPException(status_code=403, detail="Les mots-clés personnalisés sont désactivés.")


def _load_doc_for_keyword_edit(doc_id: str, username: str) -> tuple[str, dict]:
    """Facteur commun aux deux routes ci-dessous : résout l'index,
    récupère le document, vérifie ACL et type. Retourne (doc_index, doc)."""
    doc_index = _resolve_doc_index(doc_id)
    try:
        doc = es.get(index=doc_index, id=doc_id)["_source"]
    except Exception:
        raise HTTPException(status_code=404, detail="Document introuvable")

    if not _check_doc_access(doc, username):
        raise HTTPException(status_code=403, detail="Accès refusé")

    if doc.get("type") not in ("document", "archive_member"):
        raise HTTPException(
            status_code=400,
            detail="Les mots-clés personnalisés ne sont disponibles que pour les documents de type fichier.",
        )
    return doc_index, doc


@app.post("/document/{doc_id}/keywords")
def add_document_keyword(doc_id: str, body: DocumentKeywordBody, x_user: str | None = Header(default=None)):
    _require_custom_keywords_enabled()
    username = resolve_user(x_user)
    keyword = body.keyword.strip()
    if not keyword:
        raise HTTPException(status_code=400, detail="Mot-clé vide.")

    doc_index, doc = _load_doc_for_keyword_edit(doc_id, username)
    try:
        custom_keywords.add_keyword(es, doc_id, doc.get("source"), keyword, username)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    # Effet immédiat sur le document principal — la surcharge persistée
    # ci-dessus n'est réappliquée par le pipeline d'ingestion qu'à la
    # PROCHAINE réindexation (voir indexer.py:apply_keyword_overrides),
    # sans quoi l'utilisateur ne verrait pas son ajout tout de suite.
    #
    # refresh=True (pas "wait_for") : les index de documents passent par
    # restore_after_bulk() (indexer.py) après une indexation en masse, qui
    # fixe refresh_interval à 30s — "wait_for" attendrait alors jusqu'à
    # 30s le prochain rafraîchissement PLANIFIÉ au lieu d'en déclencher un
    # immédiatement. Coût négligeable ici (écriture d'un seul document,
    # action utilisateur peu fréquente), contrairement à un rafraîchissement
    # forcé pendant un bulk() de plusieurs milliers de documents.
    current = doc.get("keywords") or []
    if keyword not in current:
        current = current + [keyword]
        es.update(index=doc_index, id=doc_id, refresh=True, doc={"keywords": current})
    return {"keywords": current}


@app.delete("/document/{doc_id}/keywords/{keyword}")
def remove_document_keyword(doc_id: str, keyword: str, x_user: str | None = Header(default=None)):
    _require_custom_keywords_enabled()
    username = resolve_user(x_user)

    doc_index, doc = _load_doc_for_keyword_edit(doc_id, username)
    try:
        custom_keywords.remove_keyword(es, doc_id, doc.get("source"), keyword, username)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    current = [k for k in (doc.get("keywords") or []) if k != keyword]
    if current != (doc.get("keywords") or []):
        # refresh=True — voir le commentaire équivalent dans add_document_keyword().
        es.update(index=doc_index, id=doc_id, refresh=True, doc={"keywords": current})
    return {"keywords": current}


# ── Aperçu document ──────────────────────────────────────────
@app.get("/api/preview/{doc_id}")
def preview_document(
    doc_id: str,
    x_user: str | None = Header(default=None),
):
    # Vérification ACL via get_document (lève 403 si refusé)
    doc      = get_document(doc_id, x_user=x_user)
    filepath = doc["filepath"]
    ext      = doc["extension"]

    if "::" in filepath:
        # Document extrait d'une archive (.zip, .tar.*, .7z) — il n'existe
        # que temporairement pendant l'indexation, aucun aperçu possible.
        archive_path, member = filepath.split("::", 1)
        raise HTTPException(
            status_code=422,
            detail=f"Aperçu non disponible : document extrait de l'archive "
                   f"'{Path(archive_path).name}' (membre : {member})"
        )

    if not Path(filepath).exists():
        raise HTTPException(status_code=404, detail="Fichier introuvable")

    if ext == ".pdf":
        return FileResponse(filepath, media_type="application/pdf")
    if ext in {".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"}:
        return _convert_to_pdf(filepath)
    if ext == ".txt":
        return FileResponse(filepath, media_type="text/plain; charset=utf-8")
    raise HTTPException(status_code=415, detail="Format non prévisualisable")


def _convert_to_pdf(filepath: str) -> StreamingResponse:
    with tempfile.TemporaryDirectory() as tmpdir:
        subprocess.run(
            ["libreoffice", "--headless", "--convert-to", "pdf",
             "--outdir", tmpdir, filepath],
            check=True, timeout=30
        )
        pdf_name = Path(filepath).stem + ".pdf"
        content  = open(os.path.join(tmpdir, pdf_name), "rb").read()
    return StreamingResponse(
        iter([content]),
        media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename={pdf_name}"},
    )


# ── Métriques ─────────────────────────────────────────────────
@app.get("/metrics")
def get_metrics():
    """Métriques agrégées sur TOUTES les sources (via l'alias fédéré) —
    voir /admin/status pour une ventilation par source individuelle."""
    _ensure_index_exists()
    info    = es.info()
    count   = es.count(index=ES_SEARCH_ALIAS)["count"]
    stats   = es.indices.stats(index=ES_SEARCH_ALIAS)
    size_gb = stats["_all"]["total"]["store"]["size_in_bytes"] / 1e9
    by_ext  = es.search(
        index=ES_SEARCH_ALIAS, size=0,
        aggs={"by_ext": {"terms": {"field": "extension", "size": 20}}}
    )
    return {
        "indexed":      count,
        "size_gb":      round(size_gb, 2),
        "by_extension": by_ext["aggregations"]["by_ext"]["buckets"],
        "es_version":   info["version"]["number"],
        "acl_enabled":  True,
    }


# ═══════════════════════════════════════════════════════════════
# PANNEAU D'ADMINISTRATION — /admin/*
#
# Toutes ces routes exigent Depends(require_admin) : l'utilisateur
# doit être authentifié (X-User, injecté par Nginx après validation
# SSO) ET membre du groupe ADMIN_GROUP (résolu via LDAP/AD).
#
# Aucune de ces routes n'a besoin d'un accès Docker — vérification
# d'état via le réseau applicatif (HTTP/Redis/Kafka), déclenchement
# de scan/purge via publication Kafka ou requêtes ES directes. Piloter
# le nombre de workers ou démarrer/arrêter des conteneurs reste
# réservé à `manage.sh` en CLI (voir docsearch-infra).
# ═══════════════════════════════════════════════════════════════

from fastapi import BackgroundTasks


class FiletypeUpdate(BaseModel):
    enabled: bool | None = None
    max_size_mb: float | None = None
    source: str = DEFAULT_SOURCE_NAME


class ConfigUpdate(BaseModel):
    value: str


class PathFilterPattern(BaseModel):
    pattern: str
    source: str = DEFAULT_SOURCE_NAME


class PurgeRequest(BaseModel):
    pattern: str
    source: str = DEFAULT_SOURCE_NAME
    dry_run: bool = True


class ScanRequest(BaseModel):
    source: str = DEFAULT_SOURCE_NAME
    subfolder: str | None = None


class SourceCreate(BaseModel):
    name: str
    es_index: str
    subfolder: str | None = None
    label: str | None = None
    description: str | None = None


class SqlFieldMapping(BaseModel):
    column: str
    es_field: str
    es_type: str
    analyzer: str | None = None


class SqlSourceCreate(BaseModel):
    name: str
    db_type: str
    connection_ref: str
    query: str
    id_column: str
    es_index: str
    fields: list[SqlFieldMapping]
    poll_interval_seconds: int = sql_sources_config.DEFAULT_POLL_INTERVAL_SECONDS
    label: str | None = None
    description: str | None = None


class SqlDsnCreate(BaseModel):
    name: str
    dsn: str


class WebSourceCreate(BaseModel):
    name: str
    crawl_index: str
    es_index: str
    acl_public: bool = True
    poll_interval_seconds: int = web_sources_config.DEFAULT_POLL_INTERVAL_SECONDS
    label: str | None = None
    description: str | None = None


def _sources_status() -> dict:
    """Nombre de documents par source enregistrée — un index manquant
    (source enregistrée mais jamais indexée) compte pour 0 plutôt que de
    faire échouer tout /admin/status."""
    result = {}
    for name, source in file_sources_config.get_sources().items():
        try:
            result[name] = {
                "es_index": source.es_index,
                "label":    source.label,
                "folder":   source.folder,
                "indexed":  es.count(index=source.es_index)["count"],
            }
        except Exception:
            result[name] = {
                "es_index": source.es_index,
                "label":    source.label,
                "folder":   source.folder,
                "indexed":  0,
            }
    return result


@app.get("/admin/status")
def admin_status(user: str = Depends(require_admin)):
    """État de tous les composants : ES, Redis, Tika, Kafka, workers
    actifs, progression de l'indexation (lag), battement du watcher —
    plus une ventilation du nombre de documents par source."""
    status = cluster_status.get_full_status()
    status["sources"] = _sources_status()
    return status


class LabelUpdate(BaseModel):
    label: str


class DescriptionUpdate(BaseModel):
    description: str


@app.get("/admin/file-sources")
def admin_get_sources(user: str = Depends(require_admin)):
    return {
        name: {"es_index": s.es_index, "folder": s.folder, "label": s.label, "description": s.description}
        for name, s in file_sources_config.get_sources().items()
    }


@app.post("/admin/file-sources")
def admin_add_source(body: SourceCreate, user: str = Depends(require_admin)):
    try:
        # add_source() REMPLACE l'entrée existante en entier — on relit
        # searchable/collectable au préalable pour ne pas les réinitialiser
        # à True au premier "Modifier" venu (voir add_source() docstring).
        existing = file_sources_config.get_sources().get(body.name)
        return file_sources_config.add_source(
            body.name, body.es_index, subfolder=body.subfolder, label=body.label,
            searchable=existing.searchable if existing else True,
            collectable=existing.collectable if existing else True,
            description=body.description,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/admin/file-sources/{name}")
def admin_remove_source(name: str, user: str = Depends(require_admin)):
    """Retire la source du registre (le watcher arrête de l'observer) —
    NE supprime PAS l'index Elasticsearch ni les documents déjà
    indexés : utiliser /admin/purge-path pour nettoyer l'existant."""
    try:
        return file_sources_config.remove_source(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/admin/file-sources/{name}/label")
def admin_set_source_label(name: str, body: LabelUpdate, user: str = Depends(require_admin)):
    """Modifie le libellé d'affichage d'une source fichier — son nom
    (registre + champ "source" des documents déjà indexés) ne change pas."""
    try:
        return file_sources_config.set_label(name, body.label)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/admin/file-sources/{name}/description")
def admin_set_source_description(name: str, body: DescriptionUpdate, user: str = Depends(require_admin)):
    try:
        return file_sources_config.set_description(name, body.description)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/admin/sql-sources")
def admin_get_sql_sources(user: str = Depends(require_admin)):
    return {
        name: {
            "db_type":               s.db_type,
            "connection_ref":        s.connection_ref,
            "query":                 s.query,
            "id_column":             s.id_column,
            "es_index":              s.es_index,
            "poll_interval_seconds": s.poll_interval_seconds,
            "label":                 s.label,
            "description":           s.description,
            "fields": [
                {"column": f.column, "es_field": f.es_field, "es_type": f.es_type, "analyzer": f.analyzer}
                for f in s.fields
            ],
        }
        for name, s in sql_sources_config.get_sources().items()
    }


@app.post("/admin/sql-sources")
def admin_add_sql_source(body: SqlSourceCreate, user: str = Depends(require_admin)):
    """
    Enregistre (ou met à jour) une source SQL. `connection_ref` est le
    NOM d'une variable d'environnement contenant le DSN complet — jamais
    le DSN lui-même, qui ne transite donc jamais par cette route ni par
    Redis. sql-worker (docsearch-ingestion) prend en compte la nouvelle
    source sous ~5s, sans redémarrage.
    """
    try:
        # add_source() REMPLACE l'entrée existante en entier — on relit
        # searchable/collectable au préalable pour ne pas les réinitialiser
        # à True au premier "Modifier" venu (voir add_source() docstring).
        existing = sql_sources_config.get_sources().get(body.name)
        return sql_sources_config.add_source(
            name=body.name,
            db_type=body.db_type,
            connection_ref=body.connection_ref,
            query=body.query,
            id_column=body.id_column,
            es_index=body.es_index,
            fields=[f.model_dump() for f in body.fields],
            poll_interval_seconds=body.poll_interval_seconds,
            label=body.label,
            searchable=existing.searchable if existing else True,
            collectable=existing.collectable if existing else True,
            description=body.description,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/admin/sql-sources/{name}")
def admin_remove_sql_source(name: str, user: str = Depends(require_admin)):
    """Retire la source SQL du registre (sql-worker arrête de
    l'interroger) — NE supprime PAS l'index Elasticsearch ni les
    documents déjà indexés."""
    try:
        return sql_sources_config.remove_source(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/admin/sql-sources/{name}/label")
def admin_set_sql_source_label(name: str, body: LabelUpdate, user: str = Depends(require_admin)):
    """Modifie le libellé d'affichage d'une source SQL — son nom
    (registre + champ "source" des documents déjà indexés) ne change pas."""
    try:
        return sql_sources_config.set_label(name, body.label)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/admin/sql-sources/{name}/description")
def admin_set_sql_source_description(name: str, body: DescriptionUpdate, user: str = Depends(require_admin)):
    try:
        return sql_sources_config.set_description(name, body.description)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── DSN SQL chiffrés (registre dynamique, alternative aux variables
# d'environnement de connection_ref) — voir sql_dsn_registry.py ─────
@app.get("/admin/sql-dsns")
def admin_list_sql_dsns(user: str = Depends(require_admin)):
    """Liste les DSN dynamiques enregistrés (nom + indice non sensible
    schéma/hôte, jamais le DSN déchiffré ni son chiffré). Ne lève jamais
    (même comportement que /admin/sql-sources : Redis injoignable dégrade
    silencieusement vers une liste vide plutôt que de faire échouer la
    route)."""
    return sql_dsn_registry.list_names()


@app.post("/admin/sql-dsns")
def admin_add_sql_dsn(body: SqlDsnCreate, user: str = Depends(require_admin)):
    """
    Enregistre (ou remplace) un DSN chiffré dans Redis, sous un nom au
    format variable d'environnement — ce nom devient ensuite utilisable
    comme connection_ref d'une source SQL, à condition qu'aucune variable
    d'environnement de ce nom n'existe déjà (elle resterait sinon
    prioritaire, voir docsearch-ingestion/app/sql_indexer.py::_resolve_dsn).
    Nécessite DSN_ENCRYPTION_KEY, définie à l'identique côté docsearch-api
    (chiffrement ici) ET côté sql-worker/indexer-init (déchiffrement pour
    se connecter réellement) — voir docsearch-infra/.env.example. Aucune
    connexion à la base n'est testée ici : seule la forme du DSN est
    vérifiée.
    """
    try:
        return sql_dsn_registry.add_dsn(body.name, body.dsn)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.delete("/admin/sql-dsns/{name}")
def admin_remove_sql_dsn(name: str, user: str = Depends(require_admin)):
    """Retire un DSN chiffré du registre — toute source SQL dont le
    connection_ref pointe encore vers ce nom échouera à son prochain
    passage (sauf si une variable d'environnement du même nom existe) ;
    aucune vérification qu'une source l'utilise encore, cohérent avec
    DELETE /admin/sql-sources/{name} qui ne vérifie pas non plus les
    dépendances inverses."""
    try:
        return sql_dsn_registry.remove_dsn(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/admin/web-sources")
def admin_get_web_sources(user: str = Depends(require_admin)):
    return {
        name: {
            "crawl_index":           s.crawl_index,
            "es_index":              s.es_index,
            "acl_public":            s.acl_public,
            "poll_interval_seconds": s.poll_interval_seconds,
            "label":                 s.label,
            "description":           s.description,
            "paused":                s.paused,
        }
        for name, s in web_sources_config.get_sources().items()
    }


@app.post("/admin/web-sources")
def admin_add_web_source(body: WebSourceCreate, user: str = Depends(require_admin)):
    """
    Enregistre (ou met à jour) une source web. `crawl_index` est l'index
    ES intermédiaire écrit par Elastic Open Web Crawler (son
    `output_index`, schéma brut du crawler) — DIFFÉRENT de `es_index`
    (schéma DocSearch final). web-worker (docsearch-ingestion) prend en
    compte la nouvelle source sous ~5s, sans redémarrage.
    """
    try:
        # add_source() REMPLACE l'entrée existante en entier — on relit
        # searchable/collectable au préalable pour ne pas les réinitialiser
        # à True au premier "Modifier" venu (voir add_source() docstring).
        existing = web_sources_config.get_sources().get(body.name)
        return web_sources_config.add_source(
            name=body.name,
            crawl_index=body.crawl_index,
            es_index=body.es_index,
            acl_public=body.acl_public,
            poll_interval_seconds=body.poll_interval_seconds,
            label=body.label,
            searchable=existing.searchable if existing else True,
            collectable=existing.collectable if existing else True,
            description=body.description,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/admin/web-sources/{name}")
def admin_remove_web_source(name: str, user: str = Depends(require_admin)):
    """Retire la source web du registre (web-worker arrête de la
    synchroniser) — NE supprime PAS les index Elasticsearch (crawl_index
    ni es_index) ni les documents déjà indexés."""
    try:
        return web_sources_config.remove_source(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/admin/web-sources/{name}/label")
def admin_set_web_source_label(name: str, body: LabelUpdate, user: str = Depends(require_admin)):
    """Modifie le libellé d'affichage d'une source web — son nom
    (registre + champ "source" des documents déjà indexés) ne change pas."""
    try:
        return web_sources_config.set_label(name, body.label)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/admin/web-sources/{name}/description")
def admin_set_web_source_description(name: str, body: DescriptionUpdate, user: str = Depends(require_admin)):
    try:
        return web_sources_config.set_description(name, body.description)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


class PauseUpdate(BaseModel):
    paused: bool


@app.post("/admin/web-sources/{name}/pause")
def admin_set_web_source_paused(name: str, body: PauseUpdate, user: str = Depends(require_admin)):
    """
    Suspend/reprend la synchronisation crawl_index -> es_index pour une
    source web (web-worker saute cette source à chaque tick tant que
    paused=true). Ne pilote PAS le conteneur Elastic Open Web Crawler
    lui-même (aucun accès Docker depuis cette API) : si ce conteneur
    tourne en continu (mode "schedule"), il continue d'écrire dans
    crawl_index — seule la répercussion vers DocSearch s'arrête. Les
    documents déjà indexés dans es_index restent cherchables.
    """
    try:
        return web_sources_config.set_paused(name, body.paused)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Vue d'ensemble unifiée (les 3 types de sources confondus) ─────
# Les panneaux /admin/file-sources, /admin/sql-sources, /admin/web-sources
# ci-dessus restent le CRUD dédié à chaque type (champs spécifiques :
# dossier pour un fichier, requête pour du SQL, crawl_index pour du
# web). Cette route sert un usage différent et transverse : une seule
# liste avec le compte de documents et la bascule "recherche activée",
# indépendamment du type — pour ça, la source doit être identifiable
# sans ambiguïté par (type, name), d'où le paramètre `type` explicite
# plutôt que de chercher le nom dans les trois registres.

class SearchableUpdate(BaseModel):
    searchable: bool


class CollectableUpdate(BaseModel):
    collectable: bool


_SOURCE_REGISTRIES = {
    "file": file_sources_config,
    "sql":  sql_sources_config,
    "web":  web_sources_config,
}


def _all_sources_status() -> dict:
    """Fusionne les trois registres de sources en une seule liste, avec
    le nombre de documents et la taille sur disque de chaque index — un
    index manquant (source enregistrée mais jamais indexée, ou vidée)
    compte pour 0 plutôt que de faire échouer tout l'appel."""
    result = {}
    for type_, registry in _SOURCE_REGISTRIES.items():
        for name, s in registry.get_sources().items():
            try:
                indexed = es.count(index=s.es_index)["count"]
            except Exception:
                indexed = 0
            try:
                # size_in_bytes de l'index PRIMAIRE (pas x nombre de
                # replicas) — c'est l'espace occupé par les données elles-
                # mêmes, l'unité pertinente ici plutôt que l'empreinte
                # disque totale du cluster (voir /metrics pour celle-ci,
                # calculée sur l'alias fédéré ES_SEARCH_ALIAS en entier).
                size_bytes = es.indices.stats(index=s.es_index)["_all"]["primaries"]["store"]["size_in_bytes"]
            except Exception:
                size_bytes = 0
            result[name] = {
                "type":       type_,
                "es_index":   s.es_index,
                "label":       getattr(s, "label", None) or name,
                "description": getattr(s, "description", None) or "",
                "searchable":  s.searchable,
                "collectable": s.collectable,
                "indexed":     indexed,
                "size_bytes":  size_bytes,
            }
    return result


@app.get("/admin/all-sources")
def admin_get_all_sources(user: str = Depends(require_admin)):
    return _all_sources_status()


@app.post("/admin/all-sources/{name}/searchable")
def admin_set_source_searchable(
    name: str, body: SearchableUpdate,
    type: str = Query(..., description="file, sql ou web"),
    user: str = Depends(require_admin),
):
    """Active/désactive la RECHERCHE pour une source, quel que soit son
    type — n'affecte jamais l'ingestion (watcher/sql-worker/web-worker
    continuent normalement), seulement la visibilité dans /search."""
    registry = _SOURCE_REGISTRIES.get(type)
    if registry is None:
        raise HTTPException(status_code=400, detail=f"Type de source invalide : '{type}' (attendu file, sql ou web)")
    try:
        registry.set_searchable(name, body.searchable)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _all_sources_status()


@app.post("/admin/all-sources/{name}/collectable")
def admin_set_source_collectable(
    name: str, body: CollectableUpdate,
    type: str = Query(..., description="file, sql ou web"),
    user: str = Depends(require_admin),
):
    """Active/désactive l'ajout à une collection pour les documents
    d'une source, quel que soit son type — n'affecte ni l'ingestion ni
    la recherche (voir add_collection_document() et
    set_collectable() dans chaque registre)."""
    registry = _SOURCE_REGISTRIES.get(type)
    if registry is None:
        raise HTTPException(status_code=400, detail=f"Type de source invalide : '{type}' (attendu file, sql ou web)")
    try:
        registry.set_collectable(name, body.collectable)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _all_sources_status()


@app.get("/admin/filetypes")
def admin_get_filetypes(source: str = Query(DEFAULT_SOURCE_NAME), user: str = Depends(require_admin)):
    return filetype_config.get_config(source)


@app.post("/admin/filetypes/reset")
def admin_reset_filetypes(source: str = Query(DEFAULT_SOURCE_NAME), user: str = Depends(require_admin)):
    # Route déclarée AVANT /admin/filetypes/{extension} — sinon FastAPI
    # matcherait "reset" comme une extension et cette route ne serait
    # jamais atteinte.
    return filetype_config.reset_to_default(source)


@app.post("/admin/filetypes/{extension}")
def admin_set_filetype(extension: str, body: FiletypeUpdate, user: str = Depends(require_admin)):
    return filetype_config.set_filetype(extension, enabled=body.enabled, max_size_mb=body.max_size_mb, source=body.source)


@app.delete("/admin/filetypes/{extension}")
def admin_remove_filetype(extension: str, source: str = Query(DEFAULT_SOURCE_NAME), user: str = Depends(require_admin)):
    try:
        return filetype_config.remove_filetype(extension, source)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/admin/config")
def admin_get_config(user: str = Depends(require_admin)):
    return runtime_config.get_runtime_config()


@app.post("/admin/config/reset")
def admin_reset_config(user: str = Depends(require_admin)):
    # Route déclarée AVANT /admin/config/{key} — sinon FastAPI matcherait
    # "reset" comme une clé de paramètre et cette route ne serait jamais
    # atteinte.
    return runtime_config.reset_to_default()


@app.post("/admin/config/{key}")
def admin_set_config(key: str, body: ConfigUpdate, user: str = Depends(require_admin)):
    try:
        return runtime_config.set_param(key, body.value)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/admin/path-filters")
def admin_get_path_filters(source: str = Query(DEFAULT_SOURCE_NAME), user: str = Depends(require_admin)):
    return path_filter.get_config(source)


@app.post("/admin/path-filters/exclude")
def admin_exclude_path(body: PathFilterPattern, user: str = Depends(require_admin)):
    return path_filter.add_excluded(body.pattern, body.source)


@app.post("/admin/path-filters/include")
def admin_include_path(body: PathFilterPattern, user: str = Depends(require_admin)):
    return path_filter.add_included(body.pattern, body.source)


@app.post("/admin/path-filters/remove")
def admin_remove_path_filter(body: PathFilterPattern, user: str = Depends(require_admin)):
    # POST plutôt que DELETE avec le motif dans l'URL : un motif comme
    # "finance/confidentiel" contient des "/" qui casseraient un
    # paramètre de chemin FastAPI.
    return path_filter.remove_filter(body.pattern, body.source)


@app.post("/admin/purge-path")
def admin_purge_path(body: PurgeRequest, user: str = Depends(require_admin)):
    """dry_run=True (défaut) : aperçu sans suppression. Toujours
    appeler en dry-run d'abord depuis l'interface avant confirmation.
    Opère sur l'index de `body.source` uniquement (défaut : source
    par défaut, rétrocompatible avec un client qui n'envoie pas ce champ)."""
    try:
        n = admin_scan.purge_path(body.pattern, source_name=body.source, dry_run=body.dry_run)
        return {"pattern": body.pattern, "source": body.source, "dry_run": body.dry_run, "matched": n}
    except KeyError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/admin/scan")
def admin_trigger_scan(
    body: ScanRequest,
    background_tasks: BackgroundTasks,
    user: str = Depends(require_admin),
):
    """
    Déclenche un scan (publication Kafka) en arrière-plan pour UNE
    source — ne bloque pas la requête HTTP le temps de parcourir tout
    son dossier. Suivre la progression via GET /admin/status
    (workers.pending_documents).
    """
    def _run():
        try:
            result = admin_scan.trigger_scan(body.source, body.subfolder)
            logger.info(f"[admin] Scan terminé par {user} : {result}")
        except Exception as e:
            logger.error(f"[admin] Scan déclenché par {user} a échoué : {e}")

    background_tasks.add_task(_run)
    return {"status": "démarré", "source": body.source, "subfolder": body.subfolder or "(dossier complet)"}


# ── Mesure de satisfaction (pouce, NPS, clics, suggestions) ─────
# Quatre signaux distincts, volontairement pas fusionnés :
#   - feedback (pouce haut/bas) : explicite, par recherche (search_id).
#   - NPS : explicite, sur l'outil en général, PAS rattaché à une
#     recherche précise — occasionnel (cadence gérée côté client).
#   - clics : implicite, toujours actif (aucun flag), par recherche.
#   - suggestions : explicite, texte libre, PAS rattaché à une recherche
#     précise — point d'entrée permanent dans l'en-tête (index.html).
# feedback/NPS/suggestions sont individuellement suspendables
# (engagement_config.py) sans redémarrage ; le tracking de clic n'a pas
# cette option (signal passif, aucune UI ni friction ajoutée).

class FeedbackCreate(BaseModel):
    search_id: str
    rating: str  # "up" | "down"


class ClickCreate(BaseModel):
    search_id: str
    doc_id: str
    position: int


class NpsCreate(BaseModel):
    score: int = Field(ge=0, le=10)


class SuggestionCreate(BaseModel):
    text: str
    category: str | None = None   # "bug" | "idea" | "other", libre (pas de contrainte serveur)
    anonymous: bool = True        # défaut anonyme — l'utilisateur doit explicitement décocher pour être identifié


class EngagementConfigUpdate(BaseModel):
    feedback_enabled:    bool | None = None
    nps_enabled:         bool | None = None
    suggestions_enabled: bool | None = None


# ── Bascules d'interface (distinct de la mesure de satisfaction) ──
class UiConfigUpdate(BaseModel):
    chat_enabled:        bool | None = None
    footer_enabled:      bool | None = None
    admin_links_enabled: bool | None = None
    export_enabled:      bool | None = None
    help_enabled:        bool | None = None
    collections_enabled: bool | None = None
    custom_keywords_enabled: bool | None = None


@app.get("/ui-config")
def get_ui_config():
    """Public (pas d'auth) — l'interface de recherche l'appelle pour
    savoir si le lien "Assistant IA" doit être affiché dans l'en-tête."""
    return ui_config.get_config()


@app.get("/is-admin")
def get_is_admin(x_user: str | None = Header(default=None)):
    """
    Public (jamais de 401/403 — voir is_admin(), version non levante de
    require_admin()) : l'interface de recherche l'appelle pour savoir si
    les liens "Administration"/"Statistiques" doivent être affichés —
    ces pages échoueraient de toute façon avec un 403 pour un
    utilisateur non admin, autant ne pas les proposer.
    """
    return {"is_admin": is_admin(x_user)}


@app.post("/admin/ui-config")
def admin_set_ui_config(body: UiConfigUpdate, user: str = Depends(require_admin)):
    """Active/désactive des éléments d'interface (ex: lien Assistant IA,
    pied de page) — effectif immédiatement pour toute nouvelle page
    chargée."""
    try:
        config = ui_config.get_config()
        if body.chat_enabled is not None:
            config = ui_config.set_param("chat_enabled", body.chat_enabled)
        if body.footer_enabled is not None:
            config = ui_config.set_param("footer_enabled", body.footer_enabled)
        if body.admin_links_enabled is not None:
            config = ui_config.set_param("admin_links_enabled", body.admin_links_enabled)
        if body.export_enabled is not None:
            config = ui_config.set_param("export_enabled", body.export_enabled)
        if body.help_enabled is not None:
            config = ui_config.set_param("help_enabled", body.help_enabled)
        if body.collections_enabled is not None:
            config = ui_config.set_param("collections_enabled", body.collections_enabled)
        if body.custom_keywords_enabled is not None:
            config = ui_config.set_param("custom_keywords_enabled", body.custom_keywords_enabled)
        return config
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/engagement-config")
def get_engagement_config():
    """
    Public (pas d'auth) — l'interface de recherche l'appelle pour savoir
    si le pouce et le NPS doivent être affichés. Ne PAS confondre avec
    /admin/engagement-config (même donnée, réservé à l'admin pour
    modification) : cette route-ci n'expose rien de sensible.
    """
    return engagement_config.get_config()


@app.post("/feedback")
def submit_feedback(body: FeedbackCreate, request: Request, x_user: str | None = Header(default=None)):
    """
    Enregistre un pouce haut/bas pour une recherche précise (search_id
    renvoyé par POST /search). Simple mise à jour partielle du document
    search_logs déjà existant — écrase un avis précédent sur la même
    recherche plutôt que d'en accumuler plusieurs (un seul avis a du sens
    par recherche).
    """
    if not engagement_config.get_config()["feedback_enabled"]:
        raise HTTPException(status_code=403, detail="Le recueil d'avis est désactivé.")
    if body.rating not in ("up", "down"):
        raise HTTPException(status_code=400, detail="rating doit être 'up' ou 'down'.")
    try:
        es.update(index=search_log.SEARCH_LOG_INDEX, id=body.search_id, doc={"feedback": body.rating})
    except Exception as e:
        if "not_found" in str(e).lower():
            raise HTTPException(status_code=404, detail="search_id introuvable.")
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "ok"}


@app.post("/click")
def submit_click(body: ClickCreate):
    """
    Enregistre le clic sur UN résultat d'une recherche précise (position
    dans la liste, 0-indexée) — signal toujours actif, pas de flag
    d'activation (voir docstring de section). Append via script Painless
    plutôt qu'une mise à jour de champ simple : "clicks" est une LISTE,
    un même search_id peut recevoir plusieurs clics (résultats consultés
    un par un avant de trouver le bon).
    """
    try:
        es.update(
            index=search_log.SEARCH_LOG_INDEX,
            id=body.search_id,
            script={
                "source": (
                    "if (ctx._source.clicks == null) { ctx._source.clicks = [] } "
                    "ctx._source.clicks.add(params.click)"
                ),
                "params": {
                    "click": {
                        "doc_id":    body.doc_id,
                        "position":  body.position,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                },
            },
        )
    except Exception as e:
        # Best-effort : un clic non enregistré (search_id déjà expiré,
        # ES momentanément indisponible...) ne doit jamais remonter comme
        # erreur visible — l'utilisateur est en train de consulter un
        # document, pas d'interagir avec le tracking.
        logger.warning(f"[click] Échec d'enregistrement pour search_id={body.search_id} : {e}")
    return {"status": "ok"}


@app.post("/nps")
def submit_nps(body: NpsCreate, x_user: str | None = Header(default=None)):
    """Enregistre une réponse NPS (0-10), indépendamment de toute
    recherche précise — voir nps_log.py."""
    if not engagement_config.get_config()["nps_enabled"]:
        raise HTTPException(status_code=403, detail="Le NPS est désactivé.")
    username = resolve_user(x_user)
    nps_log.log_nps(es, username=username, score=body.score)
    return {"status": "ok"}


@app.post("/suggestions")
def submit_suggestion(body: SuggestionCreate, x_user: str | None = Header(default=None)):
    """Enregistre une suggestion libre, indépendamment de toute recherche
    précise — voir suggestion_log.py. Anonyme par défaut ; l'identité
    n'est résolue via X-User que si l'utilisateur a explicitement décoché
    "rester anonyme" côté UI (body.anonymous == False)."""
    if not engagement_config.get_config()["suggestions_enabled"]:
        raise HTTPException(status_code=403, detail="Le recueil de suggestions est désactivé.")
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="La suggestion ne peut pas être vide.")
    username = None if body.anonymous else resolve_user(x_user)
    suggestion_log.log_suggestion(es, text=text, category=body.category, username=username)
    return {"status": "ok"}


@app.post("/admin/engagement-config")
def admin_set_engagement_config(body: EngagementConfigUpdate, user: str = Depends(require_admin)):
    """Active/désactive le pouce, le NPS et/ou les suggestions —
    effectif immédiatement pour toute nouvelle page chargée (l'UI relit
    /engagement-config à chaque chargement, pas de cache long côté
    client)."""
    try:
        config = engagement_config.get_config()
        if body.feedback_enabled is not None:
            config = engagement_config.set_param("feedback_enabled", body.feedback_enabled)
        if body.nps_enabled is not None:
            config = engagement_config.set_param("nps_enabled", body.nps_enabled)
        if body.suggestions_enabled is not None:
            config = engagement_config.set_param("suggestions_enabled", body.suggestions_enabled)
        return config
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/admin/nps-summary")
def admin_nps_summary(user: str = Depends(require_admin)):
    """Score NPS agrégé + répartition détracteurs/passifs/promoteurs,
    pour la page /stats.html."""
    return nps_log.summary(es)


@app.get("/admin/suggestions")
def admin_list_suggestions(
    user:  str = Depends(require_admin),
    size:  int = 50,
    from_: int = Query(0, alias="from"),
):
    """Liste paginée des suggestions, plus récentes d'abord — pour la
    page /stats.html."""
    return suggestion_log.list_suggestions(es, size=size, from_=from_)


class SuggestionStatusUpdate(BaseModel):
    status: str


@app.post("/admin/suggestions/{suggestion_id}/status")
def admin_set_suggestion_status(suggestion_id: str, body: SuggestionStatusUpdate, user: str = Depends(require_admin)):
    """Suivi de traitement d'une suggestion (nouveau/en_cours/traite) —
    purement interne à l'équipe, n'informe jamais l'auteur (voir
    suggestion_log.py : l'anonymat par défaut rend une notification
    impossible à garantir de toute façon)."""
    try:
        suggestion_log.set_status(es, suggestion_id=suggestion_id, status=body.status)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Suggestion introuvable : {e}")
    return {"status": "ok"}


# ── Statistiques de recherche ───────────────────────────────────
@app.get("/admin/search-logs/summary")
def admin_search_logs_summary(user: str = Depends(require_admin)):
    """Compteurs agrégés + répartition par jour (14 derniers jours) pour
    les cartes de résumé de la page /stats.html."""
    try:
        res = es.search(
            index=search_log.SEARCH_LOG_INDEX,
            size=0,
            aggs={
                "unique_users": {"cardinality": {"field": "username"}},
                "unique_ips":   {"cardinality": {"field": "ip"}},
                "by_day": {
                    "date_histogram": {"field": "timestamp", "calendar_interval": "day"},
                },
                "feedback_up":   {"filter": {"term": {"feedback": "up"}}},
                "feedback_down": {"filter": {"term": {"feedback": "down"}}},
            },
        )
    except Exception as e:
        if "index_not_found" in str(e).lower():
            return {"total_searches": 0, "unique_users": 0, "unique_ips": 0, "by_day": [],
                     "feedback_up": 0, "feedback_down": 0}
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "total_searches": res["hits"]["total"]["value"],
        "unique_users":    res["aggregations"]["unique_users"]["value"],
        "unique_ips":      res["aggregations"]["unique_ips"]["value"],
        "by_day": [
            {"date": b["key_as_string"][:10], "count": b["doc_count"]}
            for b in res["aggregations"]["by_day"]["buckets"][-14:]
        ],
        "feedback_up":   res["aggregations"]["feedback_up"]["doc_count"],
        "feedback_down": res["aggregations"]["feedback_down"]["doc_count"],
    }


@app.get("/admin/search-logs/zero-results")
def admin_zero_result_searches(user: str = Depends(require_admin), size: int = 50):
    """Requêtes ayant retourné 0 résultat, groupées et comptées (les plus
    fréquentes en premier) — à partir des logs déjà collectés par chaque
    recherche (voir search_log.py), aucun nouveau tracking nécessaire.
    Aide à repérer du contenu manquant ou des requêtes mal formulées."""
    try:
        res = es.search(
            index=search_log.SEARCH_LOG_INDEX,
            size=0,
            query={"term": {"total_results": 0}},
            aggs={
                "by_query": {
                    "terms": {"field": "query.keyword", "size": size, "order": {"_count": "desc"}},
                    "aggs": {
                        "last_seen": {"max": {"field": "timestamp", "format": "strict_date_optional_time"}},
                    },
                },
            },
        )
    except Exception as e:
        if "index_not_found" in str(e).lower():
            return {"total_zero_result_searches": 0, "results": []}
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "total_zero_result_searches": res["hits"]["total"]["value"],
        "results": [
            {
                "query":     b["key"],
                "count":     b["doc_count"],
                "last_seen": b["last_seen"]["value_as_string"],
            }
            for b in res["aggregations"]["by_query"]["buckets"]
        ],
    }


# ── Journal d'audit ──────────────────────────────────────────────
@app.get("/admin/audit-log")
def admin_get_audit_log(
    user:  str = Depends(require_admin),
    size:  int = 50,
    from_: int = Query(0, alias="from"),
):
    """Liste paginée des actions d'administration, plus récentes
    d'abord — alimentée par audit_log_middleware, voir audit_log.py."""
    return audit_log.list_actions(es, size=size, from_=from_)


def _search_logs_query(q: str | None, username: str | None) -> dict:
    """Filtre partagé entre /admin/search-logs (paginé) et
    /admin/search-logs/export (export complet) — mêmes critères."""
    must = []
    if q:
        must.append({"match": {"query": q}})
    if username:
        must.append({"term": {"username": username.lower()}})
    return {"bool": {"must": must}} if must else {"match_all": {}}


@app.get("/admin/search-logs")
def admin_search_logs(
    user:     str = Depends(require_admin),
    q:        str | None = None,
    username: str | None = None,
    size:     int = 50,
    from_:    int = Query(0, alias="from"),
):
    """Liste paginée des recherches effectuées, plus récentes d'abord —
    qui, quand, depuis quelle IP, quelle requête, combien de résultats."""
    query = _search_logs_query(q, username)

    try:
        res = es.search(
            index=search_log.SEARCH_LOG_INDEX,
            query=query,
            sort=[{"timestamp": {"order": "desc"}}],
            size=size,
            from_=from_,
        )
    except Exception as e:
        if "index_not_found" in str(e).lower():
            return {"total": 0, "results": []}
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "total":   res["hits"]["total"]["value"],
        "results": [{"id": h["_id"], **h["_source"]} for h in res["hits"]["hits"]],
    }


# Plafond de lignes exportées : au-delà, l'export reste utilisable (les
# N premières lignes, plus récentes d'abord) plutôt que de saturer la
# mémoire de l'API ou du navigateur sur un historique de plusieurs
# centaines de milliers de recherches.
SEARCH_LOGS_EXPORT_MAX_ROWS = 20_000


def _join(value) -> str:
    """Aplati une valeur potentiellement multi-valuée (extension, author,
    source...) en texte lisible dans une cellule de tableur."""
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


@app.get("/admin/search-logs/export")
def admin_export_search_logs(
    user:     str = Depends(require_admin),
    q:        str | None = None,
    username: str | None = None,
):
    """
    Export XLSX de l'historique des recherches — mêmes filtres que
    GET /admin/search-logs (q, username), mais TOUTES les lignes
    correspondantes (jusqu'à SEARCH_LOGS_EXPORT_MAX_ROWS) plutôt qu'une
    seule page, pour analyse hors-ligne dans un tableur.
    """
    from openpyxl import Workbook

    query = _search_logs_query(q, username)

    wb = Workbook()
    ws = wb.active
    ws.title = "Historique des recherches"
    ws.append([
        "Date / heure", "Utilisateur", "Requête", "Champ recherché", "Source(s)",
        "Extension(s)", "Auteur(s)", "Dossier", "Période début", "Période fin",
        "Résultats", "Documents retournés", "Avis", "Clics",
    ])

    try:
        hits = es_scan(
            es,
            index=search_log.SEARCH_LOG_INDEX,
            query={"query": query, "sort": [{"timestamp": {"order": "desc"}}]},
            preserve_order=True,
        )
        for i, hit in enumerate(hits):
            if i >= SEARCH_LOGS_EXPORT_MAX_ROWS:
                break
            s = hit["_source"]
            ws.append([
                s.get("timestamp", ""),
                s.get("username", ""),
                s.get("query", ""),
                s.get("search_in", ""),
                _join(s.get("source")),
                _join(s.get("extension")),
                _join(s.get("author")),
                _join(s.get("folder")),
                s.get("date_from", ""),
                s.get("date_to", ""),
                s.get("total_results", 0),
                _join(s.get("result_files")),
                s.get("feedback", ""),
                len(s.get("clicks") or []),
            ])
    except Exception as e:
        if "index_not_found" not in str(e).lower():
            raise HTTPException(status_code=500, detail=str(e))

    for col_idx, width in enumerate([19, 14, 30, 14, 14, 14, 16, 20, 14, 14, 10, 40, 8, 8], start=1):
        ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = width

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    filename = f"historique-recherches-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}.xlsx"
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Pages ──────────────────────────────────────────────────────
# L'interface web (index.html, chat.html) est servie directement par
# Nginx depuis le projet docsearch-ui — cette API est maintenant une
# API JSON pure, sans dépendance sur des templates HTML.
# Voir docsearch-ui et la configuration nginx.conf de docsearch-infra.
