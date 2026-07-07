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
                    "total_results": {"type": "integer"},
                    "result_files":  {"type": "keyword"},
                }
            }
        })
        logger.info(f"Index '{SEARCH_LOG_INDEX}' créé.")
    _index_ready = True


def log_search(
    es: Elasticsearch,
    *,
    username: str,
    ip: str | None,
    query: str,
    search_in: str,
    total_results: int,
    result_files: list[str],
) -> None:
    """
    Enregistre un événement de recherche. Ne lève jamais d'exception —
    une recherche doit réussir même si la journalisation échoue (ES
    temporairement indisponible, IP non parsable par le mapping "ip", etc).
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
        es.index(index=SEARCH_LOG_INDEX, document=doc)
    except Exception as e:
        logger.warning(f"[search_log] Échec d'écriture du log de recherche : {e}")
