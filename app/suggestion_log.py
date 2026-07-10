# suggestion_log.py — Journalisation des suggestions libres des utilisateurs
#
# Comme le NPS (nps_log.py), pas rattaché à une recherche précise —
# c'est un point d'entrée permanent ("💡 Suggérer une idée" dans l'en-tête,
# voir index.html), indépendant de toute recherche. Index ES dédié pour
# la même raison que nps_log.py/search_log.py.

import os
import logging
from datetime import datetime, timezone

from elasticsearch import Elasticsearch

logger = logging.getLogger(__name__)

SUGGESTION_LOG_INDEX = os.getenv("SUGGESTION_LOG_INDEX", "suggestions")

_index_ready = False


def _ensure_index(es: Elasticsearch) -> None:
    global _index_ready
    if _index_ready:
        return
    if not es.indices.exists(index=SUGGESTION_LOG_INDEX):
        es.indices.create(index=SUGGESTION_LOG_INDEX, body={
            "mappings": {
                "properties": {
                    "timestamp": {"type": "date"},
                    "username":  {"type": "keyword"},
                    "category":  {"type": "keyword"},
                    "text":      {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                }
            }
        })
        logger.info(f"Index '{SUGGESTION_LOG_INDEX}' créé.")
    _index_ready = True


def log_suggestion(es: Elasticsearch, *, username: str, text: str, category: str | None) -> None:
    """Enregistre une suggestion libre. Ne lève jamais d'exception — un
    échec d'écriture ne doit jamais remonter comme erreur visible à
    l'utilisateur qui vient de soumettre son idée."""
    try:
        _ensure_index(es)
        doc = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "username":  username,
            "text":      text,
        }
        if category:
            doc["category"] = category
        es.index(index=SUGGESTION_LOG_INDEX, document=doc)
    except Exception as e:
        logger.warning(f"[suggestion_log] Échec d'écriture de la suggestion : {e}")


def list_suggestions(es: Elasticsearch, *, size: int, from_: int) -> dict:
    """Liste paginée, plus récentes d'abord — pour la page /stats.html."""
    try:
        res = es.search(
            index=SUGGESTION_LOG_INDEX,
            query={"match_all": {}},
            sort=[{"timestamp": {"order": "desc"}}],
            size=size,
            from_=from_,
        )
    except Exception as e:
        if "index_not_found" in str(e).lower():
            return {"total": 0, "results": []}
        raise

    return {
        "total":   res["hits"]["total"]["value"],
        "results": [{"id": h["_id"], **h["_source"]} for h in res["hits"]["hits"]],
    }
