# plugin_sources_config.py — Registre dynamique des sources portées par
# un module complémentaire (plugin)
#
# COPIE SYNCHRONISÉE — toute modification doit être répercutée dans les
# DEUX dépôts (docsearch-api ET docsearch-ingestion), comme pour les
# trois registres natifs. Ce module ne fait QUE l'entrée/sortie Redis :
# le modèle, la validation et les politiques d'ACL vivent dans le contrat
# partagé (docsearch_contract/plugins.py), qui n'existe qu'en un
# exemplaire et dont la copie est vérifiée à chaque build. C'est la
# différence avec les trois registres natifs, dont les règles sont
# recopiées à la main.
#
# Une "source plugin" = un connecteur tiers qui pousse des documents DÉJÀ
# EXTRAITS sur le topic Kafka `documents-ready` ; plugin_worker.py les
# valide et plugin_indexer.py les indexe. À distinguer des trois autres
# types : pas de dossier surveillé, pas de requête SQL, pas d'index de
# crawl — le cœur ne va rien chercher, il reçoit.
#
# Stockage (clé Redis "docsearch:config:plugin_sources") :
#   {"tickets": {
#       "plugin":     "jira",
#       "es_index":   "tickets_jira",
#       "acl_policy": "groupes",
#       "acl_groups": ["DL-SUPPORT"],
#       "acl_principaux": [],
#       "fields": [{"nom": "bureau", "es_type": "keyword", "facet": true}],
#       "label": "Tickets", "searchable": true, "collectable": true,
#       "allowed_groups": []
#   }}
#
# ⚠️  `plugin` n'est PAS décoratif : c'est le seul contrôle qui empêche un
# module d'écrire dans la source d'un autre (voir
# docsearch_contract.documents.verifier_emetteur). Le changer revient à
# changer le propriétaire de la source.
#
# Comme pour les sources SQL et web, il n'existe PAS de source par défaut :
# une installation sans source plugin enregistrée n'en a simplement aucune
# à traiter (plugin_worker.py tourne alors sans rien indexer).

import os
import json
import time
import logging

from docsearch_contract import plugins as contract_plugins
from docsearch_contract.plugins import PluginSource

logger = logging.getLogger(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
PLUGIN_SOURCES_KEY = "docsearch:config:plugin_sources"
PLUGIN_SOURCES_CACHE_TTL = int(os.getenv("PLUGIN_SOURCES_CACHE_TTL", "10"))

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
                f"[plugin_sources_config] Redis injoignable ({e}) — "
                f"aucune source plugin disponible tant qu'il reste injoignable."
            )
            _redis_unavailable_logged = True
        _redis_client = None
        return None


def _raw_sources() -> dict:
    """Dict brut {name: {...}} — cache local, sinon Redis, sinon vide.

    Le repli sur vide est le même que pour SQL et web, et il est
    délibérément FERMÉ : Redis injoignable rend zéro source plugin, donc
    aucun message n'est indexé. Un repli « on accepte tout » ferait
    écrire des documents dont on ne connaît plus la politique d'ACL.
    """
    global _cache, _cache_time

    now = time.time()
    if (now - _cache_time) < PLUGIN_SOURCES_CACHE_TTL:
        return _cache

    client = _get_redis_client()
    if client is not None:
        try:
            raw = client.get(PLUGIN_SOURCES_KEY)
            _cache = json.loads(raw) if raw else {}
            _cache_time = now
            return _cache
        except Exception as e:
            logger.warning(f"[plugin_sources_config] Erreur lecture Redis : {e} — repli sur vide")

    _cache = {}
    _cache_time = now
    return _cache


def get_sources() -> dict[str, PluginSource]:
    """Toutes les sources plugin enregistrées, {name: PluginSource}."""
    return {
        name: contract_plugins.depuis_dict(name, entry)
        for name, entry in _raw_sources().items()
    }


def get_source(name: str) -> PluginSource:
    """Une source plugin par son nom. Lève KeyError si inconnue."""
    raw = _raw_sources()
    if name not in raw:
        raise KeyError(
            f"Source plugin inconnue : '{name}'. Sources disponibles : "
            f"{', '.join(raw.keys()) or '(aucune)'}"
        )
    return contract_plugins.depuis_dict(name, raw[name])


def _read_write(mutate) -> dict:
    client = _get_redis_client()
    if client is None:
        raise RuntimeError(
            "Redis injoignable — impossible d'enregistrer la configuration. "
            "Vérifiez que le service redis tourne (./manage.sh status)."
        )
    raw = client.get(PLUGIN_SOURCES_KEY)
    sources = json.loads(raw) if raw else {}

    mutate(sources)

    client.set(PLUGIN_SOURCES_KEY, json.dumps(sources))
    global _cache, _cache_time
    _cache = sources
    _cache_time = time.time()
    return sources


