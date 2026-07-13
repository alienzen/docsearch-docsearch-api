# alert_notifications.py — Notifications in-app des alertes sur
# recherches sauvegardées (voir saved_searches.py : alert_enabled — et
# alert_worker.py, qui dépose une notification ici après chaque
# vérification positive).
#
# Choix : in-app uniquement (badge + liste dans index.html), pas d'email
# — DocSearch n'a aujourd'hui aucune brique SMTP, et un email ferait
# sortir des titres de documents potentiellement confidentiels (filtrés
# par ACL à l'intérieur de l'app) hors du périmètre d'accès contrôlé.
#
# Stockage : une clé Redis par utilisateur ("docsearch:alerts:{user}"),
# liste JSON plafonnée aux ALERT_HISTORY_LIMIT dernières notifications —
# contrairement à search_log.py/nps_log.py (journaux dans ES, volume
# illimité, à but d'audit), ceci est un flux "à traiter" affiché à
# l'utilisateur : pas besoin de le garder indéfiniment.

import os
import json
import uuid
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
KEY_PREFIX = "docsearch:alerts:"
ALERT_HISTORY_LIMIT = int(os.getenv("ALERT_HISTORY_LIMIT", "50"))

_redis_client = None
_redis_unavailable_logged = False


def _get_redis_client():
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    try:
        import redis
        _redis_client = redis.Redis(
            host=REDIS_HOST, port=REDIS_PORT,
            decode_responses=True, socket_connect_timeout=2, socket_timeout=2,
        )
        _redis_client.ping()
        return _redis_client
    except Exception as e:
        global _redis_unavailable_logged
        if not _redis_unavailable_logged:
            logger.warning(f"[alert_notifications] Redis injoignable ({e})")
            _redis_unavailable_logged = True
        _redis_client = None
        return None


def _load(username: str) -> list[dict]:
    client = _get_redis_client()
    if client is None:
        return []
    raw = client.get(KEY_PREFIX + username)
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(f"[alert_notifications] Contenu invalide pour '{username}' — repli sur liste vide")
        return []


def list_notifications(username: str) -> list[dict]:
    """Notifications de l'utilisateur, la plus récente en premier. Liste
    vide (pas d'exception) si Redis est injoignable ou si l'utilisateur
    n'a aucune notification — même principe que saved_searches.list_saved()."""
    try:
        return sorted(_load(username), key=lambda n: n["checked_at"], reverse=True)
    except KeyError:
        logger.warning(f"[alert_notifications] Contenu invalide pour '{username}' — repli sur liste vide")
        return []


def unseen_count(username: str) -> int:
    return sum(1 for n in list_notifications(username) if not n.get("seen"))


def add_notification(username: str, saved_search_id: str, saved_search_name: str, new_count: int) -> dict | None:
    """Dépose une notification pour l'utilisateur. Tolérant : ne lève
    jamais — appelé depuis la boucle de fond d'alert_worker.py, une panne
    Redis ne doit faire échouer que cette notification, jamais tout le tick
    (voir update_alert_check(), même logique côté saved_searches.py)."""
    client = _get_redis_client()
    if client is None:
        return None

    notifications = _load(username)
    entry = {
        "id": uuid.uuid4().hex,
        "saved_search_id": saved_search_id,
        "saved_search_name": saved_search_name,
        "new_count": new_count,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "seen": False,
    }
    notifications.append(entry)
    # Garde uniquement les plus récentes — voir ALERT_HISTORY_LIMIT en tête
    # de fichier, ceci est un flux à traiter, pas un journal d'audit.
    notifications = sorted(notifications, key=lambda n: n["checked_at"], reverse=True)[:ALERT_HISTORY_LIMIT]
    client.set(KEY_PREFIX + username, json.dumps(notifications))
    return entry


def mark_seen(username: str, notif_id: str) -> list[dict]:
    """Marque une notification comme lue. Idempotent : un id déjà lu ou
    absent ne lève pas d'erreur, la liste est simplement retournée telle quelle."""
    client = _get_redis_client()
    if client is None:
        return []
    notifications = _load(username)
    for n in notifications:
        if n.get("id") == notif_id:
            n["seen"] = True
    client.set(KEY_PREFIX + username, json.dumps(notifications))
    return sorted(notifications, key=lambda n: n["checked_at"], reverse=True)


def mark_all_seen(username: str) -> list[dict]:
    client = _get_redis_client()
    if client is None:
        return []
    notifications = _load(username)
    for n in notifications:
        n["seen"] = True
    client.set(KEY_PREFIX + username, json.dumps(notifications))
    return sorted(notifications, key=lambda n: n["checked_at"], reverse=True)
