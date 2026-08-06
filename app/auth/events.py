# auth/events.py — Journal des tentatives de connexion
#
# Index Elasticsearch dédié, sur le motif d'audit_log.py et search_log.py
# (volume potentiellement significatif, pagination native ES plus adaptée
# qu'une liste JSON dans Redis).
#
# Ce qui distingue ce journal de audit_log.py : celui-là n'enregistre que
# les actions RÉUSSIES, parce qu'un échec n'y représente aucun changement
# réel. Ici c'est l'inverse — **toutes les branches de sortie sont
# journalisées**, et les échecs sont précisément ce qu'on vient y lire :
# rate limit dépassé, identifiants refusés, annuaire injoignable, succès.
# Une série de refus sur un même identifiant est le signal qu'on attend de
# ce journal ; ne pas l'écrire le rendrait aveugle à ce pour quoi il existe.
#
# ⚠️  Le mot de passe ne transite jamais dans ce module, sous aucune forme —
# ni en clair, ni haché, ni tronqué. Vérifié par un test dédié
# (tests/test_login_events.py), qui inspecte le document produit.

import logging
import os
import time
from datetime import datetime, timezone

from elasticsearch import Elasticsearch

from auth import config

logger = logging.getLogger(__name__)

LOGIN_EVENTS_INDEX = config.LOGIN_EVENTS_INDEX
ES_HOST = os.getenv("ES_HOST", "http://es01:9200")

_es_client: Elasticsearch | None = None

# Instant de la dernière vérification d'existence de l'index, et sa durée de
# validité.
#
# ⚠️  Un simple drapeau « vérifié une fois » ne suffit PAS, et ce n'est pas
# théorique : l'index peut disparaître sous un processus qui tourne
# (suppression d'exploitation, purge, restauration de sauvegarde). Le
# drapeau court-circuite alors `_ensure_index`, l'écriture part sur un index
# absent, et **Elasticsearch le recrée tout seul** — sans le mapping ni les
# réglages définis ici, donc avec un réplica que ce cluster mono-nœud ne
# pourra jamais allouer. Constaté le 2026-08-06 : index supprimé à la main,
# recréé en `rep=1` à la connexion suivante, cluster en jaune.
#
# Un TTL plutôt qu'une vérification par écriture : c'est déjà l'idiome du
# dépôt pour les caches de configuration (RUNTIME_CONFIG_CACHE_TTL,
# FILETYPE_CONFIG_CACHE_TTL), et le coût est d'un appel `exists` par
# fenêtre et par processus.
_index_verifie_a: float = 0.0
INDEX_CHECK_TTL_SECONDS = int(os.getenv("LOGIN_EVENTS_INDEX_CHECK_TTL", "300"))

# Issues possibles, valeurs stables — elles servent de filtre dans le
# panneau d'administration, les renommer casserait les tableaux de bord.
SUCCESS = "succes"
INVALID_CREDENTIALS = "identifiants_refuses"
RATE_LIMITED = "trop_de_tentatives"
PROVIDER_UNAVAILABLE = "fournisseur_indisponible"
ACCESS_DENIED = "acces_refuse"


def _client() -> Elasticsearch:
    global _es_client
    if _es_client is None:
        _es_client = Elasticsearch(ES_HOST, request_timeout=5)
    return _es_client


#: Réglages et mapping de l'index — constante plutôt qu'un littéral enfoui
#: dans la fonction : c'est la source de vérité que reprennent le test de
#: non-régression et, au besoin, une recréation manuelle à la main.
INDEX_BODY = {
            # Réplicas explicitement à 0, comme TOUS les autres index de
            # cette installation (documents, admin_audit_log, nps_responses,
            # suggestions…). Sans ce réglage, ES applique son défaut de 1 et
            # l'index reste jaune à perpétuité sur un cluster mono-nœud : un
            # réplica n'a nulle part où aller. Constaté sur l'installation de
            # dev, où login_events était le seul index à porter rep=1.
            "settings": {"number_of_shards": 1, "number_of_replicas": 0},
            "mappings": {
                "properties": {
                    "timestamp":   {"type": "date"},
                    # Identifiant TEL QUE PRÉSENTÉ, même quand aucun compte
                    # ne lui correspond : c'est ce qui permet de repérer une
                    # série de tentatives sur un compte qui n'existe pas.
                    "identifier":  {"type": "keyword"},
                    "outcome":     {"type": "keyword"},
                    "method":      {"type": "keyword"},
                    "ip":          {"type": "keyword"},
                    "user_agent":  {"type": "text"},
                    "detail":      {"type": "text"},
                    # Vrai quand la session a été ouverte par le harnais de
                    # développement (KERBEROS_DEV_PRINCIPAL) et non par un
                    # vrai ticket : une trace d'audit ne doit jamais laisser
                    # croire à une connexion Kerberos authentique.
                    "simulated":   {"type": "boolean"},
                }
            }
        }


def _ensure_index(es: Elasticsearch) -> None:
    global _index_verifie_a
    if time.monotonic() - _index_verifie_a < INDEX_CHECK_TTL_SECONDS:
        return
    if not es.indices.exists(index=LOGIN_EVENTS_INDEX):
        es.indices.create(index=LOGIN_EVENTS_INDEX, body=INDEX_BODY)
        logger.info(f"Index '{LOGIN_EVENTS_INDEX}' créé.")
    _index_verifie_a = time.monotonic()


def record(
    *,
    identifier: str,
    outcome: str,
    method: str,
    ip: str | None = None,
    user_agent: str | None = None,
    detail: str = "",
    simulated: bool = False,
) -> None:
    """Enregistre une tentative. **Ne lève jamais** : une panne du journal ne
    doit ni faire échouer une connexion légitime, ni faire réussir une
    connexion refusée. L'échec d'écriture part en warning applicatif."""
    try:
        es = _client()
        _ensure_index(es)
        es.index(index=LOGIN_EVENTS_INDEX, document={
            "timestamp":  datetime.now(timezone.utc).isoformat(),
            "identifier": (identifier or "").strip().lower(),
            "outcome":    outcome,
            "method":     method,
            "ip":         ip or "",
            "user_agent": (user_agent or "")[:500],
            "detail":     detail[:500],
            "simulated":  simulated,
        })
    except Exception as e:
        logger.warning(f"[login_events] Échec d'écriture : {e}")


def list_events(es: Elasticsearch, *, size: int, from_: int, outcome: str = "") -> dict:
    """Liste paginée, plus récentes d'abord — pour le panneau
    d'administration."""
    query = {"match_all": {}} if not outcome else {"term": {"outcome": outcome}}
    try:
        res = es.search(
            index=LOGIN_EVENTS_INDEX,
            query=query,
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
