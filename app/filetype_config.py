# ⚠️  COPIE SYNCHRONISÉE depuis docsearch-ingestion/app/filetype_config.py —
# ces deux fichiers doivent rester identiques (même clés Redis, même
# logique). Toute modification doit être répercutée dans les DEUX
# dépôts. Dupliqué ici (plutôt qu'importé) car docsearch-api ne peut
# pas dépendre du code de docsearch-ingestion dans l'architecture
# multi-dépôts — Redis reste la seule source de vérité partagée.

# filetype_config.py — Configuration dynamique des types de fichiers, par source
#
# Permet d'activer/désactiver des extensions et de fixer une taille
# maximale par type, SANS redémarrer producer.py / worker.py / watcher.py.
# Chaque SOURCE (file_sources_config.py) a sa propre configuration,
# indépendante des autres — un même type de fichier peut par exemple
# être autorisé pour "documents" et désactivé pour "finance".
#
# Stockage : une clé Redis par source ("docsearch:config:filetypes" pour
# la source par défaut, "docsearch:config:filetypes:<source>" pour les
# autres — voir _redis_key), contenant chacune un objet JSON. Même
# principe que path_filter.py.
#
# Résilience : si Redis est injoignable ou la clé absente, repli sur
# DEFAULT_CONFIG (codé en dur, identique pour toutes les sources) —
# l'ingestion ne s'arrête jamais à cause d'un problème de configuration.
#
# Cache local : la config de chaque source est relue depuis Redis au
# plus une fois toutes les CONFIG_CACHE_TTL secondes (défaut 10s), pour
# ne pas taper Redis à chaque fichier traité tout en restant réactif
# aux changements.

import os
import json
import time
import logging

from file_sources_config import DEFAULT_SOURCE_NAME

logger = logging.getLogger(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
CONFIG_KEY_BASE = "docsearch:config:filetypes"
CONFIG_CACHE_TTL = int(os.getenv("FILETYPE_CONFIG_CACHE_TTL", "10"))

# Configuration par défaut — utilisée si Redis est injoignable ou si la
# clé n'existe pas encore (premier démarrage), pour n'importe quelle
# source. Reprend les extensions historiquement supportées (ancienne
# constante SUPPORTED) avec des tailles max raisonnables. "default"
# s'applique à toute extension non listée explicitement (désactivée par
# défaut, comme avant).
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
    # Archives — la clé correspond à archive_extractor.archive_kind(),
    # PAS à path.suffix (qui donnerait ".gz" pour "x.tar.gz", pas
    # "tar.gz"). max_size_mb ici limite la taille du FICHIER ARCHIVE
    # lui-même avant extraction — distinct de archive_max_total_size_mb
    # (runtime_config.py) qui limite la taille décompressée totale.
    "zip":     {"enabled": True,  "max_size_mb": 500},
    "tar":     {"enabled": True,  "max_size_mb": 500},
    "tar.gz":  {"enabled": True,  "max_size_mb": 500},
    "tgz":     {"enabled": True,  "max_size_mb": 500},
    "tar.bz2": {"enabled": True,  "max_size_mb": 500},
    "tbz2":    {"enabled": True,  "max_size_mb": 500},
    "tar.xz":  {"enabled": True,  "max_size_mb": 500},
    "txz":     {"enabled": True,  "max_size_mb": 500},
    "7z":      {"enabled": True,  "max_size_mb": 500},
    "default": {"enabled": False, "max_size_mb": 10},
}

# Cache et client par process — indexés par source, comme path_filter.py
# (chaque source a sa propre config, indépendante des autres).
_cache: dict[str, dict] = {}
_cache_time: dict[str, float] = {}
_redis_client = None
_redis_unavailable_logged = False


def _redis_key(source: str) -> str:
    # La source par défaut garde la clé historique (sans suffixe) pour
    # ne pas perdre la configuration déjà en place sur une installation
    # existante lors de la mise à jour vers le multi-source.
    if source == DEFAULT_SOURCE_NAME:
        return CONFIG_KEY_BASE
    return f"{CONFIG_KEY_BASE}:{source}"


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


def get_config(source: str = DEFAULT_SOURCE_NAME) -> dict:
    """
    Retourne la configuration par type de fichier pour `source` — depuis
    le cache local si encore valide, sinon relue depuis Redis (avec
    repli sur défaut).
    """
    now = time.time()
    if source in _cache and (now - _cache_time.get(source, 0)) < CONFIG_CACHE_TTL:
        return _cache[source]

    client = _get_redis_client()
    if client is not None:
        try:
            raw = client.get(_redis_key(source))
            if raw:
                _cache[source] = json.loads(raw)
                _cache_time[source] = now
                return _cache[source]
        except Exception as e:
            logger.warning(f"[filetype_config] Erreur lecture Redis : {e} — repli sur défaut")

    # Rien en cache, Redis injoignable ou clé absente : défaut
    _cache[source] = DEFAULT_CONFIG
    _cache_time[source] = now
    return _cache[source]


