# audit_log.py — Journal d'audit des actions d'administration
#
# Enregistre chaque mutation admin RÉUSSIE (POST/DELETE/PUT sous /admin/*
# ayant répondu avec un statut de succès) : qui, quoi, quand. Alimenté
# par un middleware générique (voir search_api.py::audit_log_middleware)
# plutôt que par un appel explicite dans chaque endpoint — une nouvelle
# route de mutation est donc auditée automatiquement dès sa création,
# sans modification de ce module ni oubli possible d'en ajouter un.
#
# Un échec (validation 400, 404, Redis/ES injoignable...) n'est PAS
# journalisé : il ne représente aucun changement réel, l'enregistrer
# serait trompeur pour qui relit le journal.
#
# Index ES dédié (comme search_log.py/nps_log.py/suggestion_log.py) —
# volume potentiellement significatif dans une installation active,
# pagination native ES plus adaptée qu'une simple liste JSON dans Redis.

import os
import logging
from datetime import datetime, timezone

from elasticsearch import Elasticsearch

logger = logging.getLogger(__name__)

AUDIT_LOG_INDEX = os.getenv("AUDIT_LOG_INDEX", "admin_audit_log")

_index_ready = False

# Clés de corps de requête à ne JAMAIS journaliser en clair — ex: "dsn"
# (POST /admin/sql-dsns) contient un DSN complet avec mot de passe. Le
# journal d'audit est un index ES séparé du registre chiffré dédié
# (sql_dsn_registry.py) : il ne doit jamais devenir une fuite parallèle
# du même secret.
_SENSITIVE_KEYS = {"dsn", "password", "secret", "token"}


def _ensure_index(es: Elasticsearch) -> None:
    global _index_ready
    if _index_ready:
        return
    if not es.indices.exists(index=AUDIT_LOG_INDEX):
        es.indices.create(index=AUDIT_LOG_INDEX, body={
            "mappings": {
                "properties": {
                    "timestamp":    {"type": "date"},
                    "username":     {"type": "keyword"},
                    "method":       {"type": "keyword"},
                    # "path" est le PATRON de route (ex: "/admin/file-sources/{name}/label"),
                    # pas l'URL réellement appelée — les valeurs variables
                    # vivent dans path_params, ça permet de filtrer/compter
                    # par type d'action sans exploser en une entrée par nom
                    # de source.
                    "path":         {"type": "keyword"},
                    "path_params":  {"type": "object", "enabled": False},
                    "body":         {"type": "object", "enabled": False},
                    "status_code":  {"type": "integer"},
                }
            }
        })
        logger.info(f"Index '{AUDIT_LOG_INDEX}' créé.")
    _index_ready = True


def _redact(body: dict) -> dict:
    return {k: ("***" if k.lower() in _SENSITIVE_KEYS else v) for k, v in body.items()}


def log_action(
    es: Elasticsearch, *, username: str, method: str, path: str,
    path_params: dict, body: dict | None, status_code: int,
) -> None:
    """Enregistre une action admin. Ne lève jamais d'exception — un échec
    d'écriture du journal ne doit jamais faire échouer l'action admin
    elle-même (appelé après coup par le middleware, une fois la réponse
    déjà produite)."""
    try:
        _ensure_index(es)
        es.index(index=AUDIT_LOG_INDEX, document={
            "timestamp":    datetime.now(timezone.utc).isoformat(),
            "username":     username,
            "method":       method,
            "path":         path,
            "path_params":  path_params or {},
            "body":         _redact(body) if body else {},
            "status_code":  status_code,
        })
    except Exception as e:
        logger.warning(f"[audit_log] Échec d'écriture : {e}")


def list_actions(es: Elasticsearch, *, size: int, from_: int) -> dict:
    """Liste paginée, plus récentes d'abord — pour la page /stats.html."""
    try:
        res = es.search(
            index=AUDIT_LOG_INDEX,
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
