# log_retention.py — Durée de conservation des journaux
#
# Cinq index de journalisation grandissaient jusqu'ici sans aucune limite :
# recherches, connexions, audit d'administration, réponses NPS et
# suggestions. Ni ILM, ni purge, rien. Deux problèmes distincts, et le
# second est le plus important :
#
# 1. Le disque. Au-delà du flood-stage watermark (95 %), Elasticsearch
#    passe ses index en LECTURE SEULE : le cluster reste « green », les
#    voyants du panneau d'administration restent au vert, et les avis, les
#    notes de satisfaction et les statistiques se perdent en silence.
#    C'est arrivé sur la VM de développement le 2026-08-10.
# 2. Des données personnelles conservées sans durée fixée. Ces index
#    portent un identifiant, le texte des recherches et une adresse IP.
#    Sur un service de l'État, la durée de conservation est une décision
#    qui doit être prise, écrite, et tenue — pas une conséquence de la
#    taille du disque.
#
# ── Ce que ce module ne touche JAMAIS ────────────────────────────────
#
# `custom_keywords` et `saved_collections` sont des DONNÉES UTILISATEUR,
# pas des traces : un mot-clé posé sur un document ou une collection n'a
# pas de raison d'expirer. La liste ci-dessous est donc explicite et
# close — jamais un motif du genre `*_logs`, qui emporterait un jour un
# index créé par quelqu'un d'autre.
#
# ── Pourquoi delete_by_query et pas ILM ──────────────────────────────
#
# ILM serait plus propre sur le principe, mais suppose de convertir ces
# index en flux de données ou en alias à rollover, donc de migrer
# l'existant d'une installation en service. Hors sujet pour un besoin qui
# se règle par une requête quotidienne. À reconsidérer le jour où l'un de
# ces index deviendra assez gros pour que la suppression par requête coûte.

import os
import logging
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

import runtime_config
import search_log
import nps_log
import suggestion_log
import audit_log

logger = logging.getLogger(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

# Marque du dernier passage. La durée de vie de la clé EST l'intervalle :
# tant qu'elle existe, le passage n'est pas dû. Pas de date à comparer,
# pas d'horloge à faire coïncider entre plusieurs exemplaires du worker.
VERROU_KEY = "docsearch:retention:dernier_passage"
INTERVALLE_SECONDES = int(os.getenv("RETENTION_INTERVAL_SECONDS", str(24 * 3600)))

# Plafond de suppression par passage et par journal. Le but n'est pas de
# tout rattraper d'un coup : une première purge sur une installation
# ancienne pourrait porter sur des millions de documents et occuper le
# cluster pendant des heures. Bornée, elle est prévisible, elle se
# raconte (« 100 000 supprimés »), et le reliquat part le lendemain.
MAX_DOCS_PAR_PASSAGE = int(os.getenv("RETENTION_MAX_DOCS", "100000"))

# Débit maximal demandé à Elasticsearch. Une purge non bridée se voit à
# l'écran des utilisateurs qui cherchent pendant ce temps.
DEBIT_MAX = float(os.getenv("RETENTION_REQUESTS_PER_SECOND", "1000"))


class Journal(NamedTuple):
    """Un index de journalisation et sa clé de réglage.

    `index` est une FONCTION et non une chaîne : le nom vient d'une
    constante de module que les tests remplacent par un index jetable, et
    qu'une variable d'environnement peut changer au démarrage. Résolu à
    l'appel, jamais figé à l'import.
    """
    cle: str
    libelle: str
    index: object   # Callable[[], str]


def _index_login_events() -> str:
    # Importé ici et non en tête : auth/config.py lit l'environnement à
    # l'import et tire toute la configuration d'authentification derrière
    # lui — inutile de l'imposer à qui importe ce module pour autre chose.
    from auth import config as auth_config
    return auth_config.LOGIN_EVENTS_INDEX


JOURNAUX: tuple[Journal, ...] = (
    Journal("retention_search_logs_days", "recherches", lambda: search_log.SEARCH_LOG_INDEX),
    Journal("retention_login_events_days", "connexions", _index_login_events),
    Journal("retention_audit_log_days", "audit d'administration", lambda: audit_log.AUDIT_LOG_INDEX),
    Journal("retention_nps_days", "réponses NPS", lambda: nps_log.NPS_LOG_INDEX),
    Journal("retention_suggestions_days", "suggestions", lambda: suggestion_log.SUGGESTION_LOG_INDEX),
)

# Tous ces index datent leurs documents du même champ. Vérifié : c'est le
# cas des cinq (search_log.py, auth/events.py, audit_log.py, nps_log.py,
# suggestion_log.py).
CHAMP_DATE = "timestamp"


def _jours(journal: Journal) -> int:
    """0 ou moins = conservation illimitée, et c'est écrit tel quel dans
    l'interface d'administration. Une valeur illisible vaut illimitée :
    sur un mécanisme qui supprime, le doute profite à la conservation."""
    try:
        return int(runtime_config.get_param(journal.cle))
    except (TypeError, ValueError):
        logger.warning(f"[retention] Valeur illisible pour {journal.cle} — conservation illimitée")
        return 0


def _limite(jours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=jours)).isoformat()


