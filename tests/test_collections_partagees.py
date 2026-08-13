# tests/test_collections_partagees.py — Partager une collection donne-t-il
# la référence sans donner le droit ?
#
# Une collection ne stocke que des identifiants de documents ; le contenu
# est relu à l'affichage, à travers l'ACL du lecteur. Partager, c'est
# donc dire « regarde ces documents-là », pas « je t'y donne accès ».
# Ce fichier protège les quatre propriétés qui en découlent :
#
# 1. Un destinataire voit la collection partagée à SON groupe, et
#    seulement celle-là.
# 2. Il ne peut pas la modifier — écrire reste au propriétaire.
# 3. Il peut la dupliquer : c'est sa porte de sortie, et la copie lui
#    appartient.
# 4. On ne partage qu'avec un groupe dont on est soi-même membre. Sans
#    cette borne, le premier usage serait de s'adresser à toute
#    l'organisation.
#
# Elasticsearch est le vrai (principe 1 de conftest.py) : c'est sa requête
# de visibilité qui est testée. Index jetable (principe 2) — jamais
# `saved_collections`, où vivent les collections de l'installation de
# développement.

import pytest

import cluster_status
import saved_collections

requiert_es = pytest.mark.requires_elasticsearch

INDEX_SONDE = "docsearch_test_sonde_collections"
DELAI_ES = 60

ALICE = "sonde.alice"
BOB = "sonde.bob"
GROUPE = "finance"


@pytest.fixture(scope="module")
def es():
    from elasticsearch import Elasticsearch

    client = Elasticsearch(cluster_status.ES_HOST, request_timeout=DELAI_ES, max_retries=0)
    yield client
    client.close()


@pytest.fixture
def collections(es, monkeypatch):
    monkeypatch.setattr(saved_collections, "SAVED_COLLECTIONS_INDEX", INDEX_SONDE)
    monkeypatch.setattr(saved_collections, "_index_ready", False)
    if es.indices.exists(index=INDEX_SONDE):
        es.indices.delete(index=INDEX_SONDE)
    yield es
    if es.indices.exists(index=INDEX_SONDE):
        es.indices.delete(index=INDEX_SONDE)


def _collection_alice(es, documents=("doc-a", "doc-b")):
    entree = saved_collections.create_collection(es, ALICE, "Dossier Client X")
    for document in documents:
        saved_collections.add_document(es, ALICE, entree["id"], document)
    return entree["id"]


# ── 1. Visibilité ────────────────────────────────────────────

@requiert_es
def test_une_collection_est_personnelle_par_defaut(collections):
    """Le défaut ne change pas : ce qui n'est pas partagé ne se voit
    pas, même en appartenant à des groupes."""
    _collection_alice(collections)

    assert saved_collections.list_collections(collections, BOB, [GROUPE]) == []


@requiert_es
def test_le_partage_rend_visible_au_groupe(collections):
    identifiant = _collection_alice(collections)
    saved_collections.set_sharing(collections, ALICE, identifiant, [GROUPE], [GROUPE])

    vues = saved_collections.list_collections(collections, BOB, [GROUPE])

    assert [c["id"] for c in vues] == [identifiant]
    # Le destinataire doit savoir d'où elle vient : sans ça, elle surgit
    # de nulle part dans son menu.
    assert vues[0]["owner"] == ALICE
    assert vues[0]["owned"] is False


@requiert_es
def test_un_autre_groupe_ne_voit_rien(collections):
    identifiant = _collection_alice(collections)
    saved_collections.set_sharing(collections, ALICE, identifiant, [GROUPE], [GROUPE])

    assert saved_collections.list_collections(collections, BOB, ["autre-groupe"]) == []


@requiert_es
def test_le_proprietaire_garde_la_main(collections):
    identifiant = _collection_alice(collections)
    saved_collections.set_sharing(collections, ALICE, identifiant, [GROUPE], [GROUPE])

    sienne = saved_collections.list_collections(collections, ALICE, [GROUPE])[0]

    assert sienne["owned"] is True
    assert sienne["shared_with"] == [GROUPE]


@requiert_es
def test_departager_la_retire_des_destinataires(collections):
    identifiant = _collection_alice(collections)
    saved_collections.set_sharing(collections, ALICE, identifiant, [GROUPE], [GROUPE])
    saved_collections.set_sharing(collections, ALICE, identifiant, [], [GROUPE])

    assert saved_collections.list_collections(collections, BOB, [GROUPE]) == []


# ── 2. Écrire reste au propriétaire ──────────────────────────

@requiert_es
def test_un_destinataire_ne_peut_pas_modifier(collections):
    """Pas de verrouillage à écrire : le destinataire n'écrit pas, il
    duplique (test suivant)."""
    identifiant = _collection_alice(collections)
    saved_collections.set_sharing(collections, ALICE, identifiant, [GROUPE], [GROUPE])

    for action in (
        lambda: saved_collections.rename_collection(collections, BOB, identifiant, "Détourné"),
        lambda: saved_collections.add_document(collections, BOB, identifiant, "doc-c"),
        lambda: saved_collections.remove_document(collections, BOB, identifiant, "doc-a"),
        lambda: saved_collections.set_sharing(collections, BOB, identifiant, [], [GROUPE]),
    ):
        with pytest.raises(KeyError):
            action()

    # `delete_collection` ne lève PAS — il est idempotent de longue date
    # (voir sa docstring) : un identifiant inconnu ou appartenant à
    # quelqu'un d'autre laisse simplement la liste inchangée. Ce qui
    # compte n'est donc pas l'exception, c'est que la collection d'Alice
    # soit toujours là.
    saved_collections.delete_collection(collections, BOB, identifiant)
    assert [c["id"] for c in saved_collections.list_collections(collections, ALICE)] == [identifiant]

    # Et rien n'a bougé de son contenu.
    sienne = saved_collections.list_collections(collections, ALICE)[0]
    assert sienne["name"] == "Dossier Client X"
    assert sienne["doc_ids"] == ["doc-a", "doc-b"]
    assert sienne["shared_with"] == [GROUPE]


# ── 3. La porte de sortie ────────────────────────────────────

@requiert_es
def test_un_destinataire_peut_dupliquer(collections):
    identifiant = _collection_alice(collections)
    saved_collections.set_sharing(collections, ALICE, identifiant, [GROUPE], [GROUPE])

    apres = saved_collections.duplicate_collection(collections, BOB, identifiant, [GROUPE])

    copies = [c for c in apres if c["owned"] and c["owner"] == BOB]
    assert len(copies) == 1
    assert copies[0]["doc_ids"] == ["doc-a", "doc-b"]
    assert copies[0]["shared_with"] == []   # une copie ne se repartage pas toute seule


@requiert_es
def test_on_ne_duplique_pas_ce_qu_on_ne_voit_pas(collections):
    identifiant = _collection_alice(collections)   # jamais partagée

    with pytest.raises(KeyError):
        saved_collections.duplicate_collection(collections, BOB, identifiant, [GROUPE])


# ── 4. La borne du partage ───────────────────────────────────

@requiert_es
def test_on_ne_partage_qu_avec_ses_propres_groupes(collections):
    """Sans cette borne, le premier usage serait de pousser une
    collection à toute l'organisation."""
    identifiant = _collection_alice(collections)

    with pytest.raises(PermissionError):
        saved_collections.set_sharing(collections, ALICE, identifiant, ["direction"], [GROUPE])

    # Et rien n'a été écrit.
    assert saved_collections.list_collections(collections, ALICE)[0]["shared_with"] == []
