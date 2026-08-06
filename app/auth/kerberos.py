# auth/kerberos.py — Connexion automatique par ticket (SPNEGO/Negotiate)
#
# Transposé de charlie/app-api-auth/app/auth_providers/kerberos.py, dont les
# arbitrages ont été trouvés par l'échec plutôt que par raisonnement : les
# relire avant d'en défaire un. Ce qui change ici tient en une ligne — un
# ticket résout vers un identifiant annuaire, pas vers une ligne `users`,
# DocSearch n'ayant pas de table d'utilisateurs.
#
# Trois opérations, dont une seule exige un KDC :
#
#   1. `identifier_from_principal` — du principal Kerberos à l'identifiant
#      annuaire. Fonction PURE (ni réseau, ni configuration lue en douce) :
#      c'est elle qui décide qui entre, elle doit être testable seule.
#   2. `KerberosAuthProvider.identity_from_principal` — de l'identifiant à
#      une identité résolue, par recherche annuaire.
#   3. `accept_token` — du jeton SPNEGO au principal, par GSSAPI. Seule
#      opération qui exige un keytab et un KDC ; le harnais
#      KERBEROS_DEV_PRINCIPAL la court-circuite, et elle seule.
#
# ## Les deux invariants portés ici
#
# **Kerberos authentifie, l'annuaire renseigne.** Le ticket prouve *qui*, et
# rien de plus d'exploitable simplement — le PAC d'AD contient bien les SID
# de groupes, mais le décoder lierait ce service à un format Microsoft. Nom,
# mail et memberOf viennent donc d'une recherche par bind technique
# (`directory.lookup_user`), sans aucune vérification de mot de passe : il
# n'y a rien à vérifier, le ticket l'a déjà fait.
#
# **Un humain = un identifiant.** `alice.admin@REALM` résout vers le même
# `alice.admin` que le formulaire, donc vers les mêmes recherches
# enregistrées, collections et alertes. D'où la normalisation en minuscules
# ci-dessous : le KDC rend la forme canonique du compte quand un formulaire
# rend la forme saisie, et deux formes donneraient deux jeux de données
# personnelles à la même personne.

import logging

from auth import accounts, config, directory
from auth.base import (
    AuthenticationError,
    AuthProvider,
    AuthProviderUnavailableError,
    LoginCredentials,
    ResolvedIdentity,
)

logger = logging.getLogger(__name__)


class KerberosNotApplicable(NotImplementedError):
    """Levée par `authenticate()`, qui n'a pas de sens pour ce fournisseur."""


#: Mécanismes GSSAPI acceptés. SPNEGO est ce que le navigateur présente ;
#: Kerberos est ce vers quoi il doit négocier. Tout autre résultat — NTLM au
#: premier chef — est REFUSÉ : SPNEGO sait négocier NTLM, mécanisme nettement
#: plus faible, et rien ici ne saurait quoi faire de l'identité qu'il produit.
#: Vérifié APRÈS complétion du contexte, sur le mécanisme réellement négocié,
#: et non à l'acquisition des identifiants — restreindre là aux seuls OID
#: Kerberos ferait échouer les jetons SPNEGO des navigateurs.
_SPNEGO_OID = "1.3.6.1.5.5.2"
_KERBEROS_OIDS = frozenset({
    "1.2.840.113554.1.2.2",  # krb5, l'OID normalisé
    "1.2.840.48018.1.2.2",   # variante historique de certains SDK Microsoft
    _SPNEGO_OID,
})


def dev_harness_principal() -> str | None:
    """Principal simulé, ou None.

    Trois garde-fous, non négociables :
      · aucun effet si API_ENV=production — et l'API refuse même de démarrer
        dans ce cas (voir guardrails.py), plutôt que d'ignorer la variable ;
      · encadré d'avertissement au démarrage, plus le WARNING ci-dessous à
        chaque connexion ainsi ouverte ;
      · l'événement de connexion est marqué `simulated` (voir events.py),
        pour qu'aucune trace d'audit ne laisse croire à un vrai ticket.
    """
    if not config.KERBEROS_DEV_PRINCIPAL or config.IS_PRODUCTION:
        return None
    return config.KERBEROS_DEV_PRINCIPAL


