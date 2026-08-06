"""Kerberos : ce qui se teste sans KDC — c'est-à-dire l'essentiel.

La frontière est plus favorable qu'il n'y paraît. Le mapping
principal→identifiant est une fonction pure, et c'est elle qui décide qui
entre. Le contrôle du keytab précède l'import de `gssapi`, donc se vérifie
seul. Ne reste hors de portée que l'acceptation d'un ticket authentique,
marquée `requires_kerberos` — il n'y a aucun KDC sur cette VM.
"""

import pytest
from auth import config, kerberos
from auth.base import AuthenticationError, AuthProviderUnavailableError

REALM = "DOCSEARCH.TEST"


def _gssapi_installee() -> bool:
    try:
        import gssapi  # noqa: F401
        return True
    except ImportError:
        return False


# ── Le mapping principal → identifiant ───────────────────────

def test_principal_nominatif():
    assert kerberos.identifier_from_principal("alice.admin@DOCSEARCH.TEST", realm=REALM) == "alice.admin"


def test_identifiant_canonise():
    """Le KDC rend la forme canonique du compte ; l'invariant « un humain =
    un identifiant » exige qu'elle rejoigne celle du formulaire."""
    assert kerberos.identifier_from_principal("Alice.Admin@DOCSEARCH.TEST", realm=REALM) == "alice.admin"


def test_realm_etranger_refuse():
    """LE contrôle du module : sans lui, une relation d'approbation entre
    domaines laisserait entrer alice@AUTRE-REALM sous l'identité d'alice."""
    with pytest.raises(AuthenticationError):
        kerberos.identifier_from_principal("alice.admin@AUTRE.REALM", realm=REALM)


def test_realm_sensible_a_la_casse():
    """Les noms de realm le sont (RFC 4120 §6.1). Être laxiste ici
    reviendrait à accepter un realm qu'on n'a pas nommé."""
    with pytest.raises(AuthenticationError):
        kerberos.identifier_from_principal("alice.admin@docsearch.test", realm=REALM)


@pytest.mark.parametrize("principal", [
    "HTTP/docsearch.domaine.fr@DOCSEARCH.TEST",  # le compte de service de l'application elle-même
    "alice/admin@DOCSEARCH.TEST",                # instance administrative
])
def test_principal_multi_composants_refuse(principal):
    with pytest.raises(AuthenticationError):
        kerberos.identifier_from_principal(principal, realm=REALM)


@pytest.mark.parametrize("principal", ["alice.admin", "alice@a@b", "@DOCSEARCH.TEST"])
def test_principal_malforme_refuse(principal):
    with pytest.raises(AuthenticationError):
        kerberos.identifier_from_principal(principal, realm=REALM)


def test_realm_non_configure_est_une_panne_pas_un_refus():
    """503 et non 401 : présenter une erreur d'exploitation comme des
    identifiants incorrects fait chercher au mauvais endroit."""
    with pytest.raises(AuthProviderUnavailableError):
        kerberos.identifier_from_principal("alice.admin@DOCSEARCH.TEST", realm="")


# ── L'acceptation du ticket ──────────────────────────────────

def test_keytab_absent_est_une_indisponibilite(monkeypatch):
    """Et ce contrôle précède l'import de `gssapi` : c'est la panne la plus
    courante (keytab pas encore déployé), elle mérite le message le plus
    précis — pas « gssapi n'est pas installée », qui serait le seul visible
    partout où la bibliothèque manque, l'hôte de développement compris."""
    monkeypatch.setattr(config, "KERBEROS_KEYTAB", "")
    with pytest.raises(AuthProviderUnavailableError, match="KERBEROS_KEYTAB"):
        kerberos.accept_token(b"nimporte-quoi")


@pytest.mark.skipif(
    not _gssapi_installee(),
    reason="gssapi absente — présente dans l'image, pas sur l'hôte de développement",
)
def test_keytab_vide_sollicite_reellement_gssapi(monkeypatch, tmp_path):
    """La part de l'acceptation qui s'éprouve SANS KDC.

    Un keytab vide fait vraiment entrer dans `gssapi`, qui rend une vraie
    GSSError : cela prouve que la bibliothèque est câblée et que son refus
    devient bien une indisponibilité (503) et non un refus
    d'identifiants (401). Aucun ticket n'entre en jeu.

    Se saute proprement sur l'hôte de développement, où `gssapi` ne
    s'installe pas (elle se compile contre libkrb5-dev, présent dans
    l'image seulement) ; s'exécute dans le conteneur."""
    keytab = tmp_path / "vide.keytab"
    keytab.write_bytes(b"")
    monkeypatch.setattr(config, "KERBEROS_KEYTAB", str(keytab))
    monkeypatch.setattr(config, "KERBEROS_SPN", "")

    with pytest.raises(AuthProviderUnavailableError):
        kerberos.accept_token(b"\x60\x00")


def test_ntlm_absent_des_mecanismes_acceptes():
    """SPNEGO sait négocier NTLM, nettement plus faible. L'OID de NTLM
    (1.3.6.1.4.1.311.2.2.10) ne doit jamais figurer dans la liste."""
    assert "1.3.6.1.4.1.311.2.2.10" not in kerberos._KERBEROS_OIDS
    assert kerberos._SPNEGO_OID in kerberos._KERBEROS_OIDS


@pytest.mark.requires_kerberos
def test_ticket_authentique_accepte():
    """Le seul chemin que rien ne peut exercer ici. À reprendre au premier
    essai contre un vrai KDC."""
    raise AssertionError("à écrire quand un KDC existe")


# ── Le harnais de développement ──────────────────────────────

def test_harnais_inactif_en_production(monkeypatch):
    monkeypatch.setattr(config, "KERBEROS_DEV_PRINCIPAL", "alice.admin@DOCSEARCH.TEST")
    monkeypatch.setattr(config, "IS_PRODUCTION", True)
    assert kerberos.dev_harness_principal() is None


def test_harnais_actif_hors_production(monkeypatch):
    monkeypatch.setattr(config, "KERBEROS_DEV_PRINCIPAL", "alice.admin@DOCSEARCH.TEST")
    monkeypatch.setattr(config, "IS_PRODUCTION", False)
    assert kerberos.dev_harness_principal() == "alice.admin@DOCSEARCH.TEST"


# ── La route ─────────────────────────────────────────────────

def test_sso_desactive_repond_501(client, monkeypatch):
    monkeypatch.setattr("runtime_config.get_param", lambda *a, **k: "false")
    assert client.get("/auth/login/kerberos").status_code == 501


def test_sans_en_tete_le_serveur_defie(client, monkeypatch):
    monkeypatch.setattr("runtime_config.get_param", lambda *a, **k: "true")
    reponse = client.get("/auth/login/kerberos")
    assert reponse.status_code == 401
    assert reponse.headers["WWW-Authenticate"] == "Negotiate"


def test_jeton_illisible_refuse_sans_redefier(client, monkeypatch):
    """401 SANS WWW-Authenticate : redéfier ferait reboucler le navigateur
    sur un ticket qu'on vient de refuser."""
    monkeypatch.setattr("runtime_config.get_param", lambda *a, **k: "true")
    reponse = client.get(
        "/auth/login/kerberos", headers={"Authorization": "Negotiate pas-du-base64!!"},
    )
    assert reponse.status_code == 401
    assert "WWW-Authenticate" not in reponse.headers
