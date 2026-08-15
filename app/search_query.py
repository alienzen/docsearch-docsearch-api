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

from auth.directory import get_effective_groups
import sql_sources_config
import runtime_config
# Vue générique des trois registres, partagée avec search_api.py — les
# registres fichiers et web ne sont plus importés ici : seule la lecture
# du mapping de colonnes des sources SQL (facettes personnalisées) reste
# spécifique à un type.
import source_registries


def field_sets(exact: bool = False) -> dict:
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

    `exact=True` bascule sur les sous-champs `.exact` (voir CHAMP_EXACT
    dans docsearch-ingestion/app/indexer.py) : même texte, analysé sans
    racinisation, sans mots vides et sans synonymes, mais toujours en
    minuscules et sans accents. Les POIDS sont les mêmes des deux côtés :
    ils disent qu'un mot trouvé dans un titre compte plus que dans le
    corps, ce qui ne dépend pas de la façon dont le texte est analysé.

    ⚠️ Un `multi_match` sur un champ absent du mapping ne lève AUCUNE
    erreur — il ne matche rien. Un index qui n'a pas reçu la migration
    (`./manage.sh migrer-exact --apply`) est donc simplement muet en
    recherche exacte, sans le moindre signal.
    """
    cfg = runtime_config.get_runtime_config()
    filename = cfg.get("search_boost_filename", 6)
    title = cfg.get("search_boost_title", 4)
    keywords = cfg.get("search_boost_keywords", 2)

    def champ(nom: str, ordinaire: str, poids=None) -> str:
        base = f"{nom}.exact" if exact else ordinaire
        return f"{base}^{poids}" if poids else base

    return {
        "all": [
            champ("content",  "content"),
            champ("title",    "title",         title),
            champ("filename", "filename",      filename),
            champ("author",   "author.text"),
            champ("keywords", "keywords.text", keywords),
        ],
        "title":    [champ("title",    "title")],
        "author":   [champ("author",   "author.text")],
        "keywords": [champ("keywords", "keywords.text")],
        "filepath": [champ("filepath", "filepath.text")],
    }


# Tolérance aux fautes de frappe. Le `AUTO` d'Elasticsearch vaut
# `AUTO:3,6` : une correction dès 3 caractères, DEUX à partir de 6.
# Beaucoup trop lâche sur ce corpus, et pour deux raisons distinctes :
#
# - la clause ordinaire interroge des champs RACINISÉS, donc la distance
#   d'édition s'applique à un radical et non à un mot : « congés » y est
#   devenu « cong », et une correction sur 4 caractères ramenait `cont`,
#   `conv`, `long`, `gong`, `congo`… soit 7303 documents pour 1971
#   réellement concernés (mesuré sur l'index de la VM de dev) ;
# - à deux corrections, le vocabulaire administratif français est plein
#   de faux amis exactement à cette distance : « délégation » ramenait
#   `dérogation`, `délation`, `allégation`, `délectation` ; « convention »
#   ramenait `conception`, `conviction`, `conversion`, `congestion`.
#
# `AUTO:5,99` se lit : aucune correction en dessous de 5 caractères, une
# seule au-delà. Le second seuil est volontairement hors d'atteinte —
# c'est la façon d'écrire « jamais deux corrections » avec `AUTO`, dont
# on garde le premier seuil qui, lui, est indispensable (sans lui,
# « loi » appellerait `roi`, `lot`, `voi`…).
FUZZINESS = "AUTO:5,99"

# Poids de la branche de rattrapage orthographique (voir
# build_text_clause). Inférieur à 1 à dessein : un document qui ne
# répond QUE par une faute de frappe ne doit pas passer devant un
# document qui contient réellement le mot, ou l'un de ses synonymes.
BOOST_FLOU = 0.5


def est_phrase(query_text: str) -> bool:
    """Vrai si la requête est encadrée de guillemets, donc à chercher
    comme une phrase (ordre et adjacence des mots respectés).

    Une seule définition pour les trois points d'usage, l'aide au zéro
    résultat comprise : c'est elle qui décide si les guillemets doivent
    être retirés avant d'afficher la requête à l'utilisateur.
    """
    return len(query_text) >= 2 and query_text.startswith('"') and query_text.endswith('"')


def build_text_clause(query_text: str, search_in: str = "all", exact: bool = False) -> dict:
    """Clause `must` du texte libre — source unique de /search, de
    l'export et de la vérification d'alertes.

    Deux dimensions INDÉPENDANTES s'y croisent, et les confondre est
    l'erreur naturelle :

    - les **guillemets** disent « ces mots, dans cet ordre » (adjacence) ;
    - le mode **exact** dit « ces mots, tels qu'écrits » (pas de
      racinisation, pas de synonymes, pas de tolérance aux fautes).

    On peut donc vouloir l'un sans l'autre, et les quatre combinaisons
    ont un sens. En particulier `exact` sans guillemets reste une
    recherche en OU sur les mots, comme la recherche ordinaire : ce qui
    change est la façon dont chaque mot est comparé, pas le nombre de
    mots exigés.

    La tolérance aux fautes (`fuzziness`) est incompatible avec les deux :
    une recherche exacte qui rattraperait les fautes de frappe ne serait
    exacte pour personne.

    La recherche ORDINAIRE, elle, rend un `bool` à deux branches en OU et
    non un `multi_match` — voir ci-dessous pourquoi les deux ne peuvent
    pas tenir dans la même clause.
    """
    sets = field_sets(exact=exact)
    fields = sets.get(search_in, sets["all"])

    if not query_text:
        return {"match_all": {}}
    if est_phrase(query_text):
        return {"multi_match": {
            "query":  query_text[1:-1].strip(),
            "fields": fields,
            "type":   "phrase",
        }}
    if exact:
        return {"multi_match": {"query": query_text, "fields": fields}}

    # Deux branches en OU, et c'est la SEULE façon d'avoir à la fois le
    # thésaurus et la tolérance aux fautes.
    #
    # ⚠️ Lucene abandonne la fuzziness sur toute position portant
    # plusieurs jetons, EN SILENCE. Une position à jeton unique passe par
    # `newTermQuery`, qui applique la fuzziness ; une position élargie par
    # le thésaurus passe par `newSynonymQuery`, qui construit une
    # `SynonymQuery` de termes bruts, sans distance d'édition. Ajouter
    # « DRH, congés » au thésaurus transformait donc la requête « congés »
    # de `(cong~1 …43 termes…)` en `Synonym(cong drh)` : la règle ajoutait
    # 2 documents et en retirait 5330, sans erreur ni trace, et
    # l'administrateur constatait MOINS de résultats après avoir ajouté un
    # synonyme. Le panneau de test du thésaurus ne pouvait pas le montrer,
    # les jetons produits (`drh`, `cong`) étant, eux, parfaitement corrects.
    #
    # La branche floue porte donc sur les sous-champs `.exact`, les seuls
    # sans filtre de synonymes (voir ANALYSE dans
    # docsearch-ingestion/app/indexer.py) : la fuzziness y survit quel que
    # soit le contenu du thésaurus. Elle y travaille de surcroît sur des
    # mots entiers et non sur des radicaux, seul niveau où une distance
    # d'édition veut dire quelque chose.
    #
    # ⚠️ Un index qui n'a pas reçu `./manage.sh migrer-exact --apply` n'a
    # pas de sous-champs `.exact` : la branche de rattrapage y est
    # simplement muette (un `multi_match` sur un champ absent ne lève
    # rien), et la recherche s'y comporte comme la seule première branche
    # — sans tolérance aux fautes, mais sans erreur non plus. Même angle
    # mort que la recherche exacte, et même remède.
    exacts = field_sets(exact=True)
    return {"bool": {
        "should": [
            {"multi_match": {"query": query_text, "fields": fields}},
            {"multi_match": {
                "query":     query_text,
                "fields":    exacts.get(search_in, exacts["all"]),
                "fuzziness": FUZZINESS,
                "boost":     BOOST_FLOU,
            }},
        ],
        # Explicite : un `should` seul vaut déjà 1, mais cette clause est
        # imbriquée dans le `must` d'un autre `bool` et la règle « should
        # devient facultatif dès qu'il y a un must » est assez proche
        # pour qu'on ne veuille pas laisser la question ouverte.
        "minimum_should_match": 1,
    }}


def build_acl_filter(username: str) -> dict:
    """Identique à build_acl_filter() dans search_api.py — voir
    l'avertissement de cohérence en tête de fichier."""
    user_groups = get_effective_groups(username)
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
    """Restriction par groupe AD/LDAP d'une source.

    N'est plus une copie de search_api.py : la règle vit dans le contrat
    partagé (docsearch_contract/sources.py), que les deux fichiers
    appellent via source_registries. C'était l'une des divergences que
    l'avertissement en tête de fichier annonçait — celle-là ne peut plus
    se produire."""
    return source_registries.visible_par(s, user_groups)


