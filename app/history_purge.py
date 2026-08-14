# history_purge.py — Quand l'utilisateur veut vider son historique
#
# Deux listes personnelles se lisent dans `search_logs` : « Mes recherches
# récentes » et « Vos derniers documents consultés » (voir
# user_history.py). Ce module porte de quoi les vider, chacune de son
# côté.
#
# Les deux gestes RÉÉCRIVENT LE JOURNAL, et aucun des deux ne se contente
# plus de masquer (2026-08-14). Mais ils ne le réécrivent pas de la même
# façon — l'un anonymise, l'autre supprime — et cet écart, qui n'est pas
# une inconséquence, est tout le sujet de cet en-tête : il tient à ce que
# le clic est imbriqué DANS la recherche, quand la recherche, elle, ne
# l'est dans rien.
#
# ── Les recherches : une anonymisation (2026-08-14) ──────────────────
#
# Effacer ses recherches ÉCRIT DANS LE JOURNAL. Ce qui désigne
# NOMMÉMENT leur auteur en est ôté — nom d'utilisateur, adresse IP — et
# tout le reste demeure : le texte cherché, le nombre de résultats, les
# temps, l'avis pouce, les clics, et les groupes (voir
# CHAMPS_NOMINATIFS, qui dit pourquoi ceux-là restent). La recherche
# continue donc de compter dans les statistiques de l'installation, par
# service compris ; elle ne nomme plus personne.
#
# Anonymiser plutôt que supprimer, pour deux raisons :
#
# 1. Le même document porte les statistiques d'administration, l'avis
#    pouce déposé sur la recherche et la trace d'exploitation. Un
#    utilisateur qui range son écran décide de ce qui le désigne, pas de
#    la comptabilité de l'installation.
# 2. La durée de conservation est déjà une décision prise, écrite et
#    tenue ailleurs : log_retention.py supprime pour de bon, au terme
#    réglé dans l'administration. Une seconde voie de suppression, celle-là
#    à la main de chacun, brouillerait ce qui est censé être un délai.
#
# ⚠️ C'est IRRÉVERSIBLE : rien ne permet de rattacher après coup une
# recherche anonymisée à qui l'a lancée. C'est le but, et l'interface le
# dit avant de le faire.
#
# ⚠️ Conséquence assumée, elle aussi dite à l'écran : les clics sont
# imbriqués (`nested`) DANS le document de la recherche qui les a
# produits. Anonymiser ses recherches détache donc du même geste les
# documents ouverts depuis, et « Vos derniers documents consultés » perd
# ce qui précède l'effacement. On ne peut pas rendre une recherche anonyme
# en gardant nominatif le clic qu'elle porte : ce serait le même document
# désignant toujours la même personne. Ce point était, jusqu'à cette
# date, la première raison de ne rien réécrire du tout ; le choix est
# maintenant l'inverse, et il se paie ici.
#
# ── Les documents consultés : une suppression du détail ──────────────
#
# Vider CETTE liste-là écrit aussi dans le journal, mais autrement, et le
# mot juste n'est pas « anonymiser » : les clics antérieurs sont
# SUPPRIMÉS du document de leur recherche — quel document, à quelle
# heure, à quelle position — et il n'en reste que le NOMBRE, dans
# `clicks_erased`.
#
# Pourquoi pas la même anonymisation qu'au-dessus : ce qui rattache un
# clic à quelqu'un n'est pas dans le clic, c'est le `username` de la
# recherche qui le contient. L'anonymiser emporterait donc l'historique
# de recherche — que l'utilisateur n'a PAS demandé d'effacer. Entre
# détruire ce qu'il demande d'effacer et détruire ce qu'il ne demande
# pas, le choix est fait : le dommage ne doit pas dépasser la demande.
# La contrepartie du geste inverse, elle, est inévitable (voir plus
# haut) — on peut retirer un clic d'une recherche nommée, on ne peut pas
# nommer un clic dans une recherche anonyme.
#
# Ce qui reste est donc un compte, et il a sa raison d'être : « cette
# recherche a mené à trois consultations » est le signal d'engagement que
# l'installation lit vraiment (colonne « Clics », export). Effacer sans
# rien laisser aurait fait passer ces recherches pour infructueuses, ce
# qui est faux — un journal qui ment par omission ne vaut pas mieux
# qu'un écran qui ment.
#
# ⚠️ Irréversible là aussi : ni le document ouvert ni la date ne se
# reconstituent.
#
# ── Les marqueurs, en second rideau ──────────────────────────────────
#
# Les deux dates restent posées et lues, mais elles ne sont plus le
# mécanisme : elles couvrent les effacements demandés AVANT cette
# version (sans quoi un historique vidé hier reparaîtrait après mise à
# jour) et l'événement journalisé à la seconde près, encore invisible du
# moteur au moment de la réécriture.
#
# Stockage : une clé Redis par utilisateur, comme saved_searches.py —
# `docsearch:historique_purge:{user}` → {"searches": iso, "documents": iso}.

