# nps_log.py — Journalisation des réponses au NPS (Net Promoter Score)
#
# Contrairement à search_log.py (un événement par recherche), le NPS
# n'est PAS rattaché à une recherche précise — c'est une question
# ponctuelle sur l'outil en général ("recommanderiez-vous DocSearch ?"),
# affichée occasionnellement (voir engagement_config.py pour le flag
# d'activation, et index.html pour la cadence d'affichage côté client).
# Index ES séparé de search_logs pour cette raison.

import os
import logging
from datetime import datetime, timezone

from elasticsearch import Elasticsearch

logger = logging.getLogger(__name__)

NPS_LOG_INDEX = os.getenv("NPS_LOG_INDEX", "nps_responses")

_index_ready = False


def _ensure_index(es: Elasticsearch) -> None:
    global _index_ready
    if _index_ready:
        return
    if not es.indices.exists(index=NPS_LOG_INDEX):
        es.indices.create(index=NPS_LOG_INDEX, body={
            "mappings": {
                "properties": {
                    "timestamp": {"type": "date"},
                    "username":  {"type": "keyword"},
                    "score":     {"type": "integer"},
                }
            }
        })
        logger.info(f"Index '{NPS_LOG_INDEX}' créé.")
    _index_ready = True


def log_nps(es: Elasticsearch, *, username: str, score: int) -> None:
    """Enregistre une réponse NPS (0-10). Ne lève jamais d'exception —
    un échec d'écriture ne doit jamais remonter comme erreur visible à
    l'utilisateur qui vient de répondre à la question."""
    try:
        _ensure_index(es)
        es.index(index=NPS_LOG_INDEX, document={
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "username":  username,
            "score":     score,
        })
    except Exception as e:
        logger.warning(f"[nps_log] Échec d'écriture de la réponse NPS : {e}")


def summary(es: Elasticsearch) -> dict:
    """
    Score NPS agrégé (%promoteurs - %détracteurs, standard du calcul)
    plus la répartition détracteurs (0-6) / passifs (7-8) / promoteurs
    (9-10) et le nombre total de réponses — pour la page /stats.html.
    """
    try:
        res = es.search(
            index=NPS_LOG_INDEX,
            size=0,
            aggs={
                "detractors": {"filter": {"range": {"score": {"lte": 6}}}},
                "passives":   {"filter": {"range": {"score": {"gte": 7, "lte": 8}}}},
                "promoters":  {"filter": {"range": {"score": {"gte": 9}}}},
            },
        )
    except Exception as e:
        if "index_not_found" in str(e).lower():
            return {"total_responses": 0, "nps_score": None, "detractors": 0, "passives": 0, "promoters": 0}
        raise

    total = res["hits"]["total"]["value"]
    detractors = res["aggregations"]["detractors"]["doc_count"]
    passives   = res["aggregations"]["passives"]["doc_count"]
    promoters  = res["aggregations"]["promoters"]["doc_count"]

    nps_score = None
    if total > 0:
        nps_score = round(((promoters - detractors) / total) * 100)

    return {
        "total_responses": total,
        "nps_score":        nps_score,
        "detractors":       detractors,
        "passives":         passives,
        "promoters":        promoters,
    }
