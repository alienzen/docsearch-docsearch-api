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
import unicodedata

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

# Séparateurs de mots À L'INTÉRIEUR d'un terme keyword, devant lesquels
# une saisie a le droit de commencer : « Bruno Marchand », « Dupont,
# Martin », « Jean-Pierre Roy ». Classe de caractères Lucene, où le point
# et le tiret sont échappés — non échappé, le tiret y formerait un
# intervalle.
_SEPARATEURS = r"[ ,;:/'\.\-]"


def _replier(texte: str) -> str:
    """Forme de comparaison : minuscules, accents ôtés.

    Repli par décomposition Unicode plutôt que par table de
    correspondance, qui oublierait toujours un caractère — même procédé
    que pinned.normaliser(), qui, lui, réduit en plus les espaces : une
    requête épinglée se compare en entier, alors qu'ici on compare des
    débuts et des morceaux.
    """
    return "".join(
        c for c in unicodedata.normalize("NFD", texte)
        if unicodedata.category(c) != "Mn"
    ).casefold()


# Lettre repliée → toutes ses variantes de casse ET d'accent. Lucene n'a
# pas plus de drapeau « sans accent » que de drapeau « sans casse » :
# l'une comme l'autre s'obtiennent par classe de caractères, et autant
# les produire d'un seul tenant.
#
# La table est CONSTRUITE et non écrite : parcourir le latin étendu une
# fois à l'import coûte moins cher qu'une liste à maintenir, et surtout
# n'oublie personne — un « Ș » a autant le droit d'être tapé sans son
# signe qu'un « é », et l'annuaire porte des noms de toute l'Europe.
def _table_variantes() -> dict[str, str]:
    table: dict[str, str] = {}
    for point in range(ord("A"), 0x0250):   # latin de base, 1, étendu A et B
        lettre = chr(point)
        base = _replier(lettre)
        # Écarte ce qui se replie en DEUX lettres (« ß » → « ss ») : dans
        # une classe de caractères, chacune serait acceptée séparément,
        # et « s » proposerait alors les termes en « ß ».
        if lettre.isalpha() and len(base) == 1:
            table[base] = table.get(base, "") + lettre
    return table


