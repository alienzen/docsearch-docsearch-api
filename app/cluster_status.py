# cluster_status.py — État des composants DocSearch
#
# Vérifie la santé de chaque composant SANS accès Docker (docsearch-api
# n'a pas et ne doit pas avoir le socket Docker monté) : tout se fait
# via le réseau applicatif normal (HTTP, Redis, Kafka), exactement
# comme le ferait n'importe quel client.
#
# Point notable : le nombre de workers actifs et la progression de
# l'indexation en cours sont TOUS LES DEUX déduits du groupe de
# consumers Kafka "indexer-workers" — Kafka sait déjà combien de
# membres sont vivants dans le groupe, et le "lag" (messages publiés
# non encore traités) donne directement l'avancement de l'indexation.

import os
import time
import json
import logging
import httpx

logger = logging.getLogger(__name__)

ES_HOST         = os.getenv("ES_HOST", "http://localhost:9200")
REDIS_HOST      = os.getenv("REDIS_HOST", "redis")
REDIS_PORT      = int(os.getenv("REDIS_PORT", "6379"))
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "kafka:9092")
KAFKA_TOPIC     = os.getenv("KAFKA_TOPIC", "documents-to-index")
KAFKA_GROUP_ID  = "indexer-workers"
TIKA_SERVERS    = os.getenv("TIKA_SERVERS", "http://localhost:9998").split(",")

HEARTBEAT_KEY = "docsearch:heartbeat:watcher"
HEARTBEAT_STALE_AFTER = 60  # secondes — au-delà, watcher considéré "silencieux"


def check_elasticsearch() -> dict:
    try:
        r = httpx.get(f"{ES_HOST}/_cluster/health", timeout=5)
        r.raise_for_status()
        data = r.json()
        return {"up": True, "status": data.get("status"), "cluster_name": data.get("cluster_name")}
    except Exception as e:
        return {"up": False, "error": str(e)}


def check_redis() -> dict:
    try:
        import redis
        client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_connect_timeout=3, socket_timeout=3)
        client.ping()
        return {"up": True}
    except Exception as e:
        return {"up": False, "error": str(e)}


def check_tika() -> dict:
    results = []
    for server in TIKA_SERVERS:
        try:
            r = httpx.get(f"{server}/tika", timeout=5)
            results.append({"server": server, "up": r.status_code == 200})
        except Exception as e:
            results.append({"server": server, "up": False, "error": str(e)})
    up_count = sum(1 for r in results if r["up"])
    return {"up": up_count > 0, "instances": results, "up_count": up_count, "total": len(results)}


def check_kafka_broker() -> dict:
    try:
        from kafka import KafkaConsumer
        c = KafkaConsumer(bootstrap_servers=KAFKA_BOOTSTRAP, consumer_timeout_ms=3000)
        topics = c.topics()
        c.close()
        return {"up": True, "topic_exists": KAFKA_TOPIC in topics}
    except Exception as e:
        return {"up": False, "error": str(e)}


def check_workers_and_progress() -> dict:
    """
    Retourne à la fois le nombre de workers actifs (membres du groupe
    de consumers Kafka) et la progression de l'indexation (lag :
    messages publiés sur le topic mais pas encore traités par un worker).
    """
    try:
        from kafka import KafkaConsumer, KafkaAdminClient
        from kafka.structs import TopicPartition

        admin = KafkaAdminClient(bootstrap_servers=KAFKA_BOOTSTRAP)

        # Nombre de workers actifs dans le groupe
        active_workers = 0
        try:
            groups = admin.describe_consumer_groups([KAFKA_GROUP_ID])
            if groups:
                active_workers = len(groups[0].members)
        except Exception as e:
            logger.warning(f"[cluster_status] describe_consumer_groups a échoué : {e}")

        # Lag = somme sur toutes les partitions de (offset de fin - offset validé)
        consumer = KafkaConsumer(bootstrap_servers=KAFKA_BOOTSTRAP, group_id=None)
        partitions = consumer.partitions_for_topic(KAFKA_TOPIC)
        lag = None
        if partitions:
            tps = [TopicPartition(KAFKA_TOPIC, p) for p in partitions]
            end_offsets = consumer.end_offsets(tps)
            committed = admin.list_consumer_group_offsets(KAFKA_GROUP_ID)
            lag = 0
            for tp in tps:
                end = end_offsets.get(tp, 0)
                entry = committed.get(tp)
                current = entry.offset if entry and entry.offset is not None and entry.offset >= 0 else 0
                lag += max(0, end - current)
        consumer.close()
        admin.close()

        return {"active_workers": active_workers, "pending_documents": lag}
    except Exception as e:
        return {"active_workers": None, "pending_documents": None, "error": str(e)}


def check_watcher_heartbeat() -> dict:
    try:
        import redis
        client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_connect_timeout=3, socket_timeout=3)
        raw = client.get(HEARTBEAT_KEY)
        if not raw:
            return {"alive": False, "reason": "Aucun battement reçu (watcher jamais démarré, ou Redis vidé)"}
        data = json.loads(raw)
        age = time.time() - data["ts"]
        alive = age < HEARTBEAT_STALE_AFTER
        return {"alive": alive, "last_seen_seconds_ago": round(age, 1)}
    except Exception as e:
        return {"alive": False, "error": str(e)}


def get_full_status() -> dict:
    """Agrège l'état de tous les composants en un seul appel."""
    return {
        "elasticsearch": check_elasticsearch(),
        "redis":         check_redis(),
        "tika":          check_tika(),
        "kafka":         check_kafka_broker(),
        "workers":       check_workers_and_progress(),
        "watcher":       check_watcher_heartbeat(),
    }
