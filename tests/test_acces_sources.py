# tests/test_acces_sources.py — Une source désactivée ou interdite l'est
# par TOUS les chemins, pas seulement en recherche.
#
# Ce que ces tests verrouillent, mesuré en dev avant correction sur un
# compte non-administrateur :
#
#   - `GET /document/{id}` rendait le contenu intégral d'un document dont
#     la source était désactivée (searchable=false), et `/api/preview/{id}`
#     servait le fichier lui-même — alors que la même source rendait zéro
#     résultat en recherche. Un identifiant suffisait : lien copié avant la
#     désactivation, document laissé dans une collection ;
#   - `POST /search` distinguait deux refus par leur réponse : 400 « Source
#     inconnue » pour un nom absent des registres, 200 avec zéro résultat
#     pour une source existante mais interdite. La différence énumérait les
#     sources qu'on cache à l'utilisateur, depuis un simple lien profond
#     bricolé à la main.
#
# ── Sur ce qui est injecté ici ───────────────────────────────────────
#
# `_searchable_source_names()` et `get_effective_groups()` sont remplacés,
# et RIEN d'autre : ce sont les deux ENTRÉES des fonctions testées, pas
# leur logique. Ce qui est éprouvé — la décision d'accès elle-même — est
# le vrai code. Les fournisseurs derrière ces deux entrées ont déjà leurs
# tests (annuaire dans test_annuaire.py), et les faire intervenir ici
# obligerait à écrire dans le Redis de configuration de l'installation de
# dev, que ces tests ne doivent jamais salir (voir conftest.py).

import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

import search_api  # noqa: E402

# Sources telles que les verrait un utilisateur donné : "ouverte" est
# cherchable, les autres ne le sont pas — parce qu'elles sont désactivées,
# réservées à un groupe dont il n'est pas membre, ou retirées du registre.
# Les trois cas produisent exactement la même absence de cette liste, et
# c'est le point : le code d'accès n'a pas à savoir laquelle des trois.
SOURCES_OUVERTES = ["ouverte"]

BOB = "bob.user"


@pytest.fixture
def acces(monkeypatch):
    """Fixe ce que bob.user peut chercher, et ses groupes annuaire."""
    monkeypatch.setattr(search_api, "_searchable_source_names", lambda username: list(SOURCES_OUVERTES))
    monkeypatch.setattr(search_api, "get_effective_groups", lambda username: ["docsearch-users"])


def doc(source, **acl):
    """Document indexé minimal — public par défaut, pour que le refus
    attendu vienne bien de la source et non de l'ACL."""
    return {"source": source, "filename": "public.txt", "acl": {"public": True, **acl}}


# ── Accès direct par identifiant (_check_doc_access) ─────────────────

def test_document_d_une_source_ouverte_reste_accessible(acces):
    assert search_api._check_doc_access(doc("ouverte"), BOB) is True


def test_document_public_d_une_source_non_cherchable_est_refuse(acces):
    """Le cas mesuré : document PUBLIC, donc accepté par l'ACL, mais dans
    une source que la recherche ne rend pas. L'accès direct doit refuser
    aussi, sans quoi désactiver une source ne cache rien à qui détient un
    identifiant."""
    assert search_api._check_doc_access(doc("desactivee"), BOB) is False


def test_document_sans_champ_source_est_refuse(acces):
    """Le filtre {"terms": {"source": [...]}} de /search ne remonte jamais
    un document sans ce champ : l'accès direct s'aligne, plutôt que de
    traiter l'absence de source comme une absence de restriction."""
    assert search_api._check_doc_access({"acl": {"public": True}}, BOB) is False


def test_acl_privee_refusee_meme_sur_une_source_ouverte(acces):
    """Non-régression : la source cherchable n'autorise rien par
    elle-même, l'ACL du document décide toujours."""
    prive = doc("ouverte", public=False, owner="alice.admin", users=[], groups=[])
    assert search_api._check_doc_access(prive, BOB) is False


def test_acl_de_groupe_toujours_honoree(acces):
    """Non-régression dans l'autre sens : un document partagé avec un
    groupe de l'utilisateur reste accessible."""
    partage = doc("ouverte", public=False, owner="alice.admin", groups=["docsearch-users"])
    assert search_api._check_doc_access(partage, BOB) is True


def test_acl_sql_a_plat_toujours_lue(acces):
    """Non-régression : une source SQL projette "acl.public" en clé PLATE
    (voir _doc_acl). Le contrôle de source ne doit pas court-circuiter
    cette normalisation."""
    sql = {"source": "ouverte", "acl.public": True}
    assert search_api._check_doc_access(sql, BOB) is True


# ── Sources demandées par la requête (_requested_source_names) ───────

def test_aucune_source_demandee_ne_pose_aucun_filtre(acces):
    """None, et non [] : « pas de filtre de source » (recherche fédérée)
    ne veut pas dire « filtre qui ne matche rien »."""
    assert search_api._requested_source_names(None, BOB) is None
    assert search_api._requested_source_names([], BOB) is None
    assert search_api._requested_source_names("", BOB) is None


def test_source_ouverte_conservee(acces):
    assert search_api._requested_source_names(["ouverte"], BOB) == ["ouverte"]


def test_source_unique_en_chaine_acceptee(acces):
    """`source` accepte une chaîne comme une liste — la syntaxe avancée
    `source:ouverte` de la barre de recherche passe par là."""
    assert search_api._requested_source_names("ouverte", BOB) == ["ouverte"]


def test_source_interdite_ecartee_sans_elargir_la_recherche(acces):
    """Liste VIDE, jamais None : rendre None ferait retomber l'appelant
    sur la recherche fédérée, et un permalien nommant une source interdite
    élargirait les résultats à toutes les autres au lieu de n'en rendre
    aucun."""
    assert search_api._requested_source_names(["interdite"], BOB) == []


def test_melange_seules_les_sources_ouvertes_survivent(acces):
    assert search_api._requested_source_names(["ouverte", "interdite"], BOB) == ["ouverte"]


def test_source_inexistante_et_source_interdite_indistinguables(acces):
    """Le cœur du correctif : aucune réponse, aucune exception ne doit
    permettre de dire si le nom demandé existe. Un 400 « Source inconnue »
    d'un côté et zéro résultat de l'autre suffisait à énumérer les sources
    cachées depuis un lien profond."""
    interdite = search_api._requested_source_names(["interdite"], BOB)
    inexistante = search_api._requested_source_names(["nexistepas"], BOB)
    assert interdite == inexistante == []
