# saved_collections.py — Collections de documents enregistrées par utilisateur
#
# Permet à un utilisateur de regrouper des documents dans une ou plusieurs
# collections nommées (ex: "Dossier Client X", "À lire"), retrouvables plus
# tard sans repasser par une recherche. Purement un confort utilisateur —
# comme saved_searches.py, sans rapport avec l'indexation, donc pas de
# copie synchronisée côté docsearch-ingestion. Strictement personnel : une
# collection n'est jamais visible par un autre utilisateur que son créateur.
#
# Stockage : index ES dédié (SAVED_COLLECTIONS_INDEX), un document par
# collection, indexé sous l'id de la collection elle-même (uuid) — id,
# name, doc_ids, created_at, plus un champ username qui ne sort jamais des
# fonctions de ce module (jamais renvoyé à l'appelant, comme dans la
# version Redis où le username était implicite dans la clé). Une
# collection ne stocke QUE des identifiants de document — le contenu réel
# (titre, ACL...) est relu à l'affichage via GET /document/{id}, qui
# applique déjà la vérification ACL : ça évite de dupliquer cette logique
# ici, et garantit qu'un document devenu inaccessible entre-temps (ACL
# changée, document supprimé) n'est jamais exposé via une collection.
#
# Écritures avec refresh="wait_for" : contrairement à search_log.py/
# suggestion_log.py/audit_log.py (logs à fort volume, cohérence
# différée acceptable), une collection est la donnée elle-même, pas un
# journal — l'utilisateur doit revoir immédiatement l'effet de son
# action (créer une collection puis la retrouver via GET /collections ne
# doit pas dépendre du refresh_interval ES). Volume faible (actions
# manuelles d'un utilisateur), le coût du refresh forcé est négligeable ici.

import os
import uuid
import logging
from datetime import datetime, timezone

from elasticsearch import Elasticsearch, NotFoundError

logger = logging.getLogger(__name__)

SAVED_COLLECTIONS_INDEX = os.getenv("SAVED_COLLECTIONS_INDEX", "saved_collections")

_index_ready = False
_es_unavailable_logged = False


def _ensure_index(es: Elasticsearch) -> None:
    global _index_ready
    if _index_ready:
        return
    if not es.indices.exists(index=SAVED_COLLECTIONS_INDEX):
        es.indices.create(index=SAVED_COLLECTIONS_INDEX, body={
            "mappings": {
                "properties": {
                    "username":   {"type": "keyword"},
                    "name":       {"type": "keyword"},
                    "created_at": {"type": "date"},
                    "doc_ids":    {"type": "keyword"},
                }
            }
        })
        logger.info(f"Index '{SAVED_COLLECTIONS_INDEX}' créé.")
    _index_ready = True


def _unavailable() -> RuntimeError:
    return RuntimeError(
        "Elasticsearch injoignable — impossible d'enregistrer/consulter les "
        "collections de documents. Vérifiez que le service elasticsearch tourne."
    )


def _to_entry(hit_id: str, source: dict) -> dict:
    return {
        "id":         hit_id,
        "name":       source["name"],
        "created_at": source["created_at"],
        "doc_ids":    source.get("doc_ids", []),
    }


def _get_owned(es: Elasticsearch, username: str, collection_id: str) -> dict:
    """Récupère le document ES d'une collection et vérifie que `username`
    en est bien le propriétaire. Lève KeyError si la collection n'existe
    pas OU appartient à quelqu'un d'autre — les deux cas sont
    indiscernables pour l'appelant, comme avec la clé Redis
    par-utilisateur d'origine (un id d'une autre collection n'y existait
    tout simplement pas)."""
    try:
        res = es.get(index=SAVED_COLLECTIONS_INDEX, id=collection_id)
    except NotFoundError:
        raise KeyError(f"Collection inconnue : '{collection_id}'")
    except Exception:
        raise _unavailable()
    if res["_source"].get("username") != username:
        raise KeyError(f"Collection inconnue : '{collection_id}'")
    return res["_source"]


