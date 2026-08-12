# tests/test_retention_journaux.py — La purge des journaux supprime-t-elle
# ce qu'il faut, et RIEN d'autre ?
#
# C'est le premier mécanisme de DocSearch qui supprime des données sans
# que personne l'ait demandé au moment où il le fait. Ce qui se vérifie
# ici, dans l'ordre :
#
# 1. **Ce qui reste.** Un document plus jeune que la durée de conservation
#    ne doit pas partir, et une durée à 0 (« illimitée ») ne doit rien
#    supprimer du tout — pas même « presque rien ».
# 2. **Ce qui n'est jamais touché.** custom_keywords et saved_collections
#    sont des données utilisateur, pas des traces. La liste des journaux
#    est explicite et close ; ce test la relit.
# 3. **La borne par passage.** Une première purge sur une installation
#    ancienne porterait sur des millions de documents : elle doit être
#    plafonnée, et le reliquat partir le lendemain.
# 4. **La trace.** La purge du journal d'audit s'inscrit dans le journal
#    d'audit. C'est la trace qui protège l'administrateur : elle ne peut
#    pas être la seule à disparaître sans rien laisser.
#
# Elasticsearch est le vrai (principe 1 de conftest.py) : c'est lui qui
# exécute le delete_by_query, et lui seul peut dire si la requête de date
# est juste. Seule la LECTURE DE CONFIGURATION est remplacée, par un
# `get_param` de test — écrire les vraies durées passerait par la clé
# Redis de configuration de l'installation de développement, que ces
# tests n'ont pas à modifier (principe 2).

from datetime import datetime, timedelta, timezone

import pytest

import audit_log
import cluster_status
import log_retention
import runtime_config

requiert_es = pytest.mark.requires_elasticsearch
requiert_redis = pytest.mark.requires_redis

INDEX_SONDE = "docsearch_test_sonde_retention"
INDEX_AUDIT = "docsearch_test_sonde_retention_audit"

# Même constat que les autres modules à index jetable sur cette VM.
DELAI_ES = 60


def _il_y_a(jours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=jours)).isoformat()


@pytest.fixture(scope="module")
def es():
    from elasticsearch import Elasticsearch

    client = Elasticsearch(cluster_status.ES_HOST, request_timeout=DELAI_ES, max_retries=0)
    yield client
    client.close()


def _supprimer(es, index: str) -> None:
    if es.indices.exists(index=index):
        es.indices.delete(index=index)


@pytest.fixture(scope="module")
def index_sonde(es):
    _supprimer(es, INDEX_SONDE)
    es.indices.create(index=INDEX_SONDE, mappings={"properties": {"timestamp": {"type": "date"}}})
    yield es
    _supprimer(es, INDEX_SONDE)


@pytest.fixture
def journal_sonde(monkeypatch, index_sonde):
    """Fait porter le journal des recherches sur l'index jetable, et
    n'expose que celui-là : les quatre autres journaux ne doivent pas
    partir sur les index de l'installation de développement."""
    monkeypatch.setattr(
        log_retention,
        "JOURNAUX",
        (log_retention.Journal("retention_search_logs_days", "sonde", lambda: INDEX_SONDE),),
    )
    return index_sonde


def _peupler(es, index: str, ages: list[int]) -> None:
    """Un document par âge (en jours), puis rafraîchit — sans quoi la
    purge ne verrait rien et le test passerait au vert pour la mauvaise
    raison."""
    es.delete_by_query(index=index, query={"match_all": {}}, refresh=True, conflicts="proceed")
    for age in ages:
        es.index(index=index, document={"timestamp": _il_y_a(age)})
    es.indices.refresh(index=index)


def _regler(monkeypatch, valeur) -> None:
    monkeypatch.setattr(runtime_config, "get_param", lambda cle: valeur)


def _compter(es, index: str) -> int:
    es.indices.refresh(index=index)
    return es.count(index=index)["count"]


# ── 1. Ce qui part, ce qui reste ─────────────────────────────

@requiert_es
def test_supprime_au_dela_de_la_duree_et_garde_le_reste(monkeypatch, journal_sonde):
    es = journal_sonde
    _peupler(es, INDEX_SONDE, [400, 380, 100, 10])
    _regler(monkeypatch, 365)

    rapport = log_retention.purger(es)

    assert rapport[0]["supprimés"] == 2
    assert _compter(es, INDEX_SONDE) == 2


@requiert_es
def test_zero_signifie_conservation_illimitee(monkeypatch, journal_sonde):
    """0 n'est pas « purger tout » ni « purger aujourd'hui » : c'est
    « ne rien supprimer », et c'est écrit tel quel dans l'interface."""
    es = journal_sonde
    _peupler(es, INDEX_SONDE, [4000, 400, 10])
    _regler(monkeypatch, 0)

    assert log_retention.purger(es) == []
    assert _compter(es, INDEX_SONDE) == 3


@requiert_es
def test_une_valeur_illisible_ne_supprime_rien(monkeypatch, journal_sonde):
    """Sur un mécanisme qui supprime, le doute profite à la conservation."""
    es = journal_sonde
    _peupler(es, INDEX_SONDE, [4000, 400])
    _regler(monkeypatch, "beaucoup")

    assert log_retention.purger(es) == []
    assert _compter(es, INDEX_SONDE) == 2


