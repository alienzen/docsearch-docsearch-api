# auth/tokens.py — Émission et vérification des JWT RS256
#
# RS256 avec `kid` dès le départ, et un JWKS publié
# (GET /auth/.well-known/jwks.json) : DocSearch n'a aujourd'hui qu'un seul
# service qui valide ces jetons, mais le format asymétrique coûte la même
# chose à écrire qu'un HS256 et évite d'avoir à partager un secret le jour
# où un second consommateur apparaît.
#
# Deux types de jetons, et la différence de contenu est délibérée :
#
#   access  — 15 min, porte l'identité affichable (nom, mail). Aucun claim
#             `groups` : les groupes se résolvent par l'annuaire à chaque
#             fois (voir directory.get_effective_groups). Les figer dans un
#             jeton en ferait une seconde source de vérité, périmée dès
#             qu'un compte change de groupe — et c'est toujours la source
#             périmée qui finit par décider.
#   refresh — 7 jours, volontairement minimal, et valide SEULEMENT s'il
#             existe aussi en Redis (voir sessions.py). Un jeton
#             correctement signé mais absent du magasin — déconnexion,
#             révocation — est refusé.

import base64
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import jwt as pyjwt
from cryptography.hazmat.primitives import serialization

from auth import config
from auth.base import ResolvedIdentity

# noqa S105 sur les deux lignes : ce sont les valeurs de la revendication
# `typ` des jetons, pas des secrets. La règle se déclenche sur le nom de
# la constante (« TOKEN »), pas sur son contenu. Elle reste active partout
# ailleurs — c'est elle qui attraperait un vrai mot de passe en dur.
ACCESS_TOKEN_TYPE = "access"  # noqa: S105
REFRESH_TOKEN_TYPE = "refresh"  # noqa: S105


class KeyLoadError(Exception):
    """Clé RS256 manquante ou illisible — erreur de configuration fatale.

    Traduite en 503 : sans clé, aucune session ne peut s'ouvrir, mais ce
    n'est jamais la faute des identifiants présentés."""


@lru_cache(maxsize=1)
def _private_key():
    if not config.JWT_PRIVATE_KEY_PATH:
        raise KeyLoadError(
            "JWT_PRIVATE_KEY_PATH non configuré — lancer "
            "scripts/generer-cles.py, voir README."
        )
    try:
        with open(config.JWT_PRIVATE_KEY_PATH, "rb") as fh:
            return serialization.load_pem_private_key(fh.read(), password=None)
    except OSError as exc:
        raise KeyLoadError(
            f"Clé privée JWT illisible ({config.JWT_PRIVATE_KEY_PATH}) : {exc}. "
            "Vérifier le montage du volume et les permissions (l'UID est "
            "celui MAPPÉ dans le conteneur, pas celui de l'hôte)."
        ) from exc


@lru_cache(maxsize=1)
def _public_key():
    if not config.JWT_PUBLIC_KEY_PATH:
        raise KeyLoadError("JWT_PUBLIC_KEY_PATH non configuré (voir README).")
    try:
        with open(config.JWT_PUBLIC_KEY_PATH, "rb") as fh:
            return serialization.load_pem_public_key(fh.read())
    except OSError as exc:
        raise KeyLoadError(
            f"Clé publique JWT illisible ({config.JWT_PUBLIC_KEY_PATH}) : {exc}."
        ) from exc


def _kid() -> str:
    if not config.JWT_ACTIVE_KID:
        raise KeyLoadError("JWT_ACTIVE_KID non configuré (voir README).")
    return config.JWT_ACTIVE_KID


def reset_keys() -> None:
    """Oublie les clés mémorisées (tests, rotation manuelle)."""
    _private_key.cache_clear()
    _public_key.cache_clear()


def keys_available() -> bool:
    """Sans lever — sert au démarrage et à /health pour dire clairement
    « l'authentification n'est pas configurée » plutôt que de laisser
    découvrir le problème à la première connexion."""
    try:
        _private_key()
        _public_key()
        _kid()
        return True
    except KeyLoadError:
        return False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _base_claims(sub: str, token_type: str, expires_at: datetime) -> dict:
    now = _now()
    return {
        "iss": config.JWT_ISSUER,
        "aud": config.JWT_AUDIENCE,
        "sub": sub,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": str(uuid.uuid4()),
        "token_type": token_type,
    }


def create_access_token(identity: ResolvedIdentity, *, auth_method: str) -> tuple[str, datetime]:
    expires_at = _now() + timedelta(minutes=config.JWT_ACCESS_TOKEN_TTL_MINUTES)
    payload = _base_claims(identity.login, ACCESS_TOKEN_TYPE, expires_at)
    payload.update({
        # Informatif : par quelle porte la personne est entrée. Journalisé,
        # affiché en diagnostic — jamais consulté pour accorder un droit.
        "auth_method": auth_method,
        "name": identity.display_name,
        "email": identity.email,
    })
    token = pyjwt.encode(payload, _private_key(), algorithm="RS256", headers={"kid": _kid()})
    return token, expires_at


def create_refresh_token(login: str) -> tuple[str, str, datetime]:
    expires_at = _now() + timedelta(days=config.JWT_REFRESH_TOKEN_TTL_DAYS)
    payload = _base_claims(login, REFRESH_TOKEN_TYPE, expires_at)
    token = pyjwt.encode(payload, _private_key(), algorithm="RS256", headers={"kid": _kid()})
    return token, payload["jti"], expires_at


def decode_token(token: str, *, expected_type: str) -> dict:
    """Vérifie signature RS256, exp/nbf, iss, aud, puis `token_type`.

    Lève une sous-classe de jwt.InvalidTokenError — à l'appelant HTTP d'en
    faire un 401. Le contrôle de `token_type` n'est pas cosmétique : sans
    lui, un refresh token (7 jours) serait accepté là où un access token
    (15 min) est attendu."""
    payload = pyjwt.decode(
        token,
        _public_key(),
        algorithms=["RS256"],
        audience=config.JWT_AUDIENCE,
        issuer=config.JWT_ISSUER,
    )
    if payload.get("token_type") != expected_type:
        raise pyjwt.InvalidTokenError(
            f"Type de jeton inattendu (attendu={expected_type!r}, "
            f"reçu={payload.get('token_type')!r})."
        )
    return payload


def _int_to_base64url(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def build_jwks() -> dict:
    """Document JWKS (RFC 7517).

    Une seule clé publiée, mais la structure en liste est déjà prête pour
    une rotation : l'ancienne et la nouvelle clé cohabiteront le temps que
    les jetons signés avec l'ancienne expirent, chacune reconnue par son
    `kid`. La rotation elle-même n'est pas implémentée — seul le format
    l'est."""
    numbers = _public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": _kid(),
                "n": _int_to_base64url(numbers.n),
                "e": _int_to_base64url(numbers.e),
            }
        ]
    }
