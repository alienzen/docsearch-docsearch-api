"""Annuaire : échappement du filtre, groupes effectifs, régimes d'erreur.

Les tests marqués `requires_ldap` tapent le VRAI annuaire de dev de la VM
(~/ldap-test-stack, base dc=docsearch,dc=test) et se sautent proprement
s'il est arrêté. Un annuaire bouchonné ne prouverait pas grand-chose : ce
qu'on veut vérifier ici, c'est justement que le filtre, les attributs et
les memberOf se comportent comme attendu contre un vrai serveur.
"""

import pytest
from auth import accounts, config, directory

from tests.conftest import ALICE, BOB, LDAP_PASSWORD

# ── Échappement du filtre — sans annuaire ────────────────────

def test_filtre_echappe_les_caracteres_speciaux(env_auth):
    """Le défaut de ldap_resolver.py : un identifiant contrôlé par
    l'appelant sortait du filtre prévu et choisissait l'entrée qu'il
    voulait."""
    filtre = directory.build_user_filter("alice*)(uid=*")
    assert "*)(uid=*" not in filtre
    assert filtre.count("(") == filtre.count(")")


@pytest.mark.parametrize("hostile", ["*", "a)(objectClass=*", "a\\", "a\x00b"])
def test_filtre_reste_bien_forme(env_auth, hostile):
    filtre = directory.build_user_filter(hostile)
    assert filtre.startswith("(|(uid=") and filtre.endswith("))")
    assert filtre.count("(") == filtre.count(")")


def test_cn_extrait_du_dn():
    assert directory.cn_from_dn("cn=docsearch-admins,ou=groups,dc=docsearch,dc=test") == "docsearch-admins"
    assert directory.cn_from_dn("CN=DocSearch-Admins,OU=Groups") == "docsearch-admins"
    # Tolère un annuaire qui rendrait déjà des noms courts.
    assert directory.cn_from_dn("docsearch-users") == "docsearch-users"


# ── Régimes d'erreur ─────────────────────────────────────────

def test_annuaire_desactive_ne_leve_pas(env_auth, monkeypatch):
    """LDAP_ENABLED=false n'est pas une panne : c'est une installation qui
    filtre uniquement sur les ACL POSIX."""
    monkeypatch.setattr(config, "LDAP_ENABLED", False)
    assert directory.get_user_groups("alice.admin", strict=True) == []


def test_annuaire_injoignable_degrade_ou_leve(env_auth, monkeypatch):
    """Les deux régimes, et la raison de leur coexistence : sur le chemin
    de la RECHERCHE une liste vide est plus restrictive, donc acceptable ;
    sur celui de l'AUTORISATION elle se traduirait par « accès réservé au
    groupe … », c'est-à-dire une panne déguisée en refus de droits."""
    monkeypatch.setattr(config, "LDAP_ENABLED", True)
    monkeypatch.setattr(config, "LDAP_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "LDAP_PORT", 1)  # personne n'écoute
    monkeypatch.setattr(config, "LDAP_USE_SSL", False)
    monkeypatch.setattr(config, "LDAP_ALLOW_PLAINTEXT_INSECURE", True)
    directory.invalidate_cache()

    assert directory.get_user_groups("alice.admin") == []
    with pytest.raises(directory.DirectoryUnavailableError):
        directory.get_user_groups("alice.admin", strict=True)


def test_clair_refuse_sans_derogation(env_auth, monkeypatch):
    monkeypatch.setattr(config, "LDAP_ENABLED", True)
    monkeypatch.setattr(config, "LDAP_USE_SSL", False)
    monkeypatch.setattr(config, "LDAP_ALLOW_PLAINTEXT_INSECURE", False)
    with pytest.raises(directory.DirectoryUnavailableError, match="LDAPS"):
        directory.lookup_user("alice.admin")


# ── Groupes effectifs ────────────────────────────────────────

@pytest.mark.requires_redis
def test_groupes_effectifs_unissent_annuaire_et_compte_local(env_auth, monkeypatch, compte_secours):
    """L'union, et non un choix : c'est ce qui fait qu'un compte de secours
    reste opérant quand l'annuaire est en panne (il rend alors []) sans
    cesser de l'être quand il fonctionne."""
    monkeypatch.setattr(config, "LDAP_ENABLED", False)
    compte_secours("secours.admin", "motdepasse-de-test-1234", ["docsearch-users", "docsearch-admins"])
    groupes = directory.get_effective_groups("secours.admin")
    assert set(groupes) == {"docsearch-users", "docsearch-admins"}


# ── Contre l'annuaire réel ───────────────────────────────────

@pytest.mark.requires_ldap
def test_recherche_utilisateur_reel(env_ldap):
    utilisateur = directory.lookup_user(ALICE)
    assert utilisateur.uid == ALICE
    assert "docsearch-admins" in utilisateur.groups
    assert "docsearch-users" in utilisateur.groups


@pytest.mark.requires_ldap
def test_groupes_d_un_utilisateur_non_admin(env_ldap):
    assert directory.get_user_groups(BOB) == ["docsearch-users"]


@pytest.mark.requires_ldap
def test_authentification_reelle(env_ldap):
    utilisateur = directory.authenticate(ALICE, LDAP_PASSWORD)
    assert utilisateur.uid == ALICE


@pytest.mark.requires_ldap
def test_mot_de_passe_faux_refuse(env_ldap):
    with pytest.raises(directory.DirectoryAuthError):
        directory.authenticate(ALICE, "mauvais-mot-de-passe")


@pytest.mark.requires_ldap
def test_mot_de_passe_vide_refuse(env_ldap):
    """Un bind LDAP à mot de passe vide est un « unauthenticated bind » :
    il peut réussir sans rien vérifier."""
    with pytest.raises(directory.DirectoryAuthError):
        directory.authenticate(ALICE, "")


@pytest.mark.requires_ldap
def test_identifiant_hostile_ne_trouve_personne(env_ldap):
    """L'injection de filtre, contre le vrai annuaire : sans échappement,
    `*` ramènerait la première entrée venue."""
    with pytest.raises(directory.DirectoryAuthError):
        directory.lookup_user("*")
    with pytest.raises(directory.DirectoryAuthError):
        directory.lookup_user("alice*)(uid=*")


@pytest.mark.requires_ldap
def test_casse_de_l_identifiant_sans_effet(env_ldap):
    """`Alice.Admin` et `alice.admin` désignent la même personne."""
    assert directory.get_effective_groups(accounts.normalize_login("Alice.Admin")) == \
           directory.get_effective_groups(ALICE)
