# custom_keywords.py — Mots-clés personnalisés ajoutés/retirés par les utilisateurs
#
# Contrairement à saved_collections.py, ce n'est PAS une donnée personnelle
# par utilisateur : une surcharge de mots-clés est attachée au DOCUMENT
# lui-même et visible de tous ceux qui y ont accès (mêmes règles ACL que la
# lecture du document — voir _check_doc_access() dans search_api.py).
#
# Stockage : index ES dédié (CUSTOM_KEYWORDS_INDEX), un document par
# fichier, indexé sous le même id que le document principal (doc_id) — pas
# de scan nécessaire pour retrouver la surcharge d'un document donné.
#
# "added"/"removed" plutôt qu'une simple liste finale : le contenu réel
# (mots-clés extraits par Tika) change à chaque réindexation du fichier
# (édition, ./manage.sh init) — voir indexer.py:apply_keyword_overrides(),
# qui réapplique added/removed par-dessus les mots-clés fraîchement
# extraits à CHAQUE indexation. Une simple liste finale serait écrasée par
# la prochaine extraction Tika sans distinction entre "vient du fichier" et
# "ajouté à la main".
#
# Exclusion mutuelle : un mot-clé ne peut jamais être à la fois dans added
# et removed — ajouter annule un retrait précédent (et inversement), ce qui
# donne un "annuler" naturel en rappelant l'opération inverse.
#
# Écritures avec refresh=True (forcé, pas "wait_for") : l'utilisateur doit
# voir l'effet de son ajout/retrait immédiatement. "wait_for" attendrait le
# PROCHAIN rafraîchissement planifié au lieu d'en déclencher un tout de
# suite — sans conséquence sur un index à l'intervalle par défaut (1s),
# mais potentiellement plusieurs dizaines de secondes si cet index est un
# jour optimisé pour un import en masse (voir le même choix, pour une
# raison bien réelle cette fois, dans search_api.py:add_document_keyword).

import os
import logging
from datetime import datetime, timezone

from elasticsearch import Elasticsearch, NotFoundError

logger = logging.getLogger(__name__)

CUSTOM_KEYWORDS_INDEX = os.getenv("CUSTOM_KEYWORDS_INDEX", "custom_keywords")

_index_ready = False


def _ensure_index(es: Elasticsearch) -> None:
    global _index_ready
    if _index_ready:
        return
    if not es.indices.exists(index=CUSTOM_KEYWORDS_INDEX):
        es.indices.create(index=CUSTOM_KEYWORDS_INDEX, body={
            "mappings": {
                "properties": {
                    "doc_id":     {"type": "keyword"},
                    "source":     {"type": "keyword"},
                    "added":      {"type": "keyword"},
                    "removed":    {"type": "keyword"},
                    "updated_by": {"type": "keyword"},
                    "updated_at": {"type": "date"},
                }
            }
        })
        logger.info(f"Index '{CUSTOM_KEYWORDS_INDEX}' créé.")
    _index_ready = True


def _unavailable() -> RuntimeError:
    return RuntimeError(
        "Elasticsearch injoignable — impossible d'enregistrer les mots-clés "
        "personnalisés. Vérifiez que le service elasticsearch tourne."
    )


def _get_entry(es: Elasticsearch, doc_id: str) -> dict:
    try:
        return es.get(index=CUSTOM_KEYWORDS_INDEX, id=doc_id)["_source"]
    except NotFoundError:
        return {"added": [], "removed": []}
    except Exception:
        raise _unavailable()


def _save(es: Elasticsearch, doc_id: str, source: str | None,
          added: list[str], removed: list[str], username: str) -> None:
    _ensure_index(es)
    try:
        es.index(
            index=CUSTOM_KEYWORDS_INDEX, id=doc_id, refresh=True,
            document={
                "doc_id":     doc_id,
                "source":     source,
                "added":      added,
                "removed":    removed,
                "updated_by": username,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception:
        raise _unavailable()


def add_keyword(es: Elasticsearch, doc_id: str, source: str | None, keyword: str, username: str) -> None:
    """Idempotent : ajouter un mot-clé déjà présent dans "added" ne fait
    rien de plus. Un mot-clé précédemment retiré ("removed") en est
    simplement ôté — annule le retrait plutôt que de créer un état
    incohérent où il serait à la fois ajouté et retiré."""
    entry = _get_entry(es, doc_id)
    removed = [k for k in entry.get("removed", []) if k != keyword]
    added = entry.get("added", [])
    if keyword not in added:
        added = added + [keyword]
    _save(es, doc_id, source, added, removed, username)


def remove_keyword(es: Elasticsearch, doc_id: str, source: str | None, keyword: str, username: str) -> None:
    """Symétrique de add_keyword() : si le mot-clé avait été ajouté à la
    main, on annule simplement cet ajout plutôt que de le marquer en plus
    comme retiré (il n'a jamais fait partie des mots-clés extraits par
    Tika, "removed" n'a de sens que pour masquer un mot-clé du fichier)."""
    entry = _get_entry(es, doc_id)
    added = entry.get("added", [])
    removed = entry.get("removed", [])
    if keyword in added:
        added = [k for k in added if k != keyword]
    elif keyword not in removed:
        removed = removed + [keyword]
    _save(es, doc_id, source, added, removed, username)
