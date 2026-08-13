# tests/test_resultats_partiels.py — Une réponse Elasticsearch amputée
# ne doit jamais passer pour un résultat.
#
# Le défaut protégé ici a coûté une demi-journée le 2026-08-13 : une
# facette personnalisée déclarée sur un champ `text` posait une
# agrégation `terms` impossible, Elasticsearch répondait 200 avec 13
# shards sur 14 en échec, et l'API lisait `hits.total` sans regarder
# `_shards`. La recherche fédérée annonçait « 0 résultat » sur 23 000
# documents, en 5 ms, sans erreur ni log — pendant que la même recherche
# restreinte à une source répondait normalement.
#
# Elasticsearch est le vrai (principe 1 de conftest.py), et il l'est ici
# plus qu'ailleurs : c'est la FORME EXACTE de sa réponse partielle qui
# est en cause. Une réponse fabriquée à la main ne prouverait que ma
# lecture de la documentation — or c'est de cette lecture que le défaut
# est né.
#
# ⚠️  Il faut DEUX index sous un alias, et non un seul : quand TOUS les
# shards échouent, ES lève une vraie erreur, que l'API remontait déjà
# correctement. Le silence n'apparaît qu'en réponse PARTIELLE — un shard
# qui répond, un qui échoue — c'est-à-dire exactement la situation d'une
# recherche fédérée sur `docsearch-all`, où les index n'ont pas tous le
# même mapping.

import pytest

import cluster_status

requiert_es = pytest.mark.requires_elasticsearch

ALIAS_SONDE  = "docsearch_test_sonde_partiels"
INDEX_TEXTE  = "docsearch_test_sonde_partiels_texte"    # `titre` en text  → agrégation impossible
INDEX_MOTCLE = "docsearch_test_sonde_partiels_motcle"   # `titre` en keyword → agrégation possible
DELAI_ES = 60


@pytest.fixture(scope="module")
def es():
    from elasticsearch import Elasticsearch

    client = Elasticsearch(cluster_status.ES_HOST, request_timeout=DELAI_ES, max_retries=0)
    yield client
    client.close()


@pytest.fixture(scope="module")
def sonde(es):
    """Deux index jetables sous un alias, ne différant QUE par le type de
    `titre` — le seul ingrédient du défaut."""
    for index, type_titre, nombre in ((INDEX_TEXTE, "text", 5), (INDEX_MOTCLE, "keyword", 2)):
        if es.indices.exists(index=index):
            es.indices.delete(index=index)
        es.indices.create(
            index=index,
            mappings={
                "properties": {
                    "titre":  {"type": type_titre},
                    "bureau": {"type": "keyword"},
                    "source": {"type": "keyword"},
                    "acl":    {"properties": {"public": {"type": "boolean"}}},
                }
            },
            aliases={ALIAS_SONDE: {}},
        )
        for i in range(nombre):
            es.index(
                index=index,
                document={
                    "titre":  f"rapport {i}",
                    "bureau": "B12",
                    # `source` doit être une source réellement cherchable :
                    # /search filtre dessus avant toute chose.
                    "source": "documents",
                    "acl":    {"public": True},
                },
            )
        es.indices.refresh(index=index)

    yield ALIAS_SONDE

    es.indices.delete(index=[INDEX_TEXTE, INDEX_MOTCLE])


@pytest.fixture(scope="module")
def api(sonde):
    """Le module de l'API, branché sur l'alias jetable.

    `search_api` charge FastAPI, Kafka et LDAP à l'import — c'est le prix
    d'un dépôt où les modules sont à plat, et les tests d'identité le
    paient déjà."""
    import search_api

    precedent_alias = search_api.ES_SEARCH_ALIAS
    search_api.ES_SEARCH_ALIAS = sonde
    yield search_api
    search_api.ES_SEARCH_ALIAS = precedent_alias


def _agreger(es, champ: str) -> dict:
    """Réponse brute d'ES à une agrégation `terms` sur `champ` — même
    forme que celle construite par facet_agg()."""
    return es.search(
        index=ALIAS_SONDE,
        size=0,
        query={"match_all": {}},
        aggs={"facette": {"terms": {"field": champ, "size": 20}}},
    ).body


# ── 1. La prémisse, vérifiée sur le vrai moteur ──────────────

@requiert_es
def test_elasticsearch_rend_un_compte_faux_sans_lever_d_erreur(sonde, es):
    """Ce que fait vraiment ES, et qui justifie les tests suivants :
    l'index dont le shard échoue disparaît du compte, en silence.

    Sept documents existent, le client n'en voit que deux — et rien dans
    la réponse, hors `_shards`, ne le dit."""
    res = _agreger(es, "titre")

    assert res["_shards"]["failed"] == 1
    assert res["_shards"]["successful"] == 1
    assert res["hits"]["total"]["value"] == 2        # 7 documents indexés
    # La facette est fausse de la même façon, et c'est le plus perfide :
    # elle n'est pas vide, elle est incomplète — donc crédible.
    buckets = res["aggregations"]["facette"]["buckets"]
    assert sum(b["doc_count"] for b in buckets) == 2


# ── 2. Le garde-fou ──────────────────────────────────────────

@requiert_es
def test_un_resultat_partiel_leve_une_erreur_au_lieu_d_un_compte_faux(api, es):
    """LE test de ce fichier : plutôt que de rendre le compte tronqué,
    l'API refuse la réponse."""
    from fastapi import HTTPException

    res = _agreger(es, "titre")

    with pytest.raises(HTTPException) as erreur:
        api._verifier_shards(res, "search")

    assert erreur.value.status_code == 500
    # Le motif d'ES doit être remonté tel quel : c'est lui, et lui seul,
    # qui désigne le champ fautif.
    assert "Fielddata is disabled" in erreur.value.detail
    assert INDEX_TEXTE in erreur.value.detail


@requiert_es
def test_une_reponse_complete_passe_sans_bruit(api, es):
    """Le chemin nominal ne paie rien : même agrégation sur un champ
    agrégeable dans les deux index, aucun refus."""
    res = _agreger(es, "bureau")

    assert res["_shards"]["failed"] == 0
    api._verifier_shards(res, "search")             # ne lève pas
    assert res["hits"]["total"]["value"] == 7


# ── 3. Bout en bout, par la route ────────────────────────────

@requiert_es
@pytest.mark.requires_redis
@pytest.mark.requires_ldap
def test_la_recherche_refuse_au_lieu_d_annoncer_zero(api):
    """La panne du 2026-08-13 rejouée par POST /search : une facette
    personnalisée sur un champ `text` doit faire échouer la recherche,
    pas lui faire annoncer zéro résultat.

    Seul `_active_custom_facets` est remplacé — par ce que le registre
    des sources SQL renvoyait vraiment ce jour-là. Tout le reste de la
    route est le vrai : ACL résolue sur l'annuaire de dev, filtre de
    sources lu dans Redis, requête envoyée au vrai moteur."""
    from fastapi import HTTPException

    precedent = api._active_custom_facets
    api._active_custom_facets = lambda source_names, username=None: {"titre": "Titre"}
    try:
        with pytest.raises(HTTPException) as erreur:
            api.search(_RequeteDeRecherche(), _RequeteHttp(), user="alice.admin")
    finally:
        api._active_custom_facets = precedent

    assert erreur.value.status_code == 500
    assert "Fielddata is disabled" in erreur.value.detail


class _RequeteDeRecherche:
    """Les attributs de SearchQuery que search() lit — un objet nu plutôt
    que le modèle Pydantic, pour que l'ajout d'un champ facultatif au
    modèle ne réécrive pas ce test."""
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
