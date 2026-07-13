# saved_searches.py — Recherches enregistrées par utilisateur
#
# Permet à un utilisateur de sauvegarder une recherche (requête + filtres
# actifs) sous un nom, et de la retrouver plus tard sans avoir à
# reconstruire ses critères. Purement un confort utilisateur — sans
# rapport avec l'indexation, donc pas de copie synchronisée côté
# docsearch-ingestion (contrairement à filetype_config.py/runtime_config.py).
#
# Stockage : une clé Redis par utilisateur ("docsearch:saved_searches:{user}")
# contenant la liste JSON de ses recherches enregistrées.
#
# Chaque entrée peut aussi porter une alerte (alert_enabled/alert_frequency/
# last_alert_check, gérés via set_alert()/update_alert_check()) — voir
# alert_worker.py, qui rejoue périodiquement les critères pour détecter les
# nouveaux documents, et alert_notifications.py, qui stocke le résultat.

import os
import json
import uuid
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
KEY_PREFIX = "docsearch:saved_searches:"

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
            logger.warning(f"[saved_searches] Redis injoignable ({e})")
            _redis_unavailable_logged = True
        _redis_client = None
        return None


def _require_client():
    client = _get_redis_client()
    if client is None:
        raise RuntimeError(
            "Redis injoignable — impossible d'enregistrer/consulter les "
            "recherches sauvegardées. Vérifiez que le service redis tourne "
            "(docker compose ps redis)."
        )
    return client


def list_saved(username: str) -> list[dict]:
    """Retourne les recherches sauvegardées d'un utilisateur, la plus
    récente en premier. Liste vide (pas d'exception) si Redis est
    injoignable — un utilisateur sans recherches sauvegardées n'est pas
    une erreur, juste un cas de repli identique à celui de Redis en panne."""
    client = _get_redis_client()
    if client is None:
        return []
    raw = client.get(KEY_PREFIX + username)
    if not raw:
        return []
    try:
        return sorted(json.loads(raw), key=lambda s: s["created_at"], reverse=True)
    except (json.JSONDecodeError, KeyError):
        logger.warning(f"[saved_searches] Contenu invalide pour '{username}' — repli sur liste vide")
        return []


def save_search(username: str, name: str, criteria: dict) -> dict:
    """Ajoute une recherche à la liste de l'utilisateur et la persiste
    immédiatement dans Redis. Lève une exception si Redis est injoignable
    (une sauvegarde doit être fiable, pas de sens à "faire semblant")."""
    client = _require_client()
    raw = client.get(KEY_PREFIX + username)
    saved = json.loads(raw) if raw else []

    entry = {
        "id": uuid.uuid4().hex,
        "name": name.strip(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        **criteria,
        # Alerte désactivée par défaut (voir alert_worker.py) — last_alert_check
        # reste vide tant qu'aucune alerte n'a jamais été activée, set_alert()
        # le fixe à l'activation.
        "alert_enabled": False,
        "alert_frequency": "daily",
        "last_alert_check": None,
    }
    saved.append(entry)
    client.set(KEY_PREFIX + username, json.dumps(saved))
    return entry


def delete_saved(username: str, search_id: str) -> list[dict]:
    """Retire une recherche sauvegardée par id. Idempotent : un id déjà
    absent ne lève pas d'erreur, la liste est simplement inchangée."""
    client = _require_client()
    raw = client.get(KEY_PREFIX + username)
    saved = json.loads(raw) if raw else []
    saved = [s for s in saved if s.get("id") != search_id]
    client.set(KEY_PREFIX + username, json.dumps(saved))
    return saved


def set_alert(username: str, search_id: str, enabled: bool, frequency: str) -> dict:
    """Active/désactive l'alerte d'une recherche sauvegardée, ou change sa
    fréquence. Remet systématiquement last_alert_check à maintenant plutôt
    que de garder l'ancienne valeur : sans ça, une recherche qui matche déjà
    des milliers de documents existants déclencherait une notification
    massive dès le premier tick après (ré)activation, sur des documents qui
    n'ont en réalité rien de "nouveau". Lève KeyError si l'id est inconnu."""
    client = _require_client()
    raw = client.get(KEY_PREFIX + username)
    saved = json.loads(raw) if raw else []
    for s in saved:
        if s.get("id") == search_id:
            s["alert_enabled"] = enabled
            s["alert_frequency"] = frequency
            s["last_alert_check"] = datetime.now(timezone.utc).isoformat()
            client.set(KEY_PREFIX + username, json.dumps(saved))
            return s
    raise KeyError(search_id)


def update_alert_check(username: str, search_id: str, checked_at: str) -> None:
    """Appelé par alert_worker.py après chaque vérification (qu'elle ait
    trouvé de nouveaux résultats ou non) pour avancer le curseur — sans ça,
    les mêmes documents "nouveaux" seraient re-signalés à chaque tick.
    Échec silencieux si Redis est injoignable : un tick de vérification
    manqué n'est pas fatal, il sera retenté au suivant."""
    client = _get_redis_client()
    if client is None:
        return
    raw = client.get(KEY_PREFIX + username)
    saved = json.loads(raw) if raw else []
    for s in saved:
        if s.get("id") == search_id:
            s["last_alert_check"] = checked_at
            break
    client.set(KEY_PREFIX + username, json.dumps(saved))


def list_users_with_saved_searches() -> list[str]:
    """Noms des utilisateurs ayant au moins une recherche sauvegardée —
    utilisé par alert_worker.py pour savoir qui parcourir. Pas d'index
    séparé : un simple scan des clés Redis existantes, cohérent avec le
    reste du module (une clé par utilisateur, rien d'autre)."""
    client = _get_redis_client()
    if client is None:
        return []
    return [key[len(KEY_PREFIX):] for key in client.scan_iter(match=KEY_PREFIX + "*")]