_VARIANTES = _table_variantes()


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

    Comparaison repliée (voir `_replier`) : ni la casse ni les accents ne
    séparent, « repartition » retrouve « répartition ». Le filtrage se
    faisant en Python sur un lot déjà relu, les deux côtés se replient à
    la lecture — contrairement à ce que disait cette note jusqu'au
    2026-08-13, rien n'a besoin d'être normalisé à l'écriture du journal.
    """
    saisie = _replier(prefix)
    debut, ailleurs = [], []
    for entree in recent_queries(es, username, TAILLE_POOL):
        texte = _replier(entree["query"])
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

    L'insensibilité à la casse ET aux accents s'obtient lettre par lettre
    (`[bB]`, `[EeÉéÈè…]`) : la syntaxe de Lucene n'a de drapeau ni pour
    l'une ni pour les autres, et les classes viennent de `_VARIANTES`.
    Le repli joue dans les deux sens — « Emilie » propose « Émilie
    Dubois », « Émilie » aussi — et il ne touche QUE la comparaison : le
    terme proposé garde ses accents, puisque c'est celui de l'index.
    Le reste est échappé
    — une parenthèse tapée par l'utilisateur produirait sinon une erreur
    400, et un `.*` collé dans la barre ferait balayer tout le
    dictionnaire de termes de l'index.

    L'expression doit correspondre au terme ENTIER (c'est la règle de
    `include`), d'où le `.*` final : sans lui, « bud » ne trouverait que
    l'auteur nommé exactement « bud ».

    Et d'où, symétriquement, le groupe optionnel de tête : les champs
    agrégés sont des `keyword`, donc NON tokenisés — l'auteur y est un
    terme unique « Bruno Marchand ». Ancrée au seul début du terme,
    l'expression ne trouvait cet auteur que sur son prénom, alors que la
    recherche elle-même le trouve sur son nom (elle interroge le
    sous-champ analysé `author.text`). Le groupe autorise le match après
    un séparateur interne, sans autoriser le match n'importe où :
    « chand » ne doit pas proposer « Marchand ».
    """
    morceaux = []
    for caractere in prefix[:MAX_PREFIXE]:
        variantes = _VARIANTES.get(_replier(caractere), "")
        if len(variantes) < 2 and caractere.isalpha():
            # Hors du latin étendu — grec, cyrillique… — la table n'a
            # rien : reste au moins la casse, comme avant le repli.
            minuscule, majuscule = caractere.lower(), caractere.upper()
            if minuscule != majuscule:
                variantes = minuscule + majuscule
        if len(variantes) > 1:
            morceaux.append(f"[{variantes}]")
        elif caractere in _RESERVES:
            morceaux.append("\\" + caractere)
        else:
            morceaux.append(caractere)
    return f"(.*{_SEPARATEURS})?" + "".join(morceaux) + ".*"


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
# Ce raisonnement vaut d'autant plus depuis que regex_prefixe() admet le
# match en frontière de mot (2026-08-13) : son `.*` de tête prive
# l'automate du saut par préfixe, et le dictionnaire est désormais
# balayé ENTIER quelle que soit la saisie. Sans effet mesurable sur ces
# deux champs — 4-6 ms avant, 2-4 ms après, à chaud, agrégations
# cumulées — mais l'écart se creuserait sur un champ à forte cardinalité.
#
# Le repli d'accents du même jour élargit encore chaque classe (« e »
# en compte une vingtaine), sans effet mesurable non plus : 2-5 ms,
# `took` d'ES sur les mêmes agrégations. Ce qui coûte est le nombre de
# TERMES parcourus, pas la largeur des classes qui les filtrent.
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

# L'agrégation trie par nombre de documents, alors que la liste rendue
# trie d'abord par position du match (voir corpus_terms). Lui demander
# exactement `limit` buckets ferait donc trancher ES sur un critère qui
# n'est pas celui du classement final : un auteur commençant par la
# saisie, mais rare, serait coupé au profit d'un auteur fréquent qui ne
# la porte qu'en second mot. On en demande plus qu'il n'en faut, et on
# tranche après reclassement.
FACTEUR_BUCKETS = 4


def corpus_terms(es, index: str, filtres: list, prefix: str, limit: int = 5) -> list[dict]:
    """Auteurs et mots-clés du corpus correspondant à la saisie.

    Correspondre veut dire « commencer par la saisie », mais aussi « la
    voir commencer un de ses mots » : « Marchand » propose « Bruno
    Marchand », faute de quoi les suggestions démentiraient la recherche,
    qui, elle, trouve cet auteur par son nom (voir regex_prefixe).

    Les termes qui COMMENCENT par la saisie sont rendus en premier —
    c'est ce qu'on attend d'une autocomplétion, et c'est déjà la règle de
    matching_queries() pour la moitié « historique » de la même liste.

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
            champ: {"terms": {
                "field": champ, "include": regex, "size": limit * FACTEUR_BUCKETS,
            }}
            for champ, _ in CHAMPS_CORPUS
        },
        timeout=TIMEOUT_CORPUS,
    )
    # Repli identique à celui de l'expression, sans quoi le reclassement
    # démentirait la sélection : « Émilie Dubois », proposé sur la saisie
    # « emilie », doit aussi être reconnu comme COMMENÇANT par elle.
    saisie = _replier(prefix)
    propositions = []
    for champ, nature in CHAMPS_CORPUS:
        debut, ailleurs = [], []
        for bucket in res["aggregations"][champ]["buckets"]:
            terme = {"text": bucket["key"], "kind": nature, "count": bucket["doc_count"]}
            rang = debut if _replier(bucket["key"]).startswith(saisie) else ailleurs
            rang.append(terme)
        propositions += (debut + ailleurs)[:limit]
    return propositions
