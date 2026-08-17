# tests/test_historique_navigation.py — Une recherche véritable se
# distingue-t-elle d'un tour de page ?
#
# Chaque clic sur « Suivant » relance /search et écrit une ligne de plus,
# rigoureusement identique à la précédente. L'historique d'administration
# présentait donc une requête consultée sur cinq pages comme cinq
# recherches — un compte faux, et faux dans le sens qui flatte.
#
# Trois propriétés, dont deux protègent l'HISTORIQUE DÉJÀ ÉCRIT :
#
#   1. le numéro de page est enregistré, dérivé de `from`/`size` ;
#   2. `exact=False` est écrit, et pas avalé par un test de vérité — sans
#      quoi « recherche ordinaire » et « ligne antérieure au champ »
#      deviendraient indistinguables, et la colonne mentirait sur tout
#      l'historique ;
#   3. le filtre « recherches véritables » écarte les pages 2 et au-delà
#      SANS écarter les lignes anciennes, qui n'ont pas de numéro de page
#      du tout. Un `term page = 1` les aurait toutes fait disparaître —
#      soit, sur une installation en service, la quasi-totalité du
#      journal.
#
# La troisième est celle qui justifie ce fichier : elle ne se voit pas en
# relisant la requête, elle se voit en la faisant tourner contre un index
# qui contient les deux générations de lignes.
#
# ⚠️ Comme tout module créant un index jetable, ces tests exigent du
# DISQUE et pas seulement un moteur qui répond — voir l'avertissement
# détaillé en tête de test_temps_recherche.py.

import pytest

import cluster_status
import search_api
import search_log

requiert_es = pytest.mark.requires_elasticsearch

INDEX_SONDE = "docsearch_test_sonde_historique_navigation"
DELAI_ES = 60


@pytest.fixture(scope="module")
def es():
    from elasticsearch import Elasticsearch

    client = Elasticsearch(cluster_status.ES_HOST, request_timeout=DELAI_ES, max_retries=0)
    yield client
    client.close()


@pytest.fixture(scope="module")
def journal(es):
    """Un journal portant les DEUX générations de lignes : celles écrites
    par le code d'aujourd'hui, et une ligne « héritée » sans `page` ni
    `exact`, écrite directement pour reproduire l'existant d'une
    installation en service."""
    precedent_index, precedent_pret = search_log.SEARCH_LOG_INDEX, search_log._index_ready
    precedent_es = search_api.es
    search_log.SEARCH_LOG_INDEX = INDEX_SONDE
    search_log._index_ready = False
    search_api.es = es

    es.indices.delete(index=INDEX_SONDE, ignore_unavailable=True)

    def journaliser(**champs):
        search_log.log_search(
            es, username="bob.user", ip=None, source=None, result_files=[],
            **{"search_in": "all", "total_results": 3, **champs},
        )

    journaliser(query="budget", page=1, exact=False, duration_ms=120.0, groups=["rh"])
    journaliser(query="budget", page=2, exact=False, duration_ms=900.0, groups=["rh"])
    # Un AVIS donné depuis la page 3 : le cas qui interdit d'appliquer le
    # filtre aux avis (voir la docstring de admin_search_logs_summary).
    identifiant_page_3 = search_log.log_search(
        es, username="bob.user", ip=None, source=None, result_files=[],
        search_in="all", total_results=3, query="budget", page=3, exact=False,
        duration_ms=1500.0, groups=["rh"],
    )
    es.update(index=INDEX_SONDE, id=identifiant_page_3, doc={"feedback": "up"}, refresh=True)
    journaliser(query="délégation", page=1, exact=True, duration_ms=50.0, groups=["rh"])
    # La ligne héritée : ni `page` ni `exact`, comme tout ce que le
    # journal contenait avant ce chantier.
    es.index(index=INDEX_SONDE, document={
        "timestamp": "2026-08-01T09:00:00+00:00",
        "username": "bob.user", "query": "ancienne", "search_in": "all",
        "total_results": 5, "result_files": [], "groups": [],
    })
    es.indices.refresh(index=INDEX_SONDE)

    yield es

    es.indices.delete(index=INDEX_SONDE, ignore_unavailable=True)
    search_log.SEARCH_LOG_INDEX, search_log._index_ready = precedent_index, precedent_pret
    search_api.es = precedent_es


def _lignes(sans_navigation=False):
    """`size`/`from_` passés explicitement : appelée hors FastAPI, la
    fonction reçoit sinon l'objet `Query(0)` en guise de défaut, qu'ES ne
    sait pas sérialiser."""
    reponse = search_api.admin_search_logs(
        user="alice.admin", size=50, from_=0, exclude_pagination=sans_navigation,
    )
    return reponse["results"]


