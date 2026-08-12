# tests/test_synonymes.py — Le thésaurus fait-il ce qu'il promet, et le
# fait-il À CHAUD ?
#
# Ce chantier repose sur trois propriétés d'Elasticsearch dont deux
# échouent EN SILENCE si on s'y prend mal — d'où des tests contre le vrai
# moteur, seul juge en la matière :
#
# 1. **L'ordre des filtres.** Les synonymes doivent passer AVANT le
#    stemmer : sinon l'expansion n'est pas racinisée alors que le texte
#    indexé l'a été, et la recherche ne trouve rien. Aucune erreur,
#    aucune trace : juste zéro résultat.
# 2. **Le rechargement à chaud.** Une règle ajoutée doit prendre effet
#    sans réindexer les documents — c'est toute la raison de mettre le
#    filtre dans un analyseur de RECHERCHE.
# 3. **La recherche entre guillemets ne s'élargit pas.** « terme exact »
#    veut dire exact ; l'utilisateur qui en tape ne s'attend pas à ce
#    qu'on élargisse sa requête.
#
# Le jeu de synonymes est celui de la sonde, jamais celui de
# l'installation (principe 2 de conftest.py) : les modules concernés sont
# détournés vers un identifiant dédié, supprimé avant et après.

import pytest

import cluster_status
import synonyms

requiert_es = pytest.mark.requires_elasticsearch

INDEX_SONDE = "docsearch_test_sonde_synonymes"
JEU_SONDE = "docsearch_test_sonde_fr"
DELAI_ES = 60

# Repris à l'identique de docsearch-ingestion/app/indexer.py (ANALYSE) —
# c'est CETTE forme que les tests valident, ordre des filtres compris.
ANALYSE = {
    "analyzer": {
        "french": {
            "tokenizer": "standard",
            "filter": ["lowercase", "french_stop", "french_stemmer"],
        },
        "french_search": {
            "tokenizer": "standard",
            "filter": ["lowercase", "synonymes_fr", "french_stop", "french_stemmer"],
        },
    },
    "filter": {
        "french_stop": {"type": "stop", "stopwords": "_french_"},
        "french_stemmer": {"type": "stemmer", "language": "light_french"},
        "synonymes_fr": {
            "type": "synonym_graph",
            "synonyms_set": JEU_SONDE,
            "updateable": True,
        },
    },
}

MAPPING_CONTENT = {
    "content": {
        "type": "text",
        "analyzer": "french",
        "search_analyzer": "french_search",
        "search_quote_analyzer": "french",
    }
}


@pytest.fixture(scope="module")
def es():
    from elasticsearch import Elasticsearch

    client = Elasticsearch(cluster_status.ES_HOST, request_timeout=DELAI_ES, max_retries=0)
    yield client
    client.close()


@pytest.fixture(scope="module")
def thesaurus(es, monkeypatch_module=None):
    """Index de sonde migré comme le ferait `./manage.sh migrer-synonymes`,
    et jeu de synonymes dédié."""
    precedent = synonyms.SYNONYMS_SET
    synonyms.SYNONYMS_SET = JEU_SONDE

    if es.indices.exists(index=INDEX_SONDE):
        es.indices.delete(index=INDEX_SONDE)
    try:
        es.synonyms.delete_synonym(id=JEU_SONDE)
    except Exception:
        pass

    # Volontairement créé SANS l'analyseur de recherche, puis migré plus
    # bas : c'est le chemin des installations déjà en service, le seul
    # qu'une installation neuve ne rencontre jamais.
    es.indices.create(
        index=INDEX_SONDE,
        settings={
            "number_of_shards": 1,
            "number_of_replicas": 0,
            "analysis": {
                "analyzer": ANALYSE["analyzer"]["french"] and {"french": ANALYSE["analyzer"]["french"]},
                "filter": {k: v for k, v in ANALYSE["filter"].items() if k != "synonymes_fr"},
            },
        },
        mappings={"properties": {"content": {"type": "text", "analyzer": "french"}}},
    )
    es.index(
        index=INDEX_SONDE,
        document={"content": "note de la direction des ressources humaines"},
        refresh=True,
    )

    yield es

    synonyms.SYNONYMS_SET = precedent
    es.indices.delete(index=INDEX_SONDE)
    try:
        es.synonyms.delete_synonym(id=JEU_SONDE)
    except Exception:
        pass


