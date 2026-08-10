# search_log.py — Journalisation des recherches pour la page stats admin
#
# Chaque recherche réussie sur /search est indexée dans un index ES
# dédié (SEARCH_LOG_INDEX, séparé de l'index documents) : qui, quand,
# depuis quelle IP, quelle requête, combien de résultats. Sert
# uniquement la page /stats.html — un échec d'écriture ici ne doit
# JAMAIS faire échouer une recherche (best-effort, erreur juste loguée).

import os
import json
import time
import logging
from datetime import datetime, timezone

from elasticsearch import Elasticsearch

logger = logging.getLogger(__name__)

SEARCH_LOG_INDEX = os.getenv("SEARCH_LOG_INDEX", "search_logs")

# ── Santé de la journalisation (voir _record_health) ─────────
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
SEARCH_LOG_HEALTH_KEY = "docsearch:health:search_log"
# Une écriture Redis par minute au plus tant que l'état ne change pas :
# le panneau d'administration n'a pas besoin d'une horloge à la seconde,
# et une écriture par recherche serait un coût permanent pour une donnée
# consultée trois fois par an. Un CHANGEMENT d'état, lui, est écrit
# immédiatement — c'est tout l'intérêt du dispositif.
HEALTH_REFRESH_SECONDS = 60

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

# Idem, ajoutés après coup : critères de filtrage actifs au moment de la
# recherche (facettes cumulatives, période) — purement informatif pour
# /stats.html, aucune recherche n'est jamais rejouée à partir de ces champs.
_CRITERIA_PROPERTIES = {
    "extension": {"type": "keyword"},
    "author":    {"type": "keyword"},
    "folder":    {"type": "keyword"},
    "keywords":  {"type": "keyword"},
    "date_from": {"type": "keyword"},
    "date_to":   {"type": "keyword"},
}

# Groupes LDAP de l'utilisateur AU MOMENT de la recherche — pour agréger
# les avis par service (voir /admin/search-logs/summary). Ajoutés là et
# non à la réception de l'avis : /feedback ne fait qu'une mise à jour
# partielle du document, seules les recherches AYANT REÇU un avis en
# porteraient alors. Écrits dès la recherche, tous les documents en ont,
# et la donnée servira à d'autres découpages (recherches par service,
# taux de résultats nuls par groupe).
#
# Enregistrés plutôt que résolus à l'affichage : la valeur reflète ainsi
# l'appartenance de l'époque, pas celle du jour de la consultation.
_GROUP_PROPERTIES = {"groups": {"type": "keyword"}}


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
                    **_CRITERIA_PROPERTIES,
                    **_GROUP_PROPERTIES,
                }
            }
        })
        logger.info(f"Index '{SEARCH_LOG_INDEX}' créé.")
    else:
        # Index déjà créé par une version antérieure (avant l'ajout du
        # feedback/tracking de clic/critères) — complète son mapping sans
        # y toucher autrement. Idempotent, appelable à chaque démarrage.
        es.indices.put_mapping(
            index=SEARCH_LOG_INDEX,
            properties={**_ENGAGEMENT_PROPERTIES, **_CRITERIA_PROPERTIES, **_GROUP_PROPERTIES},
        )
    _index_ready = True


# ── Santé de la journalisation ───────────────────────────────
#
# log_search() avale ses exceptions — une recherche doit aboutir même si
# le journal tombe — et c'est le bon choix. Mais l'échec est alors
# TOTALEMENT invisible : côté utilisateur, `search_id` passe à None et
# trois fonctionnalités disparaissent sans un mot (pouce haut/bas, popup
# NPS, tracking de clic) ; côté administration, rien ne le signale. Le
# 2026-08-10, le disque de la VM a franchi le flood-stage watermark
# d'Elasticsearch (95 %), qui a passé ses index en read-only : il a fallu
# lire le journal de l'API pour comprendre pourquoi les pouces avaient
# disparu, alors que le cluster restait « green » et que tous les
# voyants du panneau d'administration étaient au vert.
#
# D'où cette trace : le résultat de la dernière tentative d'écriture, que
# /admin/status expose en carte d'état.
#
# Dans Redis et non en variable de module : l'API tourne en plusieurs
# exemplaires en production (rôle « frontend »), et le panneau interroge
# celui que Nginx désigne. Un état local ne décrirait que l'instance
# tirée au sort. Redis est déjà une dépendance dure de l'API et le
# support du battement de cœur du watcher, juste à côté.

_redis_client = None
_redis_unavailable_logged = False

_last_health_ok: bool | None = None
_last_health_write: float = 0.0


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
            logger.warning(
                f"[search_log] Redis injoignable ({e}) — la carte d'état « journalisation » "
                f"du panneau d'administration restera muette."
            )
            _redis_unavailable_logged = True
        _redis_client = None
        return None


