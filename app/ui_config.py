# ui_config.py — Bascules de fonctionnalités d'interface, suspendables
# depuis l'admin (distinct d'engagement_config.py, dédié à la mesure de
# satisfaction — celui-ci couvre des éléments d'interface sans rapport,
# comme la visibilité du lien "Assistant IA").
#
# Propre à docsearch-api (pas de copie dans docsearch-ingestion) : ces
# réglages ne concernent que l'interface de recherche et l'API qui la
# sert, aucun processus d'ingestion n'en a besoin.
#
# Même principe que engagement_config.py (clé Redis unique en JSON,
# cache local, repli sur des valeurs par défaut si Redis est injoignable,
# booléens purs validés côté FastAPI avant d'arriver ici).

import os
import json
import time
import logging

logger = logging.getLogger(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
UI_CONFIG_KEY = "docsearch:config:ui"
UI_CONFIG_CACHE_TTL = int(os.getenv("UI_CONFIG_CACHE_TTL", "10"))

DEFAULT_UI_CONFIG = {
    "chat_enabled":        True,   # lien "Assistant IA" dans l'en-tête de recherche
    "footer_enabled":      True,   # pied de page de la page de recherche
    "admin_links_enabled": True,   # liens "Administration"/"Statistiques" (en-tête + pied de page) —
                                    # combiné en ET logique avec /is-admin côté index.html : un
                                    # utilisateur admin ne les voit QUE si ce flag est aussi actif.
    "export_enabled":      True,   # boutons "Exporter en XLSX/DOCX" des résultats de recherche —
                                    # ne bloque que l'affichage côté index.html ; POST /search/export
                                    # reste refusé côté API si ce flag est désactivé (voir search_api.py).
    "help_enabled":        True,   # lien "❓ Aide" dans l'en-tête de recherche + raccourci clavier "?" —
                                    # ne bloque pas l'accès direct à /help.html (page statique sans
                                    # donnée sensible), seulement sa mise en avant dans l'UI.
    "collections_enabled": True,   # sélection de documents + collections personnelles ("📋 Mes
                                    # collections") — désactivé, TOUTES les routes /collections
                                    # renvoient 403 (voir search_api.py) : les collections déjà
                                    # créées restent dans leur index ES, simplement inaccessibles
                                    # tant que le flag est désactivé.
}

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
                f"[ui_config] Redis injoignable ({e}) — "
                f"repli sur la configuration par défaut (tout activé)."
            )
            _redis_unavailable_logged = True
        _redis_client = None
        return None


def get_config() -> dict:
    """Retourne {chat_enabled} — cache local, sinon Redis, sinon défaut
    (tout activé)."""
    global _cache, _cache_time

    now = time.time()
    if _cache and (now - _cache_time) < UI_CONFIG_CACHE_TTL:
        return _cache

    client = _get_redis_client()
    if client is not None:
        try:
            raw = client.get(UI_CONFIG_KEY)
            if raw:
                # Fusion avec les défauts : un nouveau flag ajouté après
                # coup et absent de Redis retombe sur sa valeur par
                # défaut plutôt que de faire disparaître la clé.
                merged = dict(DEFAULT_UI_CONFIG)
                merged.update(json.loads(raw))
                _cache = merged
                _cache_time = now
                return _cache
        except Exception as e:
            logger.warning(f"[ui_config] Erreur lecture Redis : {e} — repli sur défaut")

    _cache = dict(DEFAULT_UI_CONFIG)
    _cache_time = now
    return _cache


def set_param(key: str, value: bool) -> dict:
    """Modifie un flag (ex: chat_enabled) et le persiste immédiatement
    dans Redis."""
    if key not in DEFAULT_UI_CONFIG:
        raise ValueError(
            f"Paramètre inconnu : '{key}'. Valeurs possibles : "
            f"{', '.join(DEFAULT_UI_CONFIG.keys())}"
        )

    client = _get_redis_client()
    if client is None:
        raise RuntimeError(
            "Redis injoignable — impossible d'enregistrer la configuration. "
            "Vérifiez que le service redis tourne (docker compose ps redis)."
        )

    raw = client.get(UI_CONFIG_KEY)
    config = dict(DEFAULT_UI_CONFIG)
    if raw:
        config.update(json.loads(raw))
    config[key] = bool(value)

    client.set(UI_CONFIG_KEY, json.dumps(config))

    global _cache, _cache_time
    _cache = config
    _cache_time = time.time()

    return config
