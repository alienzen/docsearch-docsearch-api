# ⚠️  COPIE de docsearch-ingestion/app/runtime_config.py. Ce qui doit rester
# identique entre les deux dépôts : la CLÉ REDIS et la LOGIQUE (lecture,
# cache, fusion, écriture). Toute modification de celles-ci est à
# répercuter dans les deux.
#
# DEFAULT_RUNTIME, en revanche, n'a pas à être identique : chaque dépôt y
# déclare les paramètres dont il est propriétaire, et tous se retrouvent
# dans la même clé Redis par fusion. Les réglages OCR appartiennent ainsi
# à l'ingestion, les poids de recherche à l'API — ni l'un ni l'autre n'a
# de sens de l'autre côté. Conséquence à connaître : set_param() refuse
# une clé absente du DEFAULT_RUNTIME local, chaque dépôt ne pouvant
# écrire que ce qu'il déclare.
#
# Dupliqué (plutôt qu'importé) car docsearch-api ne peut pas dépendre du
# code de docsearch-ingestion dans l'architecture multi-dépôts — Redis
# reste la seule source de vérité partagée.

# runtime_config.py — Paramètres opérationnels modifiables à chaud
#
# Complète filetype_config.py (dédié aux extensions/tailles) pour les
# autres réglages qui bénéficient d'être ajustables sans redémarrage :
# limites d'archives, cadence de flush du worker, intervalle de
# surveillance du watcher.
#
# Même principe : une clé Redis unique en JSON, cache local, repli sur
# les variables d'environnement (elles-mêmes avec valeur par défaut)
# si Redis est injoignable.
#
# Certains réglages ne peuvent pas être "vraiment" pris en compte sans
# petite action côté appelant (ex: le watcher doit redémarrer son
# observateur si watcher_poll_interval change, une Kafka
# max_poll_records ne peut pas changer sans recréer le consumer) —
# ces cas sont documentés au point d'usage plutôt qu'ici.

import os
import json
import time
import logging

