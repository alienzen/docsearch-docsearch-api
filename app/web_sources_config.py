# web_sources_config.py — Registre dynamique des sources web
#
# Port de docsearch-ingestion/app/web_sources_config.py — dupliqué à
# l'identique ici (impossible d'importer un autre dépôt dans l'architecture
# multi-dépôts, même rationale que sql_sources_config.py). Ce module est
# totalement autonome (Redis uniquement, aucune dépendance vers le reste de
# l'ingestion) : il peut être copié tel quel entre les deux dépôts sans
# adaptation. COPIE SYNCHRONISÉE — toute modification doit être répercutée
# dans les DEUX dépôts (docsearch-api ET docsearch-ingestion).
#
# Une "source web" = un site crawlé par Elastic Open Web Crawler vers un
# index Elasticsearch INTERMÉDIAIRE (son propre schéma : url, title, body,
# headings, meta_description, last_crawled_at...), que web_indexer.py relit
# à intervalle régulier pour le transformer vers le schéma DocSearch
# (doc_id, filepath, content, acl) et l'indexer dans son propre index final.
# Même principe que sql_sources_config.py (registre vivant dans Redis, relu
# à chaud par web_worker.py, sans redémarrage de conteneur) mais un modèle
# différent : pas de requête SQL, une paire d'index ES (source brute du
# crawler -> index final DocSearch).
#
# Stockage (clé Redis "docsearch:config:web_sources") :
#   {"cc_decisions": {
#       "crawl_index": "cc_decisions_raw",
#       "es_index":    "cc_decisions",
#       "acl_public":  true,
#       "poll_interval_seconds": 3600
#   }}
#
# Le crawl lui-même (découverte d'URLs, respect de crawl_rules/robots.txt,
# extraction HTML) est entièrement délégué à Elastic Open Web Crawler —
# ce module ne fait QUE gérer le registre de la seconde étape (transfert
# crawl_index -> es_index). Voir web_indexer.py pour cette transformation.
#
# Contrairement à file_sources_config.py, il n'existe PAS de source par défaut :
# une installation sans source web enregistrée n'en a simplement aucune à
# traiter (web_worker.py tourne alors sans rien faire).

import os
import re
import json
import time
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
WEB_SOURCES_KEY = "docsearch:config:web_sources"
WEB_SOURCES_CACHE_TTL = int(os.getenv("WEB_SOURCES_CACHE_TTL", "10"))

DEFAULT_POLL_INTERVAL_SECONDS = 3600

# Nom de source/index valides : alphanumérique + tiret/underscore, jamais
# vide — même contrainte que file_sources_config.py / sql_sources_config.py.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Styles de mise en page des résultats disponibles pour cette source dans
# l'interface de recherche — voir file_sources_config.py:DISPLAY_STYLES
# (même ensemble, dupliqué ici comme le reste de ce module autonome).
DISPLAY_STYLES = {"default", "compact"}


@dataclass(frozen=True)
class WebSource:
    name: str
    crawl_index: str     # index ES intermédiaire écrit par Elastic Open Web Crawler
    es_index: str         # index ES final DocSearch (rejoint ES_SEARCH_ALIAS)
    acl_public: bool
    poll_interval_seconds: int
    label: str = ""
    searchable: bool = True
    collectable: bool = True
    description: str = ""
    paused: bool = False  # web_worker.py saute cette source tant que True (voir set_paused)
    display_style: str = "default"


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
                f"[web_sources_config] Redis injoignable ({e}) — "
                f"aucune source web disponible tant qu'il reste injoignable."
            )
            _redis_unavailable_logged = True
        _redis_client = None
        return None


def _raw_sources() -> dict:
    """Retourne le dict brut {name: {...}} — cache local, sinon Redis,
    sinon vide (pas de repli "source par défaut", comme pour le SQL)."""
    global _cache, _cache_time

    now = time.time()
    if (now - _cache_time) < WEB_SOURCES_CACHE_TTL:
        return _cache

    client = _get_redis_client()
    if client is not None:
        try:
            raw = client.get(WEB_SOURCES_KEY)
            _cache = json.loads(raw) if raw else {}
            _cache_time = now
            return _cache
        except Exception as e:
            logger.warning(f"[web_sources_config] Erreur lecture Redis : {e} — repli sur vide")

    _cache = {}
    _cache_time = now
    return _cache


