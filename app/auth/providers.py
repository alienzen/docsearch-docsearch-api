# auth/providers.py — Les fournisseurs identifiant/mot de passe
#
# Deux fournisseurs, une règle : **le client ne choisit pas lequel est
# sollicité**. POST /auth/login reçoit un identifiant et un mot de passe
# sans dire par quelle voie s'authentifier, et c'est le serveur qui tranche
# — l'existence d'un compte de secours local portant cet identifiant est le
# discriminant (voir router.py::pick_provider).
#
# **Un seul fournisseur par tentative, aucun repli de l'un vers l'autre.**
# Le montage inverse aurait trois défauts, tous constatés ailleurs :
#   1. il masquerait les pannes — un annuaire injoignable produit un échec,
#      donc un second essai en local, donc « identifiants incorrects »
#      affiché à quelqu'un dont les identifiants sont parfaitement valides ;
#   2. il fuiterait la nature du compte, un 401 puis un 200 sur deux routes
#      indiquant à qui observe le réseau comment le compte a été créé ;
#   3. il dédoublerait le compteur de rate limiting et la ligne d'audit.
#
# Kerberos n'est pas ici : son dialogue n'a pas la forme d'un couple
# identifiant/mot de passe (voir kerberos.py).

import logging

from auth import accounts, directory
from auth.base import (
    AuthenticationError,
    AuthProvider,
    AuthProviderUnavailableError,
    LoginCredentials,
    ResolvedIdentity,
)

logger = logging.getLogger(__name__)


class LdapAuthProvider(AuthProvider):
    """Bind technique pour trouver le DN, puis bind avec les identifiants
    présentés. Le mot de passe n'est jamais stocké ni journalisé."""

    name = "ldap"

    def authenticate(self, credentials: LoginCredentials) -> ResolvedIdentity:
        login = accounts.normalize_login(credentials.identifier)
        try:
            user = directory.authenticate(login, credentials.secret)
        except directory.DirectoryAuthError as exc:
            raise AuthenticationError(str(exc)) from exc
        except directory.DirectoryUnavailableError as exc:
            raise AuthProviderUnavailableError(str(exc)) from exc

        return ResolvedIdentity(
            login=login,
            display_name=user.cn or login,
            email=user.mail,
            groups=user.groups,
        )


class LocalAuthProvider(AuthProvider):
    """Comptes de secours (voir accounts.py). Vérification seule : ce
    fournisseur ne crée jamais de compte."""

    name = "local"

    def authenticate(self, credentials: LoginCredentials) -> ResolvedIdentity:
        login = accounts.normalize_login(credentials.identifier)
        account = accounts.verify_password(login, credentials.secret)
        if account is None:
            raise AuthenticationError("Identifiant ou mot de passe incorrect.")

        logger.warning(
            "[auth] Connexion par COMPTE DE SECOURS local : %s. Ces comptes "
            "existent pour la panne d'annuaire — une connexion locale en "
            "période normale mérite un coup d'œil.",
            login,
        )
        return ResolvedIdentity(
            login=login,
            display_name=account.get("display_name") or login,
            email=account.get("email") or None,
            groups=list(account.get("groups") or []),
        )


LDAP_PROVIDER = LdapAuthProvider()
LOCAL_PROVIDER = LocalAuthProvider()


def pick_provider(identifier: str) -> AuthProvider:
    """Choisit le fournisseur pour cette tentative.

    L'existence d'un compte de secours local est le discriminant. Il n'en
    existe que si l'exploitation en a créé un explicitement : un compte
    local n'est donc jamais tenté contre l'annuaire, ni l'inverse."""
    if accounts.has_account(identifier):
        return LOCAL_PROVIDER
    return LDAP_PROVIDER
