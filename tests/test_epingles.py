# tests/test_epingles.py — Un résultat épinglé met-il en avant sans
# jamais autoriser ?
#
# C'est la seule fonctionnalité de DocSearch où l'administration désigne
# nommément des documents à faire remonter. La question qui compte n'est
# donc pas « remontent-ils » mais « remontent-ils POUR QUI » : un
# épinglage est une mise en avant, jamais une autorisation, et le
# document doit rester invisible à qui n'a pas le droit de le voir.
#
# Elasticsearch est le vrai (principe 1 de conftest.py) : c'est sa
# relecture filtrée qui est testée. Le registre, lui, vit dans Redis —
# les tests écrivent sous la clé de production et la restaurent, faute
# d'une clé dédiée (le module lit une constante) : elle est sauvegardée
# avant et remise après, y compris en cas d'échec.

import pytest

import cluster_status
import pinned

requiert_es = pytest.mark.requires_elasticsearch
requiert_redis = pytest.mark.requires_redis

INDEX_SONDE = "docsearch_test_sonde_epingles"
DELAI_ES = 60

PUBLIC = "doc-public"
PRIVE = "doc-prive"


@pytest.fixture(scope="module")
def es():
    from elasticsearch import Elasticsearch

    client = Elasticsearch(cluster_status.ES_HOST, request_timeout=DELAI_ES, max_retries=0)
    yield client
    client.close()


@pytest.fixture
def registre():
    """Sauvegarde et restaure le registre réel : ces tests écrivent dans
    la configuration de l'installation de développement, qu'ils n'ont pas
    à laisser modifiée (principe 2 de conftest.py)."""
    client = pinned._get_redis_client()
    if client is None:
        pytest.skip("Redis injoignable")
    avant = client.get(pinned.PINNED_KEY)
    client.delete(pinned.PINNED_KEY)
    yield
    if avant is None:
        client.delete(pinned.PINNED_KEY)
    else:
        client.set(pinned.PINNED_KEY, avant)


@pytest.fixture(scope="module")
def api(es):
    import search_api

    if es.indices.exists(index=INDEX_SONDE):
        es.indices.delete(index=INDEX_SONDE)
    es.indices.create(
        index=INDEX_SONDE,
        mappings={
            "properties": {
                "content": {"type": "text"},
                "filename": {"type": "keyword"},
                "source": {"type": "keyword"},
                "acl": {"properties": {
                    "public": {"type": "boolean"},
                    "groups": {"type": "keyword"},
                    "users": {"type": "keyword"},
                    "owner": {"type": "keyword"},
                }},
            }
        },
    )
    for identifiant, public in ((PUBLIC, True), (PRIVE, False)):
        es.index(
            index=INDEX_SONDE,
            id=identifiant,
            document={
                "content": "note sur les congés",
                "filename": f"{identifiant}.pdf",
                "source": "documents",
                "acl": {"public": public, "groups": ["direction"], "users": [], "owner": "personne"},
            },
        )
    es.indices.refresh(index=INDEX_SONDE)

    precedent_es, precedent_alias = search_api.es, search_api.ES_SEARCH_ALIAS
    search_api.es = es
    search_api.ES_SEARCH_ALIAS = INDEX_SONDE
    yield search_api
    search_api.es, search_api.ES_SEARCH_ALIAS = precedent_es, precedent_alias
    es.indices.delete(index=INDEX_SONDE)


@pytest.fixture
def epingler(monkeypatch):
    """Pose des épinglages SANS passer par Redis.

    Ce que les tests d'ACL ci-dessous éprouvent est la relecture filtrée,
    pas le registre — celui-ci a ses propres tests. Les en découpler leur
    permet de tourner partout : le Redis de DocSearch vit dans le réseau
    des conteneurs, hors de portée d'un pytest lancé sur l'hôte, et ces
    tests-là sont trop importants pour se sauter en silence."""
    def poser(identifiants: list[str]) -> None:
        monkeypatch.setattr(pinned, "pour_requete", lambda requete: identifiants)
    return poser


@pytest.fixture
def sans_groupe(monkeypatch, api):
    """Un utilisateur sans aucun groupe : il ne voit que le public."""
    monkeypatch.setattr(api, "build_acl_filter", lambda username: {"bool": {"should": [
        {"term": {"acl.public": True}},
        {"term": {"acl.owner": username}},
    ], "minimum_should_match": 1}})
    monkeypatch.setattr(api, "_searchable_source_names", lambda username: ["documents"])
    return api


# ── Normalisation ────────────────────────────────────────────

def test_normalise_casse_accents_et_espaces():
    """« Congés », « conges » et «  CONGÉS  » sont la même intention, et
    personne ne pensera à épingler les trois."""
    assert pinned.normaliser("  CONGÉS   payés ") == "conges payes"


# ── Le registre ──────────────────────────────────────────────

@requiert_redis
def test_epingle_et_relit(registre):
    pinned.definir("Congés", ["a", "b"])
    assert pinned.pour_requete("conges") == ["a", "b"]


@requiert_redis
def test_une_liste_vide_retire_la_regle(registre):
    """Même geste que « supprimer » : deux chemins pour un seul résultat
    en feraient un de trop."""
    pinned.definir("congés", ["a"])
    pinned.definir("congés", [])
    assert pinned.pour_requete("congés") == []
    assert pinned.lister() == []


@requiert_redis
def test_plafonne_le_nombre_de_documents(registre):
    pinned.definir("congés", [f"doc{i}" for i in range(20)])
    assert len(pinned.pour_requete("congés")) == pinned.MAX_PAR_REQUETE


@requiert_redis
def test_refuse_une_requete_vide(registre):
    with pytest.raises(ValueError):
        pinned.definir("   ", ["a"])


# ── L'ACL, qui est tout l'enjeu ──────────────────────────────

@requiert_es
def test_un_epingle_visible_remonte(sans_groupe, epingler):
    epingler([PUBLIC])

    documents = sans_groupe._documents_epingles("Congés", "bob.user")

    assert [d["id"] for d in documents] == [PUBLIC]
    assert documents[0]["pinned"] is True


@requiert_es
def test_un_epingle_interdit_ne_remonte_pas(sans_groupe, epingler):
    """LE test de ce fichier : épingler ne donne aucun droit. Celui qui
    n'a pas accès au document ne le voit pas, et rien à l'écran ne lui
    apprend qu'il existe."""
    epingler([PRIVE])

    assert sans_groupe._documents_epingles("Congés", "bob.user") == []


@requiert_es
def test_l_ordre_est_celui_de_l_administration(sans_groupe, epingler):
    """Quand quelqu'un épingle plusieurs documents, il les a classés :
    l'ordre d'Elasticsearch n'a rien à dire ici."""
    epingler([PRIVE, PUBLIC])   # le privé sera filtré

    assert [d["id"] for d in sans_groupe._documents_epingles("congés", "bob.user")] == [PUBLIC]


@requiert_es
def test_un_document_supprime_disparait_de_lui_meme(sans_groupe, epingler):
    """Un épinglage qui pointe vers un document effacé ne doit pas
    produire de ligne vide — c'est le panneau d'administration qui le
    signale, pas l'écran de recherche."""
    epingler(["identifiant-inexistant", PUBLIC])

    assert [d["id"] for d in sans_groupe._documents_epingles("congés", "bob.user")] == [PUBLIC]


@requiert_es
def test_aucun_epinglage_ne_coute_rien(sans_groupe):
    """Le cas courant : pas de règle, pas de recherche supplémentaire."""
    assert sans_groupe._documents_epingles("requête sans épinglage", "bob.user") == []
