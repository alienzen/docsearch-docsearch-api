# search_query.py — Construction de la requête ES à partir de critères de
# recherche sauvegardée (utilisé par alert_worker.py pour vérifier les
# alertes en arrière-plan — voir saved_searches.py : alert_enabled).
#
# ⚠️  Doit rester en cohérence avec la construction de requête de /search
# (search_api.py, fonction search()) — toute évolution de la logique de
# filtrage faite là-bas (nouvelle facette, nouveau champ cherché, nouvelle
# règle ACL) doit être répercutée ici, sinon une alerte pourrait signaler
# des documents qu'une recherche manuelle ne trouverait pas (ou l'inverse).
# Exception : field_sets() ci-dessous n'est PAS à recopier — c'est
# l'inverse, search_api l'importe depuis ici. Les poids étant réglables
# depuis l'administration, ils ne pouvaient pas rester en trois copies.
# Volontairement une implémentation séparée plutôt qu'un import direct de
# search_api : ce dernier charge FastAPI, Kafka, LDAP et toutes les routes
# /admin au chargement du module — inutilement lourd pour un simple worker
# de fond qui n'a besoin que de construire une requête ES. Même principe
# que la copie synchronisée de runtime_config.py entre les deux dépôts.
#
# Ne couvre QUE ce dont une vérification d'alerte a besoin : pas de
# pagination, tri, highlighting ni agrégations de facettes (inutiles pour
# compter des nouveaux résultats) — voir SavedSearchCreate dans
# search_api.py pour le schéma des critères stockés.

from ldap_resolver import get_user_groups
import file_sources_config
import sql_sources_config
import web_sources_config
import runtime_config


def field_sets() -> dict:
    """Champs interrogés par `search_in`, avec leurs poids.

    SOURCE UNIQUE des trois points d'usage — /search et l'export dans
    search_api.py, la vérification d'alertes ici. Ces trois-là portaient
    chacun leur copie littérale, que rien ne tenait synchronisée : un
    poids modifié d'un seul côté aurait fait diverger silencieusement le
    classement de l'écran, celui du fichier exporté et celui des alertes.

    Les poids sont relus à chaque appel — c'est ce qui rend le réglage
    effectif à chaud, sous le TTL du cache de runtime_config (~10 s).

    Les jeux à champ unique n'ont pas de poids : il n'y a rien à
    pondérer les uns par rapport aux autres. `author` vise le sous-champ
    analysé `author.text` et non `author`, en keyword — non tokenisé, une
    recherche en texte libre dessus ne matcherait jamais un nom partiel
    comme « Dupont » contre « Martin Dupont ».
    """
    cfg = runtime_config.get_runtime_config()
    filename = cfg.get("search_boost_filename", 6)
    title = cfg.get("search_boost_title", 4)
    keywords = cfg.get("search_boost_keywords", 2)
    return {
        "all": [
            "content",
            f"title^{title}",
            f"filename^{filename}",
            "author.text",
            f"keywords.text^{keywords}",
        ],
        "title":    ["title"],
        "author":   ["author.text"],
        "keywords": ["keywords.text"],
        "filepath": ["filepath.text"],
    }


def build_acl_filter(username: str) -> dict:
    """Identique à build_acl_filter() dans search_api.py — voir
    l'avertissement de cohérence en tête de fichier."""
    user_groups = get_user_groups(username)
    return {
        "bool": {
            "should": [
                {"term":  {"acl.public": True}},
                {"term":  {"acl.owner":  username}},
                {"term":  {"acl.users":  username}},
                {"terms": {"acl.groups": user_groups}} if user_groups
                else {"term": {"acl.groups": "__never__"}},
            ],
            "minimum_should_match": 1,
        }
    }


def _visible_to(s, user_groups: list[str]) -> bool:
    """Identique à _visible_to() dans search_api.py — voir l'avertissement
    de cohérence en tête de fichier."""
    return not s.allowed_groups or any(g in s.allowed_groups for g in user_groups)


def _searchable_source_names(username: str) -> list[str]:
    """Identique à _searchable_source_names() dans search_api.py — voir
    l'avertissement de cohérence en tête de fichier."""
    user_groups = get_user_groups(username)
    names = []
    for name, s in file_sources_config.get_sources().items():
        if s.searchable and _visible_to(s, user_groups):
            names.append(name)
    for name, s in sql_sources_config.get_sources().items():
        if s.searchable and _visible_to(s, user_groups):
            names.append(name)
    for name, s in web_sources_config.get_sources().items():
        if s.searchable and _visible_to(s, user_groups):
            names.append(name)
    return names


