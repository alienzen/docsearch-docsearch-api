# user_history.py — Ce que l'utilisateur a lui-même cherché.
#
# Deux fonctionnalités, une seule source de données : l'index search_logs,
# que search_log.py écrit déjà à chaque recherche. Rien de nouveau n'est
# collecté ici — on rend à l'utilisateur ce qui était jusqu'ici réservé
# aux statistiques d'administration.
#
#   GET /me/searches : ses dernières recherches, dédoublonnées
#   GET /suggest     : celles qui commencent par ce qu'il est en train de taper
#
# ⚠️ Le nom d'utilisateur vient TOUJOURS du jeton de session
# (Depends(current_user)) et jamais d'un paramètre de requête : ces
# fonctions sont appelées avec l'identité de l'appelant, point.
#
# ⚠️ Et surtout : on ne suggère JAMAIS les requêtes des autres. C'est la
# variante tentante — « les recherches populaires » — et c'est une fuite :
# une requête porte régulièrement le nom d'un dossier, d'une affaire ou
# d'une personne que son auteur est seul à connaître, et la suggérer le
# divulgue à qui l'ignorait. Le gisement « tout le monde » est
# volontairement inexploité.

import logging

# Le module, pas la constante : `search_log.SEARCH_LOG_INDEX` est relu à
# chaque appel, ce qui laisse un test faire porter les deux modules sur le
# même index jetable en ne patchant qu'un seul endroit — et garantit
# qu'écriture et lecture ne peuvent pas viser deux index différents.
import search_log

logger = logging.getLogger(__name__)

# Nombre de requêtes distinctes relues pour alimenter l'autocomplétion.
# Le filtrage par préfixe se fait ensuite en Python : l'`include` d'une
# agrégation ES est une expression régulière SENSIBLE À LA CASSE, ce qui
# obligerait à comparer « budget » à « Budget » à coups de classes de
# caractères pour un gain nul sur un lot de cette taille (une centaine
# d'entrées, sur un index filtré par utilisateur).
TAILLE_POOL = 200

# Au-delà, ce n'est plus une saisie mais un collage : inutile d'en faire
# une expression régulière pour Elasticsearch.
MAX_PREFIXE = 60

# Caractères réservés de la syntaxe des expressions régulières Lucene
# (celle de `include`), à échapper pour qu'un utilisateur qui tape une
# parenthèse n'obtienne pas une erreur 400 — ni un `.*` qui ferait
# balayer tout le dictionnaire de termes.
_RESERVES = set('.?+*|{}[]()"\\#@&<>~')


def recent_queries(es, username: str, limit: int = 10) -> list[dict]:
    """Les dernières recherches DE CET UTILISATEUR, dédoublonnées par
    texte, la plus récente d'abord.

    Les recherches sans texte libre (filtres seuls) sont écartées : elles
    s'afficheraient comme une ligne vide, et le format d'historique ne
    porte pas de quoi les rejouer — voir la note de `/me/searches` dans
    le README de l'API.

    Le tri d'une agrégation `terms` par sous-agrégation est approximatif
    dès qu'un index a plusieurs shards ; `search_logs` en a un seul (créé
    sans réglage explicite, voir search_log.py), donc l'ordre est ici
    exact. Il le resterait « à peu près » sinon, ce qui suffirait à un
    historique mais pas à un décompte.
    """
    res = es.search(
        index=search_log.SEARCH_LOG_INDEX,
        size=0,
        query={
            "bool": {
                "filter": [{"term": {"username": username}}],
                "must_not": [{"term": {"query.keyword": ""}}],
            }
        },
        aggs={
            "queries": {
                "terms": {
                    "field": "query.keyword",
                    "size": limit,
                    "order": {"derniere": "desc"},
                },
                "aggs": {"derniere": {"max": {"field": "timestamp"}}},
            }
        },
    )
    return [
        {
            "query": bucket["key"],
            "count": bucket["doc_count"],
            "last": bucket["derniere"].get("value_as_string"),
        }
        for bucket in res["aggregations"]["queries"]["buckets"]
    ]


def recent_documents(es, username: str, limit: int = 10) -> list[str]:
    """Identifiants des derniers documents que CET utilisateur a ouverts,
    du plus récent au plus ancien, sans doublon.

    Aucune collecte nouvelle : les clics sont enregistrés depuis toujours
    dans `search_logs`, en `nested` sous chaque recherche (voir
    `POST /click`). On ne fait que les relire pour leur auteur.

    ⚠️ Ne rend que des IDENTIFIANTS. C'est l'appelant qui relit les
    documents à travers le filtre ACL : un document dont les droits ont
    changé depuis le clic, ou qui a été supprimé, ne doit pas
    réapparaître ici sous prétexte qu'il a été consulté un jour.
    """
    res = es.search(
        index=search_log.SEARCH_LOG_INDEX,
        size=0,
        query={"bool": {"filter": [{"term": {"username": username}}]}},
        aggs={
            "clics": {
                "nested": {"path": "clicks"},
                "aggs": {
                    "documents": {
                        "terms": {
                            "field": "clicks.doc_id",
                            "size": limit,
                            "order": {"dernier": "desc"},
                        },
                        "aggs": {"dernier": {"max": {"field": "clicks.timestamp"}}},
                    }
                },
            }
        },
    )
    buckets = res["aggregations"]["clics"]["documents"]["buckets"]
    return [bucket["key"] for bucket in buckets]


