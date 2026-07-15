# access_auth.py — Autorisation d'accès à l'application DocSearch
# (toutes les pages, pas seulement /admin) : appartenance au groupe
# ACCESS_GROUP (résolu via LDAP/AD, voir ldap_resolver.py).
#
# Cible interne appelée par Nginx via auth_request sur chaque location
# de page (voir docsearch-ui/nginx.conf : location /_access_check et
# GET /auth/check-access dans search_api.py).
#
# ⚠️  ACCESS_AUTH_DISABLED=true contourne TOUT ce contrôle (y compris
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

ACCESS_GROUP = os.getenv("ACCESS_GROUP", "").strip().lower()
ACCESS_AUTH_DISABLED = os.getenv("ACCESS_AUTH_DISABLED", "false").strip().lower() == "true"

if ACCESS_AUTH_DISABLED:
    logger.warning(
        "\n"
        "╔═══════════════════════════════════════════════════════════╗\n"
        "║  ⚠️   ACCESS_AUTH_DISABLED=true                             ║\n"
        "║  Le contrôle d'accès à DocSearch est DÉSACTIVÉ.             ║\n"
        "║  N'IMPORTE QUI peut consulter l'application, même sans      ║\n"
        "║  être membre du groupe requis.                              ║\n"
        "║  Réservé aux tests locaux — retirer avant toute mise en     ║\n"
        "║  production.                                                ║\n"
        "╚═══════════════════════════════════════════════════════════╝"
    )


def require_access(x_user: str | None = Header(default=None)) -> str:
    """
    Dépendance FastAPI utilisée par GET /auth/check-access. Mêmes
    règles que require_admin() (voir admin_auth.py) mais vérifie
    ACCESS_GROUP plutôt qu'ADMIN_GROUP :
      - 401 si aucun utilisateur identifié (pas de header X-User)
      - 403 si ACCESS_GROUP n'est pas configuré (accès désactivé par
        sécurité plutôt que de laisser un défaut permissif)
      - 403 si LDAP est désactivé (impossible de vérifier un groupe
        sans résolution LDAP)
      - 403 si l'utilisateur n'appartient pas au groupe requis
    Retourne le login de l'utilisateur si l'accès est autorisé.

    Si ACCESS_AUTH_DISABLED=true, retourne immédiatement sans aucune
    vérification (voir avertissement ci-dessus).
    """
    if ACCESS_AUTH_DISABLED:
        logger.warning(f"[access_auth] Accès SANS authentification (ACCESS_AUTH_DISABLED=true) — utilisateur : {x_user or 'anonyme'}")
        return x_user or "dev-user"

    if not x_user:
        raise HTTPException(
            status_code=401,
            detail="Authentification requise (en-tête X-User absent — vérifier la configuration SSO/Nginx)"
        )

    if not ACCESS_GROUP:
        logger.warning("[access_auth] ACCESS_GROUP non configuré — accès refusé par défaut")
        raise HTTPException(
            status_code=403,
            detail="Application désactivée : ACCESS_GROUP non configuré"
        )

    if not LDAP_ENABLED:
        raise HTTPException(
            status_code=403,
            detail="Accès nécessite LDAP_ENABLED=true pour vérifier l'appartenance au groupe"
        )

    groups = [g.lower() for g in get_user_groups(x_user.lower())]
    if ACCESS_GROUP not in groups:
        logger.info(f"[access_auth] Accès refusé pour '{x_user}' (groupe '{ACCESS_GROUP}' requis)")
        raise HTTPException(
            status_code=403,
            detail=f"Accès réservé aux membres du groupe '{ACCESS_GROUP}'"
        )

    return x_user
