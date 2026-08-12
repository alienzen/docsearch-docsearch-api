# duplicates.py — Combien de fois le même document est-il indexé ?
#
# Un partage bureautique accumule les copies : « rapport.pdf »,
# « rapport - Copie.pdf », le même fichier dans le dossier de chacun.
# Rien ne le mesurait, faute d'une empreinte du CONTENU — `doc_hash` ne
# hache que le chemin, ce qui donne une valeur différente par copie.
#
# Depuis l'ajout de `content_sha256` côté ingestion (voir
# indexer.content_sha256), un simple regroupement suffit.
#
# ⚠️ Ce rapport est ADMINISTRATIF : il compte tout l'index, sans filtre
# ACL, comme les autres chiffres du panneau d'administration (volumétrie,
# répartition par extension). Il n'est donc accessible qu'aux membres du
# groupe d'administration, et jamais exposé à la recherche.
#
# ⚠️ Coût : l'agrégation parcourt les documents de l'index. Sur 4 000 000
# de documents ce n'est pas gratuit, et la donnée n'est consultée que
# quelques fois par an — d'où le cache Redis quotidien plutôt qu'un
# calcul à chaque ouverture du panneau.

import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

CACHE_KEY = "docsearch:rapport:doublons"
CACHE_TTL = int(os.getenv("DUPLICATES_CACHE_TTL", str(24 * 3600)))

# Nombre de groupes de doublons détaillés. Le total, lui, porte sur tout
# l'index : c'est la liste qui est tronquée, pas le compte.
TAILLE_LISTE = 50

_redis_client = None
_redis_indisponible_signale = False


def _get_redis_client():
    global _redis_client, _redis_indisponible_signale
    if _redis_client is not None:
        return _redis_client
    try:
        import redis
        _redis_client = redis.Redis(
            host=REDIS_HOST, port=REDIS_PORT, decode_responses=True, socket_timeout=2
        )
        _redis_client.ping()
        return _redis_client
    except Exception as e:
        if not _redis_indisponible_signale:
            logger.warning(f"[doublons] Redis injoignable ({e}) — rapport recalculé à chaque appel")
            _redis_indisponible_signale = True
        _redis_client = None
        return None


def _calculer(es, index: str) -> dict:
    """Groupes d'au moins deux documents partageant la même empreinte.

    `cardinality` plutôt qu'un décompte exact des groupes : compter
    exactement les valeurs distinctes d'un champ à forte cardinalité
    coûte bien plus cher que l'approximation, dont la marge (~1 %) est
    sans conséquence sur un indicateur d'ordre de grandeur.
    """
    res = es.search(
        index=index,
        size=0,
        # Les documents SQL et web n'ont pas d'empreinte (pas de fichier) :
        # les compter comme « sans doublon » serait juste, mais les
        # compter dans le total des documents examinés serait trompeur.
        query={"bool": {"filter": [{"exists": {"field": "content_sha256"}}]}},
        aggs={
            "empreintes": {"cardinality": {"field": "content_sha256"}},
            "groupes": {
                "terms": {
                    "field": "content_sha256",
                    "size": TAILLE_LISTE,
                    "min_doc_count": 2,
                    "order": {"gaspille": "desc"},
                },
                "aggs": {
                    # Ce qui intéresse un exploitant n'est pas le groupe
                    # le plus nombreux mais celui qui coûte le plus de
                    # place : dix copies d'une note de service pèsent
                    # moins que deux copies d'une vidéo.
                    "gaspille": {"sum": {"field": "size"}},
                    "exemple": {
                        "top_hits": {"size": 3, "_source": ["filepath", "filename", "size", "source"]}
                    },
                },
            },
        },
    )

    agregats = res["aggregations"]
    total = res["hits"]["total"]["value"]
    distincts = agregats["empreintes"]["value"]

    groupes = []
    for bucket in agregats["groupes"]["buckets"]:
        exemplaires = [h["_source"] for h in bucket["exemple"]["hits"]["hits"]]
        taille_unitaire = exemplaires[0].get("size") or 0 if exemplaires else 0
        groupes.append({
            "empreinte": bucket["key"],
            "copies": bucket["doc_count"],
            # Ce qui serait rendu en ne gardant qu'un exemplaire.
            "gaspille": max(0, int(bucket["gaspille"]["value"]) - taille_unitaire),
            "exemples": exemplaires,
        })

    return {
        "calcule_le": datetime.now(timezone.utc).isoformat(),
        # Documents portant une empreinte — pas le total de l'index, dont
        # les documents SQL et web n'en ont pas.
        "documents": total,
        "distincts": distincts,
        # Différence entre les deux : le nombre d'exemplaires en trop,
        # tous groupes confondus. C'est le chiffre à retenir.
        "copies_en_trop": max(0, total - distincts),
        "groupes": groupes,
    }


def rapport(es, index: str, rafraichir: bool = False) -> dict:
    """Rapport de doublons, servi depuis le cache sauf demande explicite.

    Le cache n'est pas une optimisation de confort : sans lui, chaque
    ouverture du panneau d'administration lancerait une agrégation sur
    tout l'index, pendant que les utilisateurs cherchent.
    """
    client = _get_redis_client()
    if client is not None and not rafraichir:
        try:
            cache = client.get(CACHE_KEY)
            if cache:
                return {**json.loads(cache), "depuis_cache": True}
        except Exception as e:
            logger.warning(f"[doublons] Lecture du cache impossible ({e})")

    resultat = _calculer(es, index)
    if client is not None:
        try:
            client.setex(CACHE_KEY, CACHE_TTL, json.dumps(resultat))
        except Exception as e:
            logger.warning(f"[doublons] Écriture du cache impossible ({e})")
    return {**resultat, "depuis_cache": False}
