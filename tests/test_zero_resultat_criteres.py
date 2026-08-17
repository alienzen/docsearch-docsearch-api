# tests/test_zero_resultat_criteres.py — Les critères remontés à côté des
# requêtes sans résultat disent-ils la vérité ?
#
# À ne pas confondre avec test_zero_resultat.py, qui protège l'aide
# proposée à l'UTILISATEUR devant un écran vide (relaxations, correction
# orthographique). Celui-ci porte sur ce que l'ADMINISTRATION voit dans
# /stats.html : quels filtres accompagnaient une requête infructueuse.
#
# L'enjeu tient en une phrase : « rien trouvé parce que le contenu
# manque » et « rien trouvé parce que le filtre était trop serré » sont
# le MÊME écran pour l'utilisateur, et n'appellent pas du tout la même
# correction. C'est le compte « sans filtre » qui les sépare, et c'est
# donc lui qui doit être juste — un compte faux orienterait vers une
# indexation inutile, ou vers un filtre à corriger là où il n'y en avait
# pas.
#
# Trois propriétés que la relecture ne donne pas, parce qu'elles sont
# celles des agrégations d'Elasticsearch :
#
#   1. les filtres rencontrés sont bien rattachés à LEUR requête, avec le
#      bon nombre d'occurrences ;
#   2. « sans filtre » ne compte que les recherches réellement nues — une
#      recherche restreinte à un champ (`search_in`) n'en est pas une ;
#   3. « Rechercher dans : Tout » n'est jamais remonté : il est écrit sur
#      toutes les lignes du journal et noierait ce qui informe.
#
# Elasticsearch est le vrai (principe 1 de conftest.py), index jetable
# (principe 2) — ces tests ÉCRIVENT des recherches, jamais dans le
# journal de l'installation de développement.
#
# ⚠️ Comme tout module créant un index jetable, ils exigent du DISQUE et
# pas seulement un moteur qui répond — voir l'avertissement détaillé en
# tête de test_temps_recherche.py.

import pytest

import cluster_status
import search_api
import search_log

requiert_es = pytest.mark.requires_elasticsearch

INDEX_SONDE = "docsearch_test_sonde_zero_criteres"
DELAI_ES = 60


@pytest.fixture(scope="module")
def es():
    from elasticsearch import Elasticsearch

    client = Elasticsearch(cluster_status.ES_HOST, request_timeout=DELAI_ES, max_retries=0)
    yield client
    client.close()


@pytest.fixture(scope="module")
def journal(es):
    """Un journal de recherches jetable, peuplé de quatre occurrences de
    « congés » aux critères différents et d'une de « budget » toujours
    filtrée.

    Ce jeu n'est pas décoratif : « congés » a une occurrence NUE, donc
    son écran vide accuse le contenu ; « budget » n'en a aucune, donc le
    sien accuse le filtre. C'est exactement la distinction que le panneau
    doit rendre lisible."""
    precedent_index, precedent_pret = search_log.SEARCH_LOG_INDEX, search_log._index_ready
    precedent_es = search_api.es
    search_log.SEARCH_LOG_INDEX = INDEX_SONDE
    search_log._index_ready = False
    search_api.es = es

    es.indices.delete(index=INDEX_SONDE, ignore_unavailable=True)

    def journaliser(**criteres):
        search_log.log_search(
            es,
            username="bob.user",
            ip=None,
            total_results=0,
            result_files=[],
            **{"search_in": "all", "source": None, **criteres},
        )

    journaliser(query="congés")                                        # nue
    journaliser(query="congés", extension=[".pdf"])
    journaliser(query="congés", extension=[".pdf"], author=["Dupont"], search_in="title")
    journaliser(query="congés", date_from="2025-01-01")
    journaliser(query="budget", source=["finance"])
    # Une recherche FRUCTUEUSE, qui ne doit apparaître nulle part : le
    # panneau ne parle que des écrans vides.
    search_log.log_search(
        es, username="bob.user", ip=None, query="congés", search_in="all", source=None,
        total_results=12, result_files=["a.pdf"], extension=[".docx"],
    )
    es.indices.refresh(index=INDEX_SONDE)

    yield es

    es.indices.delete(index=INDEX_SONDE, ignore_unavailable=True)
    search_log.SEARCH_LOG_INDEX, search_log._index_ready = precedent_index, precedent_pret
    search_api.es = precedent_es


@pytest.fixture
def lignes(journal):
    reponse = search_api.admin_zero_result_searches(user="alice.admin")
    return {ligne["query"]: ligne for ligne in reponse["results"]}


def _critere(ligne, champ, valeur=""):
    """Compte d'un critère précis, 0 s'il n'a pas été remonté."""
    for critere in ligne["criteres"]:
        if critere["champ"] == champ and critere["valeur"] == valeur:
            return critere["count"]
    return 0


@requiert_es
def test_les_criteres_sont_rattaches_a_leur_requete(lignes):
    conges = lignes["congés"]
    assert conges["count"] == 4                       # la fructueuse n'y est pas
    assert _critere(conges, "extension", ".pdf") == 2
    assert _critere(conges, "author", "Dupont") == 1
    assert _critere(conges, "periode") == 1
    # Le filtre de l'AUTRE requête n'a rien à faire ici.
    assert _critere(conges, "source", "finance") == 0
    # Celui de la recherche fructueuse non plus.
    assert _critere(conges, "extension", ".docx") == 0


@requiert_es
def test_sans_filtre_ne_compte_que_les_recherches_nues(lignes):
    """LE compte du panneau. Une seule des quatre occurrences de
    « congés » n'avait aucun filtre — celle restreinte au titre n'en est
    pas une, restreindre le champ interrogé EST un filtre."""
    assert lignes["congés"]["sans_critere"] == 1
    assert lignes["budget"]["sans_critere"] == 0


@requiert_es
def test_le_champ_interroge_est_remonte_sauf_tout(lignes):
    """« Tout » est écrit sur chaque ligne du journal : le remonter
    reviendrait à afficher un critère sur toutes les requêtes, ce qui
    n'apprend rien et pousse hors de l'écran ceux qui informent."""
    assert _critere(lignes["congés"], "search_in", "title") == 1
    assert _critere(lignes["congés"], "search_in", "all") == 0


@requiert_es
def test_les_criteres_sont_ordonnes_du_plus_frequent_au_moins(lignes):
    """L'ordre EST l'information quand la cellule est tronquée à
    l'écran : le filtre le plus souvent rencontré doit être le premier
    lu."""
    comptes = [critere["count"] for critere in lignes["congés"]["criteres"]]
    assert comptes == sorted(comptes, reverse=True)


@requiert_es
def test_un_journal_absent_ne_leve_pas(journal, monkeypatch):
    """Une installation neuve n'a pas encore d'index de journal : le
    panneau doit s'afficher vide, pas en erreur."""
    monkeypatch.setattr(search_log, "SEARCH_LOG_INDEX", "docsearch_test_index_absent")
    reponse = search_api.admin_zero_result_searches(user="alice.admin")
    assert reponse == {"total_zero_result_searches": 0, "results": [], "by_group": []}