def _gssapi():
    """Import tardif et protégé de `gssapi`.

    Tardif parce que la bibliothèque se compile contre libkrb5-dev, présent
    dans l'image mais pas sur l'hôte de développement : un import en tête de
    module rendrait tout ce fichier — donc le mapping principal→identifiant,
    et la moitié de la suite de tests — inimportable là où Kerberos n'est
    pas installé.

    Protégé parce que son absence est une erreur d'EXPLOITATION, pas une
    tentative invalide : 503, jamais un 401 qui ferait croire à un problème
    d'identifiants."""
    try:
        import gssapi
    except ImportError as exc:  # pragma: no cover — dépend de l'image
        raise AuthProviderUnavailableError(
            "La bibliothèque `gssapi` n'est pas installée : aucun ticket "
            "Kerberos ne peut être accepté."
        ) from exc
    return gssapi


def _acceptor_credentials(gssapi):
    """Identifiants serveur, lus dans le keytab. Toute erreur ici est
    serveur (keytab absent, illisible, sans la clé du SPN demandé) : 503."""
    name = None
    if config.KERBEROS_SPN:
        name = gssapi.Name(config.KERBEROS_SPN, gssapi.NameType.kerberos_principal)

    try:
        # `store` plutôt que la variable d'environnement KRB5_KTNAME : le
        # chemin reste une valeur de configuration de CE service, pas un
        # état global du processus que n'importe quoi pourrait écraser.
        return gssapi.Credentials(
            usage="accept", name=name, store={"keytab": config.KERBEROS_KEYTAB}
        )
    except gssapi.exceptions.GSSError as exc:
        raise AuthProviderUnavailableError(
            f"Keytab inutilisable ({config.KERBEROS_KEYTAB}) : {exc}"
        ) from exc


def accept_token(spnego_token: bytes) -> tuple[str, bytes | None]:
    """Accepte un jeton SPNEGO et rend `(principal, jeton de retour)`.

    Le jeton de retour est celui de l'authentification MUTUELLE : renvoyé au
    navigateur dans `WWW-Authenticate: Negotiate <base64>` sur la réponse
    finale, il lui prouve que le serveur détient bien la clé du SPN.

    Deux familles d'échec, et les confondre serait une faute de diagnostic :
    tout ce qui touche au keytab est une panne serveur (503), tout ce qui
    touche au jeton présenté est un refus (401). Le détail GSSAPI est
    journalisé dans les deux cas — c'est là que se lisent les causes
    classiques (« Clock skew too great », « Request ticket server HTTP/…
    not found in keytab »), invisibles autrement.

    ## Une seule passe

    SPNEGO peut en théorie demander plusieurs allers-retours. Ce n'est pas
    géré, et c'est un choix : porter un contexte GSSAPI d'une requête à
    l'autre supposerait un état partagé que rien ne garantit derrière Nginx
    (aucune affinité de connexion). Avec Kerberos, la négociation aboutit en
    une passe. Un contexte incomplet est donc refusé explicitement plutôt
    que traité à moitié.
    """
    # Contrôle de CONFIGURATION d'abord, avant même d'importer `gssapi` :
    # c'est la panne la plus courante (keytab pas encore déployé), et elle
    # mérite le message le plus précis. L'ordre inverse la masquerait
    # derrière « gssapi n'est pas installée » partout où la bibliothèque
    # manque — l'hôte de développement, notamment. Ordre trouvé par un test.
    if not config.KERBEROS_KEYTAB:
        raise AuthProviderUnavailableError(
            "KERBEROS_KEYTAB n'est pas configuré : aucun ticket ne peut être accepté."
        )

    gssapi = _gssapi()
    credentials = _acceptor_credentials(gssapi)

    context = gssapi.SecurityContext(creds=credentials, usage="accept")
    try:
        response_token = context.step(spnego_token)
    except gssapi.exceptions.GSSError as exc:
        # Jeton malformé, expiré, rejoué, chiffré pour un autre service, ou
        # décalage d'horloge. Message générique côté client, détail en log.
        logger.warning("[kerberos] Jeton refusé par GSSAPI : %s", exc)
        raise AuthenticationError("Ticket Kerberos invalide.") from exc

    if not context.complete:
        logger.warning(
            "[kerberos] Contexte GSSAPI incomplet après une passe : négociation "
            "multi-passes non gérée (voir la docstring d'accept_token)."
        )
        raise AuthenticationError("Négociation Kerberos incomplète.")

    mech = str(context.mech)
    if mech not in _KERBEROS_OIDS:
        # Défense contre une négociation SPNEGO qui aurait abouti à NTLM.
        logger.warning("[kerberos] Mécanisme refusé : %s (Kerberos attendu).", mech)
        raise AuthenticationError("Mécanisme d'authentification non autorisé.")

    return str(context.initiator_name), response_token


