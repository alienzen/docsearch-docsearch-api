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

# Groupes LDAP de l'utilisateur AU MOMENT de la réponse — pour agréger le
# score par service (voir summary_by_group). Déclaré à part de la
# création initiale pour être ajouté aussi aux index DÉJÀ existants :
# put_mapping fusionne, il n'écrase ni les champs présents ni les
# documents. Même motif que _ENGAGEMENT_PROPERTIES dans search_log.py.
#
# Enregistré plutôt que résolu à l'affichage : la valeur reflète ainsi
# l'appartenance de l'époque, et non celle du jour où l'on consulte la
# page. C'est le sens juste pour un historique — quelqu'un ayant changé
# de service depuis ne doit pas déplacer rétroactivement son avis.
_GROUP_PROPERTIES = {"groups": {"type": "keyword"}}


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
                    **_GROUP_PROPERTIES,
                }
            }
        })
        logger.info(f"Index '{NPS_LOG_INDEX}' créé.")
    else:
        # Index antérieur à l'ajout du champ : on le complète sans
        # toucher aux réponses déjà enregistrées, qui resteront sans
        # groupe et compteront dans le lot « non renseigné ».
        try:
            es.indices.put_mapping(index=NPS_LOG_INDEX, properties=_GROUP_PROPERTIES)
        except Exception as e:
            logger.warning(f"[nps_log] Ajout du champ 'groups' impossible : {e}")
    _index_ready = True


def log_nps(es: Elasticsearch, *, username: str, score: int, groups: list[str] | None = None) -> None:
    """Enregistre une réponse NPS (0-10). Ne lève jamais d'exception —
    un échec d'écriture ne doit jamais remonter comme erreur visible à
    l'utilisateur qui vient de répondre à la question."""
    try:
        _ensure_index(es)
        es.index(index=NPS_LOG_INDEX, document={
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "username":  username,
            "score":     score,
            "groups":    list(groups or []),
        })
    except Exception as e:
        logger.warning(f"[nps_log] Échec d'écriture de la réponse NPS : {e}")


# Le découpage standard du NPS : détracteurs 0-6, passifs 7-8,
# promoteurs 9-10. Écrit une fois, appliqué globalement ET par groupe —
# deux copies auraient fini par diverger d'une borne.
_REPARTITION_AGGS = {
    "detractors": {"filter": {"range": {"score": {"lte": 6}}}},
    "passives":   {"filter": {"range": {"score": {"gte": 7, "lte": 8}}}},
    "promoters":  {"filter": {"range": {"score": {"gte": 9}}}},
}


def _nps_score(total: int, promoters: int, detractors: int) -> int | None:
    """%promoteurs − %détracteurs, le calcul standard.

    À appliquer à CHAQUE périmètre séparément : un score de groupe ne se
    déduit pas du score global, ni d'une moyenne des scores de groupes —
    les effectifs diffèrent.
    """
    if total <= 0:
        return None
    return round(((promoters - detractors) / total) * 100)


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
                **_REPARTITION_AGGS,
                # Même découpage, par groupe. `missing` donne un lot
                # explicite aux réponses sans groupe (historique d'avant
                # la capture, utilisateur sans appartenance) : sans lui,
                # la somme des lots ne retomberait pas sur le total.
                "by_group": {
                    "terms": {"field": "groups", "size": 50, "missing": "__sans_groupe__"},
                    "aggs": _REPARTITION_AGGS,
                },
            },
        )
    except Exception as e:
        if "index_not_found" in str(e).lower():
            return {"total_responses": 0, "nps_score": None, "detractors": 0,
                    "passives": 0, "promoters": 0, "by_group": []}
        raise

    total = res["hits"]["total"]["value"]
    detractors = res["aggregations"]["detractors"]["doc_count"]
    passives   = res["aggregations"]["passives"]["doc_count"]
    promoters  = res["aggregations"]["promoters"]["doc_count"]

    return {
        "total_responses": total,
        "nps_score":        _nps_score(total, promoters, detractors),
        "detractors":       detractors,
        "passives":         passives,
        "promoters":        promoters,
        # Un utilisateur de deux groupes compte dans les deux : la somme
        # des lots dépasse le total global, ce qui est le propre d'une
        # agrégation par groupe.
        "by_group": [
            {
                "group":      b["key"],
                "responses":  b["doc_count"],
                "detractors": b["detractors"]["doc_count"],
                "passives":   b["passives"]["doc_count"],
                "promoters":  b["promoters"]["doc_count"],
                "nps_score":  _nps_score(
                    b["doc_count"], b["promoters"]["doc_count"], b["detractors"]["doc_count"]
                ),
            }
            for b in res["aggregations"]["by_group"]["buckets"]
        ],
    }