def list_collections(es: Elasticsearch, username: str) -> list[dict]:
    """Retourne les collections d'un utilisateur, la plus récente en
    premier. Liste vide (pas d'exception) si Elasticsearch est injoignable
    ou si l'index n'existe pas encore — un utilisateur sans collection
    n'est pas une erreur, juste un cas de repli identique à celui d'ES en
    panne."""
    global _es_unavailable_logged
    try:
        res = es.search(
            index=SAVED_COLLECTIONS_INDEX,
            query={"term": {"username": username}},
            sort=[{"created_at": {"order": "desc"}}],
            size=1000,
        )
    except NotFoundError:
        return []  # index pas encore créé — aucune collection créée pour l'instant, pas une erreur
    except Exception as e:
        if not _es_unavailable_logged:
            logger.warning(f"[saved_collections] Elasticsearch injoignable ({e})")
            _es_unavailable_logged = True
        return []

    return [_to_entry(h["_id"], h["_source"]) for h in res["hits"]["hits"]]


def create_collection(es: Elasticsearch, username: str, name: str) -> dict:
    """Crée une nouvelle collection vide et la persiste immédiatement dans
    Elasticsearch. Lève une exception si ES est injoignable (une création
    doit être fiable, pas de sens à "faire semblant")."""
    name = name.strip()
    if not name:
        raise ValueError("Le nom de la collection ne peut pas être vide.")

    collection_id = uuid.uuid4().hex
    entry = {
        "id":         collection_id,
        "name":       name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "doc_ids":    [],
    }

    try:
        _ensure_index(es)
        es.index(
            index=SAVED_COLLECTIONS_INDEX, id=collection_id, refresh="wait_for",
            document={**entry, "username": username},
        )
    except Exception:
        raise _unavailable()

    return entry


def rename_collection(es: Elasticsearch, username: str, collection_id: str, name: str) -> list[dict]:
    name = name.strip()
    if not name:
        raise ValueError("Le nom de la collection ne peut pas être vide.")

    _get_owned(es, username, collection_id)
    try:
        es.update(index=SAVED_COLLECTIONS_INDEX, id=collection_id, refresh="wait_for", doc={"name": name})
    except Exception:
        raise _unavailable()

    return list_collections(es, username)


def delete_collection(es: Elasticsearch, username: str, collection_id: str) -> list[dict]:
    """Retire une collection. Idempotent : un id déjà absent (ou
    appartenant à quelqu'un d'autre) ne lève pas d'erreur, la liste des
    collections est simplement inchangée."""
    try:
        doc = es.get(index=SAVED_COLLECTIONS_INDEX, id=collection_id)["_source"]
    except NotFoundError:
        return list_collections(es, username)
    except Exception:
        raise _unavailable()

    if doc.get("username") == username:
        try:
            es.delete(index=SAVED_COLLECTIONS_INDEX, id=collection_id, refresh="wait_for")
        except NotFoundError:
            pass  # déjà supprimée entre-temps (course rare) — idempotent
        except Exception:
            raise _unavailable()

    return list_collections(es, username)


def add_document(es: Elasticsearch, username: str, collection_id: str, doc_id: str) -> list[dict]:
    """Ajoute un document à une collection — idempotent, pas de doublon si
    le document y figure déjà."""
    entry = _get_owned(es, username, collection_id)
    doc_ids = entry.get("doc_ids", [])
    if doc_id not in doc_ids:
        doc_ids = doc_ids + [doc_id]
        try:
            es.update(index=SAVED_COLLECTIONS_INDEX, id=collection_id, refresh="wait_for", doc={"doc_ids": doc_ids})
        except Exception:
            raise _unavailable()

    return list_collections(es, username)


def remove_document(es: Elasticsearch, username: str, collection_id: str, doc_id: str) -> list[dict]:
    """Retire un document d'une collection. Idempotent : un doc_id déjà
    absent de la collection ne lève pas d'erreur."""
    entry = _get_owned(es, username, collection_id)
    doc_ids = [d for d in entry.get("doc_ids", []) if d != doc_id]
    try:
        es.update(index=SAVED_COLLECTIONS_INDEX, id=collection_id, refresh="wait_for", doc={"doc_ids": doc_ids})
    except Exception:
        raise _unavailable()

    return list_collections(es, username)