def identifier_from_principal(principal: str, *, realm: str) -> str:
    """Dérive l'identifiant annuaire d'un principal Kerberos.

    `alice.admin@DOCSEARCH.TEST` → `alice.admin`.

    Fonction pure : `realm` est passé explicitement plutôt que lu dans la
    configuration, pour qu'un test puisse la couvrir sans toucher à
    l'environnement du processus.

    Trois refus :

    - **realm non configuré** → `AuthProviderUnavailableError` (503) : c'est
      une erreur d'exploitation, pas une tentative invalide, et la présenter
      comme « identifiants incorrects » masquerait la panne.
    - **realm différent de celui attendu** → le contrôle le plus important
      du module : sans lui, une relation d'approbation entre domaines
      laisserait entrer `alice@AUTRE-REALM` sous l'identité de `alice`.
      Comparaison SENSIBLE À LA CASSE — les noms de realm le sont
      (RFC 4120 §6.1), et être laxiste ici reviendrait à accepter un realm
      qu'on n'a pas nommé.
    - **principal à plusieurs composants** (`HTTP/hôte@REALM`,
      `alice/admin@REALM`) : comptes de service ou instances
      administratives, jamais l'utilisateur nominatif attendu. Un `HTTP/…`
      accepté ici serait le compte de service de l'application se
      connectant à sa propre application.
    """
    if not realm:
        raise AuthProviderUnavailableError(
            "KERBEROS_REALM n'est pas configuré : aucun principal ne peut "
            "être accepté (fail-closed)."
        )

    if principal.count("@") != 1:
        raise AuthenticationError("Principal Kerberos malformé.")

    identifier, _, presented_realm = principal.partition("@")

    if presented_realm != realm:
        logger.warning(
            "[kerberos] Principal refusé : realm %r présenté, %r attendu.",
            presented_realm, realm,
        )
        raise AuthenticationError("Realm Kerberos non autorisé.")

    if not identifier:
        raise AuthenticationError("Principal Kerberos sans identifiant.")

    if "/" in identifier:
        logger.warning("[kerberos] Principal refusé : %r a plusieurs composants.", identifier)
        raise AuthenticationError("Principal Kerberos à plusieurs composants.")

    # Forme canonique — voir l'invariant « un humain = un identifiant ».
    return accounts.normalize_login(identifier)


class KerberosAuthProvider(AuthProvider):
    #: Volontairement distinct de "ldap" : ce claim ne sert qu'à dire par
    #: quelle PORTE la personne est entrée, information réelle et non
    #: reconstituable autrement — journaliser "ldap" rendrait un pic de
    #: connexions SSO indiscernable d'un pic de connexions par formulaire.
    #: L'IDENTITÉ, elle, est la même des deux côtés (même identifiant
    #: annuaire), et c'est elle seule qui porte les droits.
    name = "kerberos"

    def authenticate(self, credentials: LoginCredentials) -> ResolvedIdentity:
        # Ne devrait jamais être appelé : SPNEGO est un dialogue défi/réponse
        # entre le navigateur et le serveur, pas un couple identifiant/mot
        # de passe. Présent uniquement pour satisfaire l'interface.
        raise KerberosNotApplicable(
            "Kerberos ne s'authentifie pas par identifiant/mot de passe "
            "(voir identity_from_principal)."
        )

    def identity_from_principal(self, principal: str) -> ResolvedIdentity:
        """Principal Kerberos → identité résolue.

        Un principal valide dont l'annuaire ne connaît pas le porteur est un
        ÉCHEC (401), jamais une identité provisionnée à l'aveugle : le
        ticket prouve l'appartenance au domaine, pas le droit d'exister dans
        cette application. Le contrôle d'ACCESS_GROUP s'applique ensuite
        comme pour tout le monde."""
        identifier = identifier_from_principal(principal, realm=config.KERBEROS_REALM)

        try:
            user = directory.lookup_user(identifier)
        except directory.DirectoryAuthError as exc:
            logger.info("[kerberos] Principal accepté mais introuvable dans l'annuaire.")
            raise AuthenticationError(str(exc)) from exc
        except directory.DirectoryUnavailableError as exc:
            raise AuthProviderUnavailableError(str(exc)) from exc

        return ResolvedIdentity(
            login=identifier,
            display_name=user.cn or identifier,
            email=user.mail,
            groups=user.groups,
        )


KERBEROS_PROVIDER = KerberosAuthProvider()
