# tests/test_ecritures_bloquees.py — Suggestions et NPS acceptent-ils encore les écritures ?
#
# Ce que ces tests protègent : quand Elasticsearch passe ses index en
# lecture seule (flood-stage watermark, disque à 95 %), log_suggestion()
# et log_nps() avalent l'exception — c'est leur contrat, une idée ou une
# note envoyée ne doit jamais se retourner en erreur devant
# l'utilisateur — et /suggestions comme /nps répondent 200. Le message
# est perdu, l'interface remercie, le cluster reste « green » et rien
# nulle part ne le signale. Les sondes de cluster_status rompent ce
# silence, et l'« État des composants » les affiche.
#
# Elasticsearch est le VRAI (principe 1 de conftest.py) : ce qui est
# vérifié ici, c'est le nom exact du réglage posé par ES et le type de sa
# valeur — la chaîne "true", pas le booléen. Un bouchon ne prouverait que
# ma propre lecture de la documentation, alors que c'est précisément là
# que la sonde peut se tromper sans que rien ne se voie.
#
# UN SEUL index dédié pour les deux canaux, supprimé avant ET après
# (principe 2) : les deux constantes d'index y sont détournées le temps
# du test, jamais les vrais `suggestions` et `nps_responses`, dont la
# page Statistiques de l'installation de dev se sert.

import os

import pytest

import cluster_status
import nps_log
import suggestion_log

pytestmark = pytest.mark.requires_elasticsearch

# Nom propre au processus : ce cluster est celui de l'installation de dev,
# partagé, et deux exécutions simultanées de cette suite (deux personnes,
# ou une relance pendant qu'une autre tourne) se supprimeraient l'index
# l'une l'autre en plein test. Constaté : la seconde échouait en
# « no such index » au milieu du module, pour un défaut qui n'était pas
# le sien.
INDEX_SONDE = f"docsearch_test_sonde_ecritures_{os.getpid()}"

# Jamais créé, par définition : sert le cas « rien reçu à ce jour ». Un
# nom qui n'existe pas coûte moins cher qu'un index créé puis supprimé
# pour la seule satisfaction de le voir absent.
INDEX_ABSENT = "docsearch_test_sonde_ecritures_absent"

DELAI_ES = 30


def _ecrire_suggestion(es) -> None:
    suggestion_log.log_suggestion(es, text="une idée", category=None)


def _ecrire_nps(es) -> None:
    nps_log.log_nps(es, username="test", score=9)


# Les deux canaux, sous la forme utilisée par les tests paramétrés :
# la sonde, l'écriture qu'elle surveille, et le mot attendu dans le
# message d'un index absent.
CANAUX = [
    pytest.param(cluster_status.check_suggestions, _ecrire_suggestion, "suggestion", id="suggestions"),
    pytest.param(cluster_status.check_nps, _ecrire_nps, "réponse NPS", id="nps"),
]


def _lever_les_blocages(es) -> None:
    """Remet l'index en écriture. `None` supprime un réglage côté ES."""
    es.indices.put_settings(
        index=INDEX_SONDE,
        settings=dict.fromkeys(cluster_status.WRITE_BLOCK_SETTINGS),
    )


def _bloquer(es, reglage: str) -> None:
    es.indices.put_settings(index=INDEX_SONDE, settings={reglage: True})


def _supprimer(es) -> None:
    """Les blocages sont levés AVANT la suppression : `index.blocks.read_only`
    interdit jusqu'à la suppression de l'index — c'est toute la différence
    avec read_only_allow_delete, qui la laisse passer (d'où son nom, et
    d'où le choix d'ES de poser CELUI-LÀ au flood stage : un disque plein
    se répare en supprimant des index)."""
    if es.indices.exists(index=INDEX_SONDE):
        _lever_les_blocages(es)
        es.indices.delete(index=INDEX_SONDE)


@pytest.fixture(scope="module")
def es():
    from elasticsearch import Elasticsearch

    client = Elasticsearch(cluster_status.ES_HOST, request_timeout=DELAI_ES, max_retries=0)
    yield client
    client.close()


@pytest.fixture(scope="module")
def index_sonde(es):
    """L'index jetable, créé une seule fois pour tout le module.

    `wait_for_active_shards=0` n'est pas une optimisation : au-dessus du
    high watermark (disque à 90 %), Elasticsearch n'alloue plus aucun
    shard neuf, et la création attend alors 30 s pour rien avant de
    rendre la main sur un `shards_acknowledged: false`. C'est ce délai
    qui faisait échouer ces tests en suite complète, sur la VM même dont
    le disque saturé motive toute cette carte.

    Attendre l'allocation ne servirait de toute façon à rien ici : les
    blocages d'index sont des MÉTADONNÉES, qui se posent et se lisent sur
    un shard non alloué. Et une écriture sur index bloqué est refusée par
    le blocage avant même que le routage n'entre en jeu — c'est ce que
    vérifie test_la_sonde_decrit_une_perte_reelle_et_silencieuse, y
    compris sur un cluster incapable d'allouer.

    Une réplique de moins (`number_of_replicas: 0`) parce que ce cluster
    n'a qu'un nœud : la réplique par défaut resterait éternellement non
    allouée, à salir l'état d'un cluster partagé pour rien.
    """
    _supprimer(es)
    es.indices.create(
        index=INDEX_SONDE,
        settings={"number_of_replicas": 0},
        wait_for_active_shards=0,
    )
    yield es
    _supprimer(es)


