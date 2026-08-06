"""Jetons RS256 : signature, kid, types, JWKS."""

import time

import jwt as pyjwt
import pytest
from auth import config, tokens
from auth.base import ResolvedIdentity

IDENTITE = ResolvedIdentity(
    login="alice.admin", display_name="Alice Admin", email="alice@docsearch.test",
)


def test_access_token_porte_les_claims_attendus(env_auth):
    token, expires_at = tokens.create_access_token(IDENTITE, auth_method="ldap")
    claims = tokens.decode_token(token, expected_type=tokens.ACCESS_TOKEN_TYPE)

    assert claims["sub"] == "alice.admin"
    assert claims["iss"] == config.JWT_ISSUER
    assert claims["aud"] == config.JWT_AUDIENCE
    assert claims["auth_method"] == "ldap"
    assert claims["name"] == "Alice Admin"
    assert claims["jti"]
    assert claims["exp"] > time.time()


def test_aucun_claim_groups(env_auth):
    """Les groupes se relisent à chaque autorisation. Les figer dans un
    jeton en ferait une seconde source de vérité, périmée dès qu'un compte
    change de groupe — et c'est toujours la périmée qui finit par décider."""
    token, _ = tokens.create_access_token(IDENTITE, auth_method="ldap")
    claims = tokens.decode_token(token, expected_type=tokens.ACCESS_TOKEN_TYPE)
    assert "groups" not in claims


def test_le_kid_est_dans_l_en_tete(env_auth, rsa_keys):
    """Sans `kid`, aucune rotation de clé n'est possible : rien ne dirait
    avec laquelle un jeton a été signé."""
    token, _ = tokens.create_access_token(IDENTITE, auth_method="ldap")
    assert pyjwt.get_unverified_header(token)["kid"] == rsa_keys["kid"]


def test_refresh_token_minimal(env_auth):
    token, jti, _ = tokens.create_refresh_token("alice.admin")
    claims = tokens.decode_token(token, expected_type=tokens.REFRESH_TOKEN_TYPE)
    assert claims["jti"] == jti
    assert "name" not in claims and "email" not in claims


def test_un_type_ne_passe_pas_pour_l_autre(env_auth):
    access, _ = tokens.create_access_token(IDENTITE, auth_method="ldap")
    refresh, _, _ = tokens.create_refresh_token("alice.admin")

    with pytest.raises(pyjwt.InvalidTokenError):
        tokens.decode_token(access, expected_type=tokens.REFRESH_TOKEN_TYPE)
    with pytest.raises(pyjwt.InvalidTokenError):
        tokens.decode_token(refresh, expected_type=tokens.ACCESS_TOKEN_TYPE)


def test_signature_etrangere_refusee(env_auth, tmp_path):
    """Un jeton signé par une AUTRE clé — le cas d'une installation qui en
    accepterait une qu'elle n'a pas émise."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    autre = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = autre.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    faux = pyjwt.encode(
        {"iss": config.JWT_ISSUER, "aud": config.JWT_AUDIENCE, "sub": "alice.admin",
         "exp": int(time.time()) + 60, "token_type": "access"},
        pem, algorithm="RS256", headers={"kid": config.JWT_ACTIVE_KID},
    )
    with pytest.raises(pyjwt.InvalidTokenError):
        tokens.decode_token(faux, expected_type=tokens.ACCESS_TOKEN_TYPE)


def test_jeton_expire_refuse(env_auth, monkeypatch):
    monkeypatch.setattr(config, "JWT_ACCESS_TOKEN_TTL_MINUTES", -1)
    token, _ = tokens.create_access_token(IDENTITE, auth_method="ldap")
    with pytest.raises(pyjwt.ExpiredSignatureError):
        tokens.decode_token(token, expected_type=tokens.ACCESS_TOKEN_TYPE)


def test_jwks_publie_la_cle_publique(env_auth, client):
    document = client.get("/auth/.well-known/jwks.json").json()
    assert len(document["keys"]) == 1
    cle = document["keys"][0]
    assert cle["kty"] == "RSA" and cle["alg"] == "RS256" and cle["use"] == "sig"
    assert cle["kid"] == config.JWT_ACTIVE_KID
    # Ni la clé privée, ni aucun de ses paramètres secrets.
    assert set(cle) == {"kty", "use", "alg", "kid", "n", "e"}


def test_sans_cles_l_authentification_repond_503(client, monkeypatch):
    """Clés absentes = panne de configuration. Un 401 enverrait chercher un
    mot de passe là où il faut monter un volume."""
    monkeypatch.setattr(config, "JWT_PRIVATE_KEY_PATH", "")
    monkeypatch.setattr(config, "JWT_PUBLIC_KEY_PATH", "")
    tokens.reset_keys()
    assert client.get("/auth/.well-known/jwks.json").status_code == 503
