# tests/test_zero_resultat.py — Quand une recherche ne donne rien, ce
# qu'on propose à l'utilisateur est-il vrai, et atteignable ?
#
# Deux règles gouvernent cette aide, et ce sont elles que ces tests
# protègent :
#
# 1. **Chaque compte annoncé doit être atteignable.** « 12 résultats sans
#    le filtre .pdf » calculé hors ACL, puis un clic qui affiche une liste
#    vide, coûte plus de confiance qu'un écran vide honnête.
# 2. **La correction orthographique ne doit pas fuir.** Le correcteur
#    d'Elasticsearch travaille sur le dictionnaire de termes de l'index,
#    que l'ACL ne filtre pas : un mot tiré d'un document interdit pourrait
#    être proposé. La correction n'est rendue que si elle donne des
#    résultats VISIBLES par cet utilisateur.
#
# Elasticsearch est le vrai (principe 1 de conftest.py) : le correcteur,
# les agrégations et les comptes sont les siens — un bouchon ne
# prouverait que ma relecture. Index jetable (principe 2).

import pytest

import cluster_status

requiert_es = pytest.mark.requires_elasticsearch

INDEX_SONDE = "docsearch_test_sonde_zero_resultat"
DELAI_ES = 60

# Filtre ACL de quelqu'un qui n'a droit à rien : sa seule présence doit
# ramener toute proposition à zéro.
ACL_AVEUGLE = {"term": {"acl.public": False}}
ACL_OUVERTE = {"term": {"acl.public": True}}


@pytest.fixture(scope="module")
def es():
    from elasticsearch import Elasticsearch

    client = Elasticsearch(cluster_status.ES_HOST, request_timeout=DELAI_ES, max_retries=0)
    yield client
    client.close()


@pytest.fixture(scope="module")
def api(es):
    """Le module de l'API, branché sur l'index jetable.

    `search_api` charge FastAPI, Kafka et LDAP à l'import — c'est le prix
    d'un dépôt où les modules sont à plat, et les tests d'identité le
    paient déjà."""
    import search_api

    if es.indices.exists(index=INDEX_SONDE):
        es.indices.delete(index=INDEX_SONDE)
    es.indices.create(
        index=INDEX_SONDE,
        mappings={
            "properties": {
                "content": {"type": "text"},
                "extension": {"type": "keyword"},
                "source": {"type": "keyword"},
                "acl": {"properties": {"public": {"type": "boolean"}}},
            }
        },
    )
    # « rapport » est fréquent : c'est ce qui permet au correcteur de le
    # proposer en « popular » face à « raport », qui n'existe pas.
    for i in range(8):
        es.index(
            index=INDEX_SONDE,
            document={
                "content": f"rapport annuel numéro {i}",
                "extension": ".docx",
                "source": "documents",
                "acl": {"public": True},
            },
        )
    es.index(
        index=INDEX_SONDE,
        document={
            "content": "rapport archivé",
            "extension": ".docx",
            "source": "archives",
            "acl": {"public": True},
        },
    )
    es.indices.refresh(index=INDEX_SONDE)

    precedent_es, precedent_alias = search_api.es, search_api.ES_SEARCH_ALIAS
    search_api.es = es
    search_api.ES_SEARCH_ALIAS = INDEX_SONDE
    yield search_api
    search_api.es, search_api.ES_SEARCH_ALIAS = precedent_es, precedent_alias
    es.indices.delete(index=INDEX_SONDE)


def _must(texte: str) -> list:
    return [{"multi_match": {"query": texte, "fields": ["content"], "fuzziness": "AUTO"}}]


def _aide(api, *, texte="rapport", obligatoires=None, facettes=None, relachables=None):
    return api._aide_zero_resultat(
        must=_must(texte),
        obligatoires=obligatoires if obligatoires is not None else [ACL_OUVERTE],
        relachables=relachables or {},
        facet_filters=facettes or {},
        # L'aide rejoue la clause de /search sur la requête corrigée : elle
        # reçoit donc de quoi la reconstruire, et non une liste de champs.
        # L'index de sonde n'a ni `title` ni sous-champs `.exact` — un
        # `multi_match` sur un champ absent ne matche rien et ne lève rien,
        # seul `content` répond, ce que ces tests attendent.
        search_in="all",
        exact=False,
        query_text=texte,
    )


