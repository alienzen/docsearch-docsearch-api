# tests/test_temps_recherche.py — Le temps de chaque recherche est-il
# réellement conservé, et les recherches lentes signalées ?
#
# Ce que ces tests protègent :
#
# 1. Le piège du zéro. Elasticsearch rapporte régulièrement took=0 sur une
#    requête servie depuis son cache. Écrit avec le test de vérité employé
#    pour les autres champs de log_search() (`if extension:`), ce 0
#    disparaîtrait du journal — ne resteraient enregistrées que les
#    recherches ayant pris du temps, et toute moyenne calculée dessus
#    serait mécaniquement fausse, sans que rien ne le signale.
#
# 2. Le mapping d'un index DÉJÀ créé. Les nouveaux champs sont ajoutés par
#    put_mapping à une installation existante ; oublier cette branche
#    ferait qu'ils ne fonctionneraient que sur une installation neuve —
#    exactement le cas qu'aucun développeur ne rencontre et que toutes les
#    productions rencontrent.
#
# Elasticsearch est le VRAI (principe 1 de conftest.py) : ce qui est
# vérifié, c'est qu'ES accepte ce mapping et restitue ces valeurs. Un
# bouchon ne prouverait que ma propre relecture du code.
#
# Index dédié, supprimé avant ET après (principe 2) : jamais le véritable
# index `search_logs`, dont la page Statistiques de l'installation de dev
# se sert.
#
# ⚠️ Ces tests-là exigent du DISQUE, pas seulement un Elasticsearch qui
# répond. Au-dessus du seuil haut d'allocation
# (cluster.routing.allocation.disk.watermark.high, 90 % par défaut), ES
# refuse d'allouer le moindre shard d'un index NEUF : l'index se crée en
# rouge, chaque écriture échoue, et la seule trace est un timeout de 30 s
# à la création. Le symptôme ressemble à une panne de code ; ce n'en est
# pas une. À vérifier avant de chercher ailleurs :
#   curl -s localhost:9200/_cluster/allocation/explain?pretty \
#        -H 'Content-Type: application/json' \
#        -d '{"index":"<index>","shard":0,"primary":true}'
# La contrainte vaut pour tous les modules de tests qui créent un index
# jetable (test_suggestions.py, test_ecritures_bloquees.py), pas seulement
# celui-ci. Les tests de seuil en fin de fichier, eux, n'ont besoin de
# rien et tournent toujours.

import logging

import pytest

import cluster_status
import search_log

# Marqueur posé sur les seuls tests de persistance, PAS sur le module :
# la seconde moitié du fichier ne touche pas Elasticsearch, et un
# marqueur global l'aurait fait sauter à chaque cluster indisponible —
# soit précisément les jours où l'on a besoin de vérifier que le
# signalement des recherches lentes fonctionne encore.
requiert_es = pytest.mark.requires_elasticsearch

# Index jetable PRÉ-EXISTANT, créé sans mapping : reproduit une
# installation antérieure à la mesure, donc la branche put_mapping de
# _ensure_index — celle que traversent toutes les productions déjà en
# service, et la seule qu'une installation neuve ne rencontre jamais.
INDEX_SONDE = "docsearch_test_sonde_temps_recherche"

# Index jetable jamais créé à la main : c'est _ensure_index qui le crée,
# ce qui couvre l'autre branche (installation neuve).
INDEX_NEUF = "docsearch_test_sonde_temps_recherche_neuf"

# Même raison que dans test_suggestions.py, mesurée sur cette VM : la
# création d'un index y prend 30 s (mise à jour d'état de cluster, nœud
# unique, disque à 90 %), soit très exactement le délai qui faisait
# échouer ces tests. Les index sont donc créés UNE fois pour tout le
# module — un test qui recréerait le sien coûterait une demi-minute.
DELAI_ES = 60


def _journaliser(es, **temps):
    """Une recherche journalisée, réduite à ce qui nous intéresse ici."""
    return search_log.log_search(
        es,
        username="sonde",
        ip=None,
        query="rapport",
        search_in="all",
        source=None,
        total_results=3,
        result_files=["a.pdf"],
        **temps,
    )


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
    """L'index jetable, créé une seule fois pour tout le module, et SANS
    mapping — c'est _ensure_index qui doit y ajouter les champs de temps."""
    _supprimer(es, INDEX_SONDE)
    es.indices.create(index=INDEX_SONDE)
    yield es
    _supprimer(es, INDEX_SONDE)


@pytest.fixture
def journal(monkeypatch, index_sonde):
    """Fait porter la journalisation sur l'index jetable. Les documents
    s'y accumulent d'un test à l'autre : chacun relit le sien par son
    identifiant, aucun ne compte les lignes."""
    monkeypatch.setattr(search_log, "SEARCH_LOG_INDEX", INDEX_SONDE)
    # Variable de module : sans cette remise à zéro, _ensure_index() se
    # croirait déjà passé sur le véritable index `search_logs`.
    monkeypatch.setattr(search_log, "_index_ready", False)
    return index_sonde