@requiert_es
def test_un_index_absent_n_est_pas_une_erreur(monkeypatch, index_sonde):
    """Une fonctionnalité jamais utilisée n'a pas d'index : la purge doit
    l'ignorer, pas échouer et emporter le passage entier."""
    monkeypatch.setattr(
        log_retention,
        "JOURNAUX",
        (log_retention.Journal("retention_search_logs_days", "absent",
                               lambda: "docsearch_test_sonde_inexistant"),),
    )
    _regler(monkeypatch, 365)

    rapport = log_retention.purger(index_sonde)
    assert rapport[0]["supprimés"] == 0
    assert "erreur" not in rapport[0]


# ── 2. Ce qui n'est jamais touché ────────────────────────────

def test_les_donnees_utilisateur_ne_sont_pas_des_journaux():
    """custom_keywords et saved_collections n'ont aucune raison
    d'expirer : un mot-clé posé sur un document et une collection sont des
    données, pas des traces. La liste est explicite et close — jamais un
    motif du genre `*_logs`."""
    cles = {journal.cle for journal in log_retention.JOURNAUX}
    assert cles == {
        "retention_search_logs_days",
        "retention_login_events_days",
        "retention_audit_log_days",
        "retention_nps_days",
        "retention_suggestions_days",
    }
    for journal in log_retention.JOURNAUX:
        assert journal.index() not in ("custom_keywords", "saved_collections")


def test_chaque_journal_a_son_reglage():
    """Une clé absente de DEFAULT_RUNTIME serait refusée par set_param() :
    le réglage existerait dans le code et serait impossible à modifier
    depuis le panneau d'administration."""
    for journal in log_retention.JOURNAUX:
        assert journal.cle in runtime_config.DEFAULT_RUNTIME


# ── 3. La borne par passage ──────────────────────────────────

@requiert_es
def test_la_purge_est_plafonnee_par_passage(monkeypatch, journal_sonde):
    es = journal_sonde
    _peupler(es, INDEX_SONDE, [400, 401, 402, 403])
    _regler(monkeypatch, 365)
    monkeypatch.setattr(log_retention, "MAX_DOCS_PAR_PASSAGE", 2)

    assert log_retention.purger(es)[0]["supprimés"] == 2
    assert _compter(es, INDEX_SONDE) == 2

    # Le reliquat part au passage suivant.
    assert log_retention.purger(es)[0]["supprimés"] == 2
    assert _compter(es, INDEX_SONDE) == 0


# ── 4. La trace ──────────────────────────────────────────────

@requiert_es
def test_la_purge_du_journal_d_audit_s_inscrit_dedans(monkeypatch, es):
    # Mapping du VRAI journal d'audit, pas seulement sa date : sans
    # `method` en keyword, le mapping dynamique en ferait un champ
    # analysé, et le `term` de vérification plus bas ne trouverait rien —
    # le test échouerait pour une raison qui n'existe pas en production.
    _supprimer(es, INDEX_AUDIT)
    es.indices.create(
        index=INDEX_AUDIT,
        mappings={
            "properties": {
                "timestamp": {"type": "date"},
                "username": {"type": "keyword"},
                "method": {"type": "keyword"},
                "path": {"type": "keyword"},
                "status_code": {"type": "integer"},
            }
        },
    )
    try:
        monkeypatch.setattr(audit_log, "AUDIT_LOG_INDEX", INDEX_AUDIT)
        monkeypatch.setattr(audit_log, "_index_ready", True)
        monkeypatch.setattr(
            log_retention,
            "JOURNAUX",
            (log_retention.Journal("retention_audit_log_days", "audit", lambda: INDEX_AUDIT),),
        )
        _peupler(es, INDEX_AUDIT, [4000, 3000])
        _regler(monkeypatch, 365)

        log_retention.purger(es)
        es.indices.refresh(index=INDEX_AUDIT)

        traces = es.search(index=INDEX_AUDIT, query={"term": {"method": "DELETE"}})
        assert traces["hits"]["total"]["value"] == 1
        trace = traces["hits"]["hits"][0]["_source"]
        assert trace["username"] == "système (rétention)"
        assert "retention" in trace["path"]
        assert trace["body"]["supprimés"] == 2
    finally:
        _supprimer(es, INDEX_AUDIT)


# ── Aperçu et ordonnancement ─────────────────────────────────

@requiert_es
def test_l_apercu_ne_supprime_rien(monkeypatch, journal_sonde):
    es = journal_sonde
    _peupler(es, INDEX_SONDE, [400, 10])
    _regler(monkeypatch, 365)

    ligne = log_retention.apercu(es)[0]

    assert (ligne["total"], ligne["expirés"]) == (2, 1)
    assert _compter(es, INDEX_SONDE) == 2


@requiert_redis
def test_le_passage_n_a_lieu_qu_une_fois_par_intervalle():
    """La durée de vie de la clé EST l'intervalle : `SET NX EX` marque le
    passage et sert de verrou entre plusieurs exemplaires du worker, sans
    fenêtre entre le test et la pose."""
    client = log_retention._get_redis_client()
    client.delete(log_retention.VERROU_KEY)
    try:
        assert log_retention.passage_du() is True
        assert log_retention.passage_du() is False
    finally:
        client.delete(log_retention.VERROU_KEY)
