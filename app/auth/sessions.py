# auth/sessions.py — Sessions révocables et rate limiting (Redis)
#
# Deux mécanismes qui n'ont en commun que leur support :
#
# 1. **Le refresh token n'est valide que s'il existe aussi ici.** Un jeton
#    correctement signé mais absent du magasin — déconnexion, révocation
#    d'exploitation, jeton jamais émis par cette installation — est refusé.
#    C'est la seule chose qui rend une déconnexion réelle : sans elle,
#    « se déconnecter » ne ferait qu'effacer un cookie que la personne
#    pourrait recoller.
#
# 2. **Le rate limiting compte les ÉCHECS de connexion**, sur deux clés
#    indépendantes (identifiant et IP), chacune plafonnée sur une fenêtre
#    glissante. Bloquer si l'une OU l'autre dépasse protège à la fois de qui
#    bourrine un compte depuis plusieurs adresses et de qui bourrine
#    plusieurs comptes depuis une seule.

import json
import logging
from datetime import datetime, timezone

from auth import config, store

logger = logging.getLogger(__name__)


class RateLimitExceeded(Exception):
    """Trop de tentatives — traduit en 429."""


# ── Sessions ─────────────────────────────────────────────────

def _refresh_key(jti: str) -> str:
    return f"{store.KEY_PREFIX}refresh:{jti}"


def store_refresh_token(jti: str, *, login: str, ttl_seconds: int, auth_method: str,
                        display_name: str, email: str | None) -> None:
    """Mémorise la session. L'instantané d'identité qui l'accompagne évite un
    aller-retour annuaire à chaque /auth/refresh — les GROUPES, eux, n'y
    sont volontairement pas : ils se relisent à chaque autorisation, sans
    quoi une exclusion de groupe ne prendrait effet qu'à l'expiration du
    refresh, soit une semaine par défaut."""
    client = store.require_client()
    client.hset(_refresh_key(jti), mapping={
        "login": login,
        "auth_method": auth_method,
        "display_name": display_name,
        "email": email or "",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    client.expire(_refresh_key(jti), ttl_seconds)


def get_refresh_session(jti: str) -> dict | None:
    client = store.require_client()
    data = client.hgetall(_refresh_key(jti))
    return dict(data) if data else None


def revoke_refresh_token(jti: str) -> bool:
    client = store.require_client()
    return bool(client.delete(_refresh_key(jti)))


def revoke_all_for_login(login: str) -> int:
    """Révoque toutes les sessions d'une personne (départ, compromission).

    Un SCAN complet plutôt qu'un index par utilisateur : l'opération est
    rare et manuelle, un index secondaire serait une seconde structure à
    garder cohérente pour rien."""
    client = store.require_client()
    revoked = 0
    for key in client.scan_iter(match=f"{store.KEY_PREFIX}refresh:*", count=100):
        if client.hget(key, "login") == login:
            client.delete(key)
            revoked += 1
    if revoked:
        logger.warning("[auth] %d session(s) révoquée(s) pour %s", revoked, login)
    return revoked


# ── Rate limiting ────────────────────────────────────────────

def _rl_key(method: str, kind: str, value: str) -> str:
    return f"{store.KEY_PREFIX}rl:{method}:{kind}:{value}"


def _counters(method: str, identifier: str, ip: str) -> list[str]:
    keys = []
    if identifier:
        keys.append(_rl_key(method, "user", identifier))
    if ip:
        keys.append(_rl_key(method, "ip", ip))
    return keys


def check_rate_limit(method: str, identifier: str, ip: str) -> None:
    client = store.get_client()
    if client is None:
        # Redis injoignable : ne pas bloquer les connexions pour autant. Le
        # magasin de sessions, lui, lèvera juste après — le refus viendra de
        # là, avec le bon code (503), plutôt que d'un compteur muet.
        return
    for key in _counters(method, identifier, ip):
        current = client.get(key)
        if current is not None and int(current) >= config.RATE_LIMIT_MAX_ATTEMPTS:
            raise RateLimitExceeded(f"Trop de tentatives ({method}).")


def register_failed_attempt(method: str, identifier: str, ip: str) -> None:
    client = store.get_client()
    if client is None:
        return
    for key in _counters(method, identifier, ip):
        pipe = client.pipeline()
        pipe.incr(key)
        # nx : la fenêtre est ancrée au PREMIER échec, jamais repoussée à
        # chaque tentative — sinon un attaquant assez lent ne serait jamais
        # bloqué, il repousserait indéfiniment sa propre échéance.
        pipe.expire(key, config.RATE_LIMIT_WINDOW_SECONDS, nx=True)
        pipe.execute()


def reset_rate_limit(method: str, identifier: str, ip: str) -> None:
    client = store.get_client()
    if client is None:
        return
    keys = _counters(method, identifier, ip)
    if keys:
        client.delete(*keys)


# ── Diagnostic ───────────────────────────────────────────────

def count_active_sessions() -> int:
    client = store.get_client()
    if client is None:
        return 0
    return sum(1 for _ in client.scan_iter(match=f"{store.KEY_PREFIX}refresh:*", count=100))


def dump_session(jti: str) -> str:
    return json.dumps(get_refresh_session(jti) or {}, ensure_ascii=False)
