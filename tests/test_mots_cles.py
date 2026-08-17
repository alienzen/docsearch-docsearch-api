# tests/test_mots_cles.py — Le filtre par mot-clé ignore-t-il la casse,
# et rien d'autre ?
#
# Le champ `keywords` est un `keyword` Elasticsearch : indexé tel quel,
# sans analyse. « Budget », « budget » et « BUDGET » y sont donc trois
# termes distincts — alors qu'ils viennent tous de la propriété
# « Mots-clés » saisie à la main dans Word ou Acrobat, où la casse ne
# veut rien dire. `mots-cles:budget` ne ramenait rien sur un corpus qui
# n'écrit que « Budget », sans la moindre erreur pour le signaler.
#
# Rien de tout cela ne se vérifie en lisant le code : `case_insensitive`
# est un comportement du MOTEUR sur un type de champ précis. Un test qui
# se contenterait de comparer le dictionnaire produit par
# _keywords_filter() à un littéral attendu ne prouverait que la
# recopie — d'où des requêtes réellement exécutées ici.
#
# Trois propriétés, dont deux sont des NON-régressions :
#
#   1. la casse est ignorée, dans les deux sens (chercher en minuscules
#      trouve les majuscules, et l'inverse) ;
#   2. les ACCENTS comptent toujours — limite assumée du choix fait :
#      les effacer demanderait un `normalizer` sur le mapping, donc une
#      réindexation complète. Ce test fige la limite plutôt que de la
#      laisser se découvrir en production ;
#   3. la combinaison en ET reste un ET : le drapeau ne devait pas, au
#      passage, transformer la sélection cumulative en OU.
#
# ⚠️ Comme tout module créant un index jetable, ces tests exigent du
# DISQUE et pas seulement un moteur qui répond — voir l'avertissement
# détaillé en tête de test_temps_recherche.py.

import pytest

import cluster_status
import search_api
import search_query

requiert_es = pytest.mark.requires_elasticsearch

INDEX_SONDE = "docsearch_test_sonde_mots_cles"
DELAI_ES = 60

# Même mapping que la production pour ce champ (voir create_index dans
# docsearch-ingestion/app/indexer.py) : c'est le type `keyword` qui rend
# le filtre sensible à la casse, un `text` passerait le test sans que la
# correction serve à rien.
DOCUMENTS = {
    "maj":     ["Budget", "Congés"],
    "min":     ["budget", "conges"],
    "melange": ["BUDGET", "Rapport"],
    "autre":   ["Rapport"],
}


@pytest.fixture(scope="module")
def es():
    from elasticsearch import Elasticsearch

    client = Elasticsearch(cluster_status.ES_HOST, request_timeout=DELAI_ES, max_retries=0)
    yield client
    client.close()


@pytest.fixture(scope="module")
def index(es):
    es.indices.delete(index=INDEX_SONDE, ignore_unavailable=True)
    es.indices.create(
        index=INDEX_SONDE,
        # Nœud unique : une réplique resterait non assignée et laisserait
        # le cluster en « yellow » pour rien (même choix que
        # test_recherche_exacte.py).
        settings={"number_of_shards": 1, "number_of_replicas": 0},
        mappings={"properties": {
            "keywords": {"type": "keyword", "fields": {"text": {"type": "text"}}},
        }},
    )
    for identifiant, mots in DOCUMENTS.items():
        es.index(index=INDEX_SONDE, id=identifiant, document={"keywords": mots})
    es.indices.refresh(index=INDEX_SONDE)
    yield es
    es.indices.delete(index=INDEX_SONDE, ignore_unavailable=True)


def _trouve(es, filtre) -> set[str]:
    res = es.search(index=INDEX_SONDE, query={"bool": {"filter": [filtre]}}, size=50)
    return {h["_id"] for h in res["hits"]["hits"]}


# ── La casse, des deux côtés ─────────────────────────────────

@requiert_es
@pytest.mark.parametrize("saisi", ["budget", "Budget", "BUDGET", "BuDgEt"])
def test_la_casse_du_mot_cle_est_ignoree(index, saisi):
    """Les quatre écritures désignent le même filtre, et ramènent les
    trois documents qui portent ce mot-clé quelle qu'en soit la casse."""
    assert _trouve(index, search_api._keywords_filter(saisi)) == {"maj", "min", "melange"}


@requiert_es
def test_les_accents_comptent_toujours(index):
    """Limite assumée : « conges » ne trouve pas « Congés ». Le jour où
    l'on ajoutera un `normalizer` au mapping, c'est CE test qui devra
    changer — et il dira pourquoi."""
    assert _trouve(index, search_api._keywords_filter("conges")) == {"min"}
    assert _trouve(index, search_api._keywords_filter("congés")) == {"maj"}


@requiert_es
def test_la_casse_n_est_repliee_que_sur_l_ascii(index):
    """La limite exacte du drapeau, et elle surprend : `case_insensitive`
    est un automate Lucene, pas un `toLowerCase()` Unicode. « congéS »
    trouve « Congés » (le S est ASCII), « CONGÉS » ne le trouve pas — le
    « É » de la requête ne se replie pas sur le « é » indexé.

    Écrit ici pour que la limite soit connue plutôt que découverte en
    production. Le cas visé par la correction — une valeur capitalisée
    dans les métadonnées, tapée en minuscules dans la barre — reste
    couvert ; seul le `normalizer` évoqué dans _keywords_filter()
    couvrirait le reste."""
    assert _trouve(index, search_api._keywords_filter("congéS")) == {"maj"}
    assert _trouve(index, search_api._keywords_filter("CONGÉS")) == set()


@requiert_es
def test_un_mot_cle_absent_ne_ramene_rien(index):
    """L'insensibilité à la casse n'est pas une recherche approximative :
    elle ne doit pas ouvrir la porte aux correspondances partielles."""
    assert _trouve(index, search_api._keywords_filter("budg")) == set()


# ── Le ET, qui ne devait pas bouger ──────────────────────────

@requiert_es
def test_deux_mots_cles_restent_combines_en_et(index):
    """La propriété que le drapeau ne devait pas emporter : cocher un
    second mot-clé RESTREINT. En OU, cette requête ramènerait aussi
    « autre »."""
    assert _trouve(index, search_api._keywords_filter(["budget", "rapport"])) == {"melange"}


@requiert_es
def test_le_et_vaut_aussi_pour_le_worker_d_alertes(index):
    """search_query.py tient une COPIE du filtre pour le worker
    d'alertes : les deux doivent se comporter à l'identique, sans quoi
    une alerte cesserait de se déclencher là où la recherche manuelle
    trouve (ou l'inverse), sans erreur nulle part."""
    for mots in ("budget", ["budget", "rapport"], "congés"):
        assert _trouve(index, search_query._keywords_filter(mots)) == \
               _trouve(index, search_api._keywords_filter(mots))


# ── Sans moteur ──────────────────────────────────────────────

def test_aucun_mot_cle_ne_pose_aucun_filtre():
    """Ni `{}` ni un filtre vide : `None`, que l'appelant sait ne pas
    ajouter à sa liste de filtres."""
    assert search_api._keywords_filter(None) is None
    assert search_api._keywords_filter([]) is None
    assert search_query._keywords_filter(None) is None
