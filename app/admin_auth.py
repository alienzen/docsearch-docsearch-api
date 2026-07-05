# admin_auth.py — Autorisation d'accès au panneau d'administration
#
# Être authentifié (header X-User, injecté par Nginx après validation
# SSO) ne suffit pas à administrer DocSearch — seuls les membres du
# groupe ADMIN_GROUP (résolu via LDAP/AD, voir ldap_resolver.py) ont
# accès aux routes /admin/*.

import os
import logging
from fastapi import Header, HTTPException
from ldap_resolver import get_user_groups, LDAP_ENABLED

logger = logging.getLogger(__name__)

ADMIN_GROUP = os.getenv("ADMIN_GROUP", "").strip().lower()


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
    """
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
