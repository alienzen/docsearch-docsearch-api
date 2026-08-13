# saved_collections.py — Collections de documents enregistrées par utilisateur
#
# Permet à un utilisateur de regrouper des documents dans une ou plusieurs
# collections nommées (ex: "Dossier Client X", "À lire"), retrouvables plus
# tard sans repasser par une recherche. Purement un confort utilisateur —
# comme saved_searches.py, sans rapport avec l'indexation, donc pas de
# copie synchronisée côté docsearch-ingestion.
#
# Personnel par défaut, PARTAGEABLE avec un ou plusieurs groupes
# (`shared_with`) — voir set_sharing(). Deux règles gouvernent ce partage :
#
# 1. **Partager donne la RÉFÉRENCE, pas le droit de lecture.** Une
#    collection ne stocke que des identifiants ; chaque document est relu
#    à l'affichage via GET /document/{id}, qui applique l'ACL. Deux
#    personnes ouvrant la même collection n'y voient donc pas forcément
#    le même nombre de documents — c'est correct, et l'interface le dit
#    plutôt que de masquer l'écart.
# 2. **Seul le propriétaire écrit.** Renommer, ajouter, retirer,
#    supprimer et repartager restent réservés à lui (_get_owned) : pas de
#    verrouillage à écrire, et un destinataire qui veut sa propre version
#    la duplique.
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

# Ajouté après coup : les collections créées avant le partage n'ont pas
# ce champ, et sont donc personnelles — ce qui est le bon défaut.
_PARTAGE_PROPERTIES = {"shared_with": {"type": "keyword"}}


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
                    **_PARTAGE_PROPERTIES,
                }
            }
        })
        logger.info(f"Index '{SAVED_COLLECTIONS_INDEX}' créé.")
    else:
        # Index d'une installation antérieure au partage : put_mapping
        # ajoute le champ sans toucher aux collections existantes, qui
        # restent simplement non partagées.
        es.indices.put_mapping(index=SAVED_COLLECTIONS_INDEX, properties=_PARTAGE_PROPERTIES)
    _index_ready = True


def _unavailable() -> RuntimeError:
    return RuntimeError(
        "Elasticsearch injoignable — impossible d'enregistrer/consulter les "
        "collections de documents. Vérifiez que le service elasticsearch tourne."
    )


