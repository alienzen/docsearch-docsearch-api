# ⚠️  COPIE SYNCHRONISÉE depuis docsearch-ingestion/app/runtime_config.py — ces
# deux fichiers doivent rester identiques (même clé Redis, même
# logique). Toute modification doit être répercutée dans les DEUX
# dépôts. Dupliqué ici (plutôt qu'importé) car docsearch-api ne peut
# pas dépendre du code de docsearch-ingestion dans l'architecture
# multi-dépôts — Redis reste la seule source de vérité partagée.

# runtime_config.py — Paramètres opérationnels modifiables à chaud
#
# Complète filetype_config.py (dédié aux extensions/tailles) pour les
# autres réglages qui bénéficient d'être ajustables sans redémarrage :
# limites d'archives, cadence de flush du worker, intervalle de
# surveillance du watcher.
#
# Même principe : une clé Redis unique en JSON, cache local, repli sur
# les variables d'environnement (elles-mêmes avec valeur par défaut)
# si Redis est injoignable.
#
# Certains réglages ne peuvent pas être "vraiment" pris en compte sans
# petite action côté appelant (ex: le watcher doit redémarrer son
# observateur si watcher_poll_interval change, une Kafka
# max_poll_records ne peut pas changer sans recréer le consumer) —
# ces cas sont documentés au point d'usage plutôt qu'ici.

import os
import json
import time
import logging

logger = logging.getLogger(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
RUNTIME_CONFIG_KEY = "docsearch:config:runtime"
RUNTIME_CACHE_TTL  = int(os.getenv("RUNTIME_CONFIG_CACHE_TTL", "10"))

# Valeurs par défaut — reprennent les variables d'environnement
# existantes (elles-mêmes avec une valeur de repli) comme valeurs de
# départ. Une fois modifiés via set_param(), les réglages vivent dans
# Redis et les variables d'environnement ne servent plus que de valeur
# de repli si Redis est injoignable.
DEFAULT_RUNTIME = {
    "archive_max_files":         int(os.getenv("ARCHIVE_MAX_FILES", "5000")),
    "archive_max_total_size_mb": int(os.getenv("ARCHIVE_MAX_TOTAL_SIZE_MB", "1000")),
    "archive_max_depth":         int(os.getenv("ARCHIVE_MAX_DEPTH", "1")),
    "worker_batch_size":         int(os.getenv("WORKER_BATCH_SIZE", "200")),
    "worker_flush_interval":     int(os.getenv("WORKER_FLUSH_INTERVAL", "10")),
    "watcher_poll_interval":     int(os.getenv("WATCHER_POLL_INTERVAL", "10")),
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
                f"[runtime_config] Redis injoignable ({e}) — "
                f"repli sur la configuration par défaut (variables d'environnement)."
            )
            _redis_unavailable_logged = True
        _redis_client = None
        return None


def get_runtime_config() -> dict:
    """Retourne la config runtime — cache local, sinon Redis, sinon défaut."""
    global _cache, _cache_time

    now = time.time()
    if _cache and (now - _cache_time) < RUNTIME_CACHE_TTL:
        return _cache

    client = _get_redis_client()
    if client is not None:
        try:
            raw = client.get(RUNTIME_CONFIG_KEY)
            if raw:
                # Fusion avec les défauts : une clé absente de Redis
                # (nouveau paramètre ajouté après coup, par exemple)
                # retombe sur sa valeur par défaut plutôt que de planter.
                merged = dict(DEFAULT_RUNTIME)
                merged.update(json.loads(raw))
                _cache = merged
                _cache_time = now
                return _cache
        except Exception as e:
            logger.warning(f"[runtime_config] Erreur lecture Redis : {e} — repli sur défaut")

    _cache = dict(DEFAULT_RUNTIME)
    _cache_time = now
    return _cache


def get_param(key: str, default=None):
    """Raccourci pour lire un seul paramètre."""
    return get_runtime_config().get(key, default if default is not None else DEFAULT_RUNTIME.get(key))


def set_param(key: str, value) -> dict:
    """
    Modifie un paramètre et le persiste immédiatement dans Redis.
    Lève une exception si Redis est injoignable (une écriture doit
    être fiable, pas de sens à "faire semblant" d'avoir sauvegardé).
    """
    if key not in DEFAULT_RUNTIME:
        raise ValueError(
            f"Paramètre inconnu : '{key}'. Valeurs possibles : "
            f"{', '.join(DEFAULT_RUNTIME.keys())}"
        )

    client = _get_redis_client()
    if client is None:
        raise RuntimeError(
            "Redis injoignable — impossible d'enregistrer la configuration. "
            "Vérifiez que le service redis tourne (docker compose ps redis)."
        )

    raw = client.get(RUNTIME_CONFIG_KEY)
    config = dict(DEFAULT_RUNTIME)
    if raw:
        config.update(json.loads(raw))

    # Conserve le type d'origine (int/float) quand c'est possible,
    # pour éviter qu'une valeur saisie en chaîne casse les comparaisons
    # numériques (ex: len(buffer) >= "10" lèverait une exception).
    original_type = type(DEFAULT_RUNTIME[key])
    try:
        config[key] = original_type(value)
    except (TypeError, ValueError):
        config[key] = value

    client.set(RUNTIME_CONFIG_KEY, json.dumps(config))

    global _cache, _cache_time
    _cache = config
    _cache_time = time.time()

    return config


def reset_to_default() -> dict:
    """
    Réinitialise tous les paramètres opérationnels à DEFAULT_RUNTIME,
    écrasant tout réglage modifié via set_param(). Utile pour revenir
    d'un coup à un état connu plutôt que de réajuster chaque paramètre
    un par un.
    """
    client = _get_redis_client()
    if client is None:
        raise RuntimeError(
            "Redis injoignable — impossible d'enregistrer la configuration. "
            "Vérifiez que le service redis tourne (docker compose ps redis)."
        )

    config = dict(DEFAULT_RUNTIME)
    client.set(RUNTIME_CONFIG_KEY, json.dumps(config))

    global _cache, _cache_time
    _cache = config
    _cache_time = time.time()

    return config
