# auth/accounts.py — Comptes de secours locaux
#
# CE N'EST PAS UN SYSTÈME DE GESTION D'UTILISATEURS. DocSearch n'a ni
# inscription, ni écran de gestion de comptes, ni réinitialisation par
# email : l'appartenance à ACCESS_GROUP dans l'annuaire *est* le droit
# d'entrer. Ce module couvre un seul cas, celui qui rendait DocSearch
# totalement inaccessible : la panne d'annuaire.
#
# Sans annuaire, get_user_groups() ne rend rien, donc require_access refuse
# tout le monde — administration comprise, donc sans aucun moyen de
# diagnostiquer quoi que ce soit depuis l'application. Même raison que les
# comptes locaux de `processus/bpmn-api`, et même conclusion.
#
# ⚠️  D'où le point à ne pas manquer : **le compte porte ses propres
# groupes**. Un compte de secours sans groupes se ferait refuser par le
# contrôle d'accès qu'il est justement censé contourner — c'est l'annuaire
# qui est en panne, il n'y a personne pour dire qu'il est administrateur.
#
# Création et suppression : scripts/gerer-comptes-locaux.py, hors de l'API.
# Aucune route HTTP ne crée de compte, aucune n'en liste les hachages.

import json
import logging
from datetime import datetime, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerifyMismatchError

from auth import store

logger = logging.getLogger(__name__)

# Paramètres par défaut d'argon2-cffi (Argon2id, alignés sur les
# recommandations OWASP). Jamais bcrypt, jamais de hash maison.
_hasher = PasswordHasher()

# Vérification à vide, pour que « compte inconnu » coûte le même temps que
# « mot de passe faux » : sans elle, le temps de réponse dirait lesquels des
# identifiants présentés existent.
_DUMMY_HASH = _hasher.hash("mot-de-passe-factice-jamais-valide")


def _key(login: str) -> str:
    return f"{store.KEY_PREFIX}user:{login}"


def normalize_login(login: str) -> str:
    """Forme canonique unique d'un identifiant : minuscules, sans espaces de
    bordure.

    C'est l'invariant « un humain = un identifiant » de PLAN-AUTH-SSO.md, et
    il ne concerne pas que les comptes locaux : le KDC rend la forme
    canonique du compte quand un formulaire rend la forme saisie, et les
    recherches enregistrées, collections et alertes sont indexées par cet
    identifiant. Deux formes = deux jeux de données personnelles pour la
    même personne. Toute écriture ou comparaison d'identifiant passe par
    ici."""
    return (login or "").strip().lower()


def get_account(login: str) -> dict | None:
    """Rend le compte local, ou None s'il n'existe pas.

    Redis injoignable rend None lui aussi, volontairement : ce chemin sert
    aussi à *aiguiller* vers le bon fournisseur (voir router.py), et un 503
    y ferait échouer une connexion annuaire parfaitement valide."""
    client = store.get_client()
    if client is None:
        return None
    raw = client.hgetall(_key(normalize_login(login)))
    if not raw:
        return None
    account = dict(raw)
    try:
        account["groups"] = json.loads(account.get("groups") or "[]")
    except json.JSONDecodeError:
        account["groups"] = []
    account["disabled"] = str(account.get("disabled", "")).lower() == "true"
    return account


def has_account(login: str) -> bool:
    return get_account(login) is not None


def list_accounts() -> list[dict]:
    """Sans les hachages — cette liste est destinée à l'exploitation."""
    client = store.require_client()
    accounts = []
    for key in client.scan_iter(match=f"{store.KEY_PREFIX}user:*", count=100):
        login = key.rsplit(":", 1)[-1]
        account = get_account(login) or {}
        account.pop("password_hash", None)
        account["login"] = login
        accounts.append(account)
    return sorted(accounts, key=lambda a: a["login"])


def set_account(
    login: str,
    *,
    password: str,
    groups: list[str],
    display_name: str = "",
    email: str = "",
    disabled: bool = False,
) -> str:
    """Crée ou remplace un compte de secours. Rend l'identifiant canonique."""
    canonical = normalize_login(login)
    if not canonical:
        raise ValueError("Identifiant vide.")
    if not password:
        raise ValueError("Mot de passe vide.")
    client = store.require_client()
    client.hset(
        _key(canonical),
        mapping={
            "password_hash": _hasher.hash(password),
            "display_name": display_name or canonical,
            "email": email,
            # Minuscules : get_effective_groups() compare des groupes en
            # minuscules, comme le faisait déjà ldap_resolver.py.
            "groups": json.dumps([g.strip().lower() for g in groups if g.strip()]),
            "disabled": "true" if disabled else "false",
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    logger.warning(
        "[auth] Compte de secours local créé/modifié : %s (groupes : %s)",
        canonical, ", ".join(groups) or "aucun",
    )
    return canonical


def delete_account(login: str) -> bool:
    client = store.require_client()
    deleted = bool(client.delete(_key(normalize_login(login))))
    if deleted:
        logger.warning("[auth] Compte de secours local supprimé : %s", normalize_login(login))
    return deleted


def verify_password(login: str, password: str) -> dict | None:
    """Vérifie un mot de passe local. Rend le compte, ou None.

    Ne distingue jamais « compte inconnu », « compte désactivé » et « mot de
    passe faux » — ni par la valeur de retour, ni par le temps passé."""
    account = get_account(login)

    if account is None or account.get("disabled") or not account.get("password_hash"):
        try:
            _hasher.verify(_DUMMY_HASH, password or "")
        except VerifyMismatchError:
            pass
        return None

    try:
        _hasher.verify(account["password_hash"], password or "")
    except (VerifyMismatchError, InvalidHash):
        # InvalidHash = hachage corrompu en base. Traité comme un échec
        # d'authentification, jamais comme une erreur serveur qui laisserait
        # deviner l'état du compte.
        return None

    return account


def get_account_groups(login: str) -> list[str]:
    """Groupes portés par le compte local, [] s'il n'y en a pas.

    Appelé par directory.get_effective_groups() sur le chemin de CHAQUE
    autorisation : ne lève jamais."""
    account = get_account(login)
    if not account or account.get("disabled"):
        return []
    return list(account.get("groups") or [])
