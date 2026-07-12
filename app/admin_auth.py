# admin_auth.py — Autorisation d'accès au panneau d'administration
#
# Être authentifié (header X-User, injecté par Nginx après validation
# SSO) ne suffit pas à administrer DocSearch — seuls les membres du
# groupe ADMIN_GROUP (résolu via LDAP/AD, voir ldap_resolver.py) ont
# accès aux routes /admin/*.
#
# ⚠️  ADMIN_AUTH_DISABLED=true contourne TOUT ce contrôle (y compris
# la vérification du header X-User) — réservé aux tests locaux sans
# SSO/LDAP configurés. Ne JAMAIS l'activer en production : la
# vérification est volontairement bruyante (log à chaque requête + un
# avertissement bien visible au démarrage) pour qu'un oubli soit
# impossible à manquer dans les logs.

import os
import logging
from fastapi import Header, HTTPException
from ldap_resolver import get_user_groups, LDAP_ENABLED

logger = logging.getLogger(__name__)

ADMIN_GROUP = os.getenv("ADMIN_GROUP", "").strip().lower()
ADMIN_AUTH_DISABLED = os.getenv("ADMIN_AUTH_DISABLED", "false").strip().lower() == "true"

if ADMIN_AUTH_DISABLED:
    logger.warning(
        "\n"
        "╔═══════════════════════════════════════════════════════════╗\n"
        "║  ⚠️   ADMIN_AUTH_DISABLED=true                              ║\n"
        "║  Le contrôle d'accès du panneau /admin est DÉSACTIVÉ.       ║\n"
        "║  N'IMPORTE QUI peut modifier la configuration, purger      ║\n"
        "║  l'index et déclencher des scans SANS AUTHENTIFICATION.    ║\n"
        "║  Réservé aux tests locaux — retirer avant toute mise en     ║\n"
        "║  production.                                                ║\n"
        "╚═══════════════════════════════════════════════════════════╝"
    )


def require_admin(x_user: str | None = Header(default=None)) -> str:
    """
    Dépendance FastAPI à utiliser via Depends(require_admin) sur
    chaque route /admin/*. Lève :
      - 401 si aucun utilisateur identifié (pas de header X-User)
      - 403 si ADMIN_GROUP n'est pas configuré (accès désactivé par
        sécurité plutôt que de laisser un défaut permissif)
      - 403 si LDAP est désactivé (impossible de vérifier un groupe
        sans résolution LDAP)
      - 403 si l'utilisateur n'appartient pas au groupe administrateur
    Retourne le login de l'utilisateur si l'accès est autorisé.

    Si ADMIN_AUTH_DISABLED=true, retourne immédiatement sans aucune
    vérification (voir avertissement ci-dessus).
    """
    if ADMIN_AUTH_DISABLED:
        logger.warning(f"[admin_auth] Accès /admin SANS authentification (ADMIN_AUTH_DISABLED=true) — utilisateur : {x_user or 'anonyme'}")
        return x_user or "dev-admin"

    if not x_user:
        raise HTTPException(
            status_code=401,
            detail="Authentification requise (en-tête X-User absent — vérifier la configuration SSO/Nginx)"
        )

    if not ADMIN_GROUP:
        logger.warning("[admin_auth] ADMIN_GROUP non configuré — accès admin refusé par défaut")
        raise HTTPException(
            status_code=403,
            detail="Panneau d'administration désactivé : ADMIN_GROUP non configuré"
        )

    if not LDAP_ENABLED:
        raise HTTPException(
            status_code=403,
            detail="Panneau d'administration nécessite LDAP_ENABLED=true "
                    "pour vérifier l'appartenance au groupe administrateur"
        )

    groups = [g.lower() for g in get_user_groups(x_user.lower())]
    if ADMIN_GROUP not in groups:
        logger.info(f"[admin_auth] Accès refusé pour '{x_user}' (groupe '{ADMIN_GROUP}' requis)")
        raise HTTPException(
            status_code=403,
            detail=f"Accès réservé aux membres du groupe '{ADMIN_GROUP}'"
        )

    return x_user


def is_admin(x_user: str | None) -> bool:
    """
    Version non levante de require_admin() — répond juste "oui/non",
    pour un usage hors contrôle d'accès : l'interface de recherche
    l'appelle pour savoir si elle doit afficher les liens "Administration"
    /"Statistiques" (une page qui échouerait de toute façon avec 403
    n'a pas à être proposée). Mêmes règles, jamais d'exception.
    """
    if ADMIN_AUTH_DISABLED:
        return True
    if not x_user or not ADMIN_GROUP or not LDAP_ENABLED:
        return False
    groups = [g.lower() for g in get_user_groups(x_user.lower())]
    return ADMIN_GROUP in groups