def _migrer(es) -> None:
    """Ce que fait migrer_analyse() côté ingestion : fermer, poser
    l'analyseur, rouvrir, basculer le champ."""
    es.indices.close(index=INDEX_SONDE)
    try:
        es.indices.put_settings(index=INDEX_SONDE, settings={"analysis": ANALYSE})
    finally:
        es.indices.open(index=INDEX_SONDE)
    es.indices.put_mapping(index=INDEX_SONDE, properties=MAPPING_CONTENT)


def _trouve(es, requete: dict) -> int:
    es.indices.refresh(index=INDEX_SONDE)
    return es.search(index=INDEX_SONDE, query=requete)["hits"]["total"]["value"]


# ── Validation des règles ────────────────────────────────────

def test_refuse_la_forme_a_vers_b():
    """« a => b » remplace les termes d'origine au lieu de les compléter :
    surprenant pour tout le monde, et très mal réglable depuis une
    interface d'administration."""
    with pytest.raises(synonyms.RegleInvalide):
        synonyms.valider("drh => direction des ressources humaines")


def test_refuse_une_regle_a_un_seul_terme():
    with pytest.raises(synonyms.RegleInvalide):
        synonyms.valider("drh")


def test_normalise_les_espaces():
    assert synonyms.valider("  drh ,   direction   des ressources humaines ") == (
        "drh, direction des ressources humaines"
    )


# ── Le moteur ────────────────────────────────────────────────

@requiert_es
def test_la_migration_active_les_synonymes_sans_reindexer(thesaurus):
    """L'index existe déjà et ses documents ne bougent pas : seul
    l'analyseur DE RECHERCHE change."""
    es = thesaurus
    assert _trouve(es, {"match": {"content": "drh"}}) == 0

    _migrer(es)
    synonyms.ajouter(es, "drh, direction des ressources humaines")

    assert _trouve(es, {"match": {"content": "drh"}}) == 1


@requiert_es
def test_une_regle_ajoutee_prend_effet_a_chaud(thesaurus):
    """Ni réindexation, ni fermeture d'index, ni redémarrage : c'est
    toute la raison d'un filtre `updateable` en analyseur de recherche."""
    es = thesaurus
    assert _trouve(es, {"match": {"content": "ressources humaines de la boîte"}}) >= 0

    synonyms.ajouter(es, "rh, ressources humaines")
    assert _trouve(es, {"match": {"content": "rh"}}) == 1


@requiert_es
def test_l_expansion_passe_par_le_stemmer(thesaurus):
    """LE piège de ce chantier : avec le filtre de synonymes APRÈS le
    stemmer, l'expansion resterait non racinisée face à un index racinisé
    — zéro résultat, sans la moindre erreur. Les jetons rendus ici le
    prouvent : « ressources » ressort raciné."""
    jetons = synonyms.tester(thesaurus, INDEX_SONDE, "drh")["jetons"]

    assert "drh" in jetons
    assert "ressources" not in jetons   # la forme brute ne doit PAS sortir
    assert any(jeton.startswith("resourc") or jeton.startswith("ressourc") for jeton in jetons)


@requiert_es
def test_la_recherche_entre_guillemets_ne_s_elargit_pas(thesaurus):
    """« terme exact » veut dire exact : le search_quote_analyzer du champ
    n'a pas de synonymes."""
    es = thesaurus
    # Sans guillemets, le synonyme trouve le document…
    assert _trouve(es, {"match": {"content": "drh"}}) == 1
    # …avec, la phrase est prise au pied de la lettre.
    assert _trouve(es, {"multi_match": {"query": "drh", "fields": ["content"], "type": "phrase"}}) == 0


@requiert_es
def test_supprimer_la_derniere_regle_ne_casse_pas_la_recherche(thesaurus):
    """Retirer la dernière règle laisse un jeu VIDE, et surtout pas un jeu
    supprimé : Elasticsearch refuse (400) de supprimer un jeu référencé
    par un index, et tous nos index de documents le référencent. Un jeu
    vide se comporte comme l'absence de synonymes, l'index continuant de
    fonctionner normalement."""
    es = thesaurus
    for regle in synonyms.lister(es):
        synonyms.supprimer(es, regle["id"])

    assert synonyms.lister(es) == []
    assert _trouve(es, {"match": {"content": "direction"}}) == 1
    assert _trouve(es, {"match": {"content": "drh"}}) == 0
