# sql_sources_config.py — Registre dynamique des sources SQL
#
# Port de docsearch-ingestion/app/sql_sources_config.py — dupliqué à
# l'identique ici (impossible d'importer un autre dépôt dans
# l'architecture multi-dépôts, même rationale que file_sources_config.py /
# admin_scan.py). Ce module est totalement autonome (Redis + variables
# d'environnement, aucune dépendance vers le reste de l'ingestion) : il
# peut être copié tel quel entre les deux dépôts sans adaptation.
# COPIE SYNCHRONISÉE — toute modification doit être répercutée dans les
# DEUX dépôts (docsearch-api ET docsearch-ingestion).
#
# Une "source SQL" = une requête SELECT sur une base PostgreSQL ou MySQL,
# dont chaque ligne devient un document dans son propre index Elasticsearch.
# Même principe que file_sources_config.py (registre vivant dans Redis, relu à
# chaud par sql_worker.py, sans redémarrage de conteneur) mais un modèle
# différent : pas de dossier/watcher filesystem, une requête + un mapping
# explicite de colonnes + un identifiant de connexion.
#
# Stockage (clé Redis "docsearch:config:sql_sources") :
#   {"clients": {
#       "db_type": "postgresql",
#       "connection_ref": "CLIENTS_DB_DSN",
#       "query": "SELECT id, nom, email FROM clients WHERE actif = true",
#       "id_column": "id",
#       "es_index": "clients_sql",
#       "poll_interval_seconds": 300,
#       "fields": [
#           {"column": "id",    "es_field": "id",    "es_type": "keyword"},
#           {"column": "nom",   "es_field": "nom",   "es_type": "text", "analyzer": "french"},
#           {"column": "email", "es_field": "email", "es_type": "keyword"}
#       ]
#   }}
#
# Sécurité — IMPORTANT : `connection_ref` est le NOM d'une variable
# d'environnement contenant le DSN complet (utilisateur/mot de passe
# inclus), jamais le DSN lui-même. Le DSN ne transite donc jamais par
# Redis ni par ce module — docsearch-api n'a d'ailleurs jamais besoin de
# résoudre le DSN (aucune connexion SQL n'est faite depuis l'API :
# seul sql-worker/sql_indexer.py, côté docsearch-ingestion, se connecte
# réellement aux bases). Voir docsearch-ingestion/app/sql_indexer.py.
#
# Contrairement à file_sources_config.py, il n'existe PAS de source par défaut
# : une installation sans source SQL enregistrée n'en a simplement aucune
# à traiter (sql_worker.py tourne alors sans rien faire).

import os
import re
import json
import time
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
SQL_SOURCES_KEY = "docsearch:config:sql_sources"
SQL_SOURCES_CACHE_TTL = int(os.getenv("SQL_SOURCES_CACHE_TTL", "10"))

DEFAULT_POLL_INTERVAL_SECONDS = 300

SUPPORTED_DB_TYPES = ("postgresql", "mysql")
SUPPORTED_ES_TYPES = ("keyword", "text", "long", "double", "date", "boolean")

# Nom de source/index/colonne valides : alphanumérique + tiret/underscore,
# jamais vide — même contrainte que file_sources_config.py, pour les mêmes
# raisons (évite qu'un nom mal formé finisse comme composant d'une clé
# Redis ou d'un nom d'index ES).
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
# Nom de variable d'environnement : convention shell classique.
_ENV_VAR_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


@dataclass(frozen=True)
class FieldMapping:
    column: str      # nom de colonne tel que renvoyé par la requête SQL
    es_field: str    # nom du champ dans le document Elasticsearch
    es_type: str     # type ES (keyword, text, long, double, date, boolean)
    analyzer: str | None = None   # uniquement pertinent si es_type == "text"


@dataclass(frozen=True)
class SqlSource:
    name: str
    db_type: str
    connection_ref: str
    query: str
    id_column: str
    es_index: str
    poll_interval_seconds: int
    label: str = ""
    searchable: bool = True
    collectable: bool = True
    description: str = ""
    fields: tuple[FieldMapping, ...] = field(default_factory=tuple)


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
                f"[sql_sources_config] Redis injoignable ({e}) — "
                f"aucune source SQL disponible tant qu'il reste injoignable."
            )
            _redis_unavailable_logged = True
        _redis_client = None
        return None


