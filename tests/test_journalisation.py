# tests/test_journalisation.py — Santé de la journalisation des recherches
#
# Ce que ces tests protègent : le 2026-08-10, le disque de la VM a franchi
# le flood-stage watermark d'Elasticsearch, qui a passé ses index en
# lecture seule. log_search() a fait ce pour quoi elle est écrite — avaler
# l'erreur pour que la recherche aboutisse quand même — et plus rien n'a
# été journalisé pendant des jours, sans que le panneau d'administration
# n'en dise un mot : le cluster restait « green ». search_log.health()
# existe pour que ce silence ne se reproduise pas.
#
# Redis est le vrai (principe 1 de conftest.py) et la clé de santé est
# détournée vers un suffixe « :test » (principe 2) : ce Redis est celui de
# l'installation de dev, dont le panneau d'administration lit la vraie clé
# pendant que les tests tournent.

import time

import pytest
from elasticsearch import Elasticsearch

import search_log

pytestmark = pytest.mark.requires_redis

CLE_TEST = "docsearch:health:search_log:test"


@pytest.fixture(autouse=True)
def cle_de_test(monkeypatch):
    """Isole la clé Redis et remet à zéro l'anti-rebond entre deux tests.

    `_last_health_ok` / `_last_health_write` sont des variables de module :
    sans cette remise à zéro, le deuxième test d'un même état serait avalé
    par la fenêtre de HEALTH_REFRESH_SECONDS et lirait la valeur du premier.
    """
    monkeypatch.setattr(search_log, "SEARCH_LOG_HEALTH_KEY", CLE_TEST)
    monkeypatch.setattr(search_log, "_last_health_ok", None)
    monkeypatch.setattr(search_log, "_last_health_write", 0.0)

    client = search_log._get_redis_client()
    if client is None:
        pytest.skip("Redis injoignable")
    client.delete(CLE_TEST)
    yield client
    client.delete(CLE_TEST)


def test_sans_ecriture_l_etat_est_inconnu_pas_en_panne():
    # Installation neuve ou Redis vidé. « Inconnu » et « en panne » doivent
    # rester distincts : le panneau affiche le premier en neutre, et un
    # voyant rouge à chaque démarrage finirait par ne plus être lu.
    etat = search_log.health()
    assert etat["ok"] is None
    assert "aucune recherche" in etat["reason"]


def test_une_ecriture_reussie_rend_la_journalisation_active():
    search_log._record_health(True)

    etat = search_log.health()
    assert etat["ok"] is True
    assert etat["error"] is None
    assert etat["last_attempt_seconds_ago"] < 5


def test_un_echec_est_rapporte_avec_sa_cause():
    search_log._record_health(False, "cluster_block_exception: flood-stage watermark")

    etat = search_log.health()
    assert etat["ok"] is False
    assert "flood-stage" in etat["error"]


def test_le_retour_a_la_normale_efface_l_echec():
    # L'état est le résultat de la DERNIÈRE tentative, pas un cumul : une
    # panne réparée doit s'effacer seule, sans acquittement manuel.
    search_log._record_health(False, "erreur passagère")
    search_log._record_health(True)

    assert search_log.health()["ok"] is True


def test_un_changement_d_etat_n_attend_pas_l_anti_rebond(cle_de_test):
    # L'anti-rebond ne doit économiser que les répétitions : un passage en
    # panne juste après une réussite est précisément ce qu'on veut voir
    # tout de suite.
    search_log._record_health(True)
    horodatage_reussite = cle_de_test.get(CLE_TEST)

    search_log._record_health(False, "tombé dans la même seconde")

    assert cle_de_test.get(CLE_TEST) != horodatage_reussite
    assert search_log.health()["ok"] is False


def test_une_reussite_repetee_n_ecrit_pas_a_chaque_recherche(cle_de_test):
    # Sinon chaque recherche paierait un aller-retour Redis pour une donnée
    # consultée trois fois par an.
    search_log._record_health(True)
    premiere = cle_de_test.get(CLE_TEST)
    time.sleep(0.05)
    search_log._record_health(True)

    assert cle_de_test.get(CLE_TEST) == premiere


def test_log_search_rapporte_l_echec_quand_elasticsearch_ne_repond_pas():
    # Le câblage lui-même, et pas seulement l'aide-mémoire : c'est
    # log_search() qui doit appeler _record_health() dans SES DEUX
    # branches. Port fermé plutôt qu'un bouchon — l'exception traversée est
    # alors celle du vrai client Elasticsearch.
    es_injoignable = Elasticsearch("http://127.0.0.1:1", request_timeout=1, max_retries=0)

    search_id = search_log.log_search(
        es_injoignable,
        username="test",
        ip=None,
        query="peu importe",
        search_in="all",
        source=None,
        total_results=0,
        result_files=[],
    )

    assert search_id is None
    assert search_log.health()["ok"] is False