import os
import json
import logging
from datetime import datetime, timezone

# Le module, pas la constante : `search_log.SEARCH_LOG_INDEX` est relu à
# chaque appel, comme dans user_history.py — un test fait ainsi porter
# écriture, lecture et anonymisation sur le même index jetable en ne
# patchant qu'un seul endroit.
import search_log

logger = logging.getLogger(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
KEY_PREFIX = "docsearch:historique_purge:"

# Les deux listes, effaçables séparément — un utilisateur peut vouloir
# effacer ce qu'il a cherché sans effacer ce qu'il a ouvert.
RECHERCHES = "searches"
DOCUMENTS = "documents"
CLES = (RECHERCHES, DOCUMENTS)

# Ce qui, dans un document de `search_logs`, NOMME une personne — et rien
# d'autre :
#   username : le compte, évidemment ;
#   ip       : un poste de travail, donc son occupant.
#
# `groups` est délibérément absent de cette liste (arbitré le
# 2026-08-14) : un groupe est un service, pas quelqu'un, et les
# répartitions par service — volumes, avis, taux de résultats nuls —
# sont une lecture que l'installation utilise vraiment. L'ôter les
# aurait vidées de toutes les recherches effacées.
#
# ⚠️ Réserve à connaître, et déjà écrite dans l'aide des statistiques :
# aucun effectif minimum n'est appliqué aux répartitions par groupe. Dans
# un service très restreint, un groupe et une requête singulière peuvent
# suffire à resserrer sur une personne. L'anonymisation ôte le nom, elle
# ne fabrique pas un anonymat statistique — l'écran ne promet donc que ce
# qu'elle fait.
#
# Le texte cherché n'en est pas non plus : c'est ce que l'installation
# garde, et ce qui fait toute la valeur du journal une fois anonymisé.
CHAMPS_NOMINATIFS = ("username", "ip")

_SCRIPT_ANONYMISATION = "".join(f"ctx._source.remove('{champ}');" for champ in CHAMPS_NOMINATIFS)

# Suppression du détail des clics antérieurs, et report de leur nombre
# dans `clicks_erased` (voir search_log.py).
#
# Comparaison de dates EN TEXTE : tous les horodatages de clic viennent
# d'un seul endroit (`POST /click`, datetime.now(timezone.utc).isoformat())
# et la borne est écrite pareil, donc l'ordre lexicographique est l'ordre
# chronologique. Parser en ZonedDateTime coûterait une exception — donc
# tout l'effacement — sur un horodatage abîmé.
#
# Un clic SANS date est effacé lui aussi : il ne peut pas exister, mais
# s'il existait, le garder laisserait une consultation rattachée à un
# nom. Dans le doute, on efface — c'est le sens de la demande.
_SCRIPT_CONSULTATIONS = (
    "if (ctx._source.clicks != null) {"
    "  int avant = ctx._source.clicks.size();"
    "  ctx._source.clicks.removeIf(clic ->"
    "    clic.timestamp == null || clic.timestamp.compareTo(params.jusqu_a) <= 0);"
    "  int efface = avant - ctx._source.clicks.size();"
    "  if (efface > 0) {"
    "    def deja = ctx._source.clicks_erased;"
    "    ctx._source.clicks_erased = (deja == null ? 0 : deja) + efface;"
    "  }"
    "}"
)

# Passes de réécriture. Un document mis à jour PENDANT l'opération (un
# clic, un avis pouce) fait échouer la sienne en conflit de version ; la
# passe suivante la reprend. Deux suffisent : le conflit suppose une
# action à la seconde près, et la seconde passe ne porte plus que sur les
# quelques documents manqués. Au-delà, mieux vaut le dire à l'utilisateur
# que boucler devant lui.
PASSES_REECRITURE = 2

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
            logger.warning(f"[history_purge] Redis injoignable ({e})")
            _redis_unavailable_logged = True
        _redis_client = None
        return None


def _marqueurs(username: str) -> dict:
    client = _get_redis_client()
    if client is None:
        return {}
    try:
        raw = client.get(KEY_PREFIX + username)
        return json.loads(raw) if raw else {}
    except Exception as e:
        # Contenu invalide comme panne de lecture : repli sur « jamais
        # purgé », pour la raison dite dans purge_le().
        logger.warning(f"[history_purge] Marqueurs illisibles pour '{username}' : {e}")
        return {}


def purge_le(username: str, cle: str) -> str | None:
    """Date du dernier effacement de cette liste, ou None s'il n'y en a
    jamais eu.

    Redis injoignable → None, donc historique complet. C'est le repli
    voulu : la panne d'un cache ne doit pas faire disparaître ses
    recherches à quelqu'un qui n'a rien demandé. L'inverse — cacher par
    précaution — donnerait un historique vide sans explication, et le
    ferait revenir tout seul plus tard.

    Ce repli est devenu inoffensif : aucune des deux listes ne dépend
    plus de ce marqueur pour se vider. Les recherches anonymisées ne
    portent plus le nom sur lequel user_history filtre, et les clics
    effacés n'existent plus. Le marqueur ne couvre que les effacements
    demandés avant cette version, et l'événement encore invisible du
    moteur au moment de la réécriture.
    """
    return _marqueurs(username).get(cle)


def purger(username: str, cle: str, quand: str | None = None) -> str:
    """Pose la date d'effacement (maintenant par défaut) et la renvoie.

    `quand` sert aux deux effacements, qui doivent poser EXACTEMENT la
    borne de leur réécriture : une date même légèrement postérieure
    masquerait un événement survenu entre les deux sans l'avoir effacé —
    le pire des deux mondes.

    Lève RuntimeError si Redis est injoignable. Les appelants décident
    quoi en faire : purger_recherches() et purger_documents() n'en font
    qu'un avertissement au journal, la réécriture ayant déjà eu lieu.

    Ne s'appelle plus seule : c'est le second rideau, pas l'effacement.
    """
    if cle not in CLES:
        raise ValueError(f"Liste inconnue : {cle}")
    client = _get_redis_client()
    if client is None:
        raise RuntimeError(
            "Redis injoignable — impossible d'effacer l'historique. "
            "Vérifiez que le service redis tourne."
        )
    marqueurs = _marqueurs(username)
    marqueurs[cle] = quand or datetime.now(timezone.utc).isoformat()
    client.set(KEY_PREFIX + username, json.dumps(marqueurs))
    return marqueurs[cle]


def _reecrire(es, username: str, quoi: str, query: dict, script: dict) -> int:
    """Applique `script` à tout ce que `query` désigne, et renvoie le
    nombre de documents réécrits. Mécanique commune aux deux effacements.

    L'index est rafraîchi AVANT d'agir. Le journal s'écrit sans `refresh`
    (c'est un journal, pas une base de travail, voir search_log.py) et
    `_update_by_query` ne voit que ce qui est déjà cherchable : la
    recherche lancée — ou le document ouvert — juste avant le clic sur
    « Effacer », souvent ce qu'on veut voir partir en premier, y
    échapperait.

    Lève RuntimeError si le journal n'a pas pu être réécrit, `quoi`
    nommant l'opération dans le message rendu à l'utilisateur. Annoncer
    un effacement qu'on n'a pas obtenu serait le plus coûteux des
    mensonges d'écran : l'intéressé croirait ses traces détachées de son
    nom alors qu'elles le portent encore.
    """
    index = search_log.SEARCH_LOG_INDEX
    reecrits = 0
    try:
        if not es.indices.exists(index=index):
            # Rien n'a jamais été journalisé (installation neuve, journal
            # emporté par la rétention) : il n'y a rien à effacer, et ce
            # n'est pas un échec.
            return 0
        es.indices.refresh(index=index)
        for _ in range(PASSES_REECRITURE):
            res = es.update_by_query(
                index=index,
                query=query,
                script=script,
                refresh=True,
                # Un document en conflit est SAUTÉ, pas repris : sans
                # cela, un seul clic simultané interromprait toute
                # l'opération en laissant le reste à moitié fait. Les
                # sautés sont comptés, et la passe suivante les rattrape.
                conflicts="proceed",
            )
            reecrits += res.get("updated", 0)
            if not res.get("version_conflicts"):
                logger.info(
                    f"[history_purge] {quoi} pour '{username}' : {reecrits} document(s) réécrit(s)"
                )
                return reecrits
    except Exception as e:
        logger.error(f"[history_purge] {quoi} impossible pour '{username}' : {e}")
        raise RuntimeError(
            f"Le journal n'a pas pu être réécrit — {quoi} n'a pas eu lieu. "
            "Réessayez dans un instant."
        ) from e
    logger.error(
        f"[history_purge] {quoi} incomplète pour '{username}' : "
        f"{reecrits} document(s) réécrit(s), conflits persistants"
    )
    raise RuntimeError(
        f"Le journal a changé pendant {quoi} : elle est incomplète, et une partie de vos "
        "traces porte encore votre nom. Réessayez dans un instant."
    )


def anonymiser_recherches(es, username: str, jusqu_a: str) -> int:
    """Ôte des recherches de `username` antérieures à `jusqu_a` ce qui le
    nomme (CHAMPS_NOMINATIFS). Renvoie le nombre de recherches
    anonymisées.

    `jusqu_a` borne l'opération à ce qui précède la demande : une
    recherche lancée APRÈS l'effacement appartient à l'historique neuf et
    doit rester lisible par son auteur.
    """
    return _reecrire(
        es, username, "l'anonymisation de vos recherches",
        query={"bool": {"filter": [
            {"term": {"username": username}},
            {"range": {"timestamp": {"lte": jusqu_a}}},
        ]}},
        script={"source": _SCRIPT_ANONYMISATION},
    )


def effacer_consultations(es, username: str, jusqu_a: str) -> int:
    """Supprime des recherches de `username` le détail des clics
    antérieurs à `jusqu_a`, en reportant leur nombre dans
    `clicks_erased`. Renvoie le nombre de recherches réécrites.

    La borne porte sur la date du CLIC et non sur celle de la recherche :
    c'est ce que cette liste-là raconte. Un document ouvert ce matin
    depuis une recherche du mois dernier s'efface donc, et la recherche
    ne bouge pas.

    Le filtre `nested` n'est pas un raffinement : sans lui, toutes les
    recherches de l'utilisateur seraient réécrites, y compris celles sans
    le moindre clic à effacer — un coût inutile, et un décompte de
    documents réécrits qui ne voudrait plus rien dire.
    """
    return _reecrire(
        es, username, "l'effacement de vos consultations",
        query={"bool": {"filter": [
            {"term": {"username": username}},
            {"nested": {
                "path": "clicks",
                "query": {"range": {"clicks.timestamp": {"lte": jusqu_a}}},
            }},
        ]}},
        script={"source": _SCRIPT_CONSULTATIONS, "params": {"jusqu_a": jusqu_a}},
    )


def _effacer(es, username: str, cle: str, reecriture) -> str:
    """Réécrit le journal, puis pose le marqueur. Renvoie l'instant de
    l'effacement.

    Dans cet ordre, et pas l'inverse : la réécriture est ce que
    l'utilisateur a demandé, le marqueur n'en est que le second rideau.
    Poser la date d'abord viderait sa liste à l'écran même si le journal
    résistait — soit exactement l'effacement de façade dont ces routes ne
    se contentent plus.

    Marqueur en MEILLEUR EFFORT : ce que la réécriture a emporté ne
    remonte déjà plus dans les listes de l'intéressé (plus de nom sur les
    recherches, plus de clic sur les consultations). Redis muet ne remet
    donc rien à l'écran, et lever ici annoncerait un échec à quelqu'un
    dont les traces sont bel et bien parties — irréversiblement.
    """
    instant = datetime.now(timezone.utc).isoformat()
    reecriture(es, username, instant)
    try:
        purger(username, cle, quand=instant)
    except RuntimeError as e:
        logger.warning(
            f"[history_purge] Journal réécrit pour '{username}' ({cle}), "
            f"marqueur non posé : {e}"
        )
    return instant


def purger_recherches(es, username: str) -> str:
    """Efface les recherches de l'utilisateur : anonymisation du journal,
    puis marqueur."""
    return _effacer(es, username, RECHERCHES, anonymiser_recherches)


def purger_documents(es, username: str) -> str:
    """Efface les documents consultés de l'utilisateur : suppression du
    détail des clics, puis marqueur."""
    return _effacer(es, username, DOCUMENTS, effacer_consultations)
