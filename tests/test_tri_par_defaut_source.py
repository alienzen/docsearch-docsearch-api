# tests/test_tri_par_defaut_source.py — L'ordre qu'une source demande
# quand l'utilisateur n'a rien choisi (contrat 0.8.0, `tri_defaut`).
#
# Motivation : une source de veille RSS se lit du plus récent au plus
# ancien. La pertinence n'a guère de sens sur une recherche à un mot dans
# des dépêches, et obliger chaque utilisateur à choisir le tri à chaque
# recherche revenait à ne pas le proposer.
#
# Le point délicat n'est pas de lire la déclaration mais de savoir QUAND
# l'appliquer : le tri est une propriété de la REQUÊTE, pas de la source.
# Une recherche fédérée n'a qu'une clause de tri pour tous ses index —
# c'est cette tension que les tests ci-dessous fixent.
#
# Aucun service requis : _tri_effectif() ne fait que lire les registres,
# qu'on remplace ici par des entrées fabriquées. Le tri EST appliqué à
# Elasticsearch par _clause_de_tri(), déjà couvert sur le vrai moteur par
# test_tri_champs_absents.py — inutile de le refaire ici.

import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

import search_api  # noqa: E402
from docsearch_contract import sources as contract_sources  # noqa: E402


class SourceFabriquee:
    """Le minimum que `sources.entry()` lit — il normalise par getattr."""

    def __init__(self, es_index, tri_defaut=None):
        self.es_index = es_index
        self.searchable = True
        if tri_defaut is not None:
            self.tri_defaut = tri_defaut


REGISTRE = {
    # Deux sources de module qui demandent la date, une troisième qui
    # demande autre chose, et une source native qui ne demande rien.
    "presse":    contract_sources.entry("plugin:rss", "presse",
                                        SourceFabriquee("rss_presse", "date_modified")),
    "technique": contract_sources.entry("plugin:rss", "technique",
                                        SourceFabriquee("rss_technique", "date_modified")),
    "annuaire":  contract_sources.entry("plugin:annuaire", "annuaire",
                                        SourceFabriquee("annuaire", "filename")),
    "documents": contract_sources.entry("file", "documents",
                                        SourceFabriquee("documents")),
}


@pytest.fixture(autouse=True)
def registre(monkeypatch):
    """`_tri_effectif` lit le registre par `source_registries.trouver` —
    on le remplace, plutôt que d'exiger un Redis peuplé."""
    monkeypatch.setattr(
        search_api.source_registries, "trouver", lambda nom: REGISTRE.get(nom)
    )


# ── 1. Le choix de l'utilisateur prime toujours ──────────────

def test_un_tri_demande_est_applique_tel_quel():
    assert search_api._tri_effectif("filename", ["presse"]) == "filename"


def test_la_pertinence_demandee_explicitement_n_est_pas_ecrasee():
    """Le cas qui justifie d'avoir remplacé le défaut "_score" du modèle
    par None. Sans cette distinction, un utilisateur qui choisit
    « Pertinence » sur une source triée par date se verrait resservir la
    date, sans aucun moyen d'y échapper — le sélecteur deviendrait
    décoratif."""
    assert search_api._tri_effectif("_score", ["presse"]) == "_score"


# ── 2. Le défaut de la source, et ses limites ────────────────

def test_sans_choix_la_source_impose_le_sien():
    assert search_api._tri_effectif(None, ["presse"]) == "date_modified"


def test_deux_sources_d_accord_gardent_leur_tri():
    assert search_api._tri_effectif(None, ["presse", "technique"]) == "date_modified"


def test_une_recherche_federee_reste_en_pertinence():
    """`None` = aucun filtre de source. Appliquer le défaut d'une source
    trierait par date des documents venus de partout ailleurs, que
    personne n'a demandé de ranger ainsi."""
    assert search_api._tri_effectif(None, None) == "_score"


def test_des_sources_en_desaccord_retombent_en_pertinence():
    """Deux défauts contradictoires ne peuvent pas être satisfaits en même
    temps. Plutôt que d'en privilégier une au hasard — ce que ferait un
    `next(iter(...))` — on retient le seul ordre qu'aucune ne réclame
    contre l'autre."""
    assert search_api._tri_effectif(None, ["presse", "annuaire"]) == "_score"


def test_une_source_native_ne_reordonne_rien():
    """Aucun registre natif ne porte l'attribut : le comportement d'avant
    0.8 doit être strictement conservé pour eux."""
    assert search_api._tri_effectif(None, ["documents"]) == "_score"


def test_une_source_de_module_melee_a_une_native_ne_s_impose_pas():
    assert search_api._tri_effectif(None, ["presse", "documents"]) == "_score"


# ── 3. Robustesse ────────────────────────────────────────────

def test_une_liste_vide_ne_trie_pas_par_defaut():
    """Liste vide = tout ce qui était demandé a été écarté par les droits
    (voir _requested_source_names). La recherche ne rendra rien ; l'ordre
    n'a plus d'objet, et surtout rien ne doit être déduit de sources que
    l'utilisateur n'a pas le droit de voir."""
    assert search_api._tri_effectif(None, []) == "_score"


def test_un_nom_inconnu_est_ignore():
    assert search_api._tri_effectif(None, ["nexiste_pas"]) == "_score"


def test_un_tri_que_l_api_ne_sait_pas_poser_est_ecarte():
    """Lecture tolérante : une source enregistrée par une version
    ultérieure du contrat peut nommer un tri que cette API ne connaît
    pas. Le laisser passer le ferait arriver tel quel dans la clause ES,
    donc en shards en échec — exactement le défaut réparé la veille."""
    REGISTRE["futur"] = contract_sources.entry(
        "plugin:x", "futur", SourceFabriquee("futur", "popularite")
    )
    try:
        assert search_api._tri_effectif(None, ["futur"]) == "_score"
    finally:
        del REGISTRE["futur"]