def _relire(es, doc_id: str, index: str = INDEX_SONDE) -> dict:
    es.indices.refresh(index=index)
    return es.get(index=index, id=doc_id)["_source"]


@requiert_es
def test_les_deux_temps_sont_enregistres(journal):
    doc_id = _journaliser(journal, took_ms=12, duration_ms=34.5)

    doc = _relire(journal, doc_id)
    assert doc["took_ms"] == 12
    assert doc["duration_ms"] == pytest.approx(34.5)


@requiert_es
def test_un_temps_moteur_nul_est_conserve(journal):
    # LE test de ce fichier. `took=0` est une mesure, pas une absence de
    # mesure : ES le rapporte pour une requête servie depuis son cache.
    doc_id = _journaliser(journal, took_ms=0, duration_ms=0.4)

    doc = _relire(journal, doc_id)
    assert doc["took_ms"] == 0, "le temps moteur nul a été effacé par un test de vérité"
    assert doc["duration_ms"] == pytest.approx(0.4)


@requiert_es
def test_une_recherche_sans_mesure_reste_journalisee(journal):
    # Rétrocompatibilité : un appelant qui ne fournit pas de temps (le
    # worker d'alertes, une version antérieure) doit continuer d'écrire sa
    # ligne, simplement sans durée — et non échouer.
    doc_id = _journaliser(journal)

    doc = _relire(journal, doc_id)
    assert doc_id is not None
    assert "took_ms" not in doc
    assert "duration_ms" not in doc


def _proprietes(es, index: str) -> dict:
    return es.indices.get_mapping(index=index)[index]["mappings"]["properties"]


@requiert_es
def test_les_champs_de_temps_sont_ajoutes_a_un_index_deja_cree(journal):
    # LA branche que seule une installation déjà en service traverse :
    # l'index existe et ignore tout des durées, put_mapping doit l'y
    # amener. L'oublier ferait une fonctionnalité qui marche chez tous
    # les développeurs et chez aucun exploitant.
    _journaliser(journal, took_ms=7, duration_ms=21.0)

    proprietes = _proprietes(journal, INDEX_SONDE)
    assert proprietes["took_ms"]["type"] == "integer"
    assert proprietes["duration_ms"]["type"] == "float"


@requiert_es
def test_un_index_cree_de_zero_porte_deja_les_champs_de_temps(monkeypatch, es):
    # L'autre branche : installation neuve, l'index naît à la première
    # recherche journalisée.
    monkeypatch.setattr(search_log, "SEARCH_LOG_INDEX", INDEX_NEUF)
    monkeypatch.setattr(search_log, "_index_ready", False)
    _supprimer(es, INDEX_NEUF)
    try:
        doc_id = _journaliser(es, took_ms=7, duration_ms=21.0)

        proprietes = _proprietes(es, INDEX_NEUF)
        assert proprietes["took_ms"]["type"] == "integer"
        assert proprietes["duration_ms"]["type"] == "float"
        assert _relire(es, doc_id, INDEX_NEUF)["took_ms"] == 7
    finally:
        _supprimer(es, INDEX_NEUF)


# ── Ligne de journal des recherches lentes ───────────────────
#
# Sans Elasticsearch : c'est la décision d'écrire qui est en jeu, pas la
# mesure. `caplog` est le journal réel de la bibliothèque standard, pas un
# bouchon — ce qui est observé est bien ce qui partira dans journalctl.


@pytest.fixture
def journalisation(monkeypatch):
    import search_api

    def appeler(duration_ms: float, seuil: int):
        monkeypatch.setattr(search_api, "SLOW_SEARCH_MS", seuil)
        search_api._journaliser_temps(
            query="rapport",
            search_in="all",
            total=3,
            username="sonde",
            took_ms=1,
            duration_ms=duration_ms,
        )

    return appeler


@pytest.mark.parametrize("duree", [2000.0, 4200.0])
def test_une_recherche_au_dela_du_seuil_laisse_un_avertissement(journalisation, caplog, duree):
    # Le seuil lui-même déclenche : « 2000 ms max » se lit comme une
    # limite atteinte, pas comme une limite à dépasser.
    with caplog.at_level(logging.WARNING, logger="search_api"):
        journalisation(duree, 2000)

    assert any("Recherche lente" in m for m in caplog.messages)


def test_une_recherche_rapide_ne_laisse_aucun_avertissement(journalisation, caplog):
    with caplog.at_level(logging.WARNING, logger="search_api"):
        journalisation(120.0, 2000)

    assert caplog.messages == []


def test_un_seuil_a_zero_desactive_l_avertissement(journalisation, caplog):
    # Documenté comme le moyen de faire taire cette ligne : une recherche
    # de dix secondes ne doit alors rien écrire, sans quoi le réglage ne
    # sert à rien.
    with caplog.at_level(logging.WARNING, logger="search_api"):
        journalisation(10_000.0, 0)

    assert caplog.messages == []
