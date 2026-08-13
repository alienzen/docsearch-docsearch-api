# tests/test_recherche_exacte.py — La recherche exacte tient-elle sa
# promesse, et rien qu'elle ?
#
# La promesse tient en une phrase : « les mots tels qu'écrits, aux accents
# et aux majuscules près ». Elle se décompose en quatre propriétés, dont
# aucune n'est vérifiable en lisant le code — elles dépendent entièrement
# de ce que le moteur fait de l'analyseur, d'où des tests contre le vrai
# Elasticsearch pour la partie analyse :
#
#   1. pas de racinisation — « délégations » ne répond pas à
#      « délégation », alors que la recherche ordinaire le fait ;
#   2. pas de mots vides — « état de l'art » garde ses mots outils, sans
#      quoi l'expression serait vidée avant même d'être cherchée ;
#   3. accents et casse ignorés — « Congrès », « congres » et « CONGRES »
#      sont une seule et même requête ;
#   4. pas de synonymes — un thésaurus qui élargit une recherche exacte la
#      rend inexacte.
#
# Les deux premières échouent EN SILENCE si l'analyseur est mal composé :
# un filtre en trop ne lève aucune erreur, il élargit simplement la
# recherche sans que rien ne le signale. C'est exactement le mode de
# défaillance décrit dans test_synonymes.py, et il justifie le même
# traitement.
#
# La construction de requête, elle, se teste sans moteur : ce qui s'y joue
# est un choix de champs et de clauses, pas un comportement d'indexation.

import pytest

import cluster_status
import search_query

requiert_es = pytest.mark.requires_elasticsearch

INDEX_SONDE = "docsearch_test_sonde_exacte"
DELAI_ES = 60

# Repris de docsearch-ingestion/app/indexer.py (ANALYSE / CHAMP_EXACT) —
# c'est CETTE composition que les tests valident, la liste de filtres
# comprise. Toute divergence entre les deux fichiers doit faire échouer
# quelque chose ici.
ANALYSE = {
    "analyzer": {
        "french": {
            "tokenizer": "standard",
            "filter": ["lowercase", "french_stop", "french_stemmer"],
        },
        "exact": {
            "tokenizer": "standard",
            "filter": ["lowercase", "asciifolding"],
        },
    },
    "filter": {
        "french_stop": {"type": "stop", "stopwords": "_french_"},
        "french_stemmer": {"type": "stemmer", "language": "light_french"},
    },
}

DOCUMENTS = [
    {"content": "Délégations de service public et Congrès annuel"},
    {"content": "État de l'art des systèmes documentaires"},
]


@pytest.fixture(scope="module")
def es():
    from elasticsearch import Elasticsearch

    client = Elasticsearch(cluster_status.ES_HOST, request_timeout=DELAI_ES, max_retries=0)
    yield client
    client.close()


def _attendre_index_utilisable(es) -> None:
    """Saute les tests si le shard de l'index de sonde ne s'active pas.

    `requires_elasticsearch` ne vérifie qu'une chose : le moteur répond en
    HTTP (voir _elasticsearch_reachable dans conftest.py). Un cluster peut
    répondre parfaitement tout en refusant d'allouer le moindre nouveau
    shard — c'est le cas dès que le disque dépasse le *high watermark*
    (90 % par défaut) : l'index se crée, la création est acquittée, et
    c'est l'indexation du premier document qui échoue une minute plus tard
    sur un `unavailable_shards_exception`.

    Sans cette attente, la suite ne montre pas « l'environnement est
    saturé » mais une pile d'erreurs de setup qui ressemblent à une
    régression de la recherche exacte. On distingue donc explicitement les
    deux, et on nettoie l'index à demi créé au passage — un shard non
    alloué de plus laisse le cluster en rouge pour tout le monde.
    """
    # Deux formes pour le même signal : le client lève sur le 408 que
    # renvoie l'attente expirée, mais rend le corps (avec `timed_out`)
    # dans d'autres versions. On traite les deux plutôt que de parier sur
    # l'une — un test d'environnement qui échoue sur la façon dont
    # l'échec est signalé n'aide personne.
    try:
        expire = es.cluster.health(
            index=INDEX_SONDE, wait_for_status="yellow", timeout="15s",
        ).get("timed_out", False)
    except Exception:
        expire = True

    if not expire:
        return

    es.indices.delete(index=INDEX_SONDE, ignore_unavailable=True)
    pytest.skip(
        "Elasticsearch répond mais n'alloue plus de shard — disque au-delà du "
        "high watermark (voir GET _cluster/allocation/explain)."
    )