@requiert_es
def test_le_numero_de_page_est_enregistre(journal):
    pages = sorted(
        ligne.get("page") for ligne in _lignes() if ligne["query"] == "budget"
    )
    assert pages == [1, 2, 3]


@requiert_es
def test_une_recherche_ordinaire_enregistre_exact_a_faux(journal):
    """`False` et non l'absence du champ : c'est ce qui distingue une
    recherche ordinaire d'une ligne antérieure à la capture."""
    ordinaire = next(ligne for ligne in _lignes() if ligne["query"] == "budget")
    exacte = next(ligne for ligne in _lignes() if ligne["query"] == "délégation")
    ancienne = next(ligne for ligne in _lignes() if ligne["query"] == "ancienne")

    assert ordinaire["exact"] is False
    assert exacte["exact"] is True
    assert "exact" not in ancienne


@requiert_es
def test_le_filtre_ecarte_les_tours_de_page(journal):
    """LE filtre : « budget » n'apparaît plus qu'une fois, pour sa
    page 1, au lieu de trois."""
    requetes = [ligne["query"] for ligne in _lignes(sans_navigation=True)]
    assert requetes.count("budget") == 1
    assert requetes.count("délégation") == 1


@requiert_es
def test_le_filtre_garde_les_lignes_anterieures_au_champ(journal):
    """La propriété qui a dicté un `must_not page > 1` plutôt qu'un
    `term page = 1` : une ligne sans numéro de page n'est pas un tour de
    page, elle est INCONNUE — et l'écarter viderait l'historique de tout
    ce qui précède ce chantier."""
    requetes = [ligne["query"] for ligne in _lignes(sans_navigation=True)]
    assert "ancienne" in requetes


@requiert_es
def test_sans_le_filtre_rien_ne_disparait(journal):
    """Le comportement par défaut ne change pas : le journal reste une
    trace d'activité complète tant qu'on ne demande pas à filtrer."""
    assert len(_lignes()) == 5


# ── Les agrégats de la vue d'ensemble ────────────────────────
#
# Le résumé comptait, lui aussi, un tour de page comme une recherche. Il
# les écarte désormais — SAUF pour deux familles, et ce sont elles que
# les tests suivants protègent : un filtre appliqué trop largement
# jetterait des avis réels et masquerait les requêtes lentes.


@pytest.fixture
def resume(journal):
    return search_api.admin_search_logs_summary(user="alice.admin")


@requiert_es
def test_le_total_ne_compte_plus_les_tours_de_page(resume):
    """3 recherches véritables (« budget » page 1, « délégation », la
    ligne héritée) sur 5 lignes de journal."""
    assert resume["total_searches"] == 3
    assert resume["total_logged"] == 5


@requiert_es
def test_les_recherches_par_jour_et_par_groupe_suivent_le_total(resume):
    # Deux et non trois : l'histogramme ne couvre que les 14 derniers
    # jours, et la ligne héritée est datée du 2026-08-01. Sans le filtre,
    # la journée en compterait quatre — les trois « budget » plus
    # « délégation ».
    assert sum(jour["count"] for jour in resume["by_day"]) == 2
    volume = {ligne["group"]: ligne["count"] for ligne in resume["searches_by_group"]}
    # La ligne héritée n'a pas de groupe : elle tombe dans le lot dédié,
    # et ne doit pas disparaître au passage.
    assert volume == {"rh": 2, "__sans_groupe__": 1}


@requiert_es
def test_un_avis_donne_depuis_une_page_reste_compte(resume):
    """LE garde-fou. L'avis a été donné sur la ligne « page 3 » : c'est
    un avis réel sur des résultats réels, et l'écarter fausserait la part
    positive — qui est un rapport entre AVIS, pas entre recherches."""
    assert resume["feedback_up"] == 1
    avis_rh = next(g for g in resume["by_group"] if g["group"] == "rh")
    assert avis_rh["feedback_up"] == 1
    # Le même tableau porte le volume, lui filtré : les deux colonnes ne
    # comptent pas le même ensemble, et c'est voulu.
    assert avis_rh["searches"] == 2


@requiert_es
def test_les_temps_comptent_toujours_les_tours_de_page(resume):
    """Un tour de page est une requête pleine et entière, et c'est en
    pagination profonde que le moteur est le plus lent : les écarter
    masquerait précisément ce que ce panneau existe pour montrer.

    Les quatre durées journalisées (120, 900, 1500, 50 ms) sont toutes
    mesurées ; la moyenne le prouve mieux qu'un décompte, qu'un filtre
    mal placé aurait pu laisser juste par hasard."""
    assert resume["timing"]["measured"] == 4
    assert resume["timing"]["avg_ms"] == pytest.approx((120 + 900 + 1500 + 50) / 4)
