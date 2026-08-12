# auth/deps.py — D'où vient l'identité, et qui a le droit
#
# **Le point le plus important de tout le chantier.** Jusqu'ici, l'identité
# de l'appelant était l'en-tête HTTP `X-User`, censé être injecté par Nginx
# après validation SSO. Deux problèmes : la validation SSO n'a jamais été
# branchée (les blocs auth_request de docsearch-infra/nginx/nginx.conf
# étaient commentés depuis un an), et l'API publiant son port, n'importe
# qui pouvait poser cet en-tête lui-même —
# `curl -H "X-User: alice.admin" http://hôte:8000/admin/status` répondait
# 200.
#
# Désormais l'identité vient d'un jeton signé par cette application, qu'elle
# vérifie elle-même. Un mécanisme qui ne vit que dans la configuration d'un
# proxy est court-circuité par tout ce qui atteint le service autrement.
#
# Trois sources, dans cet ordre, et les deux dernières n'existent qu'en
# développement (verrouillées par guardrails.py, qui empêche l'API de
# démarrer si elles sont armées avec API_ENV=production) :
#
#   1. cookie d'accès  — le cas normal, posé par /auth/login
#   2. Authorization: Bearer — pour curl, les scripts, les tests
#   3. X-User / DEV_USER — harnais de recette (proxy dev-user, port 8090)

import logging

import jwt as pyjwt
from fastapi import Depends, Header, HTTPException, Request

from auth import (  # noqa: F401 — guardrails s'applique à l'import
    accounts,
    config,
    directory,
    guardrails,
    tokens,
)

logger = logging.getLogger(__name__)


# ── Résolution de l'identité ─────────────────────────────────

def _from_token(request: Request) -> str | None:
    raw = request.cookies.get(config.ACCESS_COOKIE_NAME)
    if not raw:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            raw = header[7:].strip()
    if not raw:
        return None

    try:
        claims = tokens.decode_token(raw, expected_type=tokens.ACCESS_TOKEN_TYPE)
    except tokens.KeyLoadError as exc:
        # Clés absentes : c'est une panne de configuration, pas une identité
        # invalide. Laisser passer en « non authentifié » produirait un 401
        # trompeur ; le 503 dit où chercher.
        raise HTTPException(
            status_code=503,
            detail="Authentification indisponible : clés de signature illisibles.",
        ) from exc
    except pyjwt.InvalidTokenError:
        # Expiré, mal signé, mauvaise audience, mauvais type. Aucun détail
        # côté client — le front tente un /auth/refresh sur 401 et n'a pas
        # besoin d'en savoir plus.
        return None

    return claims.get("sub") or None


def _from_dev_harness(x_user: str | None) -> str | None:
    """Les deux replis de développement. Bruyants à chaque usage : un
    harnais silencieux finit par tourner en production sans que personne le
    remarque, ce qui est exactement l'histoire d'ACCESS_AUTH_DISABLED."""
    if config.TRUST_X_USER_HEADER and x_user:
        logger.warning(
            "[auth] Identité prise dans l'en-tête X-User (TRUST_X_USER_HEADER=true) "
            "— aucune vérification : %s", x_user,
        )
        return x_user.strip().lower()

    if config.DEV_USER:
        logger.warning(
            "[auth] Identité prise dans DEV_USER — aucune authentification : %s",
            config.DEV_USER,
        )
        return config.DEV_USER.strip().lower()

    return None


def optional_user(
    request: Request,
    x_user: str | None = Header(default=None),
) -> str | None:
    """Identité de l'appelant, ou None. Ne lève jamais.

    Pour les rares routes qui ont un sens sans être authentifié — /is-admin,
    qui sert à l'interface à décider quels liens afficher, et qui doit
    répondre « non » plutôt que 401 pour un visiteur anonyme."""
    return _from_token(request) or _from_dev_harness(x_user)


