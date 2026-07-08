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
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, HTTPException, Header, Depends, Request, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from elasticsearch import Elasticsearch
from ldap_resolver import get_user_groups
from admin_auth import require_admin
import cluster_status
import admin_scan
import filetype_config
import runtime_config
import path_filter
import search_log
import saved_searches
import sources_config
from sources_config import ES_SEARCH_ALIAS, DEFAULT_SOURCE_NAME

logger = logging.getLogger(__name__)

app = FastAPI(title="DocSearch API", version="2.1.0")

ES_HOST = ES_HOST
es = Elasticsearch(ES_HOST, retry_on_timeout=True, max_retries=3, request_timeout=60)

# Utilisateur anonyme de secours (dev uniquement — désactiver en prod)
DEV_USER = DEV_USER


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
    author:          str | None = None
    folder:          str | None = None
    source:          str | None = None   # nom de source (sources_config.py) — absent = recherche fédérée sur toutes
    search_in:       str = "all"   # "all" | "title" | "author" — restreint le champ interrogé

    model_config = {"populate_by_name": True}


class SavedSearchCreate(BaseModel):
    # Reflète directement l'état de l'UI (voir `state` dans index.html),
    # pas les valeurs résolues envoyées à /search (ex: "ext" est la clé
    # du chip sélectionné — "word" — pas la liste d'extensions qu'il
    # recouvre — [docx, doc]) : ça permet de restaurer l'interface
    # (chip actif, champs) directement depuis l'enregistrement, sans
    # avoir à inverser une résolution.
    name:      str
    query:     str
    search_in: str = "all"
    ext:       str = "all"
    author:    str | None = None
    folder:    str | None = None
    source:    str | None = None
    date_from: str | None = None
    date_to:   str | None = None
    sort:      str = "_score"


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


def _resolve_search_index(source_name: str | None) -> str:
    """
    Détermine l'index (ou l'alias fédéré) à interroger pour /search.
    `source_name` absent -> alias fédéré (recherche sur toutes les
    sources) ; sinon l'index précis de la source demandée.
    """
    if not source_name:
        return ES_SEARCH_ALIAS
    try:
        return sources_config.get_source(source_name).es_index
    except KeyError as e:
        raise HTTPException(status_code=400, detail=str(e))


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