def _to_source(name: str, entry: dict) -> WebSource:
    return WebSource(
        name=name,
        crawl_index=entry["crawl_index"],
        es_index=entry["es_index"],
        acl_public=bool(entry.get("acl_public", True)),
        poll_interval_seconds=int(entry.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS)),
        label=entry.get("label") or name,
        searchable=entry.get("searchable", True),
        collectable=entry.get("collectable", True),
        description=entry.get("description") or "",
        paused=entry.get("paused", False),
        display_style=entry.get("display_style") or "default",
    )


def get_sources() -> dict[str, WebSource]:
    """Retourne toutes les sources web enregistrées, {name: WebSource}."""
    return {name: _to_source(name, entry) for name, entry in _raw_sources().items()}


def get_source(name: str) -> WebSource:
    """Retourne une source web par son nom. Lève KeyError si inconnue."""
    raw = _raw_sources()
    if name not in raw:
        raise KeyError(
            f"Source web inconnue : '{name}'. Sources disponibles : {', '.join(raw.keys()) or '(aucune)'}"
        )
    return _to_source(name, raw[name])


def _validate_name(name: str, label: str) -> None:
    if not _NAME_RE.match(name):
        raise ValueError(
            f"{label} invalide : '{name}' — attendu : lettres minuscules, "
            f"chiffres, '-' ou '_', commençant par une lettre/chiffre."
        )


def _read_write(mutate) -> dict:
    client = _get_redis_client()
    if client is None:
        raise RuntimeError(
            "Redis injoignable — impossible d'enregistrer la configuration. "
            "Vérifiez que le service redis tourne (docker compose ps redis)."
        )
    raw = client.get(WEB_SOURCES_KEY)
    sources = json.loads(raw) if raw else {}

    mutate(sources)

    client.set(WEB_SOURCES_KEY, json.dumps(sources))
    global _cache, _cache_time
    _cache = sources
    _cache_time = time.time()
    return sources


def add_source(
    name: str, crawl_index: str, es_index: str,
    acl_public: bool = True, poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
    label: str | None = None, searchable: bool = True, collectable: bool = True,
    description: str | None = None,
) -> dict:
    """
    Enregistre une nouvelle source web (ou met à jour une source existante
    du même nom). `crawl_index` doit correspondre à `output_index` de la
    config Elastic Open Web Crawler pour ce site — jamais le même index que
    `es_index` (l'un est le format brut du crawler, l'autre le schéma
    DocSearch final).

    ATTENTION : REMPLACE entièrement l'entrée existante (pas de fusion
    partielle) — un appelant qui réenregistre une source déjà configurée
    doit relire `searchable`/`collectable` au préalable (voir
    /admin/all-sources) et les repasser explicitement, sous peine de les
    réinitialiser à True.
    """
    _validate_name(name, "Nom de source")
    _validate_name(crawl_index, "Nom d'index de crawl")
    _validate_name(es_index, "Nom d'index Elasticsearch")
    if crawl_index == es_index:
        raise ValueError(
            "'crawl_index' et 'es_index' doivent être différents — le premier reçoit le "
            "format brut du crawler, le second le schéma DocSearch transformé."
        )
    if poll_interval_seconds < 30:
        raise ValueError("poll_interval_seconds doit être >= 30.")

    def mutate(sources):
        for other_name, other in sources.items():
            if other_name != name and other.get("es_index") == es_index:
                raise ValueError(
                    f"L'index '{es_index}' est déjà utilisé par la source web '{other_name}'."
                )
        # Vérifie aussi contre les sources fichiers et SQL — un même index
        # partagé entre deux types de sources mélangerait deux schémas
        # incompatibles dans le même index.
        from file_sources_config import get_sources as get_file_sources
        from sql_sources_config import get_sources as get_sql_sources
        for other_name, other in get_file_sources().items():
            if other.es_index == es_index:
                raise ValueError(
                    f"L'index '{es_index}' est déjà utilisé par la source fichier '{other_name}'."
                )
        for other_name, other in get_sql_sources().items():
            if other.es_index == es_index:
                raise ValueError(
                    f"L'index '{es_index}' est déjà utilisé par la source SQL '{other_name}'."
                )
        sources[name] = {
            "crawl_index":            crawl_index,
            "es_index":               es_index,
            "acl_public":             acl_public,
            "poll_interval_seconds":  poll_interval_seconds,
            "label":                  label or name,
            "searchable":             searchable,
            "collectable":            collectable,
            "description":            description or "",
        }

    return _read_write(mutate)


