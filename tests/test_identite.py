"""D'où vient l'identité — et d'où elle ne vient plus.

Le test qui compte le plus de tout ce dépôt est
`test_en_tete_x_user_ne_vaut_plus_identite` : avant ce chantier,
`curl -H "X-User: alice.admin" http://hôte:8000/admin/status` répondait
200. C'est cette ligne qui vérifie que ce n'est plus vrai.
"""

import pytest
from auth import accounts, config, deps, tokens
from auth.base import ResolvedIdentity


def _cookie_valide(user: str = "alice.admin") -> str:
    identity = ResolvedIdentity(login=user, display_name=user)
    token, _ = tokens.create_access_token(identity, auth_method="ldap")
    return token


# ── L'en-tête n'est plus une identité ────────────────────────

def test_en_tete_x_user_ne_vaut_plus_identite(client):
    """Le défaut historique, en une ligne."""
    reponse = client.get("/admin/status", headers={"X-User": "alice.admin"})
    assert reponse.status_code == 401


def test_sans_rien_du_tout(client):
    assert client.get("/admin/status").status_code == 401


def test_en_tete_x_user_accepte_seulement_si_le_harnais_est_arme(client, monkeypatch):
    """Le proxy de recette (port 8090) doit continuer de fonctionner — mais
    seulement quand l'exploitation l'a explicitement demandé."""
    monkeypatch.setattr(config, "TRUST_X_USER_HEADER", True)
    identite = deps.optional_user(_requete_nue(), "Alice.Admin")
    assert identite == "alice.admin", "l'identifiant doit être canonisé"


def test_dev_user_ne_sert_que_de_dernier_recours(monkeypatch):
    monkeypatch.setattr(config, "DEV_USER", "bob.user")
    assert deps.optional_user(_requete_nue(), None) == "bob.user"


def _requete_nue():
    """Requête minimale, sans cookie ni en-tête — le strict nécessaire pour
    exercer les dépendances hors du routage."""
    from starlette.requests import Request
    return Request({"type": "http", "headers": [], "method": "GET", "path": "/", "query_string": b""})


# ── Le jeton, lui, vaut identité ─────────────────────────────

def test_cookie_valide_ouvre_l_acces(client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_AUTH_DISABLED", True)  # on teste l'identité, pas les groupes
    client.cookies.set(config.ACCESS_COOKIE_NAME, _cookie_valide())
    assert client.get("/is-admin").json()["user"] == "alice.admin"


def test_jeton_bearer_accepte_pour_les_scripts(client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_AUTH_DISABLED", True)
    reponse = client.get("/is-admin", headers={"Authorization": f"Bearer {_cookie_valide('bob.user')}"})
    assert reponse.json()["user"] == "bob.user"


def test_jeton_illisible_vaut_absence_d_identite(client):
    client.cookies.set(config.ACCESS_COOKIE_NAME, "pas-un-jeton")
    assert client.get("/is-admin").json()["user"] is None
    assert client.get("/admin/status").status_code == 401


def test_un_refresh_ne_vaut_pas_un_acces(client):
    """Sans le contrôle de `token_type`, un jeton de 7 jours serait accepté
    là où un jeton de 15 minutes est attendu."""
    refresh, _, _ = tokens.create_refresh_token("alice.admin")
    client.cookies.set(config.ACCESS_COOKIE_NAME, refresh)
    assert client.get("/admin/status").status_code == 401


# ── La forme canonique de l'identifiant ──────────────────────

@pytest.mark.parametrize("saisi,attendu", [
    ("Alice.Admin", "alice.admin"),
    ("  alice.admin  ", "alice.admin"),
    ("ALICE.ADMIN", "alice.admin"),
    ("", ""),
])
def test_identifiant_canonique(saisi, attendu):
    """Un humain = un identifiant. Le KDC rend la forme canonique du compte
    quand le formulaire rend la forme saisie : sans cette normalisation, la
    même personne aurait deux jeux de recherches enregistrées selon la
    porte d'entrée empruntée."""
    assert accounts.normalize_login(saisi) == attendu


def test_le_sub_du_jeton_est_l_identifiant_canonique(env_auth):
    identity = ResolvedIdentity(login=accounts.normalize_login("Alice.Admin"), display_name="Alice")
    token, _ = tokens.create_access_token(identity, auth_method="ldap")
    claims = tokens.decode_token(token, expected_type=tokens.ACCESS_TOKEN_TYPE)
    assert claims["sub"] == "alice.admin"
