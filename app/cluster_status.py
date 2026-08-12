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

# Blocages d'index qui font échouer une écriture, et ce qu'ils racontent
# (voir check_suggestions). Elasticsearch pose lui-même le premier au
# franchissement du flood-stage watermark ; les deux autres demandent une
# intervention explicite.
WRITE_BLOCK_SETTINGS = {
    "index.blocks.read_only_allow_delete":
        "disque saturé (flood-stage watermark) — Elasticsearch a passé l'index en lecture seule",
    "index.blocks.read_only": "index placé en lecture seule",
    "index.blocks.write":     "écritures bloquées sur l'index",
}


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
        return {
            "alive": alive,
            "last_seen_seconds_ago": round(age, 1),
            # Identité de l'image d'ingestion, écrite dans le battement
            # par watcher.py. Absente d'un battement laissé par une
            # version antérieure du watcher, d'où les .get().
            "version":    data.get("version"),
            "commit":     data.get("commit"),
            "build_date": data.get("build_date"),
        }
    except Exception as e:
        return {"alive": False, "error": str(e)}


def _check_write_blocks(index: str, *, rien_recu: str) -> dict:
    """Cet index accepte-t-il encore les écritures ?

    Sondé ACTIVEMENT, à la différence de search_log.health() qui rapporte
    le résultat de la dernière écriture réelle. Le rythme n'a rien de
    comparable : il se fait une recherche par minute, une suggestion ou
    une réponse NPS par semaine dans un bon mois. Un état déduit de la
    dernière écriture réelle afficherait donc « actif » pendant toutes
    les semaines qui suivent le blocage, c'est-à-dire exactement pendant
    la panne.

    Rien d'autre ne la signale : log_suggestion() et log_nps() avalent
    leur exception (c'est leur contrat), /suggestions et /nps répondent
    200, et l'interface remercie l'utilisateur dont le message vient
    d'être perdu.

    Ce qui est regardé est le BLOCAGE lui-même, pas sa cause : le
    flood-stage watermark (disque à 95 %) pose
    `index.blocks.read_only_allow_delete`, mais un `index.blocks.write`
    posé à la main produit exactement le même silence, cluster « green »
    compris.

    `rien_recu` : ce que veut dire un index absent pour CE canal-là — il
    naît à la première écriture reçue, son absence n'est donc pas une
    panne mais l'absence de toute contribution.
    """
    try:
        r = httpx.get(
            f"{ES_HOST}/{index}/_settings/index.blocks.*",
            params={"flat_settings": "true"}, timeout=5,
        )
        if r.status_code == 404:
            # Ne rien savoir n'est pas une panne — même raisonnement que
            # search_log.health(), le panneau l'affiche en neutre.
            return {"ok": None, "index": index, "reason": rien_recu}
        r.raise_for_status()
        # Réponse vide ({}) tant qu'aucun blocage n'est posé ; sinon une
        # entrée par index CONCRET, d'où le parcours des valeurs plutôt
        # qu'un accès par nom — la variable d'environnement pourrait
        # désigner un alias. Les valeurs sont rendues en chaînes ("true").
        blocages = sorted({
            libelle
            for reglages in r.json().values()
            for cle, libelle in WRITE_BLOCK_SETTINGS.items()
            if str(reglages.get("settings", {}).get(cle, "")).lower() == "true"
        })
        if blocages:
            return {"ok": False, "index": index, "error": " ; ".join(blocages)}
        return {"ok": True, "index": index}
    except Exception as e:
        # Elasticsearch injoignable : sa propre carte le dit déjà en rouge,
        # inutile d'en allumer une seconde pour la même panne.
        return {"ok": None, "index": index, "reason": str(e)}


# Les deux canaux par lesquels un utilisateur envoie quelque chose sans
# jamais savoir si c'est arrivé. Ils tombent ensemble — même blocage
# d'index — mais chacun a sa carte : réparer le disque sans savoir que
# des idées ET des notes ont été perdues n'est pas la même information.
def check_suggestions() -> dict:
    """Recueil des suggestions libres — voir _check_write_blocks()."""
    import suggestion_log

    return _check_write_blocks(
        suggestion_log.SUGGESTION_LOG_INDEX,
        rien_recu="aucune suggestion reçue à ce jour (index pas encore créé)",
    )


def check_nps() -> dict:
    """Réponses à la question de satisfaction — voir _check_write_blocks()."""
    import nps_log

    return _check_write_blocks(
        nps_log.NPS_LOG_INDEX,
        rien_recu="aucune réponse NPS reçue à ce jour (index pas encore créé)",
    )


def check_versions(watcher: dict) -> dict:
    """Identité des composants DocSearch déployés.

    L'API se décrit elle-même ; l'ingestion passe par le battement de
    cœur du watcher, seul processus d'ingestion dont l'API lise déjà
    l'état. Le TTL de 120 s du battement a un effet appréciable ici : la
    version affichée est nécessairement celle d'un processus VIVANT, un
    composant arrêté n'affiche rien plutôt qu'une valeur périmée.

    ⚠️  Portée réelle de la ligne « ingestion » : le watcher ne tourne que
    sur ingest-1 (voir quadlet/install-units.sh, --with-singletons). Les
    workers Kafka d'ingest-2 et ingest-3 partagent la même image mais se
    mettent à jour machine par machine : pendant une mise à jour rolling,
    cette ligne ne dit rien de leur version. L'interface le signale.

    L'interface web ne figure pas ici : elle n'a pas d'exécution côté
    serveur, sa version est figée dans son bundle au build et c'est elle
    qui la rapporte (voir docsearch-ui-vue/vite.config.ts).
    """
    import version

    composants = {"api": version.infos()}
    if watcher.get("version"):
        composants["ingestion"] = {
            "version":    watcher["version"],
            "commit":     watcher.get("commit"),
            "build_date": watcher.get("build_date"),
            "source":     "watcher (ingest-1)",
        }
    return composants


def get_full_status() -> dict:
    """Agrège l'état de tous les composants en un seul appel."""
    import search_log

    watcher = check_watcher_heartbeat()
    return {
        "elasticsearch": check_elasticsearch(),
        "redis":         check_redis(),
        "tika":          check_tika(),
        "kafka":         check_kafka_broker(),
        "workers":       check_workers_and_progress(),
        "watcher":       watcher,
        # Ne se déduit d'aucune des lignes ci-dessus : un cluster « green »
        # peut parfaitement refuser toute écriture (blocage read-only sur
        # dépassement du flood-stage watermark), et c'est justement le cas
        # qui a motivé cette carte. Voir search_log.health().
        "search_log":    search_log.health(),
        # Même angle mort que la ligne ci-dessus, autres symptômes : le
        # blocage en lecture seule fait aussi disparaître les suggestions
        # et les réponses NPS, sans que personne ne l'apprenne — ni
        # l'administrateur, ni l'utilisateur, que l'interface remercie
        # quand même.
        "suggestions":   check_suggestions(),
        "nps":           check_nps(),
        "versions":      check_versions(watcher),
    }
