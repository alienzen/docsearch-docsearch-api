# plugin_ui_config.py — Accroches d'interface des modules installés
#
# COPIE SYNCHRONISÉE avec docsearch-ingestion (où `manage.sh plugin
# install` l'appelle dans un conteneur jetable pour ÉCRIRE). Comme
# plugin_sources_config.py, ce module n'est que de l'entrée/sortie Redis :
# le vocabulaire et la validation vivent dans le contrat partagé
# (docsearch_contract/interface.py).
#
# ── Pourquoi Redis et pas le manifeste installé ──────────────
#
# Les manifestes vivent dans /etc/docsearch/plugins de la machine où le
# module tourne — l'ingestion. L'API, elle, tourne sur le frontal et ne
# voit pas ce répertoire. Ce qui doit atteindre le NAVIGATEUR est donc
# écrit dans Redis à l'installation, exactement comme les sources : ce qui
# est machine-local reste sur disque, ce qui est commun à la grappe passe
# par Redis.
#
# Stockage (clé "docsearch:config:plugin_ui") :
#   {"assistant": {"enabled": true,
#                  "nav": [{"libelle": "Assistant", "chemin": "/ext/assistant/",
#                           "icone": "fr-icon-chat-3-line"}]}}
#
# `enabled` suit `plugin enable/disable` : un module arrêté ne laisse pas
# son entrée dans le menu de tout le monde, où elle mènerait à un 502.

import os
import json
import time
import logging

logger = logging.getLogger(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
PLUGIN_UI_KEY = "docsearch:config:plugin_ui"
PLUGIN_UI_CACHE_TTL = int(os.getenv("PLUGIN_UI_CACHE_TTL", "10"))

_cache: dict = {}
_cache_time: float = 0.0
_redis_client = None
_redis_unavailable_logged = False


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
                f"[plugin_ui_config] Redis injoignable ({e}) — aucune accroche "
                f"d'interface tant qu'il reste injoignable."
            )
            _redis_unavailable_logged = True
        _redis_client = None
        return None


def _raw() -> dict:
    """Repli FERMÉ sur vide : Redis injoignable retire les entrées de
    menu des modules plutôt que d'en afficher d'obsolètes. Une entrée
    manquante se remarque et se répare ; une entrée qui mène à un module
    retiré rend un 502 que personne ne sait interpréter."""
    global _cache, _cache_time
    now = time.time()
    if (now - _cache_time) < PLUGIN_UI_CACHE_TTL:
        return _cache

    client = _get_redis_client()
    if client is not None:
        try:
            brut = client.get(PLUGIN_UI_KEY)
            _cache = json.loads(brut) if brut else {}
            _cache_time = now
            return _cache
        except Exception as e:
            logger.warning(f"[plugin_ui_config] Erreur lecture Redis : {e} — repli sur vide")

    _cache = {}
    _cache_time = now
    return _cache


def entrees_de_menu() -> list[dict]:
    """Entrées de menu des modules ACTIFS, triées par libellé.

    Le tri n'est pas cosmétique : sans lui, l'ordre du menu dépendrait de
    l'ordre d'insertion dans Redis, donc de l'ordre d'installation — et
    changerait sous les yeux des utilisateurs à chaque mise à jour d'un
    module.
    """
    entrees = []
    for nom, module in _raw().items():
        if not module.get("enabled", False):
            continue
        for entree in module.get("nav") or []:
            entrees.append({
                "module":  nom,
                "libelle": entree.get("libelle", ""),
                "chemin":  entree.get("chemin", ""),
                "icone":   entree.get("icone"),
            })
    return sorted(entrees, key=lambda e: e["libelle"].lower())


def enregistrer(nom: str, nav: list[dict], enabled: bool = False) -> dict:
    """Écrit les accroches d'un module. Appelé par `manage.sh plugin
    install`, jamais par l'API — qui n'a aucune route d'écriture ici."""
    client = _get_redis_client()
    if client is None:
        raise RuntimeError("Redis injoignable — accroches d'interface non enregistrées.")
    brut = client.get(PLUGIN_UI_KEY)
    modules = json.loads(brut) if brut else {}
    # `enabled` n'est PAS écrasé pour un module déjà connu : une mise à
    # jour ne doit pas rallumer dans le menu un module qu'un
    # administrateur avait éteint (même règle que searchable pour les
    # sources).
    ancien = modules.get(nom) or {}
    modules[nom] = {"enabled": ancien.get("enabled", enabled), "nav": nav}
    client.set(PLUGIN_UI_KEY, json.dumps(modules))
    _invalider()
    return modules


def set_actif(nom: str, actif: bool) -> dict:
    client = _get_redis_client()
    if client is None:
        raise RuntimeError("Redis injoignable — état du module non modifié.")
    brut = client.get(PLUGIN_UI_KEY)
    modules = json.loads(brut) if brut else {}
    if nom in modules:
        modules[nom]["enabled"] = bool(actif)
        client.set(PLUGIN_UI_KEY, json.dumps(modules))
        _invalider()
    return modules


def retirer(nom: str) -> dict:
    client = _get_redis_client()
    if client is None:
        raise RuntimeError("Redis injoignable — accroches d'interface non retirées.")
    brut = client.get(PLUGIN_UI_KEY)
    modules = json.loads(brut) if brut else {}
    modules.pop(nom, None)
    client.set(PLUGIN_UI_KEY, json.dumps(modules))
    _invalider()
    return modules


def _invalider() -> None:
    global _cache_time
    _cache_time = 0.0