def set_searchable(name: str, searchable: bool) -> dict:
    """
    Active/désactive la RECHERCHE pour une source web, sans toucher à
    l'ingestion : web_worker.py continue de synchroniser crawl_index vers
    es_index normalement, seuls ses documents cessent d'apparaître dans
    /search (docsearch-api).
    """
    def mutate(sources):
        if name not in sources:
            raise KeyError(f"Source web inconnue : '{name}'")
        sources[name]["searchable"] = searchable

    return _read_write(mutate)


def set_collectable(name: str, collectable: bool) -> dict:
    """Active/désactive l'ajout des documents de cette source web à une
    collection — voir file_sources_config.set_collectable() pour le
    détail, même principe."""
    def mutate(sources):
        if name not in sources:
            raise KeyError(f"Source web inconnue : '{name}'")
        sources[name]["collectable"] = collectable

    return _read_write(mutate)


def set_display_style(name: str, display_style: str) -> dict:
    """Change le style d'affichage des résultats de cette source web —
    voir file_sources_config.set_display_style() pour le détail, même
    principe."""
    if display_style not in DISPLAY_STYLES:
        raise ValueError(
            f"Style d'affichage invalide : '{display_style}'. "
            f"Valeurs possibles : {', '.join(sorted(DISPLAY_STYLES))}"
        )

    def mutate(sources):
        if name not in sources:
            raise KeyError(f"Source web inconnue : '{name}'")
        sources[name]["display_style"] = display_style

    return _read_write(mutate)


def set_paused(name: str, paused: bool) -> dict:
    """
    Suspend/reprend le CRAWL pour une source web — tant que True,
    web_worker.py saute cette source à chaque tick (voir web_worker.py),
    donc crawl_index n'est plus relu ni transformé vers es_index. Ne
    pilote PAS le conteneur Elastic Open Web Crawler lui-même (ce module
    n'a aucune visibilité Docker) : si ce conteneur tourne en mode
    "schedule" en continu, il continue d'écrire dans crawl_index — seule
    la RÉPERCUSSION vers DocSearch est mise en pause. Les documents déjà
    dans es_index restent cherchables (contrairement à searchable=False).
    """
    def mutate(sources):
        if name not in sources:
            raise KeyError(f"Source web inconnue : '{name}'")
        sources[name]["paused"] = paused

    return _read_write(mutate)


def remove_source(name: str) -> dict:
    """
    Retire une source web du registre — web_worker.py arrête de la
    synchroniser. NE supprime PAS les index Elasticsearch (ni le crawl_index,
    ni l'es_index) ni les documents déjà indexés.
    """
    def mutate(sources):
        if name not in sources:
            raise KeyError(f"Source web inconnue : '{name}'")
        sources.pop(name, None)

    return _read_write(mutate)


def set_label(name: str, label: str) -> dict:
    """
    Modifie le LIBELLÉ d'affichage d'une source web, sans toucher à son
    nom (clé de registre), son crawl_index ni son es_index —
    contrairement à l'ancien rename_source(), le nom qui identifie la
    source dans le registre et dans le champ "source" des documents déjà
    indexés ne change jamais, donc aucune répercussion sur l'index ES
    n'est nécessaire ici.
    """
    if not label.strip():
        raise ValueError("Le libellé ne peut pas être vide.")

    def mutate(sources):
        if name not in sources:
            raise KeyError(f"Source web inconnue : '{name}'")
        sources[name]["label"] = label.strip()

    return _read_write(mutate)


def set_description(name: str, description: str) -> dict:
    """Modifie la description d'une source web (texte libre, affiché
    dans l'admin — n'affecte ni l'ingestion ni la recherche)."""

    def mutate(sources):
        if name not in sources:
            raise KeyError(f"Source web inconnue : '{name}'")
        sources[name]["description"] = description.strip()

    return _read_write(mutate)
