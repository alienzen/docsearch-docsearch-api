# auth/store.py — Le client Redis de l'authentification
#
# Même motif que saved_searches.py / runtime_config.py : un client paresseux,
# mémorisé, qui rend None si Redis est injoignable. La différence est dans ce
# que les appelants en font — ici, Redis injoignable n'est jamais silencieux :
# une session ne peut ni s'ouvrir ni se révoquer sans lui, et le dissimuler
# reviendrait à laisser croire qu'une déconnexion a eu lieu.
#
# Toutes les clés vivent sous "docsearch:auth:" :
#   docsearch:auth:user:<login>     hash    compte de secours local
#   docsearch:auth:refresh:<jti>    hash    session ouverte — l'absence = révoquée
#   docsearch:auth:rl:<...>         string  compteur de rate limiting

import logging
import os

logger = logging.getLogger(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

KEY_PREFIX = "docsearch:auth:"

_redis_client = None
_redis_unavailable_logged = False


class StoreUnavailable(RuntimeError):
    """Redis injoignable. Traduit en 503 par le routeur — jamais en 401 : une
    panne d'infrastructure ne se présente pas comme un mot de passe faux."""


def get_client():
    global _redis_client, _redis_unavailable_logged
    if _redis_client is not None:
        return _redis_client
    try:
        import redis
        _redis_client = redis.Redis(
            host=REDIS_HOST, port=REDIS_PORT,
            decode_responses=True, socket_connect_timeout=2, socket_timeout=2,
        )
        _redis_client.ping()
        _redis_unavailable_logged = False
        return _redis_client
    except Exception as e:
        if not _redis_unavailable_logged:
            logger.warning(f"[auth] Redis injoignable ({e})")
            _redis_unavailable_logged = True
        _redis_client = None
        return None


def require_client():
    client = get_client()
    if client is None:
        raise StoreUnavailable(
            "Redis injoignable — aucune session ne peut être ouverte, "
            "renouvelée ni révoquée. Vérifier l'unité docsearch-redis."
        )
    return client


def reset_client() -> None:
    """Oublie le client mémorisé. Utile aux tests, qui changent d'instance
    Redis entre deux cas, et après une panne franche."""
    global _redis_client
    _redis_client = None