@pytest.fixture(scope="module")
def index(es):
    """Index de sonde portant le même couple de champs que la production :
    `content` analysé en français, `content.exact` en analyseur exact."""
    if es.indices.exists(index=INDEX_SONDE):
        es.indices.delete(index=INDEX_SONDE)

    es.indices.create(
        index=INDEX_SONDE,
        # `wait_for_active_shards=0` : sans lui, la création BLOQUE 30 s
        # quand le disque dépasse le high watermark et qu'aucun shard neuf
        # ne peut être alloué. On rend la main tout de suite et c'est
        # _attendre_index_utilisable() qui tranche — un test
        # d'environnement doit conclure vite, pas faire patienter la suite
        # entière pour un verdict connu d'avance.
        wait_for_active_shards=0,
        settings={
            "number_of_shards": 1,
            # Zéro réplique : sur cette VM à un seul nœud, une réplique
            # reste éternellement non assignée et laisse le cluster en
            # « yellow » — un bruit dont les tests n'ont pas besoin.
            "number_of_replicas": 0,
            "analysis": ANALYSE,
        },
        mappings={
            "properties": {
                "content": {
                    "type": "text",
                    "analyzer": "french",
                    "fields": {"exact": {"type": "text", "analyzer": "exact"}},
                }
            }
        },
    )
    _attendre_index_utilisable(es)

    for document in DOCUMENTS:
        es.index(index=INDEX_SONDE, document=document, refresh=True)

    yield es

    es.indices.delete(index=INDEX_SONDE, ignore_unavailable=True)


def _trouve(es, requete: str, champ: str) -> int:
    """Nombre de documents trouvés pour `requete` sur `champ`.

    Sans `fuzziness` d'aucun côté : ce qui est mesuré ici est l'effet de
    l'ANALYSEUR, et la tolérance aux fautes le masquerait en rattrapant
    précisément les écarts qu'on veut voir subsister.
    """
    res = es.search(
        index=INDEX_SONDE,
        query={"multi_match": {"query": requete, "fields": [champ]}},
    )
    return res["hits"]["total"]["value"]


# ── L'analyseur : ce que le moteur fait vraiment ────────────────────

@requiert_es
def test_le_pluriel_ne_repond_plus_au_singulier(index):
    # La propriété centrale, et la seule raison d'être de tout le
    # chantier : « Délégations » est indexé, « délégation » ne doit PAS le
    # trouver en recherche exacte. La recherche ordinaire, elle, continue
    # de le faire — c'est la comparaison qui donne son sens au test, une
    # assertion isolée sur le champ exact passerait tout aussi bien si
    # l'index était vide.
    assert _trouve(index, "délégation", "content") == 1
    assert _trouve(index, "délégation", "content.exact") == 0
    assert _trouve(index, "délégations", "content.exact") == 1


@requiert_es
@pytest.mark.parametrize("requete", ["Congrès", "congres", "CONGRES", "congrès"])
def test_les_accents_et_la_casse_sont_ignores(index, requete):
    # « aux accents et aux majuscules près » : les quatre écritures sont
    # une seule requête. C'est ce que garantissent `lowercase` et
    # `asciifolding`, et c'est ce qui distingue cette recherche exacte
    # d'une comparaison littérale d'octets.
    assert _trouve(index, requete, "content.exact") == 1


@requiert_es
def test_les_mots_vides_sont_conserves(index):
    # « état de l'art » : sans mots vides, l'expression perd « de » et
    # « l' » et ne veut plus rien dire. Le filtre french_stop est donc
    # absent de l'analyseur exact — ce test le prouve par l'usage.
    res = index.search(
        index=INDEX_SONDE,
        query={"match_phrase": {"content.exact": "état de l'art"}},
    )
    assert res["hits"]["total"]["value"] == 1


def _jetons(es, analyseur: dict, texte: str) -> list[str]:
    """Jetons produits par un analyseur DÉCRIT EN LIGNE, sans index.

    Volontairement sans index : `_analyze` accepte une composition
    anonyme, ce qui n'alloue aucun shard et ne dépend donc pas de l'état
    du disque. Les tests qui suivent tournent ainsi même quand le cluster
    refuse toute nouvelle allocation — et ce sont eux qui portent la
    propriété centrale du chantier.
    """
    return [
        j["token"]
        for j in es.indices.analyze(**analyseur, text=texte)["tokens"]
    ]


@requiert_es
def test_l_analyseur_exact_ne_produit_aucun_radical(es):
    # Vérification directe de la composition de l'analyseur, et la plus
    # parlante : `_analyze` dit ce que le moteur fait RÉELLEMENT du texte,
    # là où un test de recherche n'en montre que la conséquence. Si
    # quelqu'un ajoute un stemmer par symétrie avec l'analyseur français,
    # c'est ici que ça se voit en clair.
    exact = {
        "tokenizer": ANALYSE["analyzer"]["exact"]["tokenizer"],
        "filter": ["lowercase", "asciifolding"],
    }
    francais = {
        "tokenizer": "standard",
        "filter": [
            "lowercase",
            {"type": "stop", "stopwords": "_french_"},
            {"type": "stemmer", "language": "light_french"},
        ],
    }
    texte = "Délégations de Congrès CONGRES congres"

    # Le pluriel survit, les mots vides restent, accents et casse sont
    # repliés sur une seule forme.
    assert _jetons(es, exact, texte) == [
        "delegations", "de", "congres", "congres", "congres",
    ]
    # La comparaison donne son sens au test : l'analyseur français, lui,
    # racinise (« deleg ») et supprime « de ». C'est précisément ce que la
    # recherche exacte doit cesser de faire.
    assert _jetons(es, francais, texte) == ["deleg", "congr", "congr", "congr"]


