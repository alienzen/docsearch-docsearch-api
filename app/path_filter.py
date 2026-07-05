# ⚠️  COPIE SYNCHRONISÉE depuis docsearch-ingestion/app/path_filter.py — ces
# deux fichiers doivent rester identiques (même clé Redis, même
# logique). Toute modification doit être répercutée dans les DEUX
# dépôts. Dupliqué ici (plutôt qu'importé) car docsearch-api ne peut
# pas dépendre du code de docsearch-ingestion dans l'architecture
# multi-dépôts — Redis reste la seule source de vérité partagée.

# path_filter.py — Inclusion / exclusion de sous-dossiers par motifs glob
#
# Permet d'exclure ou de restreindre l'indexation à certains sous-dossiers
# de DOCS_FOLDER, modifiable à chaud (Redis), sans redémarrage. Même
# principe que filetype_config.py et runtime_config.py.
#
# Structure stockée (clé Redis "docsearch:config:pathfilters") :
#   {"excluded": ["motif1", "motif2", ...], "included": [...]}
#
# Règles :
#   - Les chemins sont TOUJOURS relatifs à DOCS_FOLDER (jamais absolus)
#   - "excluded" est prioritaire sur "included" : un chemin exclu reste
#     exclu même s'il correspond aussi à un motif inclus
#   - Si "included" est vide -> tout est autorisé (sous réserve de ne pas
#     matcher "excluded"). Si "included" contient au moins un motif,
#     seuls les chemins qui y correspondent sont autorisés (liste blanche)
#   - Motif SANS "/" (ex: "tmp", "*.cache") : correspond à un composant
#     de chemin à N'IMPORTE QUEL niveau de profondeur (comme gitignore)
#   - Motif AVEC "/" (ex: "finance/confidentiel") : ancré — correspond au
#     chemin complet ou à un préfixe de dossier (exclure un dossier
#     exclut automatiquement tout son contenu, pas seulement les
#     fichiers directement dedans)

import os
import json
import time
import fnmatch
import logging

logger = logging.getLogger(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
PATHFILTER_KEY = "docsearch:config:pathfilters"
PATHFILTER_CACHE_TTL = int(os.getenv("PATHFILTER_CACHE_TTL", "10"))

DEFAULT_CONFIG = {"excluded": [], "included": []}

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
                f"[path_filter] Redis injoignable ({e}) — "
                f"repli sur 'aucun filtre' (tout est indexé)."
            )
            _redis_unavailable_logged = True
        _redis_client = None
        return None


def get_config() -> dict:
    """Retourne {"excluded": [...], "included": [...]} — cache local,
    sinon Redis, sinon aucun filtre (tout est autorisé)."""
    global _cache, _cache_time

    now = time.time()
    if _cache and (now - _cache_time) < PATHFILTER_CACHE_TTL:
        return _cache

    client = _get_redis_client()
    if client is not None:
        try:
            raw = client.get(PATHFILTER_KEY)
            if raw:
                merged = dict(DEFAULT_CONFIG)
                merged.update(json.loads(raw))
                _cache = merged
                _cache_time = now
                return _cache
        except Exception as e:
            logger.warning(f"[path_filter] Erreur lecture Redis : {e} — repli sur défaut")

    _cache = dict(DEFAULT_CONFIG)
    _cache_time = now
    return _cache


def _normalize(rel_path: str) -> list[str]:
    rel_path = rel_path.replace(os.sep, "/").strip("/")
    return [p for p in rel_path.split("/") if p and p != "."]


def _matches_any(rel_path: str, patterns: list[str]) -> bool:
    if not patterns:
        return False
    parts = _normalize(rel_path)
    if not parts:
        return False
    full = "/".join(parts)

    for pattern in patterns:
        pattern = pattern.strip().strip("/")
        if not pattern:
            continue
        if "/" in pattern:
            # Motif ancré : chemin complet, ou préfixe de dossier
            # (exclure un dossier exclut tout ce qu'il contient)
            if fnmatch.fnmatch(full, pattern):
                return True
            for i in range(1, len(parts)):
                if fnmatch.fnmatch("/".join(parts[:i]), pattern):
                    return True
        else:
            # Motif simple : composant de chemin à n'importe quel niveau
            if any(fnmatch.fnmatch(part, pattern) for part in parts):
                return True
    return False


def is_path_allowed(rel_path: str) -> tuple[bool, str]:
    """
    Vérifie si un chemin (relatif à DOCS_FOLDER) doit être indexé.
    Retourne (autorisé: bool, raison: str).
    """
    config = get_config()
    excluded = config.get("excluded", [])
    included = config.get("included", [])

    if _matches_any(rel_path, excluded):
        return False, f"chemin exclu ('{rel_path}' correspond à un motif de la liste noire)"

    if included and not _matches_any(rel_path, included):
        return False, f"chemin hors liste blanche ('{rel_path}' ne correspond à aucun motif inclus)"

    return True, "autorisé"


def is_dir_excluded(rel_dir: str) -> bool:
    """
    Vérifie seulement l'exclusion (pas la liste blanche) — utilisé pour
    élaguer un parcours de dossier (os.walk) avant d'y descendre. La
    liste blanche n'est volontairement PAS utilisée pour l'élagage : un
    dossier "finance" ne correspond pas littéralement au motif inclus
    "finance/rapports", mais il faut quand même y descendre pour
    atteindre "finance/rapports". Seuls les fichiers sont filtrés
    contre la liste blanche (voir is_path_allowed), pas les dossiers
    parcourus.
    """
    config = get_config()
    return _matches_any(rel_dir, config.get("excluded", []))


def _read_write(mutate) -> dict:
    client = _get_redis_client()
    if client is None:
        raise RuntimeError(
            "Redis injoignable — impossible d'enregistrer la configuration. "
            "Vérifiez que le service redis tourne (docker compose ps redis)."
        )
    raw = client.get(PATHFILTER_KEY)
    config = dict(DEFAULT_CONFIG)
    if raw:
        config.update(json.loads(raw))
    config.setdefault("excluded", [])
    config.setdefault("included", [])

    mutate(config)

    client.set(PATHFILTER_KEY, json.dumps(config))
    global _cache, _cache_time
    _cache = config
    _cache_time = time.time()
    return config


def add_excluded(pattern: str) -> dict:
    def mutate(config):
        if pattern not in config["excluded"]:
            config["excluded"].append(pattern)
    return _read_write(mutate)


def add_included(pattern: str) -> dict:
    def mutate(config):
        if pattern not in config["included"]:
            config["included"].append(pattern)
    return _read_write(mutate)


def remove_filter(pattern: str) -> dict:
    """Retire un motif des deux listes (excluded et included) s'il y est."""
    def mutate(config):
        config["excluded"] = [p for p in config["excluded"] if p != pattern]
        config["included"] = [p for p in config["included"] if p != pattern]
    return _read_write(mutate)


def matches_pattern(rel_path: str, pattern: str) -> bool:
    """
    Version publique de _matches_any pour un seul motif — réutilisée
    par la commande de purge (indexer.py: purge_path) pour retrouver,
    parmi les documents déjà indexés, ceux qui correspondraient à un
    motif d'exclusion donné (même syntaxe glob que exclude-path).
    """
    return _matches_any(rel_path, [pattern])