def _raw_sources() -> dict:
    """Retourne le dict brut {name: {...}} — cache local, sinon Redis,
    sinon vide (pas de repli "source par défaut" contrairement aux
    sources fichiers : une base SQL n'a pas d'équivalent raisonnable)."""
    global _cache, _cache_time

    now = time.time()
    if (now - _cache_time) < SQL_SOURCES_CACHE_TTL:
        return _cache

    client = _get_redis_client()
    if client is not None:
        try:
            raw = client.get(SQL_SOURCES_KEY)
            _cache = json.loads(raw) if raw else {}
            _cache_time = now
            return _cache
        except Exception as e:
            logger.warning(f"[sql_sources_config] Erreur lecture Redis : {e} — repli sur vide")

    _cache = {}
    _cache_time = now
    return _cache


def _to_source(name: str, entry: dict) -> SqlSource:
    fields = tuple(
        FieldMapping(
            column=f["column"], es_field=f["es_field"], es_type=f["es_type"],
            analyzer=f.get("analyzer"),
        )
        for f in entry.get("fields", [])
    )
    return SqlSource(
        name=name,
        db_type=entry["db_type"],
        connection_ref=entry["connection_ref"],
        query=entry["query"],
        id_column=entry["id_column"],
        es_index=entry["es_index"],
        poll_interval_seconds=int(entry.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS)),
        label=entry.get("label") or name,
        searchable=entry.get("searchable", True),
        collectable=entry.get("collectable", True),
        description=entry.get("description") or "",
        fields=fields,
    )


def get_sources() -> dict[str, SqlSource]:
    """Retourne toutes les sources SQL enregistrées, {name: SqlSource}."""
    return {name: _to_source(name, entry) for name, entry in _raw_sources().items()}


def get_source(name: str) -> SqlSource:
    """Retourne une source SQL par son nom. Lève KeyError si inconnue."""
    raw = _raw_sources()
    if name not in raw:
        raise KeyError(
            f"Source SQL inconnue : '{name}'. Sources disponibles : {', '.join(raw.keys()) or '(aucune)'}"
        )
    return _to_source(name, raw[name])


def _validate_name(name: str, label: str) -> None:
    if not _NAME_RE.match(name):
        raise ValueError(
            f"{label} invalide : '{name}' — attendu : lettres minuscules, "
            f"chiffres, '-' ou '_', commençant par une lettre/chiffre."
        )


def _validate_fields(fields: list[dict], id_column: str) -> list[dict]:
    if not fields:
        raise ValueError("Le mapping 'fields' ne peut pas être vide.")

    columns = set()
    for f in fields:
        if "column" not in f or "es_field" not in f or "es_type" not in f:
            raise ValueError(
                f"Entrée de mapping invalide (attendu column/es_field/es_type) : {f}"
            )
        if f["es_type"] not in SUPPORTED_ES_TYPES:
            raise ValueError(
                f"Type ES invalide pour la colonne '{f['column']}' : '{f['es_type']}' — "
                f"valeurs possibles : {', '.join(SUPPORTED_ES_TYPES)}"
            )
        if f.get("analyzer") and f["es_type"] != "text":
            raise ValueError(
                f"'analyzer' n'a de sens que pour es_type='text' (colonne '{f['column']}')"
            )
        columns.add(f["column"])

    if id_column not in columns:
        raise ValueError(
            f"id_column '{id_column}' doit apparaître dans 'fields' (colonnes mappées : "
            f"{', '.join(sorted(columns))})"
        )
    return fields


def _validate_connection_ref(connection_ref: str) -> None:
    if not _ENV_VAR_RE.match(connection_ref):
        raise ValueError(
            f"connection_ref invalide : '{connection_ref}' — attendu un nom de variable "
            f"d'environnement (majuscules, chiffres, underscore, ex: 'CLIENTS_DB_DSN')."
        )


def _read_write(mutate) -> dict:
    client = _get_redis_client()
    if client is None:
        raise RuntimeError(
            "Redis injoignable — impossible d'enregistrer la configuration. "
            "Vérifiez que le service redis tourne (docker compose ps redis)."
        )
    raw = client.get(SQL_SOURCES_KEY)
    sources = json.loads(raw) if raw else {}

    mutate(sources)

    client.set(SQL_SOURCES_KEY, json.dumps(sources))
    global _cache, _cache_time
    _cache = sources
    _cache_time = time.time()
    return sources