def matching_queries(es, username: str, prefix: str, limit: int = 5) -> list[dict]:
    """Ses recherches passées qui correspondent à ce qu'il tape.

    Celles qui COMMENCENT par la saisie d'abord — c'est ce qu'on attend
    d'une autocomplétion — puis celles qui la contiennent ailleurs : après
    avoir cherché « budget 2025 », taper « 2025 » doit retrouver la
    recherche, sans quoi l'historique paraît trouer.

    Comparaison en `casefold()`, donc insensible à la casse mais PAS aux
    accents : « repartition » ne retrouve pas « répartition ». Un repli
    d'accents demanderait de normaliser à l'écriture du journal, ce qui
    n'est pas le sujet de cette route.
    """
    saisie = prefix.casefold()
    debut, ailleurs = [], []
    for entree in recent_queries(es, username, TAILLE_POOL):
        texte = entree["query"].casefold()
        if texte == saisie:
            continue   # proposer à l'identique ce qui est déjà tapé n'aide personne
        if texte.startswith(saisie):
            debut.append(entree)
        elif saisie in texte:
            ailleurs.append(entree)
    return (debut + ailleurs)[:limit]


def regex_prefixe(prefix: str) -> str:
    """Préfixe de saisie → expression régulière Lucene, insensible à la
    casse, pour l'`include` d'une agrégation `terms`.

    L'insensibilité à la casse s'obtient lettre par lettre (`[bB][uU]…`) :
    la syntaxe de Lucene n'a pas de drapeau pour ça. Le reste est échappé
    — une parenthèse tapée par l'utilisateur produirait sinon une erreur
    400, et un `.*` collé dans la barre ferait balayer tout le
    dictionnaire de termes de l'index.

    L'expression doit correspondre au terme ENTIER (c'est la règle de
    `include`), d'où le `.*` final : sans lui, « bud » ne trouverait que
    l'auteur nommé exactement « bud ».
    """
    morceaux = []
    for caractere in prefix[:MAX_PREFIXE]:
        minuscule, majuscule = caractere.lower(), caractere.upper()
        if caractere.isalpha() and minuscule != majuscule:
            morceaux.append(f"[{minuscule}{majuscule}]")
        elif caractere in _RESERVES:
            morceaux.append("\\" + caractere)
        else:
            morceaux.append(caractere)
    return "".join(morceaux) + ".*"


# Champs du corpus proposés en autocomplétion, et pourquoi ceux-là
# seulement — mesuré le 2026-08-12 sur la pile de développement
# (23 016 documents, 14 shards, alias `docsearch-all`) :
#
#   auteur + mots-clés + nom de fichier, include régex : 76-105 ms à
#   chaud, 1320 ms à froid.
#
# Le coût d'un `include` régex tient au balayage du dictionnaire de
# termes, donc à la CARDINALITÉ du champ, pas au nombre de documents
# retenus par le filtre. Sur ce corpus : 151 auteurs distincts, 102
# mots-clés… et 22 494 noms de fichier, soit un par document. Les deux
# premiers restent bornés par le nombre de personnes et le vocabulaire
# métier quand le corpus grandit ; le troisième croît avec lui, et à
# 4 000 000 de documents ce serait plusieurs secondes à chaque frappe.
#
# D'où ce choix : auteur et mots-clés, pas le nom de fichier. Suggérer
# des noms de fichier suppose un champ dédié (`search_as_you_type` ou
# `completion`), donc une réindexation — à traiter avec le lot C du plan
# d'évolutions, pas ici.
CHAMPS_CORPUS = (("author", "author"), ("keywords", "keyword"))

# Elasticsearch rend ce qu'il a trouvé quand ce délai est dépassé, plutôt
# que de faire attendre. Une suggestion est par nature du meilleur
# effort : mieux vaut trois propositions tout de suite que cinq une
# seconde plus tard, alors que l'utilisateur a fini de taper.
TIMEOUT_CORPUS = "300ms"


def corpus_terms(es, index: str, filtres: list, prefix: str, limit: int = 5) -> list[dict]:
    """Auteurs et mots-clés du corpus commençant par la saisie.

    ⚠️ `filtres` DOIT contenir le filtre ACL de l'appelant et la
    restriction aux sources cherchables — ce sont les mêmes que ceux de
    `/search`, et ils sont passés par l'appelant plutôt que reconstruits
    ici pour qu'il n'existe qu'une seule définition de « ce que cet
    utilisateur a le droit de voir ». Une agrégation non filtrée
    divulguerait le nom d'un auteur ou d'un mot-clé d'un document
    interdit : une agrégation fuit exactement autant qu'un résultat.
    """
    regex = regex_prefixe(prefix)
    res = es.search(
        index=index,
        size=0,
        query={"bool": {"filter": filtres}},
        aggs={
            champ: {"terms": {"field": champ, "include": regex, "size": limit}}
            for champ, _ in CHAMPS_CORPUS
        },
        timeout=TIMEOUT_CORPUS,
    )
    propositions = []
    for champ, nature in CHAMPS_CORPUS:
        for bucket in res["aggregations"][champ]["buckets"]:
            propositions.append({"text": bucket["key"], "kind": nature, "count": bucket["doc_count"]})
    return propositions
