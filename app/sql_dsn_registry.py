# sql_dsn_registry.py — Registre de DSN SQL chiffrés (ajout dynamique via
# le panneau d'administration, alternative aux variables d'environnement
# de sql_sources_config.py / .env)
#
# Port de docsearch-ingestion/app/sql_dsn_registry.py — dupliqué à
# l'identique ici, même rationale que sql_sources_config.py (impossible
# d'importer un autre dépôt dans l'architecture multi-dépôts). Ce module
# importe en revanche _ENV_VAR_RE depuis sql_sources_config.py, qui VIT
# DANS LE MÊME DÉPÔT que ce fichier (import intra-dépôt normal, pas une
# exception à la règle ci-dessus) — évite de dupliquer une troisième fois
# la même règle de forme de nom.
# COPIE SYNCHRONISÉE — toute modification doit être répercutée dans les
# DEUX dépôts (docsearch-api ET docsearch-ingestion).
#
# Contexte : connection_ref (voir sql_sources_config.py) est le NOM d'une
# variable d'environnement contenant le DSN complet — mécanisme
# intentionnellement inchangé, mais qui impose un accès .env + recréation
# de conteneur pour tout nouveau DSN. Ce module ajoute un second registre,
# 100% dynamique (aucune recréation de conteneur), pour les DSN ajoutés
# depuis le panneau d'administration : le DSN est chiffré (Fernet,
# symétrique) avant d'être stocké dans Redis, sous la clé
# DSN_ENCRYPTION_KEY — jamais en clair, ni dans Redis ni dans une réponse
# API après enregistrement (seul un "hint" — schéma + hôte, sans
# identifiants — reste consultable).
#
# PRIORITÉ : sql_indexer._resolve_dsn() essaie TOUJOURS d'abord la
# variable d'environnement correspondant à connection_ref ; ce registre
# n'est consulté qu'en repli, si aucune variable de ce nom n'existe.
# Aucune régression pour les déploiements existants : ce module est
# entièrement inerte tant qu'aucun DSN n'y est ajouté depuis l'admin.
#
# Stockage (clé Redis "docsearch:config:sql_dsns") :
#   {"clients_pg_dsn": {
#       "ciphertext": "gAAAAABm...",  # jeton Fernet, jamais le DSN en clair
#       "hint": "postgresql+psycopg2://dbhost.internal:5432/clients"
#   }}
# Le "hint" est calculé UNE FOIS à l'écriture (jamais recalculé par
# déchiffrement) : lister les DSN enregistrés ne nécessite donc jamais
# DSN_ENCRYPTION_KEY, seul resolve_dsn() (déchiffrement réel) en a besoin.

import os
import json
import time
import logging
from urllib.parse import urlsplit

from sql_sources_config import _ENV_VAR_RE as _NAME_RE

logger = logging.getLogger(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
SQL_DSNS_KEY = "docsearch:config:sql_dsns"
SQL_DSN_REGISTRY_CACHE_TTL = int(os.getenv("SQL_DSN_REGISTRY_CACHE_TTL", "10"))

_cache: dict = {}
_cache_time: float = 0.0
_redis_client = None
_redis_unavailable_logged = False

_fernet = None
_fernet_key_seen: str | None = None


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
                f"[sql_dsn_registry] Redis injoignable ({e}) — aucun DSN "
                f"dynamique disponible tant qu'il reste injoignable."
            )
            _redis_unavailable_logged = True
        _redis_client = None
        return None


def _raw_dsns() -> dict:
    """Retourne le dict brut {name: {ciphertext, hint}} — cache local,
    sinon Redis, sinon vide (jamais d'exception ici : un DSN dynamique
    absent ou Redis injoignable est un repli silencieux côté ingestion,
    voir resolve_dsn())."""
    global _cache, _cache_time
    now = time.time()
    if (now - _cache_time) < SQL_DSN_REGISTRY_CACHE_TTL:
        return _cache
    client = _get_redis_client()
    if client is not None:
        try:
            raw = client.get(SQL_DSNS_KEY)
            _cache = json.loads(raw) if raw else {}
            _cache_time = now
            return _cache
        except Exception as e:
            logger.warning(f"[sql_dsn_registry] Erreur lecture Redis : {e} — repli sur vide")
    _cache = {}
    _cache_time = now
    return _cache


def _read_write(mutate) -> dict:
    client = _get_redis_client()
    if client is None:
        raise RuntimeError(
            "Redis injoignable — impossible d'enregistrer le DSN. "
            "Vérifiez que le service redis tourne (docker compose ps redis)."
        )
    raw = client.get(SQL_DSNS_KEY)
    dsns = json.loads(raw) if raw else {}
    mutate(dsns)
    client.set(SQL_DSNS_KEY, json.dumps(dsns))
    global _cache, _cache_time
    _cache = dsns
    _cache_time = time.time()
    return dsns


def _validate_name(name: str) -> None:
    if not _NAME_RE.match(name):
        raise ValueError(
            f"Nom de DSN invalide : '{name}' — attendu un nom de variable "
            f"d'environnement (majuscules, chiffres, underscore, ex: "
            f"'CLIENTS_PG_DSN'), la même forme qu'un connection_ref classique "
            f"(voir sql_sources_config._validate_connection_ref)."
        )