def _valid_source_names(source: str | list[str] | None) -> list[str]:
    """Comme _validate_source_names() dans search_api.py, mais tolérant :
    un nom de source devenue invalide depuis l'enregistrement de l'alerte
    (source supprimée) est ignoré plutôt que de faire échouer toute la
    vérification — un worker de fond ne doit jamais planter sur une
    donnée utilisateur périmée."""
    if not source:
        return []
    names = source if isinstance(source, list) else [source]
    valid = []
    for name in names:
        try:
            file_sources_config.get_source(name)
            valid.append(name)
            continue
        except KeyError:
            pass
        try:
            sql_sources_config.get_source(name)
            valid.append(name)
            continue
        except KeyError:
            pass
        try:
            web_sources_config.get_source(name)
            valid.append(name)
        except KeyError:
            pass
    return valid


def _folder_filter(folder: str | list[str] | None) -> dict | None:
    if not folder:
        return None
    folders = folder if isinstance(folder, list) else [folder]
    should = []
    for f in folders:
        should.append({"term": {"folder": f}})
        should.append({"prefix": {"folder": f.rstrip("/") + "/"}})
    return {"bool": {"should": should, "minimum_should_match": 1}}


def _active_custom_facets(source_names: list[str], username: str) -> dict[str, str]:
    user_groups = get_user_groups(username)
    names = source_names or [
        name for name, s in sql_sources_config.get_sources().items()
        if s.searchable and _visible_to(s, user_groups)
    ]
    result: dict[str, str] = {}
    for name in names:
        try:
            source = sql_sources_config.get_source(name)
        except KeyError:
            continue
        for f in source.fields:
            if f.facet:
                result[f.es_field] = f.facet_label or f.es_field
    return result


def build_query_clauses(criteria: dict, username: str) -> dict:
    """
    Construit {"bool": {"must": ..., "filter": ...}} à partir des mêmes
    critères qu'une recherche sauvegardée (voir SavedSearchCreate — "ext"
    est accepté en plus d'"extension" pour matcher directement le schéma
    stocké par saved_searches.py) et de l'ACL de l'utilisateur.
    alert_worker.py complète le résultat avec un filtre sur `indexed_at`
    pour ne compter que les documents apparus depuis la dernière
    vérification.
    """
    query_text = (criteria.get("query") or "").strip()
    search_in = criteria.get("search_in") or "all"
    sets = field_sets()
    fields = sets.get(search_in, sets["all"])

    is_exact_phrase = len(query_text) >= 2 and query_text.startswith('"') and query_text.endswith('"')
    if not query_text:
        must = [{"match_all": {}}]
    elif is_exact_phrase:
        must = [{
            "multi_match": {
                "query":  query_text[1:-1].strip(),
                "fields": fields,
                "type":   "phrase",
            }
        }]
    else:
        must = [{
            "multi_match": {
                "query":     query_text,
                "fields":    fields,
                "fuzziness": "AUTO",
            }
        }]

    filters = [
        build_acl_filter(username),
        {"terms": {"source": _searchable_source_names(username)}},
    ]

    date_from, date_to = criteria.get("date_from"), criteria.get("date_to")
    if date_from or date_to:
        r = {}
        if date_from: r["gte"] = date_from
        if date_to:   r["lte"] = date_to
        filters.append({"range": {"date_modified": r}})

    extension = criteria.get("extension") or criteria.get("ext")
    if extension and extension != "all":
        exts = extension if isinstance(extension, list) else [extension]
        filters.append({"terms": {"extension": exts}})

    author = criteria.get("author")
    if author:
        filters.append({"terms": {"author": author if isinstance(author, list) else [author]}})

    keywords = criteria.get("keywords")
    if keywords:
        filters.append({"terms": {"keywords": keywords if isinstance(keywords, list) else [keywords]}})

    folder_filter = _folder_filter(criteria.get("folder"))
    if folder_filter:
        filters.append(folder_filter)

    source_names = _valid_source_names(criteria.get("source"))
    if source_names:
        filters.append({"terms": {"source": source_names}})

    custom_facet_defs = _active_custom_facets(source_names, username)
    for es_field in custom_facet_defs:
        values = (criteria.get("custom") or {}).get(es_field)
        if values:
            filters.append({"terms": {es_field: values}})

    return {"bool": {"must": must, "filter": filters}}
