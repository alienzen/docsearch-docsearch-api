# search_api.py — API de recherche avec filtrage ACL
# Mis à jour le 29/06/2026 — ES 9.4.2 · Tika 3.3.1.0 · ACL

import os
ES_HOST = os.getenv("ES_HOST", "http://localhost:9200")
ES_INDEX = os.getenv("ES_INDEX", "documents")
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
DOCS_FOLDER = os.getenv("DOCS_FOLDER", "/documents")
DEV_USER = os.getenv("DEV_USER", "")
LDAP_ENABLED = os.getenv("LDAP_ENABLED", "false").lower() == "true"

import subprocess
import tempfile
import logging
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, HTTPException, Header, Depends
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
    extension:       str | None = None
    has_attachments: bool | None = None
    date_from:       str | None = None   # filtre sur date_modified (voir build de la requête)
    date_to:         str | None = None   # idem
    author:          str | None = None
    folder:          str | None = None

    model_config = {"populate_by_name": True}


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


# ── Recherche ────────────────────────────────────────────────
@app.post("/search")
def search(
    req: SearchQuery,
    x_user: str | None = Header(default=None),
):
    username   = resolve_user(x_user)
    acl_filter = build_acl_filter(username)

    must = [{
        "multi_match": {
            "query":     req.query,
            "fields":    ["content", "title^2", "filename^3", "author"],
            "fuzziness": "AUTO",
        }
    }]

    filters = [acl_filter]   # ACL en premier — mis en cache par ES

    if req.extension:
        filters.append({"term": {"extension": f".{req.extension}"}})
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
        else [{req.sort: "desc"}, {"_score": "desc"}]
    )

    res = es.search(
        index=ES_INDEX,
        query={"bool": {"must": must, "filter": filters}},
        highlight={"fields": {
            "content": {"fragment_size": 200, "number_of_fragments": 2}
        }},
        sort=sort_clause,
        from_=req.from_,
        size=req.size,
        source=["filename", "filepath", "extension", "title", "author",
                "size", "date_created", "date_modified", "indexed_at", "has_attachments", "folder",
                "acl.owner", "acl.groups", "acl.public"],
        aggs={
            "by_extension": {"terms": {"field": "extension",  "size": 10}},
            "by_author":    {"terms": {"field": "author",     "size": 10}},
            "by_folder":    {"terms": {"field": "folder_top",  "size": 10}},
        }
    )

    hits = res["hits"]["hits"]
    return {
        "total":    res["hits"]["total"]["value"],
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
        }
    }


# ── Détail document ──────────────────────────────────────────
@app.get("/document/{doc_id}")
def get_document(
    doc_id: str,
    x_user: str | None = Header(default=None),
):
    username = resolve_user(x_user)

    try:
        res = es.get(index=ES_INDEX, id=doc_id)
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
    info    = es.info()
    count   = es.count(index=ES_INDEX)["count"]
    stats   = es.indices.stats(index=ES_INDEX)
    size_gb = stats["_all"]["total"]["store"]["size_in_bytes"] / 1e9
    by_ext  = es.search(
        index=ES_INDEX, size=0,
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


class PurgeRequest(BaseModel):
    pattern: str
    dry_run: bool = True


class ScanRequest(BaseModel):
    subfolder: str | None = None


@app.get("/admin/status")
def admin_status(user: str = Depends(require_admin)):
    """État de tous les composants : ES, Redis, Tika, Kafka, workers
    actifs, progression de l'indexation (lag), battement du watcher."""
    return cluster_status.get_full_status()


@app.get("/admin/filetypes")
def admin_get_filetypes(user: str = Depends(require_admin)):
    return filetype_config.get_config()


@app.post("/admin/filetypes/{extension}")
def admin_set_filetype(extension: str, body: FiletypeUpdate, user: str = Depends(require_admin)):
    return filetype_config.set_filetype(extension, enabled=body.enabled, max_size_mb=body.max_size_mb)


@app.get("/admin/config")
def admin_get_config(user: str = Depends(require_admin)):
    return runtime_config.get_runtime_config()


@app.post("/admin/config/{key}")
def admin_set_config(key: str, body: ConfigUpdate, user: str = Depends(require_admin)):
    try:
        return runtime_config.set_param(key, body.value)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/admin/path-filters")
def admin_get_path_filters(user: str = Depends(require_admin)):
    return path_filter.get_config()


@app.post("/admin/path-filters/exclude")
def admin_exclude_path(body: PathFilterPattern, user: str = Depends(require_admin)):
    return path_filter.add_excluded(body.pattern)


@app.post("/admin/path-filters/include")
def admin_include_path(body: PathFilterPattern, user: str = Depends(require_admin)):
    return path_filter.add_included(body.pattern)


@app.post("/admin/path-filters/remove")
def admin_remove_path_filter(body: PathFilterPattern, user: str = Depends(require_admin)):
    # POST plutôt que DELETE avec le motif dans l'URL : un motif comme
    # "finance/confidentiel" contient des "/" qui casseraient un
    # paramètre de chemin FastAPI.
    return path_filter.remove_filter(body.pattern)


@app.post("/admin/purge-path")
def admin_purge_path(body: PurgeRequest, user: str = Depends(require_admin)):
    """dry_run=True (défaut) : aperçu sans suppression. Toujours
    appeler en dry-run d'abord depuis l'interface avant confirmation."""
    try:
        n = admin_scan.purge_path(body.pattern, dry_run=body.dry_run)
        return {"pattern": body.pattern, "dry_run": body.dry_run, "matched": n}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/admin/scan")
def admin_trigger_scan(
    body: ScanRequest,
    background_tasks: BackgroundTasks,
    user: str = Depends(require_admin),
):
    """
    Déclenche un scan (publication Kafka) en arrière-plan — ne bloque
    pas la requête HTTP le temps de parcourir tout DOCS_FOLDER. Suivre
    la progression via GET /admin/status (workers.pending_documents).
    """
    def _run():
        try:
            result = admin_scan.trigger_scan(body.subfolder)
            logger.info(f"[admin] Scan terminé par {user} : {result}")
        except Exception as e:
            logger.error(f"[admin] Scan déclenché par {user} a échoué : {e}")

    background_tasks.add_task(_run)
    return {"status": "démarré", "subfolder": body.subfolder or "(dossier complet)"}


# ── Pages ──────────────────────────────────────────────────────
# L'interface web (index.html, chat.html) est servie directement par
# Nginx depuis le projet docsearch-ui — cette API est maintenant une
# API JSON pure, sans dépendance sur des templates HTML.
# Voir docsearch-ui et la configuration nginx.conf de docsearch-infra.