def set_filetype(
    extension: str, enabled: bool | None = None, max_size_mb: float | None = None,
    source: str = DEFAULT_SOURCE_NAME,
) -> dict:
    """
    Met à jour (ou crée) l'entrée d'une extension dans la config de
    `source`, et la persiste immédiatement dans Redis. Lève une
    exception si Redis est injoignable (contrairement à la lecture,
    l'écriture doit être fiable — pas de sens à "faire semblant" d'avoir
    sauvegardé un réglage).
    """
    extension = extension.lower().lstrip(".")
    client = _get_redis_client()
    if client is None:
        raise RuntimeError(
            "Redis injoignable — impossible d'enregistrer la configuration. "
            "Vérifiez que le service redis tourne (docker compose ps redis)."
        )

    key = _redis_key(source)
    raw = client.get(key)
    config = json.loads(raw) if raw else dict(DEFAULT_CONFIG)

    current = config.get(extension, dict(DEFAULT_CONFIG["default"]))
    if enabled is not None:
        current["enabled"] = enabled
    if max_size_mb is not None:
        current["max_size_mb"] = max_size_mb
    config[extension] = current

    client.set(key, json.dumps(config))

    _cache[source] = config
    _cache_time[source] = time.time()

    return config


def reset_to_default(source: str = DEFAULT_SOURCE_NAME) -> dict:
    """
    Réinitialise la configuration de `source` à DEFAULT_CONFIG, écrasant
    les extensions custom ajoutées ainsi que toute activation/taille
    modifiée sur les extensions par défaut. Utile pour revenir d'un coup
    à un état connu plutôt que de supprimer/réajuster chaque entrée une
    par une. N'affecte pas les autres sources.
    """
    client = _get_redis_client()
    if client is None:
        raise RuntimeError(
            "Redis injoignable — impossible d'enregistrer la configuration. "
            "Vérifiez que le service redis tourne (docker compose ps redis)."
        )

    config = dict(DEFAULT_CONFIG)
    client.set(_redis_key(source), json.dumps(config))

    _cache[source] = config
    _cache_time[source] = time.time()

    return config


def remove_filetype(extension: str, source: str = DEFAULT_SOURCE_NAME) -> dict:
    """
    Retire complètement une extension de la configuration de `source` —
    contrairement à set_filetype(enabled=False) qui la garde désactivée
    dans la liste, ceci fait disparaître l'entrée (utile pour annuler
    l'ajout d'une extension custom). "default" ne peut pas être
    supprimée : c'est la règle de repli pour toute extension non listée.
    """
    extension = extension.lower().lstrip(".")
    if extension == "default":
        raise ValueError("l'entrée 'default' ne peut pas être supprimée")

    client = _get_redis_client()
    if client is None:
        raise RuntimeError(
            "Redis injoignable — impossible d'enregistrer la configuration. "
            "Vérifiez que le service redis tourne (docker compose ps redis)."
        )

    key = _redis_key(source)
    raw = client.get(key)
    config = json.loads(raw) if raw else dict(DEFAULT_CONFIG)
    config.pop(extension, None)
    client.set(key, json.dumps(config))

    _cache[source] = config
    _cache_time[source] = time.time()

    return config


def is_allowed(extension: str, size_bytes: int, source: str = DEFAULT_SOURCE_NAME) -> tuple[bool, str]:
    """
    Vérifie si un fichier de cette extension et cette taille doit être
    indexé selon la configuration courante de `source`.

    Retourne (autorisé: bool, raison: str) — la raison est toujours
    renseignée (même en cas d'autorisation) pour faciliter le logging.
    """
    config = get_config(source)
    ext = extension.lower().lstrip(".")
    rule = config.get(ext, config.get("default", DEFAULT_CONFIG["default"]))

    if not rule.get("enabled", False):
        return False, f"extension .{ext} désactivée (source '{source}')"

    max_size_mb = rule.get("max_size_mb")
    if max_size_mb is not None:
        max_bytes = max_size_mb * 1024 * 1024
        if size_bytes > max_bytes:
            size_mb = size_bytes / (1024 * 1024)
            return False, (
                f"fichier .{ext} trop volumineux "
                f"({size_mb:.1f} Mo > limite {max_size_mb} Mo, source '{source}')"
            )

    return True, "autorisé"


def get_enabled_extensions(source: str = DEFAULT_SOURCE_NAME) -> set[str]:
    """Retourne l'ensemble des extensions actuellement activées (avec le
    point) pour `source`."""
    config = get_config(source)
    return {f".{ext}" for ext, rule in config.items()
            if ext != "default" and rule.get("enabled", False)}