# ── Recherche ────────────────────────────────────────────────
@app.post("/search")
def search(
    req: SearchQuery,
    request: Request,
    x_user: str | None = Header(default=None),
):
    _ensure_index_exists()
    search_index = _resolve_search_index(req.source)
    username   = resolve_user(x_user)
    acl_filter = build_acl_filter(username)

    # search_in restreint la recherche à un seul champ plutôt que tous
    # ("Tout" par défaut). "author" utilise le sous-champ analysé
    # author.text (pas le "author" brut, en keyword — non tokenisé,
    # une recherche en texte libre dessus ne matcherait jamais un nom
    # partiel comme "Dupont" contre "Martin Dupont").
    FIELD_SETS = {
        "all":      ["content", "title^4", "filename^6", "author.text"],
        "title":    ["title"],
        "author":   ["author.text"],
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

    if is_exact_phrase:
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

    filters = [acl_filter]   # ACL en premier — mis en cache par ES

    if req.extension:
        # Accepte une extension unique ("pdf") ou une famille de formats
        # (["docx", "doc"]) — utile pour un filtre "Word" qui doit couvrir
        # à la fois le format moderne et l'ancien format binaire 97-2003.
        exts = req.extension if isinstance(req.extension, list) else [req.extension]
        filters.append({"terms": {"extension": [f".{e}" for e in exts]}})
    if req.has_attachments:
        filters.append({"term": {"has_attachments": True}})
    if req.date_from or req.date_to:
        r = {}
        if req.date_from: r["gte"] = req.date_from
        if req.date_to:   r["lte"] = req.date_to
        filters.append({"range": {"date_modified": r}})
    if req.author:
        filters.append({"term": {"author": req.author}})
    if req.folder:
        # Correspond au dossier exact OU à tout sous-dossier en dessous
        # (ex: folder="Finance" matche "Finance" et "Finance/Rapports")
        filters.append({
            "bool": {
                "should": [
                    {"term":   {"folder": req.folder}},
                    {"prefix": {"folder": req.folder.rstrip("/") + "/"}},
                ],
                "minimum_should_match": 1,
            }
        })

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
            index=search_index,
            query={"bool": {"must": must, "filter": filters}},
            highlight={
                "fields": {
                    "content": {"fragment_size": 200, "number_of_fragments": 2}
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
            source=["filename", "filepath", "extension", "title", "author",
                    "size", "date_created", "date_modified", "indexed_at", "has_attachments", "folder",
                    "source", "acl.owner", "acl.groups", "acl.public"],
            aggs={
                "by_extension": {"terms": {"field": "extension",  "size": 10}},
                "by_author":    {"terms": {"field": "author",     "size": 10}},
                "by_folder":    {"terms": {"field": "folder_top",  "size": 10}},
                "by_source":    {"terms": {"field": "source",     "size": 20}},
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

    search_log.log_search(
        es,
        username=username,
        ip=get_client_ip(request),
        query=req.query,
        search_in=req.search_in,
        total_results=total,
        result_files=[h["_source"].get("filename", "") for h in hits],
    )

    return {
        "total":    total,
        "username": username,
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
            "extensions": res["aggregations"]["by_extension"]["buckets"],
            "authors":    res["aggregations"]["by_author"]["buckets"],
            "folders":    res["aggregations"]["by_folder"]["buckets"],
            "sources":    res["aggregations"]["by_source"]["buckets"],
        }
    }


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

    # Vérification ACL avant de retourner les détails
    acl         = doc.get("acl", {})
    user_groups = get_user_groups(username)

    allowed = (
        acl.get("public", False)
        or acl.get("owner")  == username
        or username in acl.get("users",  [])
        or any(g in acl.get("groups", []) for g in user_groups)
    )
    if not allowed:
        raise HTTPException(status_code=403, detail="Accès refusé")

    return doc


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


def _sources_status() -> dict:
    """Nombre de documents par source enregistrée — un index manquant
    (source enregistrée mais jamais indexée) compte pour 0 plutôt que de
    faire échouer tout /admin/status."""
    result = {}
    for name, source in sources_config.get_sources().items():
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


@app.get("/admin/sources")
def admin_get_sources(user: str = Depends(require_admin)):
    return {
        name: {"es_index": s.es_index, "folder": s.folder, "label": s.label}
        for name, s in sources_config.get_sources().items()
    }


@app.post("/admin/sources")
def admin_add_source(body: SourceCreate, user: str = Depends(require_admin)):
    try:
        return sources_config.add_source(
            body.name, body.es_index, subfolder=body.subfolder, label=body.label
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/admin/sources/{name}")
def admin_remove_source(name: str, user: str = Depends(require_admin)):
    """Retire la source du registre (le watcher arrête de l'observer) —
    NE supprime PAS l'index Elasticsearch ni les documents déjà
    indexés : utiliser /admin/purge-path pour nettoyer l'existant."""
    try:
        return sources_config.remove_source(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/admin/filetypes")
def admin_get_filetypes(user: str = Depends(require_admin)):
    return filetype_config.get_config()


@app.post("/admin/filetypes/reset")
def admin_reset_filetypes(user: str = Depends(require_admin)):
    # Route déclarée AVANT /admin/filetypes/{extension} — sinon FastAPI
    # matcherait "reset" comme une extension et cette route ne serait
    # jamais atteinte.
    return filetype_config.reset_to_default()


@app.post("/admin/filetypes/{extension}")
def admin_set_filetype(extension: str, body: FiletypeUpdate, user: str = Depends(require_admin)):
    return filetype_config.set_filetype(extension, enabled=body.enabled, max_size_mb=body.max_size_mb)


@app.delete("/admin/filetypes/{extension}")
def admin_remove_filetype(extension: str, user: str = Depends(require_admin)):
    try:
        return filetype_config.remove_filetype(extension)
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
            },
        )
    except Exception as e:
        if "index_not_found" in str(e).lower():
            return {"total_searches": 0, "unique_users": 0, "unique_ips": 0, "by_day": []}
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "total_searches": res["hits"]["total"]["value"],
        "unique_users":    res["aggregations"]["unique_users"]["value"],
        "unique_ips":      res["aggregations"]["unique_ips"]["value"],
        "by_day": [
            {"date": b["key_as_string"][:10], "count": b["doc_count"]}
            for b in res["aggregations"]["by_day"]["buckets"][-14:]
        ],
    }


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
    must = []
    if q:
        must.append({"match": {"query": q}})
    if username:
        must.append({"term": {"username": username.lower()}})
    query = {"bool": {"must": must}} if must else {"match_all": {}}

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


# ── Pages ──────────────────────────────────────────────────────
# L'interface web (index.html, chat.html) est servie directement par
# Nginx depuis le projet docsearch-ui — cette API est maintenant une
# API JSON pure, sans dépendance sur des templates HTML.
# Voir docsearch-ui et la configuration nginx.conf de docsearch-infra.
