# auth/base.py — L'interface AuthProvider
#
# Un point d'extension unique pour ajouter un fournisseur d'identité sans
# toucher au routeur, à l'émission des jetons ni au rate limiting. Repris de
# charlie/app-api-auth/app/auth_providers/base.py, à une différence près :
# `ResolvedIdentity` porte ici des groupes en noms courts (et non des DN),
# parce que DocSearch n'a pas de table de correspondance groupe → rôle — les
# noms de groupes sont directement ce que compare le contrôle d'accès et ce
# que porte le champ ACL des documents indexés.

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class AuthenticationError(Exception):
    """Identifiants invalides, quel que soit le fournisseur.

    Message volontairement générique : ne transporte jamais l'information de
    savoir si c'est l'identifiant ou le mot de passe qui est en cause, ni si
    le compte existe. C'est cette exception, et uniquement elle, que le
    routeur traduit en 401 avec le message générique unique."""


class AuthProviderUnavailableError(Exception):
    """Fournisseur injoignable ou mal configuré (annuaire arrêté, keytab
    absent, Redis coupé).

    Traduit en 503, jamais en 401 : présenter une panne comme des
    identifiants invalides envoie chercher un mot de passe là où il faut
    redémarrer un service."""


@dataclass
class LoginCredentials:
    """Forme générique d'une tentative identifiant/mot de passe. Suffisante
    pour l'annuaire et pour les comptes de secours ; Kerberos ne passe
    jamais par ce type — SPNEGO est un dialogue défi/réponse, pas un couple
    d'identifiants (voir kerberos.py)."""

    identifier: str
    secret: str


@dataclass
class ResolvedIdentity:
    """Ce qu'un fournisseur sait d'un utilisateur une fois l'authentification
    réussie.

    `login` est la forme canonique de l'identifiant (minuscules) : c'est le
    `sub` du jeton, la clé des recherches enregistrées, des collections et
    des alertes, et la valeur que voit tout le reste de l'API. Un seul
    espace de noms, quelle que soit la porte d'entrée empruntée."""

    login: str
    display_name: str
    email: str | None = None
    groups: list[str] = field(default_factory=list)


class AuthProvider(ABC):
    """Un fournisseur d'identité."""

    #: Valeur du claim `auth_method` du jeton — informative, journalisée.
    #: Ne JAMAIS s'en servir pour prendre une décision d'autorisation : les
    #: droits se résolvent par les groupes, quelle que soit la porte d'entrée.
    name: str

    @abstractmethod
    def authenticate(self, credentials: LoginCredentials) -> ResolvedIdentity: ...