def _record_health(ok: bool, error: str | None = None) -> None:
    """Mémorise le résultat de la dernière tentative d'écriture.

    Ne lève jamais : appelée depuis log_search(), dont le contrat est de
    ne jamais faire échouer une recherche. Une santé qu'on n'arrive pas à
    écrire ne doit pas coûter plus cher que la panne qu'elle décrit.
    """
    global _last_health_ok, _last_health_write
    try:
        now = time.time()
        inchange = ok == _last_health_ok
        if inchange and (now - _last_health_write) < HEALTH_REFRESH_SECONDS:
            return

        client = _get_redis_client()
        if client is None:
            return

        # Le message d'erreur est tronqué : une exception Elasticsearch
        # embarque volontiers la requête entière, illisible dans une
        # carte et inutile pour décider quoi faire.
        client.set(
            SEARCH_LOG_HEALTH_KEY,
            json.dumps({"ok": ok, "ts": now, "error": (error or None) and error[:300]}),
        )
        _last_health_ok = ok
        _last_health_write = now
    except Exception as e:
        logger.debug(f"[search_log] Santé non enregistrée : {e}")


def health() -> dict:
    """État de la journalisation pour /admin/status.

    `ok` à None = on ne sait pas, ce qui n'est PAS une panne : aucune
    recherche n'a encore été lancée depuis que la clé existe (Redis vidé,
    installation neuve). Le panneau l'affiche en neutre plutôt qu'en
    rouge — un voyant qui crie au démarrage n'est plus lu ensuite.
    """
    try:
        client = _get_redis_client()
        if client is None:
            return {"ok": None, "reason": "Redis injoignable"}
        raw = client.get(SEARCH_LOG_HEALTH_KEY)
        if not raw:
            return {"ok": None, "reason": "aucune recherche journalisée depuis le démarrage"}
        data = json.loads(raw)
        return {
            "ok": bool(data.get("ok")),
            "last_attempt_seconds_ago": round(time.time() - data.get("ts", 0), 1),
            "error": data.get("error"),
        }
    except Exception as e:
        return {"ok": None, "reason": str(e)}


def log_search(
    es: Elasticsearch,
    *,
    username: str,
    groups: list[str] | None = None,
    ip: str | None,
    query: str,
    search_in: str,
    source: str | list[str] | None,
    total_results: int,
    result_files: list[str],
    extension: str | list[str] | None = None,
    author: str | list[str] | None = None,
    folder: str | list[str] | None = None,
    keywords: str | list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> str | None:
    """
    Enregistre un événement de recherche. Ne lève jamais d'exception —
    une recherche doit réussir même si la journalisation échoue (ES
    temporairement indisponible, IP non parsable par le mapping "ip", etc).
    Retourne l'ID du document créé (None en cas d'échec) — c'est ce
    "search_id" que le frontend renvoie ensuite pour rattacher un avis
    (pouce) ou un clic à CETTE recherche précise (voir /feedback, /click).

    `source` : nom(s) de la/des source(s) (file_sources_config.py) sur
    lesquelles la recherche a été restreinte (sélection cumulative
    possible), ou None/liste vide pour une recherche fédérée (toutes
    sources) — voir search_api.py:search(). Le champ ES "source" est un
    keyword, nativement multi-valué : aucun changement de mapping requis
    pour stocker une liste.

    extension/author/folder/keywords/date_from/date_to : critères de
    filtrage actifs au moment de la recherche (facettes cumulatives —
    extension/author/folder/keywords acceptent une liste —, période) —
    purement informatif pour /stats.html ("Historique des recherches"),
    jamais réutilisés pour rejouer la recherche.
    """
    try:
        _ensure_index(es)
        doc = {
            "timestamp":     datetime.now(timezone.utc).isoformat(),
            "username":      username,
            "groups":        list(groups or []),
            "query":         query,
            "search_in":     search_in,
            "total_results": total_results,
            "result_files":  result_files,
        }
        if ip:
            doc["ip"] = ip
        if extension:
            doc["extension"] = extension
        if author:
            doc["author"] = author
        if folder:
            doc["folder"] = folder
        if keywords:
            doc["keywords"] = keywords
        if date_from:
            doc["date_from"] = date_from
        if date_to:
            doc["date_to"] = date_to
        if source:
            doc["source"] = source
        res = es.index(index=SEARCH_LOG_INDEX, document=doc)
        _record_health(True)
        return res.get("_id")
    except Exception as e:
        logger.warning(f"[search_log] Échec d'écriture du log de recherche : {e}")
        _record_health(False, str(e))
        return None
