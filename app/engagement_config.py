# engagement_config.py — Suspension des fonctionnalités de mesure de
# satisfaction (pouce haut/bas par recherche, popup NPS périodique)
#
# Propre à docsearch-api (pas de copie dans docsearch-ingestion) : ces
# réglages ne concernent que l'interface de recherche et l'API qui la
# sert, aucun processus d'ingestion n'en a besoin.
#
# Même principe que runtime_config.py (clé Redis unique en JSON, cache
# local, repli sur des valeurs par défaut si Redis est injoignable) mais
# des booléens purs plutôt que des nombres — évite le piège de
# runtime_config.set_param() où un cast `bool("false")` vaudrait True
# (chaîne non vide). Ici, la validation Pydantic du corps de requête
# (FastAPI) garantit déjà un vrai bool avant d'arriver ici.
#
# Le tracking de clic n'a PAS de flag ici : c'est un signal passif (aucun
# widget, aucune friction pour l'utilisateur), il reste toujours actif.

import os
import json
import time
import logging

logger = logging.getLogger(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
ENGAGEMENT_CONFIG_KEY = "docsearch:config:engagement"
ENGAGEMENT_CACHE_TTL  = int(os.getenv("ENGAGEMENT_CONFIG_CACHE_TTL", "10"))

DEFAULT_ENGAGEMENT = {
    "feedback_enabled": True,   # pouce haut/bas après chaque recherche
    "nps_enabled":      True,   # popup "recommanderiez-vous...", périodique
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
                f"[engagement_config] Redis injoignable ({e}) — "
                f"repli sur la configuration par défaut (tout activé)."
            )
            _redis_unavailable_logged = True
        _redis_client = None
        return None


def get_config() -> dict:
    """Retourne {feedback_enabled, nps_enabled} — cache local, sinon
    Redis, sinon défaut (tout activé)."""
    global _cache, _cache_time

    now = time.time()
    if _cache and (now - _cache_time) < ENGAGEMENT_CACHE_TTL:
        return _cache

    client = _get_redis_client()
    if client is not None:
        try:
            raw = client.get(ENGAGEMENT_CONFIG_KEY)
            if raw:
                # Fusion avec les défauts : un nouveau flag ajouté après
                # coup et absent de Redis retombe sur sa valeur par
                # défaut plutôt que de faire disparaître la clé.
                merged = dict(DEFAULT_ENGAGEMENT)
                merged.update(json.loads(raw))
                _cache = merged
                _cache_time = now
                return _cache
        except Exception as e:
            logger.warning(f"[engagement_config] Erreur lecture Redis : {e} — repli sur défaut")

    _cache = dict(DEFAULT_ENGAGEMENT)
    _cache_time = now
    return _cache


def set_param(key: str, value: bool) -> dict:
    """Modifie un flag (feedback_enabled ou nps_enabled) et le persiste
    immédiatement dans Redis."""
    if key not in DEFAULT_ENGAGEMENT:
        raise ValueError(
            f"Paramètre inconnu : '{key}'. Valeurs possibles : "
            f"{', '.join(DEFAULT_ENGAGEMENT.keys())}"
        )

    client = _get_redis_client()
    if client is None:
        raise RuntimeError(
            "Redis injoignable — impossible d'enregistrer la configuration. "
            "Vérifiez que le service redis tourne (docker compose ps redis)."
        )

    raw = client.get(ENGAGEMENT_CONFIG_KEY)
    config = dict(DEFAULT_ENGAGEMENT)
    if raw:
        config.update(json.loads(raw))
    config[key] = bool(value)

    client.set(ENGAGEMENT_CONFIG_KEY, json.dumps(config))

    global _cache, _cache_time
    _cache = config
    _cache_time = time.time()

    return config