def current_user(
    request: Request,
    x_user: str | None = Header(default=None),
) -> str:
    """Identité de l'appelant. 401 si personne.

    Remplace `resolve_user(x_user)` : plus de repli sur « anonymous », qui
    donnait une identité — et donc des recherches enregistrées, des
    collections et des alertes — à qui n'en avait aucune."""
    login = optional_user(request, x_user)
    if not login:
        raise HTTPException(
            status_code=401,
            detail="Authentification requise.",
        )
    return login


# ── Autorisation ─────────────────────────────────────────────

def _groups_or_503(login: str) -> list[str]:
    """Groupes effectifs, en mode STRICT : un annuaire injoignable donne un
    503 et non une liste vide.

    C'est la correction d'un défaut de fond du contrôle d'accès précédent —
    une panne d'annuaire s'y traduisait par « Accès réservé aux membres du
    groupe docsearch-users », message qui envoie chercher un droit manquant
    là où il faut redémarrer un service."""
    try:
        return directory.get_effective_groups(login, strict=True)
    except directory.DirectoryUnavailableError as exc:
        logger.error("[auth] Autorisation impossible pour %s : %s", login, exc)
        raise HTTPException(
            status_code=503,
            detail="Annuaire injoignable : les droits ne peuvent pas être vérifiés.",
        ) from exc


def _require_group(login: str, group: str, *, what: str) -> str:
    if not group:
        logger.warning("[auth] %s non configuré — accès refusé par défaut.", what)
        raise HTTPException(
            status_code=403,
            detail=f"{what} non configuré : accès désactivé.",
        )

    if not config.LDAP_ENABLED and not accounts.has_account(login):
        # Sans annuaire ET sans compte de secours, aucun groupe ne peut être
        # établi. Refus explicite plutôt qu'un défaut permissif.
        raise HTTPException(
            status_code=403,
            detail="Vérification des groupes impossible : LDAP_ENABLED=false.",
        )

    if group not in _groups_or_503(login):
        logger.info("[auth] Accès refusé pour '%s' (groupe '%s' requis).", login, group)
        raise HTTPException(
            status_code=403,
            detail=f"Accès réservé aux membres du groupe '{group}'.",
        )
    return login


def require_access(user: str = Depends(current_user)) -> str:
    """Droit d'utiliser DocSearch : appartenance à ACCESS_GROUP.

    Utilisée par GET /auth/check-access, la cible interne du auth_request de
    Nginx qui garde chaque page."""
    if config.ACCESS_AUTH_DISABLED:
        logger.warning("[auth] Accès SANS contrôle (ACCESS_AUTH_DISABLED=true) — %s", user)
        return user
    return _require_group(user, config.ACCESS_GROUP, what="ACCESS_GROUP")


def require_admin(user: str = Depends(current_user)) -> str:
    """Droit d'administrer : appartenance à ADMIN_GROUP.

    Être authentifié ne suffit pas — c'est un contrôle EN PLUS de
    require_access, pas à sa place."""
    if config.ADMIN_AUTH_DISABLED:
        logger.warning("[auth] Accès /admin SANS contrôle (ADMIN_AUTH_DISABLED=true) — %s", user)
        return user
    return _require_group(user, config.ADMIN_GROUP, what="ADMIN_GROUP")


def is_admin(login: str | None) -> bool:
    """Version non levante de require_admin(), pour l'affichage : une page
    qui échouerait de toute façon en 403 n'a pas à être proposée. Ne lève
    jamais, y compris si l'annuaire est injoignable — dans le doute, on
    n'affiche pas le lien."""
    if config.ADMIN_AUTH_DISABLED:
        return True
    if not login or not config.ADMIN_GROUP:
        return False
    return config.ADMIN_GROUP in directory.get_effective_groups(login)


def user_groups(login: str | None) -> list[str]:
    """Groupes effectifs, sans lever — pour l'affichage et le diagnostic."""
    if not login:
        return []
    return directory.get_effective_groups(login)
