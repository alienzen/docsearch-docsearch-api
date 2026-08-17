# tests/test_suggestions.py — Le recueil des suggestions, et surtout leur
# suppression
#
# Deux modules citaient déjà ce fichier (test_temps_recherche.py,
# test_ecritures_bloquees.py) pour la contrainte de disque qu'il partage
# avec eux ; il naît ici avec la suppression, qui est le premier geste
# IRRÉVERSIBLE de ce module — un statut se corrige, un effacement non.
#
# Ce qui se joue et qu'aucune relecture ne prouve :
#
#   1. la suppression retire vraiment le document de l'index, et la
#      liste rechargée juste après ne le montre plus. C'est tout l'enjeu
#      du `refresh=True` : sans lui, ES acquitte la suppression et le
#      document reste visible jusqu'au prochain rafraîchissement (1 s par
#      défaut) — la page de statistiques recharge bien plus vite que ça,
#      et la ligne effacée y réapparaît, ce qui se lit comme un échec ;
#   2. un identifiant inconnu lève NotFoundError, que l'API traduit en
#      404 — distinct du 503 d'un moteur muet, parce que les deux
#      n'appellent pas la même réaction (recharger vs réessayer) ;
#   3. l'anonymat survit à la suppression d'une AUTRE suggestion : rien
#      dans le geste ne doit toucher aux voisines.
#
# Elasticsearch est le vrai (principe 1 de conftest.py) : ce qui est
# testé est le comportement du moteur, pas la façon dont on l'appelle.
#
# ⚠️ Comme tout module créant un index jetable, ces tests exigent du
# DISQUE et pas seulement un moteur qui répond — voir l'avertissement
# détaillé en tête de test_temps_recherche.py.

import pytest
from elasticsearch import NotFoundError

import cluster_status
import suggestion_log

requiert_es = pytest.mark.requires_elasticsearch

# Index jetable, jamais l'index `suggestions` de l'installation de dev
# (principe 2 de conftest.py) : ces tests SUPPRIMENT des documents.
INDEX_SONDE = "docsearch_test_sonde_suggestions_admin"

# Même raison que dans test_temps_recherche.py : la création d'un index
# coûte jusqu'à 30 s sur cette VM, donc un index pour tout le module.
DELAI_ES = 60


@pytest.fixture(scope="module")
def es():
    from elasticsearch import Elasticsearch

    client = Elasticsearch(cluster_status.ES_HOST, request_timeout=DELAI_ES, max_retries=0)
    yield client
    client.close()


@pytest.fixture(scope="module")
def index(es):
    """Détourne le module vers l'index jetable, et le rend au module
    suivant tel qu'il l'a trouvé — `_index_ready` compris, sans quoi un
    module ultérieur croirait l'index de production déjà prêt."""
    precedent, precedent_pret = suggestion_log.SUGGESTION_LOG_INDEX, suggestion_log._index_ready
    suggestion_log.SUGGESTION_LOG_INDEX = INDEX_SONDE
    suggestion_log._index_ready = False

    es.indices.delete(index=INDEX_SONDE, ignore_unavailable=True)
    yield es
    es.indices.delete(index=INDEX_SONDE, ignore_unavailable=True)

    suggestion_log.SUGGESTION_LOG_INDEX = precedent
    suggestion_log._index_ready = precedent_pret


@pytest.fixture
def vide(index):
    """Un index propre avant chaque test : les identifiants sont tirés
    par ES, un test ne peut pas deviner ceux du précédent."""
    if index.indices.exists(index=INDEX_SONDE):
        index.delete_by_query(index=INDEX_SONDE, query={"match_all": {}}, refresh=True)
    return index


def _ids(es) -> list[str]:
    return [s["id"] for s in suggestion_log.list_suggestions(es, size=50, from_=0)["results"]]


@requiert_es
def test_supprime_et_la_liste_ne_la_montre_plus(vide):
    """LE test de la suppression : la relecture qui suit immédiatement.
    Une assertion sur `es.get()` seul passerait même sans `refresh=True`
    (un get est temps réel), et raterait donc exactement le défaut que ce
    test doit attraper."""
    suggestion_log.log_suggestion(vide, text="doublon à effacer", category="idea")
    suggestion_log.log_suggestion(vide, text="à garder", category="idea", username="bob.user")
    vide.indices.refresh(index=INDEX_SONDE)

    avant = suggestion_log.list_suggestions(vide, size=50, from_=0)
    a_effacer = next(s["id"] for s in avant["results"] if s["text"] == "doublon à effacer")

    suggestion_log.delete_suggestion(vide, suggestion_id=a_effacer)

    apres = suggestion_log.list_suggestions(vide, size=50, from_=0)
    assert apres["total"] == avant["total"] - 1
    assert [s["text"] for s in apres["results"]] == ["à garder"]


@requiert_es
def test_une_suggestion_inconnue_leve_not_found(vide):
    """C'est ce type d'exception, et non un silence, qui permet à l'API
    de répondre 404 plutôt que 503 : « quelqu'un l'a déjà supprimée »
    n'appelle pas la même réaction que « le moteur ne répond pas »."""
    with pytest.raises(NotFoundError):
        suggestion_log.delete_suggestion(vide, suggestion_id="identifiant-qui-n-existe-pas")


@requiert_es
def test_ne_touche_pas_aux_voisines(vide):
    """Une suppression est ciblée : le statut et l'anonymat des autres
    suggestions traversent le geste intacts."""
    suggestion_log.log_suggestion(vide, text="anonyme", category=None)
    suggestion_log.log_suggestion(vide, text="signée", category=None, username="bob.user",
                                  groups=["direction"])
    vide.indices.refresh(index=INDEX_SONDE)

    signee = next(s for s in suggestion_log.list_suggestions(vide, size=50, from_=0)["results"]
                  if s["text"] == "signée")
    suggestion_log.set_status(vide, suggestion_id=signee["id"], status="en_cours")
    vide.indices.refresh(index=INDEX_SONDE)

    anonyme = next(s for s in suggestion_log.list_suggestions(vide, size=50, from_=0)["results"]
                   if s["text"] == "anonyme")
    suggestion_log.delete_suggestion(vide, suggestion_id=anonyme["id"])

    restantes = suggestion_log.list_suggestions(vide, size=50, from_=0)["results"]
    assert len(restantes) == 1
    assert restantes[0]["status"] == "en_cours"
    assert restantes[0]["username"] == "bob.user"


@requiert_es
def test_supprimer_deux_fois_leve_la_seconde_fois(vide):
    """Deux administrateurs sur la même page : le second clic ne doit pas
    passer pour un succès, sans quoi la page prétendrait avoir supprimé
    ce qu'un autre avait déjà effacé."""
    suggestion_log.log_suggestion(vide, text="une seule fois", category=None)
    vide.indices.refresh(index=INDEX_SONDE)
    identifiant = _ids(vide)[0]

    suggestion_log.delete_suggestion(vide, suggestion_id=identifiant)
    with pytest.raises(NotFoundError):
        suggestion_log.delete_suggestion(vide, suggestion_id=identifiant)
