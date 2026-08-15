# tests/test_contrat_vendorise.py — Le contrat partagé, et la propriété
# pour laquelle il existe.
#
# `app/docsearch_contract/` est une COPIE GÉNÉRÉE de
# docsearch-infra/contract/docsearch_contract/ (voir son README). Deux
# choses sont vérifiées ici, et elles ne se recouvrent pas :
#
#   1. la copie n'a pas dérivé de sa source — quand la source est
#      atteignable, c'est-à-dire sur une machine où les dépôts sont
#      clonés côte à côte. Ailleurs (CI, qui ne clone qu'un dépôt), ce
#      test se saute, comme les tests LDAP et Kerberos ;
#   2. la propriété que le contrat apporte : search_api et search_query
#      donnent la MÊME réponse à « quelles sources cet utilisateur
#      peut-il atteindre ». C'est le seul des deux qui vaut vraiment,
#      parce qu'il tomberait aussi si quelqu'un réintroduisait une copie
#      locale de la règle.
#
# Le second tourne partout et sans service : les deux fonctions lisent
# les mêmes registres, quels qu'ils soient — ceux de l'installation de
# dev si Redis répond, les valeurs de repli sinon.

import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

import search_query  # noqa: E402
import source_registries  # noqa: E402
from docsearch_contract import CONTRACT_VERSION, sources as contract_sources  # noqa: E402

CONTRAT_VENDORISE = APP_DIR / "docsearch_contract"
CONTRAT_SOURCE = (
    Path(__file__).resolve().parent.parent.parent
    / "docsearch-infra" / "contract" / "docsearch_contract"
)


def test_version_du_contrat_lisible():
    """Un manifeste de module complémentaire déclarera cette version
    (lot 2) : elle doit rester une version sémantique lisible, pas une
    chaîne libre."""
    parties = CONTRACT_VERSION.split(".")
    assert len(parties) == 3
    assert all(p.isdigit() for p in parties)


def test_les_trois_registres_sont_declares():
    """La liaison locale doit couvrir les trois types natifs — un
    registre oublié ici rendrait ses sources invisibles PARTOUT, sans
    aucune erreur."""
    assert set(source_registries.REGISTRES) == set(contract_sources.TYPES_NATIFS)


@pytest.mark.skipif(
    not CONTRAT_SOURCE.is_dir(),
    reason="docsearch-infra non cloné à côté — dérive invérifiable ici",
)
def test_copie_identique_a_la_source():
    """La copie se régénère par `./manage.sh sync-contract` depuis
    docsearch-infra ; la modifier sur place ferait exactement ce que ce
    lot cherche à supprimer — deux exemplaires d'une même règle, dont un
    seul est lu par la moitié du système."""
    attendus = sorted(p.name for p in CONTRAT_SOURCE.glob("*.py"))
    presents = sorted(p.name for p in CONTRAT_VENDORISE.glob("*.py"))
    assert presents == attendus, "fichier ajouté ou retiré dans la copie"

    divergents = [
        nom for nom in attendus
        if (CONTRAT_SOURCE / nom).read_bytes() != (CONTRAT_VENDORISE / nom).read_bytes()
    ]
    assert not divergents, (
        f"copie divergente : {', '.join(divergents)} — porter la modification dans "
        "docsearch-infra/contract/, puis ./manage.sh sync-contract"
    )


def test_search_api_et_search_query_voient_les_memes_sources(monkeypatch):
    """LA propriété du lot 0.

    Les deux fichiers construisent leurs requêtes séparément — c'est
    délibéré, et l'avertissement en tête de search_query.py le dit. Mais
    « quelles sources cet utilisateur peut-il atteindre » n'est pas de
    la construction de requête : c'est une décision d'accès, et elle
    n'admet qu'une réponse. Une divergence ici ne se verrait pas depuis
    l'interface — elle ferait notifier une alerte sur une source que
    l'écran n'affiche plus, ou taire une alerte sur une source ouverte.

    L'annuaire est remplacé (les groupes sont l'ENTRÉE de la règle, pas
    la règle) ; les registres, eux, sont les vrais.
    """
    import search_api

    for groupes in ([], ["DL-RH"], ["DL-RH", "DL-COMPTA"]):
        monkeypatch.setattr(search_api, "get_effective_groups", lambda _u, g=groupes: g)
        monkeypatch.setattr(search_query, "get_effective_groups", lambda _u, g=groupes: g)
        assert (
            search_api._searchable_source_names("bob.user")
            == search_query._searchable_source_names("bob.user")
        )