def _to_entry(hit_id: str, source: dict, username: str | None = None) -> dict:
    """`owner` sort désormais du module, contrairement au parti pris
    d'origine : un destinataire doit savoir de qui vient la collection
    qu'il voit apparaître, sans quoi elle surgit de nulle part. Il reste
    la seule information d'identité exposée."""
    proprietaire = source.get("username")
    return {
        "id":          hit_id,
        "name":        source["name"],
        "created_at":  source["created_at"],
        "doc_ids":     source.get("doc_ids", []),
        "shared_with": source.get("shared_with", []),
        "owner":       proprietaire,
        # Décidé ici plutôt que dans l'interface : c'est ce module qui
        # sait ce qu'est un propriétaire, et l'écran ne doit pas avoir à
        # comparer des noms d'utilisateur pour savoir s'il peut modifier.
        "owned":       username is None or proprietaire == username,
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
        raise KeyError(f"Collection inconnue : '{collection_id}'") from None
    except Exception as exc:
        raise _unavailable() from exc
    if res["_source"].get("username") != username:
        raise KeyError(f"Collection inconnue : '{collection_id}'")
    return res["_source"]


def list_collections(es: Elasticsearch, username: str, groups: list[str] | None = None) -> list[dict]:
    """Les collections d'un utilisateur — les siennes, plus celles qu'un
    de ses groupes a reçues en partage — la plus récente en premier.

    `groups` vient de get_effective_groups() côté appelant : point unique
    de vérité de l'appartenance, jamais recalculé ici. Absent, seules les
    collections personnelles remontent.

    Liste vide (pas d'exception) si Elasticsearch est injoignable ou si
    l'index n'existe pas encore — un utilisateur sans collection n'est
    pas une erreur, juste un cas de repli identique à celui d'ES en
    panne."""
    global _es_unavailable_logged
    visibilite = [{"term": {"username": username}}]
    if groups:
        visibilite.append({"terms": {"shared_with": groups}})
    try:
        res = es.search(
            index=SAVED_COLLECTIONS_INDEX,
            query={"bool": {"should": visibilite, "minimum_should_match": 1}},
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

    return [_to_entry(h["_id"], h["_source"], username) for h in res["hits"]["hits"]]


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
    except Exception as exc:
        raise _unavailable() from exc

    return entry


def rename_collection(es: Elasticsearch, username: str, collection_id: str, name: str) -> list[dict]:
    name = name.strip()
    if not name:
        raise ValueError("Le nom de la collection ne peut pas être vide.")

    _get_owned(es, username, collection_id)
    try:
        es.update(index=SAVED_COLLECTIONS_INDEX, id=collection_id, refresh="wait_for", doc={"name": name})
    except Exception as exc:
        raise _unavailable() from exc

    return list_collections(es, username)


def delete_collection(es: Elasticsearch, username: str, collection_id: str) -> list[dict]:
    """Retire une collection. Idempotent : un id déjà absent (ou
    appartenant à quelqu'un d'autre) ne lève pas d'erreur, la liste des
    collections est simplement inchangée."""
    try:
        doc = es.get(index=SAVED_COLLECTIONS_INDEX, id=collection_id)["_source"]
    except NotFoundError:
        return list_collections(es, username)
    except Exception as exc:
        raise _unavailable() from exc

    if doc.get("username") == username:
        try:
            es.delete(index=SAVED_COLLECTIONS_INDEX, id=collection_id, refresh="wait_for")
        except NotFoundError:
            pass  # déjà supprimée entre-temps (course rare) — idempotent
        except Exception as exc:
            raise _unavailable() from exc

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
        except Exception as exc:
            raise _unavailable() from exc

    return list_collections(es, username)


def remove_document(es: Elasticsearch, username: str, collection_id: str, doc_id: str) -> list[dict]:
    """Retire un document d'une collection. Idempotent : un doc_id déjà
    absent de la collection ne lève pas d'erreur."""
    entry = _get_owned(es, username, collection_id)
    doc_ids = [d for d in entry.get("doc_ids", []) if d != doc_id]
    try:
        es.update(index=SAVED_COLLECTIONS_INDEX, id=collection_id, refresh="wait_for", doc={"doc_ids": doc_ids})
    except Exception as exc:
        raise _unavailable() from exc

    return list_collections(es, username)


def set_sharing(
    es: Elasticsearch, username: str, collection_id: str, groups: list[str],
    groupes_autorises: list[str] | None = None,
) -> list[dict]:
    """Partage (ou départage) une collection avec des groupes.

    ⚠️ `groupes_autorises` — les groupes effectifs du propriétaire — borne
    ce qu'il peut désigner : on ne partage qu'avec un groupe dont on fait
    soi-même partie. Sans cette borne, n'importe qui pourrait pousser une
    collection à n'importe quel groupe de l'annuaire, et le premier usage
    serait de s'adresser à toute l'organisation.

    Rappel : partager donne la référence, pas le droit de lecture. Chaque
    document reste filtré par l'ACL à l'affichage.
    """
    _get_owned(es, username, collection_id)   # propriétaire seul
    groupes = sorted({g.strip() for g in groups if g.strip()})
    if groupes_autorises is not None:
        refuses = [g for g in groupes if g not in groupes_autorises]
        if refuses:
            raise PermissionError(
                "Partage refusé : vous ne faites pas partie de "
                + ", ".join(refuses)
                + ". On ne partage qu'avec un groupe dont on est membre."
            )
    try:
        es.update(
            index=SAVED_COLLECTIONS_INDEX, id=collection_id,
            refresh="wait_for", doc={"shared_with": groupes},
        )
    except Exception as exc:
        raise _unavailable() from exc

    return list_collections(es, username, groupes_autorises)


def duplicate_collection(
    es: Elasticsearch, username: str, collection_id: str, groups: list[str] | None = None,
) -> list[dict]:
    """Recopie une collection VISIBLE (la sienne ou une partagée) dans ses
    propres collections.

    C'est la porte de sortie du destinataire : il ne peut pas modifier la
    collection d'un autre, il s'en fait une copie. Seuls les identifiants
    sont recopiés — les documents, eux, resteront filtrés par SES droits,
    comme partout ailleurs."""
    visibles = {c["id"]: c for c in list_collections(es, username, groups)}
    source = visibles.get(collection_id)
    if source is None:
        raise KeyError(f"Collection inconnue : '{collection_id}'")

    copie = create_collection(es, username, f"{source['name']} (copie)")
    if source["doc_ids"]:
        try:
            es.update(
                index=SAVED_COLLECTIONS_INDEX, id=copie["id"],
                refresh="wait_for", doc={"doc_ids": source["doc_ids"]},
            )
        except Exception as exc:
            raise _unavailable() from exc

    return list_collections(es, username, groups)