def _searchable_source_names(username: str) -> list[str]:
    """Sources atteignables par cet utilisateur — même définition, au
    sens propre, que dans search_api.py : les deux appellent la même
    fonction du contrat partagé."""
    return source_registries.noms_cherchables(get_effective_groups(username))


def _requested_source_names(
    source_names: str | list[str] | None, username: str,
) -> list[str] | None:
    """Identique à _requested_source_names() dans search_api.py — voir
    l'avertissement de cohérence en tête de fichier.

    Retourne None quand la recherche enregistrée ne filtrait sur aucune
    source, et la liste VIDE quand elle en nommait mais qu'aucune n'est
    plus atteignable. L'appelant doit poser le filtre dans ce second cas
    (voir build_query_clauses) : sans cette distinction, une alerte posée
    sur la seule source X devenait FÉDÉRÉE le jour où X disparaissait, et
    notifiait pour des documents hors de ce à quoi l'utilisateur s'était
    abonné — au lieu de ne plus rien remonter.

    Restreint aux sources cherchables PAR CET UTILISATEUR et non aux
    seules sources présentes dans les registres : c'est la même liste que
    le filtre obligatoire de build_query_clauses, donc le résultat ne
    change pas (l'intersection des deux était déjà vide), mais il n'y a
    plus qu'une définition de « source atteignable » à tenir à jour. Les
    quatre raisons d'écarter un nom — retiré du registre, renommé,
    désactivé, hors des groupes de l'utilisateur — deviennent du même
    coup indiscernables, comme dans search_api.py.

    Un nom écarté l'est SILENCIEUSEMENT, sans exception : un worker de
    fond ne doit jamais planter sur une donnée utilisateur périmée, et
    une recherche enregistrée en est une par nature (elle survit à la
    source qu'elle nomme).
    """
    if not source_names:
        return None
    names = source_names if isinstance(source_names, list) else [source_names]
    autorisees = set(_searchable_source_names(username))
    return [name for name in names if name in autorisees]