# ── 1. Relâchement de filtre ─────────────────────────────────

@requiert_es
def test_annonce_ce_que_donnerait_le_retrait_d_un_filtre(api):
    """Aucun document n'est un PDF : retirer ce filtre en découvre neuf."""
    aide = _aide(api, facettes={"extension": {"terms": {"extension": [".pdf"]}}})

    assert {"field": "extension", "count": 9} in aide["relaxations"]


@requiert_es
def test_le_compte_annonce_respecte_l_acl(api):
    """LE test de ce fichier : le même relâchement, vu par quelqu'un qui
    n'a droit à rien, ne doit annoncer aucun résultat — sinon le clic
    afficherait une liste vide."""
    aide = _aide(
        api,
        obligatoires=[ACL_AVEUGLE],
        facettes={"extension": {"terms": {"extension": [".pdf"]}}},
    )

    assert aide.get("relaxations", []) == []


@requiert_es
def test_propose_de_tout_retirer_a_partir_de_deux_filtres(api):
    """Avec un seul filtre, « sans aucun filtre » dirait exactement la
    même chose que la ligne précédente."""
    deux = {
        "extension": {"terms": {"extension": [".pdf"]}},
        "source": {"terms": {"source": ["inexistante"]}},
    }
    champs = {r["field"] for r in _aide(api, facettes=deux)["relaxations"]}
    assert "__all__" in champs

    un_seul = {"extension": {"terms": {"extension": [".pdf"]}}}
    champs = {r["field"] for r in _aide(api, facettes=un_seul)["relaxations"]}
    assert "__all__" not in champs


@requiert_es
def test_relache_aussi_la_periode(api):
    """La période n'est pas une facette mais elle vient d'un choix de
    l'utilisateur : elle doit pouvoir être relâchée comme les autres."""
    aide = _aide(
        api,
        relachables={"date": {"range": {"date_modified": {"gte": "2099-01-01"}}}},
    )
    assert {"field": "date", "count": 9} in aide["relaxations"]


# ── 2. Autres sources ────────────────────────────────────────

@requiert_es
def test_indique_les_autres_sources(api):
    aide = _aide(api, facettes={"source": {"terms": {"source": ["inexistante"]}}})

    sources = {bucket["key"]: bucket["doc_count"] for bucket in aide["sources"]}
    assert sources == {"documents": 8, "archives": 1}


# ── 3. Correction orthographique ─────────────────────────────

@requiert_es
def test_corrige_une_faute_de_frappe(api):
    aide = _aide(api, texte="raport", facettes={"extension": {"terms": {"extension": [".pdf"]}}})

    assert aide["suggestion"] == "rapport"


@requiert_es
def test_ne_corrige_pas_vers_ce_qui_est_invisible(api):
    """Le correcteur lit le dictionnaire de termes, que l'ACL ne filtre
    pas. Une correction qui ne mène à aucun document visible ne doit pas
    être proposée — elle divulguerait un mot d'un document interdit, et
    elle ne mènerait nulle part."""
    aide = _aide(
        api,
        texte="raport",
        obligatoires=[ACL_AVEUGLE],
        facettes={"extension": {"terms": {"extension": [".pdf"]}}},
    )

    assert aide.get("suggestion") is None


@requiert_es
def test_ne_propose_rien_quand_il_n_y_a_rien_a_proposer(api):
    """Un mot qui n'est ni corrigible ni débloqué par un relâchement :
    l'objet est vide, et l'écran retombe sur son message d'origine."""
    assert _aide(api, texte="xyzzynonexistent") == {}


def test_la_correction_respecte_les_positions():
    """Un remplacement naïf par str.replace toucherait aussi les
    occurrences du mot à l'intérieur d'un autre."""
    import search_api

    entrees = [{"text": "raport", "offset": 0, "length": 6, "options": [{"text": "rapport"}]}]
    assert search_api._corriger_requete("raport raportage", entrees) == "rapport raportage"


def test_pas_de_correction_si_elle_ne_change_rien():
    import search_api

    entrees = [{"text": "rapport", "offset": 0, "length": 7, "options": [{"text": "Rapport"}]}]
    assert search_api._corriger_requete("rapport", entrees) is None
