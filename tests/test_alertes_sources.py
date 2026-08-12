# tests/test_alertes_sources.py — Une alerte reste bornée à la source sur
# laquelle elle a été posée, même quand cette source disparaît.
#
# Ce que ces tests verrouillent, mesuré en dev sur une recherche
# enregistrée filtrant sur une seule source :
#
#   - le nom de la source retiré du registre (source supprimée, renommée,
#     désactivée, ou sortie des groupes de l'utilisateur), la vérification
#     d'alerte l'écartait et ne posait PLUS AUCUN filtre de source :
#     l'alerte devenait fédérée, c'est-à-dire portée sur toutes les
#     sources cherchables de l'utilisateur. Il recevait alors des
#     notifications pour des documents hors de ce à quoi il s'était
#     abonné. Les résultats restaient dans son ACL et dans ses sources
#     cherchables (filtres obligatoires, vérifiés ici aussi) — c'était un
#     défaut de justesse, pas une fuite.
#
# Le pendant côté /search est dans test_acces_sources.py
# (_requested_source_names) : même convention None / liste vide, testée
# des deux côtés parce que ce sont deux implémentations séparées que rien
# d'autre ne tient synchronisées (voir l'en-tête de app/search_query.py).
#
# ── Sur ce qui est injecté ici ───────────────────────────────────────
#
# `_searchable_source_names()` et `get_effective_groups()` sont remplacés
# — les deux ENTRÉES des fonctions testées, pas leur logique ; le tri des
# sources demandées et la construction de la requête sont le vrai code.
# `sql_sources_config.get_sources()` l'est aussi, mais pour une autre
# raison : sans lui, la construction lirait les sources SQL de
# l'installation de dev dans son Redis (voir conftest.py, principe 2), et
# ces tests ne portent pas sur les facettes personnalisées.

import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

import search_query  # noqa: E402

# Sources telles que les verrait un utilisateur donné. "ouverte" est
# cherchable, les autres ne le sont pas — retirées du registre, renommées,
# désactivées, ou réservées à un groupe dont il n'est pas membre. Les
# quatre cas produisent la même absence de cette liste, et c'est le point :
# la vérification d'alerte n'a pas à savoir laquelle des quatre.
SOURCES_OUVERTES = ["ouverte", "autre-ouverte"]

BOB = "bob.user"


@pytest.fixture
def acces(monkeypatch):
    """Fixe ce que bob.user peut chercher, ses groupes annuaire, et vide
    le registre SQL (aucune facette personnalisée en jeu)."""
    monkeypatch.setattr(search_query, "_searchable_source_names", lambda username: list(SOURCES_OUVERTES))
    monkeypatch.setattr(search_query, "get_effective_groups", lambda username: ["docsearch-users"])
    monkeypatch.setattr(search_query.sql_sources_config, "get_sources", dict)


def filtres_de_source(query: dict) -> list[list[str]]:
    """Les listes des filtres `{"terms": {"source": [...]}}` posés, dans
    l'ordre. Le premier est toujours le filtre obligatoire des sources
    cherchables ; un second n'apparaît que si l'alerte filtrait sur des
    sources précises."""
    return [
        clause["terms"]["source"]
        for clause in query["bool"]["filter"]
        if "terms" in clause and "source" in clause["terms"]
    ]


# ── Sources demandées par la recherche enregistrée ───────────────────

def test_aucune_source_demandee_ne_pose_aucun_filtre(acces):
    """None, et non [] : « l'alerte ne filtrait sur aucune source » ne
    veut pas dire « filtre qui ne matche rien »."""
    assert search_query._requested_source_names(None, BOB) is None
    assert search_query._requested_source_names([], BOB) is None
    assert search_query._requested_source_names("", BOB) is None


def test_source_ouverte_conservee(acces):
    assert search_query._requested_source_names(["ouverte"], BOB) == ["ouverte"]


def test_source_unique_en_chaine_acceptee(acces):
    """`source` est stocké tel que la recherche l'a envoyé : une chaîne
    est acceptée comme une liste (voir SavedSearchCreate)."""
    assert search_query._requested_source_names("ouverte", BOB) == ["ouverte"]


