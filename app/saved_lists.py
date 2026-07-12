# saved_lists.py — Listes de documents enregistrées par utilisateur
#
# Permet à un utilisateur de regrouper des documents dans une ou plusieurs
# listes nommées (ex: "Dossier Client X", "À lire"), retrouvables plus
# tard sans repasser par une recherche. Purement un confort utilisateur —
# comme saved_searches.py, sans rapport avec l'indexation, donc pas de
# copie synchronisée côté docsearch-ingestion. Strictement personnel : une
# liste n'est jamais visible par un autre utilisateur que son créateur.
#
# Stockage : une clé Redis par utilisateur ("docsearch:saved_lists:{user}")
# contenant la liste JSON de ses listes, chacune avec ses doc_ids. Une
# liste ne stocke QUE des identifiants de document — le contenu réel
# (titre, ACL...) est relu à l'affichage via GET /document/{id}, qui
# applique déjà la vérification ACL : ça évite de dupliquer cette logique
# ici, et garantit qu'un document devenu inaccessible entre-temps (ACL
# changée, document supprimé) n'est jamais exposé via une liste.

import os
import json
import uuid
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
KEY_PREFIX = "docsearch:saved_lists:"

_redis_client = None
_redis_unavailable_logged = False


def _get_redis_client():
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    try:
        import redis
        _redis_client = redis.Redis(
            host=REDIS_HOST, port=REDIS_PORT,
            decode_responses=True, socket_connect_timeout=2, socket_timeout=2,
        )
        _redis_client.ping()
        return _redis_client
    except Exception as e:
        global _redis_unavailable_logged
        if not _redis_unavailable_logged:
            logger.warning(f"[saved_lists] Redis injoignable ({e})")
            _redis_unavailable_logged = True
        _redis_client = None
        return None


def _require_client():
    client = _get_redis_client()
    if client is None:
        raise RuntimeError(
            "Redis injoignable — impossible d'enregistrer/consulter les "
            "listes de documents. Vérifiez que le service redis tourne "
            "(docker compose ps redis)."
        )
    return client


def list_lists(username: str) -> list[dict]:
    """Retourne les listes d'un utilisateur, la plus récente en premier.
    Liste vide (pas d'exception) si Redis est injoignable — un
    utilisateur sans liste n'est pas une erreur, juste un cas de repli
    identique à celui de Redis en panne."""
    client = _get_redis_client()
    if client is None:
        return []
    raw = client.get(KEY_PREFIX + username)
    if not raw:
        return []
    try:
        return sorted(json.loads(raw), key=lambda l: l["created_at"], reverse=True)
    except (json.JSONDecodeError, KeyError):
        logger.warning(f"[saved_lists] Contenu invalide pour '{username}' — repli sur liste vide")
        return []


def _read_write(username: str, mutate) -> list[dict]:
    """mutate reçoit la liste des listes et la modifie en place ; toute
    exception levée par mutate (nom vide, id inconnu...) interrompt
    l'écriture AVANT le client.set — aucune persistance partielle."""
    client = _require_client()
    raw = client.get(KEY_PREFIX + username)
    lists = json.loads(raw) if raw else []
    mutate(lists)
    client.set(KEY_PREFIX + username, json.dumps(lists))
    return lists


def _find(lists: list[dict], list_id: str) -> dict:
    for l in lists:
        if l.get("id") == list_id:
            return l
    raise KeyError(f"Liste inconnue : '{list_id}'")


def create_list(username: str, name: str) -> dict:
    """Crée une nouvelle liste vide et la persiste immédiatement dans
    Redis. Lève une exception si Redis est injoignable (une création doit
    être fiable, pas de sens à "faire semblant")."""
    name = name.strip()
    if not name:
        raise ValueError("Le nom de la liste ne peut pas être vide.")

    entry = {
        "id": uuid.uuid4().hex,
        "name": name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "doc_ids": [],
    }

    def mutate(lists):
        lists.append(entry)

    _read_write(username, mutate)
    return entry


def rename_list(username: str, list_id: str, name: str) -> list[dict]:
    name = name.strip()
    if not name:
        raise ValueError("Le nom de la liste ne peut pas être vide.")

    def mutate(lists):
        _find(lists, list_id)["name"] = name

    return _read_write(username, mutate)


def delete_list(username: str, list_id: str) -> list[dict]:
    """Retire une liste. Idempotent : un id déjà absent ne lève pas
    d'erreur, la liste des listes est simplement inchangée."""
    def mutate(lists):
        lists[:] = [l for l in lists if l.get("id") != list_id]

    return _read_write(username, mutate)


def add_document(username: str, list_id: str, doc_id: str) -> list[dict]:
    """Ajoute un document à une liste — idempotent, pas de doublon si le
    document y figure déjà."""
    def mutate(lists):
        entry = _find(lists, list_id)
        if doc_id not in entry["doc_ids"]:
            entry["doc_ids"].append(doc_id)

    return _read_write(username, mutate)


def remove_document(username: str, list_id: str, doc_id: str) -> list[dict]:
    """Retire un document d'une liste. Idempotent : un doc_id déjà absent
    de la liste ne lève pas d'erreur."""
    def mutate(lists):
        entry = _find(lists, list_id)
        entry["doc_ids"] = [d for d in entry["doc_ids"] if d != doc_id]

    return _read_write(username, mutate)
