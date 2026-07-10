# search_log.py — Journalisation des recherches pour la page stats admin
#
# Chaque recherche réussie sur /search est indexée dans un index ES
# dédié (SEARCH_LOG_INDEX, séparé de l'index documents) : qui, quand,
# depuis quelle IP, quelle requête, combien de résultats. Sert
# uniquement la page /stats.html — un échec d'écriture ici ne doit
# JAMAIS faire échouer une recherche (best-effort, erreur juste loguée).

import os
import logging
from datetime import datetime, timezone

from elasticsearch import Elasticsearch

logger = logging.getLogger(__name__)

SEARCH_LOG_INDEX = os.getenv("SEARCH_LOG_INDEX", "search_logs")

_index_ready = False

# Champs ajoutés après coup (feedback pouce haut/bas, clics sur les
# résultats) — déclarés à part de la création initiale pour pouvoir les
# ajouter aussi à un index DÉJÀ existant (put_mapping fusionne, n'écrase
# jamais les champs déjà présents ni les documents existants).
_ENGAGEMENT_PROPERTIES = {
    "feedback": {"type": "keyword"},   # "up" | "down", absent tant qu'aucun avis
    "clicks": {
        "type": "nested",
        "properties": {
            "doc_id":    {"type": "keyword"},
            "position":  {"type": "integer"},
            "timestamp": {"type": "date"},
        },
    },
}


def _ensure_index(es: Elasticsearch) -> None:
    global _index_ready
    if _index_ready:
        return
    if not es.indices.exists(index=SEARCH_LOG_INDEX):
        es.indices.create(index=SEARCH_LOG_INDEX, body={
            "mappings": {
                "properties": {
                    "timestamp":     {"type": "date"},
                    "username":      {"type": "keyword"},
                    "ip":            {"type": "ip"},
                    "query":         {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                    "search_in":     {"type": "keyword"},
                    "source":        {"type": "keyword"},
                    "total_results": {"type": "integer"},
                    "result_files":  {"type": "keyword"},
                    **_ENGAGEMENT_PROPERTIES,
                }
            }
        })
        logger.info(f"Index '{SEARCH_LOG_INDEX}' créé.")
    else:
        # Index déjà créé par une version antérieure (avant l'ajout du
        # feedback/tracking de clic) — complète son mapping sans y
        # toucher autrement. Idempotent, appelable à chaque démarrage.
        es.indices.put_mapping(index=SEARCH_LOG_INDEX, properties=_ENGAGEMENT_PROPERTIES)
    _index_ready = True


def log_search(
    es: Elasticsearch,
    *,
    username: str,
    ip: str | None,
    query: str,
    search_in: str,
    source: str | list[str] | None,
    total_results: int,
    result_files: list[str],
) -> str | None:
    """
    Enregistre un événement de recherche. Ne lève jamais d'exception —
    une recherche doit réussir même si la journalisation échoue (ES
    temporairement indisponible, IP non parsable par le mapping "ip", etc).
    Retourne l'ID du document créé (None en cas d'échec) — c'est ce
    "search_id" que le frontend renvoie ensuite pour rattacher un avis
    (pouce) ou un clic à CETTE recherche précise (voir /feedback, /click).

    `source` : nom(s) de la/des source(s) (sources_config.py) sur
    lesquelles la recherche a été restreinte (sélection cumulative
    possible), ou None/liste vide pour une recherche fédérée (toutes
    sources) — voir search_api.py:search(). Le champ ES "source" est un
    keyword, nativement multi-valué : aucun changement de mapping requis
    pour stocker une liste.
    """
    try:
        _ensure_index(es)
        doc = {
            "timestamp":     datetime.now(timezone.utc).isoformat(),
            "username":      username,
            "query":         query,
            "search_in":     search_in,
            "total_results": total_results,
            "result_files":  result_files,
        }
        if ip:
            doc["ip"] = ip
        if source:
            doc["source"] = source
        res = es.index(index=SEARCH_LOG_INDEX, document=doc)
        return res.get("_id")
    except Exception as e:
        logger.warning(f"[search_log] Échec d'écriture du log de recherche : {e}")
        return None