@pytest.fixture
def sonde(monkeypatch, index_sonde):
    """Fait porter LES DEUX sondes sur l'index jetable, et le rouvre entre
    deux tests — chacun pose le blocage qu'il veut voir, aucun n'hérite de
    celui du précédent."""
    monkeypatch.setattr(suggestion_log, "SUGGESTION_LOG_INDEX", INDEX_SONDE)
    monkeypatch.setattr(nps_log, "NPS_LOG_INDEX", INDEX_SONDE)
    # Variables de module : sans cette remise à zéro, _ensure_index() se
    # croirait déjà passé sur les véritables index.
    monkeypatch.setattr(suggestion_log, "_index_ready", False)
    monkeypatch.setattr(nps_log, "_index_ready", False)
    _lever_les_blocages(index_sonde)
    yield index_sonde
    _lever_les_blocages(index_sonde)


@pytest.mark.parametrize(("sonder", "_ecrire", "attendu"), CANAUX)
def test_sans_index_l_etat_est_inconnu_pas_en_panne(monkeypatch, sonder, _ecrire, attendu):
    # Installation neuve : l'index naît à la première contribution reçue.
    # « Inconnu » et « en panne » doivent rester distincts — une carte
    # rouge au démarrage de chaque installation apprendrait à
    # l'administrateur à ignorer ce voyant.
    monkeypatch.setattr(suggestion_log, "SUGGESTION_LOG_INDEX", INDEX_ABSENT)
    monkeypatch.setattr(nps_log, "NPS_LOG_INDEX", INDEX_ABSENT)

    etat = sonder()

    assert etat["ok"] is None
    assert attendu in etat["reason"]


@pytest.mark.parametrize(("sonder", "_ecrire", "_attendu"), CANAUX)
def test_un_index_ouvert_aux_ecritures_est_rapporte_ok(sonde, sonder, _ecrire, _attendu):
    assert sonder() == {"ok": True, "index": INDEX_SONDE}


@pytest.mark.parametrize(("sonder", "_ecrire", "_attendu"), CANAUX)
def test_le_blocage_du_flood_stage_watermark_est_rapporte_avec_sa_cause(
    sonde, sonder, _ecrire, _attendu
):
    _bloquer(sonde, "index.blocks.read_only_allow_delete")

    etat = sonder()
    assert etat["ok"] is False
    assert "flood-stage" in etat["error"]


@pytest.mark.parametrize(("sonder", "ecrire", "_attendu"), CANAUX)
def test_la_sonde_decrit_une_perte_reelle_et_silencieuse(sonde, sonder, ecrire, _attendu):
    # Le cœur de l'affaire : sous ce blocage, l'écriture est bel et bien
    # refusée par Elasticsearch, et pourtant le module de journalisation
    # n'en laisse rien paraître. Sans la sonde, la panne n'a aucun
    # symptôme.
    _bloquer(sonde, "index.blocks.read_only_allow_delete")

    with pytest.raises(Exception) as echec:
        sonde.index(index=INDEX_SONDE, document={"text": "une contribution"})
    assert "read-only" in str(echec.value)

    # Ne lève pas, ne retourne rien, ne dit rien — comportement voulu.
    ecrire(sonde)

    assert sonder()["ok"] is False


@pytest.mark.parametrize(("sonder", "_ecrire", "_attendu"), CANAUX)
def test_un_blocage_pose_a_la_main_est_vu_aussi(sonde, sonder, _ecrire, _attendu):
    # Même silence, autre cause : une intervention d'exploitation oubliée
    # (ou une restauration d'index laissée en écriture bloquée) produit
    # exactement les mêmes symptômes que le disque plein.
    _bloquer(sonde, "index.blocks.write")

    etat = sonder()
    assert etat["ok"] is False
    assert "flood-stage" not in etat["error"]
    assert "bloquées" in etat["error"]


@pytest.mark.parametrize(("sonder", "_ecrire", "_attendu"), CANAUX)
def test_elasticsearch_injoignable_ne_se_dit_pas_en_panne(monkeypatch, sonder, _ecrire, _attendu):
    # Port fermé plutôt qu'un bouchon. ES injoignable est une panne réelle,
    # mais sa carte à lui l'affiche déjà en rouge : une seconde carte rouge
    # pour la même cause enverrait l'administrateur chercher au mauvais
    # endroit (« les suggestions ET Elasticsearch »).
    monkeypatch.setattr(cluster_status, "ES_HOST", "http://127.0.0.1:1")

    etat = sonder()
    assert etat["ok"] is None
    assert etat["reason"]
