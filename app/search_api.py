# search_api.py — API de recherche avec filtrage ACL
# Mis à jour le 29/06/2026 — ES 9.4.2 · Tika 3.3.1.0 · ACL

import os
ES_HOST = os.getenv("ES_HOST", "http://localhost:9200")
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
DOCS_FOLDER = os.getenv("DOCS_FOLDER", "/documents")
DEV_USER = os.getenv("DEV_USER", "")
LDAP_ENABLED = os.getenv("LDAP_ENABLED", "false").lower() == "true"

import subprocess
import tempfile
import logging
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from elasticsearch import Elasticsearch
from ldap_resolver import get_user_groups

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
    date_from:       str | None = None
    date_to:         str | None = None

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
        filters.append({"range": {"date": r}})

    sort_clause = (
        [{"_score": "desc"}]
        if req.sort == "_score"
        else [{req.sort: "desc"}, {"_score": "desc"}]
    )

    res = es.search(
        index="documents",
        query={"bool": {"must": must, "filter": filters}},
        highlight={"fields": {
            "content": {"fragment_size": 200, "number_of_fragments": 2}
        }},
        sort=sort_clause,
        from_=req.from_,
        size=req.size,
        source=["filename", "filepath", "extension", "title", "author",
                "size", "date", "indexed_at", "has_attachments",
                "acl.owner", "acl.groups", "acl.public"],
        aggs={
            "by_extension": {"terms": {"field": "extension", "size": 10}},
            "by_author":    {"terms": {"field": "author",    "size": 10}},
        }
    )

    hits = res["hits"]["hits"]
    return {
        "total":    res["hits"]["total"]["value"],
        "username": username,
        "results": [
            {
                **h["_source"],
                "score":     round(h["_score"], 4),
                "highlight": h.get("highlight", {}).get("content", []),
            }
            for h in hits
        ],
        "facets": {
            "extensions": res["aggregations"]["by_extension"]["buckets"],
            "authors":    res["aggregations"]["by_author"]["buckets"],
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
        res = es.get(index="documents", id=doc_id)
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
    count   = es.count(index="documents")["count"]
    stats   = es.indices.stats(index="documents")
    size_gb = stats["_all"]["total"]["store"]["size_in_bytes"] / 1e9
    by_ext  = es.search(
        index="documents", size=0,
        aggs={"by_ext": {"terms": {"field": "extension", "size": 10}}}
    )
    return {
        "indexed":      count,
        "total":        3_000_000,
        "percent":      round(count / 3_000_000 * 100, 2),
        "size_gb":      round(size_gb, 2),
        "by_extension": by_ext["aggregations"]["by_ext"]["buckets"],
        "es_version":   "9.4.2",
        "tika_version": "3.3.1.0",
        "acl_enabled":  True,
    }


# ── Pages ──────────────────────────────────────────────────────
# L'interface web (index.html, chat.html) est servie directement par
# Nginx depuis le projet docsearch-ui — cette API est maintenant une
# API JSON pure, sans dépendance sur des templates HTML.
# Voir docsearch-ui et la configuration nginx.conf de docsearch-infra.