def add_source(
    name: str, db_type: str, connection_ref: str, query: str, id_column: str,
    es_index: str, fields: list[dict], poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
    label: str | None = None, searchable: bool = True, collectable: bool = True,
    description: str | None = None,
) -> dict:
    """
    Enregistre une nouvelle source SQL (ou met à jour une source
    existante du même nom). `fields` est la liste de mapping explicite
    colonne -> champ ES (voir _validate_fields) — aucune colonne
    renvoyée par `query` mais absente de cette liste ne sera indexée.

    ATTENTION : REMPLACE entièrement l'entrée existante (pas de fusion
    partielle) — un appelant qui réenregistre une source déjà configurée
    doit relire `searchable`/`collectable` au préalable (voir
    /admin/all-sources) et les repasser explicitement, sous peine de les
    réinitialiser à True.
    """
    _validate_name(name, "Nom de source")
    _validate_name(es_index, "Nom d'index Elasticsearch")
    _validate_connection_ref(connection_ref)
    if db_type not in SUPPORTED_DB_TYPES:
        raise ValueError(
            f"db_type invalide : '{db_type}' — valeurs possibles : {', '.join(SUPPORTED_DB_TYPES)}"
        )
    if not query.strip():
        raise ValueError("La requête SQL ('query') ne peut pas être vide.")
    fields = _validate_fields(fields, id_column)
    if poll_interval_seconds < 10:
        raise ValueError("poll_interval_seconds doit être >= 10 (évite de marteler la base).")

    def mutate(sources):
        for other_name, other in sources.items():
            if other_name != name and other.get("es_index") == es_index:
                raise ValueError(
                    f"L'index '{es_index}' est déjà utilisé par la source SQL '{other_name}'."
                )
        # Vérifie aussi contre les sources FICHIERS (file_sources_config.py) —
        # un même index partagé entre une source fichier et une source
        # SQL mélangerait deux mappings incompatibles dans le même index.
        # Import différé : évite une dépendance circulaire au chargement
        # du module (file_sources_config.py n'importe jamais celui-ci).
        from file_sources_config import get_sources as get_file_sources
        for other_name, other in get_file_sources().items():
            if other.es_index == es_index:
                raise ValueError(
                    f"L'index '{es_index}' est déjà utilisé par la source fichier '{other_name}'."
                )
        sources[name] = {
            "db_type":               db_type,
            "connection_ref":        connection_ref,
            "query":                 query,
            "id_column":             id_column,
            "es_index":              es_index,
            "poll_interval_seconds": poll_interval_seconds,
            "label":                 label or name,
            "searchable":            searchable,
            "collectable":           collectable,
            "description":           description or "",
            "fields":                fields,
        }

    return _read_write(mutate)


def set_searchable(name: str, searchable: bool) -> dict:
    """
    Active/désactive la RECHERCHE pour une source SQL, sans toucher à
    l'ingestion : sql_worker.py continue d'interroger la base à son
    intervalle normal, seuls ses documents cessent d'apparaître dans
    /search (docsearch-api).
    """
    def mutate(sources):
        if name not in sources:
            raise KeyError(f"Source SQL inconnue : '{name}'")
        sources[name]["searchable"] = searchable

    return _read_write(mutate)


def set_collectable(name: str, collectable: bool) -> dict:
    """Active/désactive l'ajout des documents de cette source SQL à une
    collection — voir file_sources_config.set_collectable() pour le
    détail, même principe."""
    def mutate(sources):
        if name not in sources:
            raise KeyError(f"Source SQL inconnue : '{name}'")
        sources[name]["collectable"] = collectable

    return _read_write(mutate)


def remove_source(name: str) -> dict:
    """
    Retire une source SQL du registre — sql_worker.py arrête de
    l'interroger. NE supprime PAS l'index Elasticsearch ni les documents
    déjà indexés (cohérent avec file_sources_config.remove_source).
    """
    def mutate(sources):
        if name not in sources:
            raise KeyError(f"Source SQL inconnue : '{name}'")
        sources.pop(name, None)

    return _read_write(mutate)


def set_label(name: str, label: str) -> dict:
    """
    Modifie le LIBELLÉ d'affichage d'une source SQL, sans toucher à son
    nom (clé de registre), sa connexion, sa requête ni son es_index —
    contrairement à l'ancien rename_source(), le nom qui identifie la
    source dans le registre et dans le champ "source" des documents déjà
    indexés ne change jamais, donc aucune répercussion sur l'index ES
    n'est nécessaire ici.
    """
    if not label.strip():
        raise ValueError("Le libellé ne peut pas être vide.")

    def mutate(sources):
        if name not in sources:
            raise KeyError(f"Source SQL inconnue : '{name}'")
        sources[name]["label"] = label.strip()

    return _read_write(mutate)


def set_description(name: str, description: str) -> dict:
    """Modifie la description d'une source SQL (texte libre, affiché
    dans l'admin — n'affecte ni l'ingestion ni la recherche)."""

    def mutate(sources):
        if name not in sources:
            raise KeyError(f"Source SQL inconnue : '{name}'")
        sources[name]["description"] = description.strip()

    return _read_write(mutate)
