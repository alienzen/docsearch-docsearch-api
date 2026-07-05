# ⚠️  COPIE SYNCHRONISÉE depuis docsearch-ingestion/app/filetype_config.py — ces
# deux fichiers doivent rester identiques (même clé Redis, même
# logique). Toute modification doit être répercutée dans les DEUX
# dépôts. Dupliqué ici (plutôt qu'importé) car docsearch-api ne peut
# pas dépendre du code de docsearch-ingestion dans l'architecture
# multi-dépôts — Redis reste la seule source de vérité partagée.

# filetype_config.py — Configuration dynamique des types de fichiers
#
# Permet d'activer/désactiver des extensions et de fixer une taille
# maximale par type, SANS redémarrer producer.py / worker.py / watcher.py.
#
# Stockage : une seule clé Redis ("docsearch:config:filetypes") contenant
# un objet JSON. Redis est déjà une dépendance du stack (worker/api en
# dépendent tous les deux) — aucune nouvelle brique n'est ajoutée.
#
# Résilience : si Redis est injoignable ou la clé absente, repli sur
# DEFAULT_CONFIG (codé en dur) — l'ingestion ne s'arrête jamais à cause
# d'un problème de configuration.
#
# Cache local : la config est relue depuis Redis au plus une fois toutes
# les CONFIG_CACHE_TTL secondes (défaut 10s), pour ne pas taper Redis à
# chaque fichier traité tout en restant réactif aux changements.

import os
import json
import time
import logging

logger = logging.getLogger(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
CONFIG_KEY = "docsearch:config:filetypes"
CONFIG_CACHE_TTL = int(os.getenv("FILETYPE_CONFIG_CACHE_TTL", "10"))

# Configuration par défaut — utilisée si Redis est injoignable ou si la
# clé n'existe pas encore (premier démarrage). Reprend les extensions
# historiquement supportées (ancienne constante SUPPORTED) avec des
# tailles max raisonnables. "default" s'applique à toute extension non
# listée explicitement (désactivée par défaut, comme avant).
DEFAULT_CONFIG = {
    "pdf":     {"enabled": True,  "max_size_mb": 50},
    "docx":    {"enabled": True,  "max_size_mb": 20},
    "doc":     {"enabled": True,  "max_size_mb": 20},
    "pptx":    {"enabled": True,  "max_size_mb": 100},
    "ppt":     {"enabled": True,  "max_size_mb": 100},
    "xlsx":    {"enabled": True,  "max_size_mb": 30},
    "xls":     {"enabled": True,  "max_size_mb": 30},
    "txt":     {"enabled": True,  "max_size_mb": 5},
    "pst":     {"enabled": True,  "max_size_mb": 2000},
    "default": {"enabled": False, "max_size_mb": 10},
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
                f"[filetype_config] Redis injoignable ({e}) — "
                f"repli sur la configuration par défaut codée en dur."
            )
            _redis_unavailable_logged = True
        _redis_client = None
        return None


def get_config() -> dict:
    """
    Retourne la configuration par type de fichier — depuis le cache local
    si encore valide, sinon relue depuis Redis (avec repli sur défaut).
    """
    global _cache, _cache_time

    now = time.time()
    if _cache and (now - _cache_time) < CONFIG_CACHE_TTL:
        return _cache

    client = _get_redis_client()
    if client is not None:
        try:
            raw = client.get(CONFIG_KEY)
            if raw:
                _cache = json.loads(raw)
                _cache_time = now
                return _cache
        except Exception as e:
            logger.warning(f"[filetype_config] Erreur lecture Redis : {e} — repli sur défaut")

    # Rien en cache, Redis injoignable ou clé absente : défaut
    _cache = DEFAULT_CONFIG
    _cache_time = now
    return _cache


def set_filetype(extension: str, enabled: bool | None = None, max_size_mb: float | None = None) -> dict:
    """
    Met à jour (ou crée) l'entrée d'une extension dans la config, et la
    persiste immédiatement dans Redis. Lève une exception si Redis est
    injoignable (contrairement à la lecture, l'écriture doit être fiable —
    pas de sens à "faire semblant" d'avoir sauvegardé un réglage).
    """
    extension = extension.lower().lstrip(".")
    client = _get_redis_client()
    if client is None:
        raise RuntimeError(
            "Redis injoignable — impossible d'enregistrer la configuration. "
            "Vérifiez que le service redis tourne (docker compose ps redis)."
        )

    raw = client.get(CONFIG_KEY)
    config = json.loads(raw) if raw else dict(DEFAULT_CONFIG)

    current = config.get(extension, dict(DEFAULT_CONFIG["default"]))
    if enabled is not None:
        current["enabled"] = enabled
    if max_size_mb is not None:
        current["max_size_mb"] = max_size_mb
    config[extension] = current

    client.set(CONFIG_KEY, json.dumps(config))

    global _cache, _cache_time
    _cache = config
    _cache_time = time.time()

    return config


def is_allowed(extension: str, size_bytes: int) -> tuple[bool, str]:
    """
    Vérifie si un fichier de cette extension et cette taille doit être
    indexé selon la configuration courante.

    Retourne (autorisé: bool, raison: str) — la raison est toujours
    renseignée (même en cas d'autorisation) pour faciliter le logging.
    """
    config = get_config()
    ext = extension.lower().lstrip(".")
    rule = config.get(ext, config.get("default", DEFAULT_CONFIG["default"]))

    if not rule.get("enabled", False):
        return False, f"extension .{ext} désactivée"

    max_size_mb = rule.get("max_size_mb")
    if max_size_mb is not None:
        max_bytes = max_size_mb * 1024 * 1024
        if size_bytes > max_bytes:
            size_mb = size_bytes / (1024 * 1024)
            return False, (
                f"fichier .{ext} trop volumineux "
                f"({size_mb:.1f} Mo > limite {max_size_mb} Mo)"
            )

    return True, "autorisé"


def get_enabled_extensions() -> set[str]:
    """Retourne l'ensemble des extensions actuellement activées (avec le point)."""
    config = get_config()
    return {f".{ext}" for ext, rule in config.items()
            if ext != "default" and rule.get("enabled", False)}