def _requete(limite: str) -> dict:
    return {"range": {CHAMP_DATE: {"lt": limite}}}


def apercu(es) -> list[dict]:
    """Ce que la purge ferait, sans rien supprimer — pour que
    l'administrateur puisse régler des durées en sachant ce qu'elles
    emportent. Un réglage destructeur qu'on ne peut pas prévisualiser ne
    se règle jamais, ou se règle une fois de trop."""
    rapport = []
    for journal in JOURNAUX:
        index = journal.index()
        jours = _jours(journal)
        ligne = {
            "cle": journal.cle,
            "libelle": journal.libelle,
            "index": index,
            "jours": jours,
            "total": 0,
            "expirés": 0,
        }
        try:
            ligne["total"] = es.count(index=index, ignore_unavailable=True)["count"]
            if jours > 0:
                ligne["expirés"] = es.count(
                    index=index, query=_requete(_limite(jours)), ignore_unavailable=True
                )["count"]
        except Exception as e:
            # Un index absent (fonctionnalité jamais utilisée) n'est pas
            # une erreur : la ligne reste à zéro.
            ligne["erreur"] = str(e)
        rapport.append(ligne)
    return rapport


def purger(es) -> list[dict]:
    """Supprime les documents expirés de chaque journal. Renvoie le
    compte par journal, qui part aussi dans le journal du service : une
    purge silencieuse de journaux est exactement ce qu'on ne veut pas."""
    rapport = []
    for journal in JOURNAUX:
        jours = _jours(journal)
        if jours <= 0:
            continue
        index = journal.index()
        try:
            res = es.delete_by_query(
                index=index,
                query=_requete(_limite(jours)),
                # Un document modifié entre la recherche et la suppression
                # (un avis « pouce » déposé sur une vieille recherche
                # pendant la purge) ne doit pas interrompre le lot.
                conflicts="proceed",
                max_docs=MAX_DOCS_PAR_PASSAGE,
                requests_per_second=DEBIT_MAX,
                # Pas de `slices` : ces index ont un seul shard, `auto`
                # n'en produirait qu'une de toute façon, et la combinaison
                # avec max_docs n'a pas la même sémantique selon les
                # versions.
                wait_for_completion=True,
                ignore_unavailable=True,
            )
        except Exception as e:
            logger.error(f"[retention] Échec de purge de {index} : {e}")
            rapport.append({"index": index, "libelle": journal.libelle, "erreur": str(e)})
            continue

        supprimes = res.get("deleted", 0)
        rapport.append({
            "index": index,
            "libelle": journal.libelle,
            "jours": jours,
            "supprimés": supprimes,
        })
        if supprimes:
            logger.info(
                f"[retention] {supprimes} document(s) supprimé(s) dans {index} "
                f"(au-delà de {jours} jours)"
            )
            # La purge du journal d'audit s'inscrit dans le journal
            # d'audit : c'est la trace qui protège l'administrateur, elle
            # ne peut pas être la seule à disparaître sans laisser de
            # trace. Le nom d'utilisateur est celui du mécanisme, aucune
            # personne n'ayant déclenché ce passage.
            if index == audit_log.AUDIT_LOG_INDEX:
                audit_log.log_action(
                    es,
                    username="système (rétention)",
                    method="DELETE",
                    path="/retention/admin_audit_log",
                    path_params={},
                    body={"jours": jours, "supprimés": supprimes},
                    status_code=200,
                )
    return rapport


# ── Ordonnancement ───────────────────────────────────────────

_redis_client = None
_redis_indisponible_signale = False


def _get_redis_client():
    global _redis_client, _redis_indisponible_signale
    if _redis_client is not None:
        return _redis_client
    try:
        import redis
        _redis_client = redis.Redis(
            host=REDIS_HOST, port=REDIS_PORT, decode_responses=True, socket_timeout=2
        )
        _redis_client.ping()
        return _redis_client
    except Exception as e:
        if not _redis_indisponible_signale:
            logger.warning(f"[retention] Redis injoignable ({e})")
            _redis_indisponible_signale = True
        _redis_client = None
        return None


def passage_du() -> bool:
    """Vrai une fois par intervalle, pour un seul appelant.

    `SET NX EX` fait les deux d'un coup : il marque le passage ET sert de
    verrou entre plusieurs exemplaires du worker, sans fenêtre entre le
    test et la pose.

    Redis injoignable → False : mieux vaut ne pas purger que purger à
    chaque tick faute de savoir quand a eu lieu le précédent.
    """
    client = _get_redis_client()
    if client is None:
        return False
    try:
        return bool(client.set(VERROU_KEY, datetime.now(timezone.utc).isoformat(),
                               nx=True, ex=INTERVALLE_SECONDES))
    except Exception as e:
        logger.warning(f"[retention] Impossible de lire le verrou ({e})")
        return False