def _derive_hint(dsn: str) -> str:
    """Schéma + hôte[:port][/base], JAMAIS d'identifiants — urlsplit sépare
    nativement user:password de host:port pour les deux formes couvertes
    par _DSN_PREFIXES (sql_indexer.py) : postgresql[+driver]:// et
    mysql[+driver]://. Retourne un indice générique plutôt que de lever
    une exception ici (la validation stricte est faite séparément par
    add_dsn)."""
    try:
        parts = urlsplit(dsn)
    except ValueError:
        return "(DSN non standard)"
    if not parts.scheme or not parts.hostname:
        return "(DSN non standard)"
    netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
    return f"{parts.scheme}://{netloc}{parts.path}"


def _get_fernet():
    """Instancie (et met en cache) le Fernet correspondant à
    DSN_ENCRYPTION_KEY — même idiome que sql_indexer._get_engine() :
    rebuild uniquement si la clé change entre deux appels (rotation)."""
    global _fernet, _fernet_key_seen
    key = os.getenv("DSN_ENCRYPTION_KEY", "")
    if not key:
        raise RuntimeError(
            "DSN_ENCRYPTION_KEY absente — impossible de chiffrer/déchiffrer un "
            "DSN dynamique. Générer une clé : python -c \"from cryptography."
            "fernet import Fernet; print(Fernet.generate_key().decode())\" puis "
            "la définir dans docsearch-infra/.env (voir .env.example)."
        )
    if _fernet is not None and key == _fernet_key_seen:
        return _fernet
    from cryptography.fernet import Fernet
    try:
        instance = Fernet(key.encode("ascii"))
    except Exception as e:
        raise RuntimeError(
            f"DSN_ENCRYPTION_KEY invalide ({e}) — attendu une clé Fernet "
            f"générée par Fernet.generate_key() (32 octets urlsafe-base64)."
        )
    _fernet, _fernet_key_seen = instance, key
    return _fernet


def list_names() -> list[dict]:
    """[{"name": ..., "hint": ...}, ...] — ne retourne JAMAIS le DSN
    déchiffré ni le ciphertext. Ne nécessite PAS DSN_ENCRYPTION_KEY (le
    hint est stocké en clair, précalculé à l'écriture) — reste consultable
    même si la clé a été retirée/tournée depuis."""
    raw = _raw_dsns()
    return [{"name": name, "hint": entry.get("hint", "")} for name, entry in sorted(raw.items())]


def add_dsn(name: str, dsn: str) -> dict:
    """Chiffre et enregistre un DSN sous ce nom (remplace un DSN existant
    du même nom). `name` doit avoir la forme d'un nom de variable
    d'environnement — c'est le nom que l'admin choisira ensuite comme
    `connection_ref` d'une source SQL. Aucune connexion à la base n'est
    testée ici (seule la FORME du DSN est vérifiée via urlsplit, sans
    dépendance driver — voir _derive_hint). Lève ValueError si le nom ou
    le DSN sont invalides, RuntimeError si Redis ou DSN_ENCRYPTION_KEY
    sont indisponibles."""
    _validate_name(name)
    if not dsn or not dsn.strip():
        raise ValueError("Le DSN ne peut pas être vide.")
    dsn = dsn.strip()
    hint = _derive_hint(dsn)
    if hint == "(DSN non standard)":
        raise ValueError(
            "DSN invalide : forme attendue "
            "'postgresql+psycopg2://user:motdepasse@host:port/base' ou "
            "'mysql+pymysql://user:motdepasse@host:port/base' (schéma et "
            "hôte obligatoires)."
        )
    fernet = _get_fernet()
    ciphertext = fernet.encrypt(dsn.encode("utf-8")).decode("ascii")

    def mutate(dsns):
        dsns[name] = {"ciphertext": ciphertext, "hint": hint}

    _read_write(mutate)
    return {"name": name, "hint": hint}


def remove_dsn(name: str) -> list[dict]:
    """Retire un DSN du registre. Retourne list_names() APRÈS suppression —
    contrairement à sql_sources_config.remove_source() (qui retourne le
    dict brut, sans donnée sensible), ce module ne doit JAMAIS renvoyer le
    dict interne {ciphertext, hint} tel quel : list_names() est la seule
    forme sûre à retourner en sortie de fonction publique."""
    def mutate(dsns):
        if name not in dsns:
            raise KeyError(f"DSN inconnu : '{name}'")
        dsns.pop(name, None)

    _read_write(mutate)
    return list_names()


def resolve_dsn(name: str) -> str | None:
    """Utilisé UNIQUEMENT côté docsearch-ingestion (sql_indexer._resolve_dsn),
    en repli quand aucune variable d'environnement de ce nom n'existe.
    Retourne None si le nom est absent du registre (repli normal, pas une
    erreur) ou si Redis est injoignable (dégradation silencieuse, cohérente
    avec le reste du polling SQL — ne doit jamais faire planter
    sql_worker.py). Lève RuntimeError seulement si le nom EXISTE mais que
    DSN_ENCRYPTION_KEY est absente/invalide ou que le déchiffrement échoue
    (clé tournée depuis l'enregistrement) — une vraie erreur de
    configuration, à ne pas avaler silencieusement."""
    entry = _raw_dsns().get(name)
    if entry is None:
        return None
    fernet = _get_fernet()
    from cryptography.fernet import InvalidToken
    try:
        return fernet.decrypt(entry["ciphertext"].encode("ascii")).decode("utf-8")
    except InvalidToken as e:
        raise RuntimeError(
            f"Impossible de déchiffrer le DSN dynamique '{name}' — "
            f"DSN_ENCRYPTION_KEY a-t-elle changé depuis son enregistrement ?"
        ) from e
