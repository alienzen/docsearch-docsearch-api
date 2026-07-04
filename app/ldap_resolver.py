# ldap_resolver.py — Résolution des groupes LDAP / Active Directory
# Intégré le 29/06/2026

import os
LDAP_ENABLED = os.getenv("LDAP_ENABLED", "false").lower() == "true"
LDAP_HOST = os.getenv("LDAP_HOST", "")
LDAP_BASE = os.getenv("LDAP_BASE", "")
LDAP_BINDDN = os.getenv("LDAP_BINDDN", "")
LDAP_PASS = os.getenv("LDAP_PASS", "")

import logging
from functools import lru_cache
from ldap3 import Server, Connection, ALL, SUBTREE

logger = logging.getLogger(__name__)

LDAP_HOST   = LDAP_HOST
LDAP_BASE   = LDAP_BASE
LDAP_BINDDN = LDAP_BINDDN
LDAP_PASS   = LDAP_PASS
LDAP_ENABLED = LDAP_ENABLED


def _get_conn() -> Connection | None:
    if not LDAP_ENABLED:
        return None
    try:
        server = Server(LDAP_HOST, get_info=ALL, connect_timeout=5)
        return Connection(server, LDAP_BINDDN, LDAP_PASS, auto_bind=True)
    except Exception as e:
        logger.error(f"Connexion LDAP échouée : {e}")
        return None


@lru_cache(maxsize=4096)
def get_user_groups(username: str) -> list[str]:
    """
    Retourne les groupes AD d'un utilisateur.
    Cache LRU de 4096 entrées — invalider au redémarrage.
    En mode LDAP_ENABLED=false, retourne une liste vide
    (la recherche se fait uniquement sur les ACL POSIX).
    """
    if not LDAP_ENABLED:
        return []

    conn = _get_conn()
    if not conn:
        return []

    try:
        conn.search(
            LDAP_BASE,
            f"(sAMAccountName={username})",
            attributes=["memberOf"],
            search_scope=SUBTREE,
        )
        if not conn.entries:
            return []

        groups = []
        for dn in conn.entries[0].memberOf:
            cn = str(dn).split(",")[0].replace("CN=", "").lower()
            groups.append(cn)
        return groups

    except Exception as e:
        logger.error(f"Erreur LDAP get_user_groups({username}) : {e}")
        return []
    finally:
        conn.unbind()


def invalidate_cache():
    """Vide le cache LDAP (à appeler périodiquement ou au rechargement)."""
    get_user_groups.cache_clear()
    logger.info("Cache LDAP vidé.")
