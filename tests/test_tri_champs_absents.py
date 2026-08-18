# tests/test_tri_champs_absents.py — Trier sur un champ que tous les
# index ne mappent pas.
#
# Défaut constaté le 2026-08-18, en production de dev : choisir « Date de
# modification » dans le sélecteur de tri produisait « Résultat
# incomplet : 1 shard(s) sur 18 en échec », premier motif
# « No mapping found for [date_modified] in order to sort on
# ([agents_sql]) ». Aucun document n'était en cause, et la recherche par
# pertinence répondait normalement sur le même corpus.
#
# La cause n'est pas dans les données mais dans les mappings : celui
# d'une source SQL ne contient que ses colonnes déclarées, plus `source`
# et `indexed_at` (sql_indexer._build_mapping). Une source SQL qui ne
# déclare pas de colonne de date n'a donc pas de `date_modified`, et
# Elasticsearch refuse de trier un index sur un champ qu'il ne connaît
# pas — il fait échouer le shard, ce que _verifier_shards() transforme à
# juste titre en refus.
#
# Elasticsearch est le vrai (principe 1 de conftest.py), et il l'est ici
# pour la même raison que dans test_resultats_partiels.py : c'est le
# comportement exact du moteur face à un champ non mappé qui est en
# cause, et c'est justement ce comportement qu'on avait mal anticipé. Un
# moteur bouchonné ne prouverait que ma lecture de la documentation.
#
# ⚠️  DEUX index sous un alias, dont UN SEUL mappe le champ trié : avec un
# seul index, un tri impossible lève une vraie erreur, que l'API
# remontait déjà. Le défaut n'existe qu'en réponse partielle — donc
# exactement la situation d'une recherche fédérée sur `docsearch-all`.

import pytest

import cluster_status

requiert_es = pytest.mark.requires_elasticsearch

ALIAS_SONDE = "docsearch_test_sonde_tri"
INDEX_DATE  = "docsearch_test_sonde_tri_date"    # mappe `date_modified`
INDEX_SANS  = "docsearch_test_sonde_tri_sans"    # ne le mappe pas — le cas d'agents_sql
DELAI_ES = 60


@pytest.fixture(scope="module")
def es():
    from elasticsearch import Elasticsearch

    client = Elasticsearch(cluster_status.ES_HOST, request_timeout=DELAI_ES, max_retries=0)
    yield client
    client.close()


@pytest.fixture(scope="module")
def sonde(es):
    """Deux index jetables sous un alias, ne différant QUE par la
    présence de `date_modified` dans le mapping — le seul ingrédient du
    défaut."""
    mappings = {
        INDEX_DATE: {
            "properties": {
                "titre":         {"type": "keyword"},
                "date_modified": {"type": "date"},
                "source":        {"type": "keyword"},
                "acl":           {"properties": {"public": {"type": "boolean"}}},
            }
        },
        # `dynamic: strict`, comme les vrais index SQL : sans ça, indexer
        # un document daté suffirait à créer le champ, et la sonde ne
        # reproduirait plus rien.
        INDEX_SANS: {
            "dynamic": "strict",
            "properties": {
                "titre":  {"type": "keyword"},
                "source": {"type": "keyword"},
                "acl":    {"properties": {"public": {"type": "boolean"}}},
            },
        },
    }
    documents = {
        INDEX_DATE: [
            {"titre": "rapport ancien",  "date_modified": "2020-01-01T00:00:00Z"},
            {"titre": "rapport recent",  "date_modified": "2026-08-01T00:00:00Z"},
        ],
        # Sans date : c'est leur place dans le classement qui est en jeu.
        INDEX_SANS: [{"titre": "agent sans date"}],
    }

    for index, mapping in mappings.items():
        if es.indices.exists(index=index):
            es.indices.delete(index=index)
        es.indices.create(index=index, mappings=mapping, aliases={ALIAS_SONDE: {}})
        for document in documents[index]:
            es.index(index=index, document={**document, "source": "documents",
                                            "acl": {"public": True}})
        es.indices.refresh(index=index)

    yield ALIAS_SONDE

    es.indices.delete(index=[INDEX_DATE, INDEX_SANS])


def _trier(es, clause) -> dict:
    return es.search(index=ALIAS_SONDE, query={"match_all": {}}, sort=clause).body


# ── 1. La prémisse, vérifiée sur le vrai moteur ──────────────

@requiert_es
def test_trier_sur_un_champ_non_mappe_fait_echouer_un_shard(sonde, es):
    """Ce que fait vraiment ES, et qui explique le message vu à l'écran.

    La clause d'AVANT le correctif : pas d'`unmapped_type`. L'index qui
    ne mappe pas le champ sort de la réponse, avec son document."""
    res = _trier(es, [{"date_modified": {"order": "desc", "missing": "_last"}}])

    assert res["_shards"]["failed"] == 1
    motif = res["_shards"]["failures"][0]["reason"]["reason"]
    assert "No mapping found" in motif and "date_modified" in motif
    # Trois documents existent, deux seulement sont comptés.
    assert res["hits"]["total"]["value"] == 2


# ── 2. Le correctif ──────────────────────────────────────────

@requiert_es
def test_unmapped_type_laisse_tous_les_shards_repondre(sonde, es):
    """La clause construite par _clause_de_tri() : aucun shard en échec,
    et le document sans date est là."""
    import search_api

    res = _trier(es, search_api._clause_de_tri("date_modified"))

    assert res["_shards"]["failed"] == 0
    assert res["hits"]["total"]["value"] == 3


@requiert_es
def test_les_documents_sans_date_se_rangent_en_fin_de_liste(sonde, es):
    """`missing: "_last"` reste respecté : ne pas porter de date ne doit
    pas valoir d'être en tête d'un tri par date."""
    import search_api

    res = _trier(es, search_api._clause_de_tri("date_modified"))
    titres = [h["_source"]["titre"] for h in res["hits"]["hits"]]

    assert titres == ["rapport recent", "rapport ancien", "agent sans date"]


@requiert_es
@pytest.mark.parametrize("tri", ["date_created", "filename", "size"])
def test_les_autres_tris_tiennent_aussi(sonde, es, tri):
    """Le défaut n'était pas propre à `date_modified` : AUCUN des index de
    la sonde ne mappe ces trois champs, et chacun cassait la recherche de
    la même façon. Le tri par taille, en particulier, échouait jusque sur
    les index de module, dont le mapping n'a pas de `size`."""
    import search_api

    res = _trier(es, search_api._clause_de_tri(tri))

    assert res["_shards"]["failed"] == 0
    assert res["hits"]["total"]["value"] == 3


# ── 3. Le tri inconnu ────────────────────────────────────────

def test_un_tri_inconnu_est_refuse_avec_un_message_utile():
    """Sans moteur : c'est une décision de l'API, pas d'Elasticsearch.

    Avant, un champ inconnu partait tel quel vers ES et produisait le
    même « Résultat incomplet » opaque. L'interface ne peut pas en
    envoyer — ce cas signale un appel direct à l'API."""
    import search_api
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as leve:
        search_api._clause_de_tri("colonne_inventee")

    assert leve.value.status_code == 400
    assert "colonne_inventee" in leve.value.detail
    # Le message dit quoi mettre à la place, pas seulement que c'est faux.
    assert "date_modified" in leve.value.detail


def test_la_pertinence_ne_porte_pas_d_unmapped_type():
    """`_score` n'est pas un champ : lui poser `unmapped_type` serait
    refusé par ES."""
    import search_api

    assert search_api._clause_de_tri("_score") == [{"_score": "desc"}]