def _folder_filter(folder: str | list[str] | None) -> dict | None:
    if not folder:
        return None
    folders = folder if isinstance(folder, list) else [folder]
    should = []
    for f in folders:
        should.append({"term": {"folder": f}})
        should.append({"prefix": {"folder": f.rstrip("/") + "/"}})
    return {"bool": {"should": should, "minimum_should_match": 1}}


def _keywords_filter(keywords) -> dict | None:
    """Identique à _keywords_filter() dans search_api.py — voir
    l'avertissement de cohérence en tête de fichier. Combinaison en ET
    (un document doit porter TOUS les mots-clés) et non en OU : une
    alerte doit signaler exactement ce qu'une recherche manuelle avec
    les mêmes critères afficherait."""
    if not keywords:
        return None
    kws = keywords if isinstance(keywords, list) else [keywords]
    return {"bool": {"filter": [{"term": {"keywords": k}} for k in kws]}}


def _active_custom_facets(source_names: list[str], username: str) -> dict[str, str]:
    user_groups = get_effective_groups(username)
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
    must = [build_text_clause(
        query_text,
        criteria.get("search_in") or "all",
        bool(criteria.get("exact")),
    )]

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

    keywords_filter = _keywords_filter(criteria.get("keywords"))
    if keywords_filter:
        filters.append(keywords_filter)

    folder_filter = _folder_filter(criteria.get("folder"))
    if folder_filter:
        filters.append(folder_filter)

    # None = la recherche enregistrée ne filtrait sur aucune source ;
    # liste vide = elle en nommait, mais plus aucune n'est atteignable, et
    # le filtre doit alors ne rien matcher (voir _requested_source_names)
    # — d'où le test sur None et non sur la vacuité.
    source_names = _requested_source_names(criteria.get("source"), username)
    if source_names is not None:
        filters.append({"terms": {"source": source_names}})

    custom_facet_defs = _active_custom_facets(source_names or [], username)
    for es_field in custom_facet_defs:
        values = (criteria.get("custom") or {}).get(es_field)
        if values:
            filters.append({"terms": {es_field: values}})

    return {"bool": {"must": must, "filter": filters}}
