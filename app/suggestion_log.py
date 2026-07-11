# suggestion_log.py — Journalisation des suggestions libres des utilisateurs
#
# Comme le NPS (nps_log.py), pas rattaché à une recherche précise —
# c'est un point d'entrée permanent ("💡 Suggérer une idée" dans l'en-tête,
# voir index.html), indépendant de toute recherche. Index ES dédié pour
# la même raison que nps_log.py/search_log.py.
#
# ANONYME PAR DÉFAUT : contrairement au pouce/NPS/clics (toujours
# rattachés à username), l'identité n'est capturée ici QUE si
# l'utilisateur a explicitement décoché "rester anonyme" dans l'UI (voir
# index.html) — le paramètre `username` est donc optionnel, et son
# absence ne doit jamais être comblée discrètement en arrière-plan.

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
                    "category":  {"type": "keyword"},
                    "text":      {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                    "username":  {"type": "keyword"},
                }
            }
        })
        logger.info(f"Index '{SUGGESTION_LOG_INDEX}' créé.")
    _index_ready = True


def log_suggestion(es: Elasticsearch, *, text: str, category: str | None, username: str | None = None) -> None:
    """Enregistre une suggestion libre. `username` n'est renseigné que si
    l'utilisateur a choisi de ne pas rester anonyme (voir docstring de
    module) — absent sinon, jamais comblé silencieusement. Ne lève jamais
    d'exception — un échec d'écriture ne doit jamais remonter comme
    erreur visible à l'utilisateur qui vient de soumettre son idée."""
    try:
        _ensure_index(es)
        doc = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "text":      text,
        }
        if category:
            doc["category"] = category
        if username:
            doc["username"] = username
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
