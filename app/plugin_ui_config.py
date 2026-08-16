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
#                           "icone": "fr-icon-chat-3-line"}],
#                  "admin_panel": [{"cle": "poll", "type": "texte", ...}],
#                  "reglages": {"poll": "300"},
#                  "restart_requis": false}}
#
# `admin_panel` est la DÉCLARATION (ce que le module veut régler, figé au
# manifeste) ; `reglages` sont les VALEURS (ce que l'administrateur a
# choisi). Les deux sont séparés parce qu'une mise à jour du module
# remplace la première sans devoir écraser la seconde.
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


def panneaux() -> dict:
    """Déclarations et valeurs de tous les modules, pour l'écran
    d'administration. Contrairement à entrees_de_menu(), un module
    DÉSACTIVÉ y figure : on doit pouvoir le régler avant de l'allumer."""
    resultat = {}
    for nom, module in _raw().items():
        resultat[nom] = {
            "enabled":        bool(module.get("enabled", False)),
            "admin_panel":    module.get("admin_panel") or [],
            "reglages":       module.get("reglages") or {},
            "restart_requis": bool(module.get("restart_requis", False)),
        }
    return resultat


def variables_env(nom: str) -> dict:
    """Réglages sous leur forme finale : {DOCSEARCH_OPT_X: "valeur"}.

    Lu par « manage.sh plugin » au moment d'écrire l'unité — c'est le
    seul chemin par lequel un réglage atteint un module, qui ne voit ni
    Redis ni Elasticsearch."""
    module = _raw().get(nom) or {}
    valeurs = module.get("reglages") or {}
    return {
        reglage["variable"]: valeurs.get(reglage["cle"], reglage.get("defaut", ""))
        for reglage in (module.get("admin_panel") or [])
    }


def set_reglages(nom: str, valeurs: dict) -> dict:
    """Enregistre les valeurs choisies par l'administrateur.

    Chaque valeur est normalisée par le CONTRAT selon le type déclaré :
    l'API ne réinvente pas la conversion, et une valeur qu'un module ne
    saurait pas relire est refusée ici plutôt qu'écrite dans une unité.

    Marque le module « à redémarrer » : les variables d'environnement
    d'un conteneur sont fixées à sa création, la nouvelle valeur ne
    prendra effet qu'au prochain démarrage. Le taire donnerait un réglage
    enregistré et sans effet — la panne silencieuse qu'on évite partout
    ailleurs dans ce produit."""
    from docsearch_contract import interface as contract_interface

    client = _get_redis_client()
    if client is None:
        raise RuntimeError("Redis injoignable — réglages non enregistrés.")
    brut = client.get(PLUGIN_UI_KEY)
    modules = json.loads(brut) if brut else {}
    module = modules.get(nom)
    if module is None:
        raise KeyError(f"Module inconnu : '{nom}'")

    declares = {r["cle"]: r for r in (module.get("admin_panel") or [])}
    inconnus = sorted(set(valeurs) - set(declares))
    if inconnus:
        raise ValueError(
            f"Réglage(s) non déclaré(s) par « {nom} » : {', '.join(inconnus)}."
        )

    retenues = dict(module.get("reglages") or {})
    for cle, valeur in valeurs.items():
        retenues[cle] = contract_interface.normaliser_valeur(declares[cle]["type"], valeur, cle)

    module["reglages"] = retenues
    module["restart_requis"] = True
    modules[nom] = module
    client.set(PLUGIN_UI_KEY, json.dumps(modules))
    _invalider()
    return module


def marquer_applique(nom: str) -> dict:
    """Efface le drapeau « à redémarrer » — appelé par manage.sh une fois
    l'unité réécrite ET le module relancé, jamais par l'API : c'est
    l'application réelle qui l'éteint, pas l'intention de l'appliquer."""
    client = _get_redis_client()
    if client is None:
        raise RuntimeError("Redis injoignable.")
    brut = client.get(PLUGIN_UI_KEY)
    modules = json.loads(brut) if brut else {}
    if nom in modules:
        modules[nom]["restart_requis"] = False
        client.set(PLUGIN_UI_KEY, json.dumps(modules))
        _invalider()
    return modules


def _accroches_actives(cle: str) -> list[dict]:
    """Accroches d'un type donné, pour les modules ACTIFS seulement.

    Un module arrêté ne laisse ni entrée de menu, ni action sur les
    cartes, ni page : elles mèneraient toutes à un 502."""
    resultat = []
    for nom, module in _raw().items():
        if not module.get("enabled", False):
            continue
        for entree in module.get(cle) or []:
            resultat.append({
                "module":  nom,
                "libelle": entree.get("libelle", ""),
                "chemin":  entree.get("chemin", ""),
                "icone":   entree.get("icone"),
            })
    return sorted(resultat, key=lambda e: e["libelle"].lower())


def actions_de_resultat() -> list[dict]:
    """Liens posés sur chaque carte de résultat par les modules actifs."""
    return _accroches_actives("result_action")


def pages() -> list[dict]:
    """Écrans de module, encadrés par l'interface du produit."""
    return _accroches_actives("page")


def enregistrer(nom: str, nav: list[dict], admin_panel: list[dict] | None = None,
                result_action: list[dict] | None = None, page: list[dict] | None = None,
                enabled: bool = False) -> dict:
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
    declaration = admin_panel or []
    # Les VALEURS déjà choisies survivent à une mise à jour du module, et
    # seules celles dont la clé reste déclarée : un réglage retiré du
    # manifeste ne doit pas rester dans une unité.
    cles = {r["cle"] for r in declaration}
    reglages = {k: v for k, v in (ancien.get("reglages") or {}).items() if k in cles}
    for reglage in declaration:
        reglages.setdefault(reglage["cle"], reglage.get("defaut", ""))
    modules[nom] = {
        "enabled":        ancien.get("enabled", enabled),
        "nav":            nav,
        "result_action":  result_action or [],
        "page":           page or [],
        "admin_panel":    declaration,
        "reglages":       reglages,
        # Une déclaration qui change réclame une réécriture d'unité.
        "restart_requis": ancien.get("admin_panel") != declaration or ancien.get("restart_requis", False),
    }
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
