# search_query.py — Construction de la requête ES à partir de critères de
# recherche sauvegardée (utilisé par alert_worker.py pour vérifier les
# alertes en arrière-plan — voir saved_searches.py : alert_enabled).
#
# ⚠️  Doit rester en cohérence avec la construction de requête de /search
# (search_api.py, fonction search()) — toute évolution de la logique de
# filtrage faite là-bas (nouvelle facette, nouveau champ cherché, nouvelle
# règle ACL) doit être répercutée ici, sinon une alerte pourrait signaler
# des documents qu'une recherche manuelle ne trouverait pas (ou l'inverse).
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

FIELD_SETS = {
    "all":      ["content", "title^4", "filename^6", "author.text", "keywords.text^2"],
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


def _searchable_source_names() -> list[str]:
    names = []
    for name, s in file_sources_config.get_sources().items():
        if s.searchable:
            names.append(name)
    for name, s in sql_sources_config.get_sources().items():
        if s.searchable:
            names.append(name)
    for name, s in web_sources_config.get_sources().items():
        if s.searchable:
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


def _active_custom_facets(source_names: list[str]) -> dict[str, str]:
    names = source_names or [name for name, s in sql_sources_config.get_sources().items() if s.searchable]
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
    fields = FIELD_SETS.get(search_in, FIELD_SETS["all"])

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
        {"terms": {"source": _searchable_source_names()}},
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

    custom_facet_defs = _active_custom_facets(source_names)
    for es_field in custom_facet_defs:
        values = (criteria.get("custom") or {}).get(es_field)
        if values:
            filters.append({"terms": {es_field: values}})

    return {"bool": {"must": must, "filter": filters}}