# ── La construction de requête : sans moteur ────────────────────────

def test_les_champs_exacts_portent_les_memes_poids():
    # Les poids disent qu'un mot trouvé dans un titre compte plus que dans
    # le corps ; ça ne dépend pas de la façon dont le texte est analysé.
    # Un jeu de poids qui ne s'appliquerait qu'à la recherche ordinaire
    # ferait diverger les deux classements sans que rien ne le signale.
    ordinaire = search_query.field_sets()
    exact = search_query.field_sets(exact=True)

    # Le poids se lit après « ^ » : on le retire avant de vérifier le
    # suffixe, sinon `title.exact^4` ne « finit » pas par `.exact` et
    # l'assertion passerait pour de mauvaises raisons.
    assert all(champ.split("^")[0].endswith(".exact") for champ in exact["all"])
    assert [c.split("^")[1] for c in ordinaire["all"] if "^" in c] == [
        c.split("^")[1] for c in exact["all"] if "^" in c
    ]
    # `author.text` est le sous-champ analysé de la recherche ordinaire ;
    # son équivalent exact est `author.exact`, PAS `author.text.exact` —
    # Elasticsearch refuse un sous-champ de sous-champ.
    assert exact["author"] == ["author.exact"]
    assert "author.exact" in exact["all"]


def test_la_recherche_exacte_ne_tolere_aucune_faute():
    # La tolérance aux fautes rattraperait exactement les écarts que la
    # recherche exacte est censée conserver : les deux ne peuvent pas
    # coexister dans la même clause.
    clause = search_query.build_text_clause("délégation", "all", exact=True)
    assert "fuzziness" not in clause["multi_match"]
    assert clause["multi_match"]["fields"] == search_query.field_sets(exact=True)["all"]

    ordinaire = search_query.build_text_clause("délégation", "all", exact=False)
    assert ordinaire["multi_match"]["fuzziness"] == "AUTO"


def test_guillemets_et_mode_exact_sont_independants():
    # Les deux dimensions se croisent : les guillemets disent « dans cet
    # ordre », le mode exact dit « tels qu'écrits ». Les quatre
    # combinaisons ont un sens, et confondre les deux notions est
    # l'erreur naturelle — d'où ce test.
    phrase_ordinaire = search_query.build_text_clause('"délégation de service"', "all")
    phrase_exacte = search_query.build_text_clause('"délégation de service"', "all", exact=True)

    for clause in (phrase_ordinaire, phrase_exacte):
        assert clause["multi_match"]["type"] == "phrase"
        assert clause["multi_match"]["query"] == "délégation de service"

    assert phrase_ordinaire["multi_match"]["fields"] == search_query.field_sets()["all"]
    assert phrase_exacte["multi_match"]["fields"] == search_query.field_sets(exact=True)["all"]


def test_une_requete_vide_matche_tout_quel_que_soit_le_mode():
    # Champ de recherche vide mais filtres actifs (syntaxe avancée seule) :
    # le mode exact ne doit pas transformer ce cas en « aucun résultat ».
    assert search_query.build_text_clause("", "all", exact=True) == {"match_all": {}}


def test_une_alerte_rejoue_le_mode_exact_de_la_recherche_enregistree(monkeypatch):
    # Une alerte doit notifier sur ce que la recherche affiche. Si le
    # critère `exact` se perdait à l'enregistrement ou à la relecture,
    # l'alerte notifierait BEAUCOUP plus large que la recherche qui l'a
    # créée — et l'utilisateur n'aurait aucun moyen de comprendre pourquoi.
    monkeypatch.setattr(search_query, "get_effective_groups", lambda _: [])
    monkeypatch.setattr(search_query, "_searchable_source_names", lambda _: ["documents"])

    exacte = search_query.build_query_clauses(
        {"query": "délégation", "exact": True}, "sonde",
    )["bool"]["must"][0]["multi_match"]
    ordinaire = search_query.build_query_clauses(
        {"query": "délégation"}, "sonde",
    )["bool"]["must"][0]["multi_match"]

    assert "fuzziness" not in exacte
    assert exacte["fields"] == search_query.field_sets(exact=True)["all"]
    assert ordinaire["fuzziness"] == "AUTO"