def test_source_disparue_ecartee_sans_federer_l_alerte(acces):
    """Liste VIDE, jamais None : rendre None ferait retomber l'appelant
    sur la recherche fédérée, et l'alerte se mettrait à notifier pour
    toutes les autres sources au lieu de n'en signaler aucune."""
    assert search_query._requested_source_names(["disparue"], BOB) == []


def test_melange_seules_les_sources_ouvertes_survivent(acces):
    demandees = ["ouverte", "disparue", "autre-ouverte"]
    assert search_query._requested_source_names(demandees, BOB) == ["ouverte", "autre-ouverte"]


def test_source_disparue_et_source_non_cherchable_indistinguables(acces):
    """L'unification des deux listes : le tri se fait sur les sources
    cherchables par l'utilisateur, plus sur la seule présence dans les
    registres. Une source encore enregistrée mais désactivée ou hors de
    ses groupes est donc écartée exactement comme une source supprimée —
    ce qu'obtenait déjà l'intersection avec le filtre obligatoire, en
    deux listes au lieu d'une."""
    supprimee = search_query._requested_source_names(["nexistepas"], BOB)
    interdite = search_query._requested_source_names(["interdite"], BOB)
    assert supprimee == interdite == []


# ── Requête complète (build_query_clauses) ───────────────────────────

def test_alerte_sur_source_disparue_ne_matche_plus_rien(acces):
    """Le cœur du correctif : le filtre est POSÉ, vide. Sans lui, la
    requête ne gardait que le filtre obligatoire, donc remontait les
    documents de toutes les sources cherchables de l'utilisateur."""
    query = search_query.build_query_clauses({"query": "rapport", "source": "disparue"}, BOB)
    assert filtres_de_source(query) == [SOURCES_OUVERTES, []]


def test_alerte_sans_filtre_de_source_reste_federee(acces):
    """Non-régression dans l'autre sens : une alerte qui n'a jamais
    filtré sur une source doit continuer de couvrir tout ce que
    l'utilisateur peut chercher."""
    query = search_query.build_query_clauses({"query": "rapport"}, BOB)
    assert filtres_de_source(query) == [SOURCES_OUVERTES]


def test_alerte_sur_source_ouverte_filtre_toujours_dessus(acces):
    query = search_query.build_query_clauses({"query": "rapport", "source": ["ouverte"]}, BOB)
    assert filtres_de_source(query) == [SOURCES_OUVERTES, ["ouverte"]]


def test_source_disparue_ne_leve_pas_d_exception(acces):
    """Une recherche enregistrée survit à la source qu'elle nomme : le
    worker doit construire une requête, pas planter le tick. Vérifié sur
    une source disparue seule ET mélangée à une source ouverte."""
    for critere in ("nexistepas", ["nexistepas"], ["ouverte", "nexistepas"]):
        query = search_query.build_query_clauses({"query": "rapport", "source": critere}, BOB)
        assert query["bool"]["must"]


def test_filtres_obligatoires_toujours_poses(acces):
    """Non-régression : l'ACL et les sources cherchables restent en tête
    des filtres, quelle que soit la source demandée. C'est ce qui fait
    que ce défaut était de justesse et non de droits."""
    query = search_query.build_query_clauses({"query": "rapport", "source": "disparue"}, BOB)
    acl, sources = query["bool"]["filter"][0], query["bool"]["filter"][1]
    assert acl["bool"]["minimum_should_match"] == 1
    assert sources == {"terms": {"source": SOURCES_OUVERTES}}


def test_autres_criteres_intacts(acces):
    """Non-régression : le reste des critères d'une recherche
    enregistrée continue d'être traduit en filtres."""
    query = search_query.build_query_clauses(
        {"query": "rapport", "source": "ouverte", "ext": ".pdf", "author": "Martin Dupont"},
        BOB,
    )
    filtres = query["bool"]["filter"]
    assert {"terms": {"extension": [".pdf"]}} in filtres
    assert {"terms": {"author": ["Martin Dupont"]}} in filtres