def add_source(
    name: str, plugin: str, es_index: str, acl_policy: str,
    acl_groups: list[str] | None = None, acl_principaux: list[str] | None = None,
    fields: list[dict] | None = None, label: str | None = None,
    description: str | None = None, searchable: bool = True,
    collectable: bool = True, allowed_groups: list[str] | None = None,
    tri_defaut: str | None = None,
) -> dict:
    """
    Enregistre une source plugin (ou remplace celle du même nom).

    ATTENTION : REMPLACE entièrement l'entrée existante (pas de fusion
    partielle) — même comportement que les trois autres registres, et
    même conséquence : réenregistrer une source déjà configurée sans
    repasser `searchable`/`collectable` les remet à True.

    La validation est celle du contrat partagé
    (docsearch_contract.plugins.valider_declaration) : elle lève
    ContratInvalide, qui est une ValueError.
    """
    if not contract_plugins.nom_valide(name):
        raise ValueError(
            f"Nom de source invalide : '{name}' — attendu : lettres minuscules, "
            f"chiffres, '-' ou '_', commençant par une lettre/chiffre."
        )

    entree = contract_plugins.valider_declaration({
        "plugin": plugin, "es_index": es_index, "acl_policy": acl_policy,
        "acl_groups": acl_groups, "acl_principaux": acl_principaux,
        "fields": fields, "label": label, "description": description,
        "searchable": searchable, "collectable": collectable,
        "allowed_groups": allowed_groups,
        # Déclaration du MODULE, pas réglage d'administrateur : elle vient
        # du manifeste à chaque installation et n'a donc pas à être
        # préservée d'une version à l'autre, contrairement à
        # searchable/collectable/allowed_groups. Omise, le contrat la
        # ramène à "_score" — et la source cesse d'imposer son ordre.
        "tri_defaut": tri_defaut,
    })

    def mutate(sources):
        _verifier_index_libre(name, entree["es_index"], sources)
        sources[name] = entree

    return _read_write(mutate)


def _verifier_index_libre(name: str, es_index: str, sources: dict) -> None:
    """Refuse un index Elasticsearch déjà pris par une AUTRE source.

    Ce n'est pas une hygiène de nommage. `plugin_indexer.reconcilier()`
    supprime, à chaque `run_end`, tout document de `es_index` qui ne porte
    pas le `run_id` de la passe qui vient de finir — SANS filtrer sur la
    source. Deux sources partageant un index se supprimeraient donc leurs
    documents l'une l'autre à chaque passe, jusqu'à ce que le garde-fou
    des 50 % bloque la réconciliation définitivement : plus rien n'est
    jamais nettoyé, et le journal ressemble à une panne du module.

    Le contrôle est le même que celui des registres natifs — voir
    `sql_sources_config.add_source`, qui vérifie déjà contre les sources
    fichiers — étendu ici aux quatre registres, puisqu'ils partagent tous
    l'alias de recherche fédérée.

    Imports différés : ces trois modules n'importent jamais celui-ci, et
    les importer en tête créerait un cycle pour rien.
    """
    for autre_nom, autre in sources.items():
        if autre_nom != name and autre.get("es_index") == es_index:
            raise ValueError(
                f"L'index '{es_index}' est déjà utilisé par la source plugin '{autre_nom}'."
            )

    from file_sources_config import get_sources as get_file_sources
    from sql_sources_config import get_sources as get_sql_sources
    from web_sources_config import get_sources as get_web_sources

    for libelle, get in (("fichier", get_file_sources), ("SQL", get_sql_sources)):
        for autre_nom, autre in get().items():
            if autre.es_index == es_index:
                raise ValueError(
                    f"L'index '{es_index}' est déjà utilisé par la source {libelle} '{autre_nom}'."
                )

    # Une source web en occupe DEUX : l'index de crawl intermédiaire écrit
    # par le crawler, et l'index final que web_indexer.py en dérive. Y
    # écrire depuis un module casserait la transformation de l'un vers
    # l'autre, il faut donc refuser les deux.
    for autre_nom, autre in get_web_sources().items():
        if es_index in (autre.es_index, autre.crawl_index):
            raise ValueError(
                f"L'index '{es_index}' est déjà utilisé par la source web '{autre_nom}'."
            )


def remove_source(name: str) -> dict:
    """Retire une source du registre. NE SUPPRIME PAS son index ES —
    même choix que les trois autres registres : les documents restent,
    simplement plus alimentés ni cherchables."""
    def mutate(sources):
        if name not in sources:
            raise KeyError(f"Source plugin inconnue : '{name}'")
        del sources[name]

    return _read_write(mutate)


def _set(name: str, clef: str, valeur) -> dict:
    def mutate(sources):
        if name not in sources:
            raise KeyError(f"Source plugin inconnue : '{name}'")
        sources[name][clef] = valeur

    return _read_write(mutate)


def set_searchable(name: str, searchable: bool) -> dict:
    """Retire (ou rend) la source à la recherche — n'affecte jamais
    l'indexation : plugin_worker.py continue d'écrire ce qu'on lui
    pousse."""
    return _set(name, "searchable", bool(searchable))


def set_collectable(name: str, collectable: bool) -> dict:
    return _set(name, "collectable", bool(collectable))


def set_label(name: str, label: str) -> dict:
    return _set(name, "label", label or "")


def set_description(name: str, description: str) -> dict:
    return _set(name, "description", description or "")


def set_allowed_groups(name: str, allowed_groups: list[str]) -> dict:
    """Restreint la visibilité de la source dans /search aux membres d'un
    des groupes AD/LDAP listés — liste vide = aucune restriction.
    Orthogonal à `acl_policy`, qui règle l'ACL par document."""
    return _set(name, "allowed_groups", list(allowed_groups or []))
