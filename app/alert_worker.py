# alert_worker.py — Vérification périodique des recherches sauvegardées
# marquées "alerte" (voir saved_searches.py : alert_enabled/alert_frequency)
#
# Rejoue, pour chaque recherche sauvegardée avec alerte active, les mêmes
# critères qu'une recherche manuelle (search_query.py — voir son
# avertissement de cohérence avec /search), restreints aux documents
# indexés depuis la dernière vérification. Filtre sur `indexed_at` (date
# d'entrée dans l'index, présente sur tous les types de documents — fichier,
# SQL, web, email PST), PAS `date_modified` : ce dernier daterait un vieux
# fichier tout juste découvert par un scan, ce qui déclencherait une alerte
# sur un document qui n'a en réalité rien de "nouveau" pour l'utilisateur.
#
# Une notification in-app est déposée si au moins un nouveau document
# correspond (alert_notifications.py) — pas d'email, voir le README pour
# le choix (pas de brique SMTP dans DocSearch, et un email ferait sortir
# des titres de documents potentiellement confidentiels du périmètre ACL).
#
# Tourne dans son propre conteneur (docsearch-infra/docker-compose.yml,
# service alert-worker) plutôt que dans l'API elle-même : un tick qui
# traîne (beaucoup d'utilisateurs, ES lent) ne doit jamais ralentir
# /search. Même principe de boucle à tick que sql_worker.py/web_worker.py
# côté docsearch-ingestion : un `last_alert_check` par recherche plutôt
# qu'un cron externe, pour répartir les vérifications dans le temps sans
# dépendance à un ordonnanceur système.

import os
import time
import logging
from datetime import datetime, timezone

from elasticsearch import Elasticsearch

import saved_searches
import alert_notifications
from search_query import build_query_clauses
from file_sources_config import ES_SEARCH_ALIAS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ES_HOST = os.getenv("ES_HOST", "http://localhost:9200")
es = Elasticsearch(ES_HOST, retry_on_timeout=True, max_retries=3, request_timeout=60)

# Cadence à laquelle le worker se réveille pour regarder QUELLES alertes
# sont dues (pas la fréquence des alertes elles-mêmes, voir FREQUENCY_SECONDS
# ci-dessous) — un tick court coûte peu (une boucle Python + Redis, ES n'est
# interrogé que pour les alertes réellement dues) et fait qu'une alerte
# activée n'attend jamais plus de ALERT_WORKER_TICK_SECONDS avant sa
# première vérification possible.
TICK_SECONDS = int(os.getenv("ALERT_WORKER_TICK_SECONDS", "60"))

FREQUENCY_SECONDS = {
    "daily":  24 * 3600,
    "weekly": 7 * 24 * 3600,
}


def _check_one(username: str, entry: dict) -> None:
    """Vérifie une recherche sauvegardée et dépose une notification si
    des documents correspondants sont apparus depuis last_alert_check.
    Avance le curseur dans tous les cas (même 0 nouveau résultat) — voir
    update_alert_check()."""
    last_check = entry.get("last_alert_check") or entry["created_at"]
    query = build_query_clauses(entry, username)
    query["bool"]["filter"].append({"range": {"indexed_at": {"gt": last_check}}})

    now = datetime.now(timezone.utc).isoformat()
    try:
        count = es.count(index=ES_SEARCH_ALIAS, query=query)["count"]
    except Exception as e:
        # Une panne ES ponctuelle ne doit affecter QUE cette recherche —
        # ni faire planter le tick entier, ni avancer son last_alert_check
        # (retentée telle quelle au prochain tick dû).
        logger.warning(f"[alert_worker] Échec de vérification « {entry.get('name')} » ({username}) : {e}")
        return

    if count > 0:
        alert_notifications.add_notification(username, entry["id"], entry.get("name", ""), count)
        logger.info(f"[alert_worker] {count} nouveau(x) résultat(s) — « {entry.get('name')} » ({username})")

    saved_searches.update_alert_check(username, entry["id"], now)


def run_tick() -> None:
    for username in saved_searches.list_users_with_saved_searches():
        for entry in saved_searches.list_saved(username):
            if not entry.get("alert_enabled"):
                continue
            frequency = entry.get("alert_frequency", "daily")
            last_check = entry.get("last_alert_check") or entry["created_at"]
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last_check)).total_seconds()
            if elapsed >= FREQUENCY_SECONDS.get(frequency, FREQUENCY_SECONDS["daily"]):
                _check_one(username, entry)


if __name__ == "__main__":
    logger.info(f"Démarrage du worker d'alertes (tick={TICK_SECONDS}s)")
    while True:
        try:
            run_tick()
        except Exception as e:
            logger.error(f"[alert_worker] Erreur de tick : {e}")
        time.sleep(TICK_SECONDS)