logger = logging.getLogger(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
RUNTIME_CONFIG_KEY = "docsearch:config:runtime"
RUNTIME_CACHE_TTL  = int(os.getenv("RUNTIME_CONFIG_CACHE_TTL", "10"))

# Valeurs par défaut — reprennent les variables d'environnement
# existantes (elles-mêmes avec une valeur de repli) comme valeurs de
# départ. Une fois modifiés via set_param(), les réglages vivent dans
# Redis et les variables d'environnement ne servent plus que de valeur
# de repli si Redis est injoignable.
DEFAULT_RUNTIME = {
    "archive_max_files":         int(os.getenv("ARCHIVE_MAX_FILES", "5000")),
    "archive_max_total_size_mb": int(os.getenv("ARCHIVE_MAX_TOTAL_SIZE_MB", "1000")),
    "archive_max_depth":         int(os.getenv("ARCHIVE_MAX_DEPTH", "1")),
    "worker_batch_size":         int(os.getenv("WORKER_BATCH_SIZE", "200")),
    "worker_flush_interval":     int(os.getenv("WORKER_FLUSH_INTERVAL", "10")),
    "watcher_poll_interval":     int(os.getenv("WATCHER_POLL_INTERVAL", "10")),

    # Poids des champs dans le score de pertinence (voir field_sets() dans
    # search_query.py). Le bon réglage dépend entièrement du corpus : des
    # noms de fichiers parlants justifient un poids élevé, des mots-clés
    # bien renseignés aussi — d'où un réglage à chaud plutôt qu'en dur.
    #
    # DÉCLARÉS EN FLOAT à dessein : set_param() coerce via
    # type(DEFAULT_RUNTIME[key]), un défaut entier interdirait donc de
    # saisir 2.5.
    #
    # Ce sont des MULTIPLICATEURS du score BM25 du champ, pas des
    # pourcentages : le champ garde par ailleurs l'avantage que lui donne
    # sa brièveté (BM25 favorise les correspondances dans les champs
    # courts), ce qui explique qu'un titre ressorte déjà sans son ×4.
    # `content` et `author` restent à 1, non réglables : ce sont les
    # références auxquelles les autres se comparent.
    "search_boost_filename":     float(os.getenv("SEARCH_BOOST_FILENAME", "6")),
    "search_boost_title":        float(os.getenv("SEARCH_BOOST_TITLE", "4")),
    "search_boost_keywords":     float(os.getenv("SEARCH_BOOST_KEYWORDS", "2")),

    # Réglages OCR (Tesseract via Tika, voir indexer.py:_ocr_headers) —
    # GLOBAUX et non par source : le pack linguistique Tesseract est figé
    # dans l'image Tika pour tout le cluster, une langue par source
    # n'aurait donc pas de sens. L'ACTIVATION de l'OCR, elle, se fait par
    # source (voir file_sources_config.py:Source.ocr_enabled).
    # Type str (pas bool) : set_param() coerce via type(DEFAULT_RUNTIME[k]),
    # et bool("false") vaut True en Python — un piège qu'on évite en
    # gardant ces réglages en chaînes plutôt qu'en booléens.
    # Déclarés ici ALORS QUE l'ingestion en est propriétaire : sans cela,
    # set_param() les refuse et ils restent inaccessibles depuis le
    # panneau d'administration, que seule l'API sert.
    "ocr_languages":             os.getenv("OCR_LANGUAGES", "fra"),
    "ocr_strategy":              os.getenv("OCR_STRATEGY", "auto"),

    # Connexion automatique par ticket Kerberos/SPNEGO (voir
    # app/auth/kerberos.py). DÉSACTIVÉE par défaut, et l'interrupteur est
    # indispensable : sans lui, une installation sans keytab répondrait un
    # défi Negotiate que personne ne peut relever, à chaque chargement de
    # page. Type str et non bool, comme les réglages OCR ci-dessus —
    # set_param() coerce via type(DEFAULT_RUNTIME[k]) et bool("false")
    # vaut True en Python.
    "sso_kerberos_enabled":      os.getenv("SSO_KERBEROS_ENABLED", "false"),

    # Durée de conservation des journaux, en jours (voir log_retention.py,
    # appelé une fois par jour par alert_worker.py). 0 = conservation
    # illimitée, et c'est écrit tel quel dans le panneau d'administration.
    #
    # Ces cinq index grandissaient jusqu'ici sans limite. Les défauts ne
    # sont pas uniformes, parce que ces journaux ne servent pas à la même
    # chose : douze mois permettent une comparaison d'une année sur
    # l'autre, deux ans donnent une tendance de satisfaction, et le
    # journal d'audit — la trace qui protège l'administrateur — se garde
    # plus longtemps que ce qu'il trace.
    "retention_search_logs_days":   int(os.getenv("RETENTION_SEARCH_LOGS_DAYS", "365")),
    "retention_login_events_days":  int(os.getenv("RETENTION_LOGIN_EVENTS_DAYS", "365")),
    "retention_audit_log_days":     int(os.getenv("RETENTION_AUDIT_LOG_DAYS", "1095")),
    "retention_nps_days":           int(os.getenv("RETENTION_NPS_DAYS", "730")),
    "retention_suggestions_days":   int(os.getenv("RETENTION_SUGGESTIONS_DAYS", "730")),
}

# Bornes des poids de pertinence. Un poids nul ou négatif produirait un
# classement absurde — et silencieusement, le moteur acceptant la requête.
BOOST_MIN = 0.1
BOOST_MAX = 100.0

_cache: dict = {}
_cache_time: float = 0.0
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
            logger.warning(
                f"[runtime_config] Redis injoignable ({e}) — "
                f"repli sur la configuration par défaut (variables d'environnement)."
            )
            _redis_unavailable_logged = True
        _redis_client = None
        return None


def get_runtime_config() -> dict:
    """Retourne la config runtime — cache local, sinon Redis, sinon défaut."""
    global _cache, _cache_time

    now = time.time()
    if _cache and (now - _cache_time) < RUNTIME_CACHE_TTL:
        return _cache

    client = _get_redis_client()
    if client is not None:
        try:
            raw = client.get(RUNTIME_CONFIG_KEY)
            if raw:
                # Fusion avec les défauts : une clé absente de Redis
                # (nouveau paramètre ajouté après coup, par exemple)
                # retombe sur sa valeur par défaut plutôt que de planter.
                merged = dict(DEFAULT_RUNTIME)
                merged.update(json.loads(raw))
                _cache = merged
                _cache_time = now
                return _cache
        except Exception as e:
            logger.warning(f"[runtime_config] Erreur lecture Redis : {e} — repli sur défaut")

    _cache = dict(DEFAULT_RUNTIME)
    _cache_time = now
    return _cache


def get_param(key: str, default=None):
    """Raccourci pour lire un seul paramètre."""
    return get_runtime_config().get(key, default if default is not None else DEFAULT_RUNTIME.get(key))


def set_param(key: str, value) -> dict:
    """
    Modifie un paramètre et le persiste immédiatement dans Redis.
    Lève une exception si Redis est injoignable (une écriture doit
    être fiable, pas de sens à "faire semblant" d'avoir sauvegardé).
    """
    if key not in DEFAULT_RUNTIME:
        raise ValueError(
            f"Paramètre inconnu : '{key}'. Valeurs possibles : "
            f"{', '.join(DEFAULT_RUNTIME.keys())}"
        )

    client = _get_redis_client()
    if client is None:
        raise RuntimeError(
            "Redis injoignable — impossible d'enregistrer la configuration. "
            "Vérifiez que le service redis tourne (docker compose ps redis)."
        )

    raw = client.get(RUNTIME_CONFIG_KEY)
    config = dict(DEFAULT_RUNTIME)
    if raw:
        config.update(json.loads(raw))

    # Conserve le type d'origine (int/float) quand c'est possible,
    # pour éviter qu'une valeur saisie en chaîne casse les comparaisons
    # numériques (ex: len(buffer) >= "10" lèverait une exception).
    original_type = type(DEFAULT_RUNTIME[key])
    try:
        config[key] = original_type(value)
    except (TypeError, ValueError):
        config[key] = value

    # Les poids de pertinence sont bornés : hors de cet intervalle, la
    # requête reste valide pour Elasticsearch mais le classement n'a plus
    # de sens. Mieux vaut un refus explicite qu'un moteur qui semble
    # obéir tout en renvoyant n'importe quoi.
    if key.startswith("search_boost_"):
        boost = config[key]
        if not isinstance(boost, (int, float)) or not (BOOST_MIN <= boost <= BOOST_MAX):
            raise ValueError(
                f"Poids invalide pour '{key}' : {value!r} — attendu un nombre "
                f"entre {BOOST_MIN} et {BOOST_MAX}."
            )

    client.set(RUNTIME_CONFIG_KEY, json.dumps(config))

    global _cache, _cache_time
    _cache = config
    _cache_time = time.time()

    return config


def reset_to_default() -> dict:
    """
    Réinitialise tous les paramètres opérationnels à DEFAULT_RUNTIME,
    écrasant tout réglage modifié via set_param(). Utile pour revenir
    d'un coup à un état connu plutôt que de réajuster chaque paramètre
    un par un.
    """
    client = _get_redis_client()
    if client is None:
        raise RuntimeError(
            "Redis injoignable — impossible d'enregistrer la configuration. "
            "Vérifiez que le service redis tourne (docker compose ps redis)."
        )

    config = dict(DEFAULT_RUNTIME)
    client.set(RUNTIME_CONFIG_KEY, json.dumps(config))

    global _cache, _cache_time
    _cache = config
    _cache_time = time.time()

    return config
