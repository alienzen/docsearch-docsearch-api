# tests/test_doublons.py — Le rapport de doublons compte-t-il ce qu'il
# prétend compter ?
#
# Le chiffre qui sera lu et cité est « copies en trop » : il doit être
# exact, et surtout ne pas confondre « documents sans empreinte » avec
# « documents sans doublon ». Les documents SQL et web n'ont pas
# d'empreinte (pas de fichier), et les documents indexés avant l'ajout du
# champ non plus tant que le rattrapage n'a pas tourné.
#
# Elasticsearch est le vrai (principe 1 de conftest.py) : ce sont ses
# agrégations qui sont testées. Index jetable (principe 2), et le cache
# Redis est neutralisé pour que chaque test mesure bien ce qu'il vient
# d'indexer.

import pytest

import cluster_status
import duplicates

requiert_es = pytest.mark.requires_elasticsearch

INDEX_SONDE = "docsearch_test_sonde_doublons"
DELAI_ES = 60


@pytest.fixture(scope="module")
def es():
    from elasticsearch import Elasticsearch

    client = Elasticsearch(cluster_status.ES_HOST, request_timeout=DELAI_ES, max_retries=0)
    yield client
    client.close()


@pytest.fixture
def corpus(es, monkeypatch):
    """Cinq documents : trois copies d'un même contenu, un unique, et un
    document sans empreinte (une source SQL, qui n'a pas de fichier)."""
    if es.indices.exists(index=INDEX_SONDE):
        es.indices.delete(index=INDEX_SONDE)
    es.indices.create(
        index=INDEX_SONDE,
        mappings={
            "properties": {
                "content_sha256": {"type": "keyword"},
                "filepath": {"type": "keyword"},
                "filename": {"type": "keyword"},
                "size": {"type": "long"},
                "source": {"type": "keyword"},
            }
        },
    )
    documents = [
        {"content_sha256": "aaa", "filepath": "/d/rapport.pdf", "filename": "rapport.pdf", "size": 1000, "source": "documents"},
        {"content_sha256": "aaa", "filepath": "/d/rapport - Copie.pdf", "filename": "rapport - Copie.pdf", "size": 1000, "source": "documents"},
        {"content_sha256": "aaa", "filepath": "/d/archive/rapport.pdf", "filename": "rapport.pdf", "size": 1000, "source": "documents"},
        {"content_sha256": "bbb", "filepath": "/d/note.docx", "filename": "note.docx", "size": 40, "source": "documents"},
        {"filepath": "sql:agents:12", "filename": "agent 12", "size": 0, "source": "agents"},
    ]
    for document in documents:
        es.index(index=INDEX_SONDE, document=document)
    es.indices.refresh(index=INDEX_SONDE)

    # Pas de cache : chaque test doit mesurer son propre corpus.
    monkeypatch.setattr(duplicates, "_get_redis_client", lambda: None)

    yield es
    es.indices.delete(index=INDEX_SONDE)


@requiert_es
def test_compte_les_copies_en_trop(corpus):
    """Trois exemplaires d'un même contenu = deux copies en trop. Le
    document unique n'en ajoute aucune."""
    rapport = duplicates.rapport(corpus, INDEX_SONDE)

    assert rapport["copies_en_trop"] == 2
    assert rapport["distincts"] == 2


@requiert_es
def test_ignore_les_documents_sans_empreinte(corpus):
    """Le document SQL n'a pas de fichier, donc pas d'empreinte : il ne
    doit compter ni dans les doublons, ni dans les documents examinés —
    l'inclure ferait dire au rapport qu'il a regardé plus large qu'en
    réalité."""
    rapport = duplicates.rapport(corpus, INDEX_SONDE)

    assert rapport["documents"] == 4


@requiert_es
def test_chiffre_la_place_rendue_par_le_dedoublonnage(corpus):
    """Ce qui intéresse un exploitant n'est pas le nombre de copies mais
    la place qu'elles occupent — et ce qui serait rendu en n'en gardant
    qu'une, donc deux fichiers sur trois ici."""
    groupe = duplicates.rapport(corpus, INDEX_SONDE)["groupes"][0]

    assert groupe["copies"] == 3
    assert groupe["gaspille"] == 2000


@requiert_es
def test_montre_ou_sont_les_copies(corpus):
    """Un groupe sans exemple de chemin n'est pas actionnable : personne
    ne saurait où aller regarder."""
    groupe = duplicates.rapport(corpus, INDEX_SONDE)["groupes"][0]

    chemins = {exemple["filepath"] for exemple in groupe["exemples"]}
    assert "/d/rapport.pdf" in chemins
    assert len(chemins) == 3


@requiert_es
def test_ne_liste_pas_les_documents_uniques(corpus):
    """Un document en un seul exemplaire n'est pas un doublon : il n'a
    rien à faire dans la liste."""
    empreintes = {g["empreinte"] for g in duplicates.rapport(corpus, INDEX_SONDE)["groupes"]}

    assert empreintes == {"aaa"}


@requiert_es
def test_un_index_sans_empreinte_du_tout_ne_casse_pas(es, monkeypatch):
    """Avant le rattrapage (backfill_hashes.py), aucun document n'a
    d'empreinte : le rapport doit dire « rien », pas échouer."""
    vide = INDEX_SONDE + "_vide"
    if es.indices.exists(index=vide):
        es.indices.delete(index=vide)
    es.indices.create(index=vide, mappings={"properties": {"content_sha256": {"type": "keyword"}}})
    es.index(index=vide, document={"filepath": "/d/sans-empreinte.pdf"}, refresh=True)
    monkeypatch.setattr(duplicates, "_get_redis_client", lambda: None)
    try:
        rapport = duplicates.rapport(es, vide)
        assert rapport["documents"] == 0
        assert rapport["copies_en_trop"] == 0
        assert rapport["groupes"] == []
    finally:
        es.indices.delete(index=vide)
