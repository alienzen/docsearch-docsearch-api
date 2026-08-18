# tests/test_facette_periode.py — La section « Période de modification »
# ne doit s'afficher que si les résultats portent une date.
#
# Le filtre de période est un `range` sur `date_modified`, et un document
# qui n'a pas ce champ n'entre dans AUCUN intervalle : cocher une période
# le fait disparaître, sans message. Toute une source peut être dans ce
# cas — une source de module qui ne pousse pas de date, comme
# docsearch-plugin-annuaire, dont les données n'en contiennent aucune.
#
# Les quatre autres facettes fixes se retirent d'elles-mêmes de l'écran :
# elles n'ont plus de seaux à montrer, et l'interface les masque (voir
# seauxAffichables() côté docsearch-ui-vue). La période, elle, n'a pas de
# seaux — deux sélecteurs de date, rien à agréger — donc rien ne disait à
# l'interface qu'elle était inutile. C'est ce compte-là qui le dit.
#
# Elasticsearch est le vrai (principe 1 de conftest.py) : la question
# posée est « qu'est-ce qu'ES fait d'un document sans le champ », et une
# réponse fabriquée à la main ne prouverait que ma lecture de la
# documentation.

import pytest

import cluster_status

requiert_es = pytest.mark.requires_elasticsearch

ALIAS_SONDE = "docsearch_test_sonde_periode"
INDEX_SONDE = "docsearch_test_sonde_periode_index"
DELAI_ES = 60


@pytest.fixture(scope="module")
def es():
    from elasticsearch import Elasticsearch

    client = Elasticsearch(cluster_status.ES_HOST, request_timeout=DELAI_ES, max_retries=0)
    yield client
    client.close()


@pytest.fixture(scope="module")
def sonde(es):
    """Deux documents ne différant QUE par la présence de la date.

    « rapport daté » porte `date_modified`, « rapport sans date » ne le
    porte pas du tout — champ ABSENT, et non null : c'est la forme que
    produit le contrat des modules, qui retire les dates nulles avant
    d'indexer (documents.py, construire_document)."""
    if es.indices.exists(index=INDEX_SONDE):
        es.indices.delete(index=INDEX_SONDE)
    es.indices.create(
        index=INDEX_SONDE,
        mappings={
            "properties": {
                "title":         {"type": "text"},
                "content":       {"type": "text"},
                "date_modified": {"type": "date"},
                # `source` doit être une source réellement cherchable :
                # /search filtre dessus avant toute chose.
                "source":        {"type": "keyword"},
                "acl":           {"properties": {"public": {"type": "boolean"}}},
            }
        },
        aliases={ALIAS_SONDE: {}},
    )
    commun = {"source": "documents", "acl": {"public": True}}
    # Un mot propre à chacun, en plus du mot commun : il faut pouvoir
    # viser le document SANS date à lui seul (dernier test).
    es.index(index=INDEX_SONDE, document={
        **commun, "title": "rapport date", "content": "rapport budget",
        "date_modified": "2026-08-14T10:00:00+00:00",
    })
    es.index(index=INDEX_SONDE, document={
        **commun, "title": "rapport sans date", "content": "rapport annuaire",
    })
    es.indices.refresh(index=INDEX_SONDE)

    yield ALIAS_SONDE
    es.indices.delete(index=INDEX_SONDE)


@pytest.fixture(scope="module")
def api(sonde):
    """Le module de l'API, branché sur l'alias jetable — même montage que
    test_resultats_partiels.py."""
    import search_api

    precedent = search_api.ES_SEARCH_ALIAS
    search_api.ES_SEARCH_ALIAS = sonde
    yield search_api
    search_api.ES_SEARCH_ALIAS = precedent


# ── 1. La prémisse, vérifiée sur le vrai moteur ──────────────

@requiert_es
def test_un_document_sans_date_n_entre_dans_aucune_periode(sonde, es):
    """Ce qui rend la section trompeuse : le document sans date n'est pas
    « hors de l'intervalle », il est introuvable par tout intervalle, si
    large soit-il."""
    res = es.search(index=ALIAS_SONDE, query={"range": {"date_modified": {
        "gte": "1970-01-01", "lte": "2100-01-01",
    }}})

    assert res["hits"]["total"]["value"] == 1


# ── 2. Le compte que l'interface attend ──────────────────────

@requiert_es
@pytest.mark.requires_redis
@pytest.mark.requires_ldap
def test_la_recherche_compte_les_resultats_dates(api):
    """LE test de ce fichier : deux résultats, un seul daté.

    Un compte, pas une liste de seaux — l'interface n'a besoin que de
    savoir s'il y a quelque chose à filtrer."""
    reponse = api.search(_RequeteDeRecherche(), _RequeteHttp(), user="alice.admin")

    assert reponse["total"] == 2
    assert reponse["facets"]["with_date"] == 1


@requiert_es
@pytest.mark.requires_redis
@pytest.mark.requires_ldap
def test_une_recherche_sans_aucun_resultat_date_compte_zero(api):
    """Le cas qui justifie tout : la recherche ne ramène que le document
    sans date, et l'interface doit pouvoir retirer la section plutôt que
    d'offrir un sélecteur qui viderait l'écran."""
    requete = _RequeteDeRecherche()
    requete.query = "annuaire"      # ne figure que dans le document sans date

    reponse = api.search(requete, _RequeteHttp(), user="alice.admin")

    assert reponse["total"] == 1
    assert reponse["facets"]["with_date"] == 0


class _RequeteDeRecherche:
    """Les attributs de SearchQuery que search() lit — un objet nu plutôt
    que le modèle Pydantic, pour que l'ajout d'un champ facultatif au
    modèle ne réécrive pas ce test (même choix que
    test_resultats_partiels.py)."""
    query = "rapport"
    search_in = "all"
    exact = False
    extension = None
    author = None
    keywords = None
    folder = None
    source = None
    custom = None
    has_attachments = False
    date_from = None
    date_to = None
    sort = "_score"
    from_ = 0
    size = 10


class _RequeteHttp:
    """Le minimum que get_client_ip() lit sur une requête Starlette."""
    headers: dict = {}
    client = None
