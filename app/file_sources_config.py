# file_sources_config.py — Registre dynamique des sources d'indexation (fichiers)
#
# Port de docsearch-ingestion/app/file_sources_config.py — dupliqué à
# l'identique ici (impossible d'importer un autre dépôt dans
# l'architecture multi-dépôts, même rationale que admin_scan.py). Ce
# module est totalement autonome (Redis + variables d'environnement,
# aucune dépendance vers le reste de l'ingestion) : il peut être copié
# tel quel entre les deux dépôts sans adaptation.
#
# Une "source fichier" = un sous-dossier de SOURCES_MOUNT indexé vers son
# propre index Elasticsearch — à distinguer des sources SQL
# (sql_sources_config.py) et web (web_sources_config.py), disponibles en
# plus depuis l'ajout de ces connecteurs. Permet d'ajouter un nouveau
# répertoire à indexer sans reconstruire ni redémarrer aucun conteneur :
# le registre vit dans Redis (même principe que path_filter.py /
# filetype_config.py / runtime_config.py), watcher/worker/producer le
# relisent à chaud.
#
# Contrainte Docker : un bind-mount est fixé à la création du conteneur —
# impossible d'en monter un nouveau dans un conteneur déjà démarré. C'est
# pourquoi TOUTES les sources vivent sous UN SEUL point de montage parent
# (SOURCES_MOUNT, ex: /sources) : ajouter une source revient à créer un
# sous-dossier de ce parent + l'enregistrer ici, jamais à modifier le
# montage lui-même (voir docsearch-infra/.env.example pour la migration
# unique DOCS_PATH -> SOURCES_ROOT à faire avant la première utilisation).
#
# Stockage (clé Redis "docsearch:config:file_sources" — migrée depuis
# l'ancien nom "docsearch:config:sources" en même temps que le
# renommage sources_config.py -> file_sources_config.py, pour être
# cohérente avec "docsearch:config:sql_sources"/"docsearch:config:web_sources") :
#   {"documents": {"subfolder": "documents", "es_index": "documents", "label": "Documents", "searchable": true},
#    "finance":   {"subfolder": "finance",   "es_index": "finance_docs", "label": "Finance", "searchable": true}}
#
# "searchable" (défaut true) n'affecte QUE la visibilité dans /search
# (docsearch-api) — une source non cherchable continue d'être surveillée
# et indexée normalement par watcher/worker/producer, elle disparaît
# seulement des résultats de recherche. Voir set_searchable().
#
# Repli par défaut (Redis injoignable ou clé absente) : une seule source
# "documents", dérivée des variables d'environnement ES_INDEX/DEFAULT_SOURCE_SUBFOLDER
# — une installation mono-source existante continue de fonctionner sans
# qu'il soit nécessaire d'enregistrer quoi que ce soit.

import os
import re
import json
import time
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
SOURCES_KEY = "docsearch:config:file_sources"
SOURCES_CACHE_TTL = int(os.getenv("SOURCES_CONFIG_CACHE_TTL", "10"))

# Point de montage fixe (identique dans tous les conteneurs) sous lequel
# vivent tous les sous-dossiers sources.
SOURCES_MOUNT = os.getenv("SOURCES_MOUNT", "/sources")

DEFAULT_SOURCE_NAME = "documents"

# Nom de sous-dossier / index de la source historique — reprend les
# anciennes variables d'environnement mono-source pour que les
# installations existantes n'aient rien à enregistrer manuellement.
_DEFAULT_SUBFOLDER = os.getenv("DEFAULT_SOURCE_SUBFOLDER", "documents")
_DEFAULT_ES_INDEX  = os.getenv("ES_INDEX", "documents")

# Alias ES partagé par toutes les sources — c'est ce qui permet à la
# recherche fédérée (docsearch-api) de voir automatiquement tout nouvel
# index créé, sans étape de configuration séparée côté API.
ES_SEARCH_ALIAS = os.getenv("ES_SEARCH_ALIAS", "docsearch-all")

# Nom de source/index valides : alphanumérique + tiret/underscore,
# jamais vide — évite qu'un nom de source finisse comme composant d'une
# clé Redis ou d'un nom d'index ES avec des caractères piégeux.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Styles de mise en page des résultats disponibles pour cette source dans
# l'interface de recherche (index.html:renderResults) — noms fixes, mais
# CONTENU éditable à chaud depuis l'admin (voir docsearch-api/
# display_styles_config.py, GET/POST /admin/display-styles). Voir
# set_display_style().
DISPLAY_STYLES = {"default", "compact", "minimal", "dense", "essentiel", "complet_sans_extrait"}

DEFAULT_SOURCES = {
    DEFAULT_SOURCE_NAME: {
        "subfolder":   _DEFAULT_SUBFOLDER,
        "es_index":    _DEFAULT_ES_INDEX,
        "label":       "Documents",
        "searchable":  True,
        "collectable": True,
        "description": "",
        "display_style": "default",
    }
}


@dataclass(frozen=True)
class Source:
    name: str
    es_index: str
    folder: str    # chemin absolu résolu (SOURCES_MOUNT/subfolder)
    label: str
    searchable: bool
    collectable: bool = True
    description: str = ""
    display_style: str = "default"
    # Active l'OCR (Tesseract via Tika, voir indexer.py:_ocr_headers) pour
    # les PDF scannés et images (jpg/png/...) de cette source — désactivé
    # par défaut car coûteux en CPU, à activer explicitement pour les
    # sources qui en ont réellement besoin (voir set_ocr_enabled()).
    ocr_enabled: bool = False


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
                f"[file_sources_config] Redis injoignable ({e}) — "
                f"repli sur la source unique par défaut ('{DEFAULT_SOURCE_NAME}')."
            )
            _redis_unavailable_logged = True
        _redis_client = None
        return None


def _raw_sources() -> dict:
    """Retourne le dict brut {name: {subfolder, es_index, label}} —
    cache local, sinon Redis, sinon la source par défaut seule."""
    global _cache, _cache_time

    now = time.time()
    if _cache and (now - _cache_time) < SOURCES_CACHE_TTL:
        return _cache

    client = _get_redis_client()
    if client is not None:
        try:
            raw = client.get(SOURCES_KEY)
            if raw:
                sources = json.loads(raw)
                if DEFAULT_SOURCE_NAME not in sources:
                    # La source par défaut est toujours disponible, même
                    # si elle n'a jamais été explicitement enregistrée.
                    sources[DEFAULT_SOURCE_NAME] = dict(DEFAULT_SOURCES[DEFAULT_SOURCE_NAME])
                _cache = sources
                _cache_time = now
                return _cache
        except Exception as e:
            logger.warning(f"[file_sources_config] Erreur lecture Redis : {e} — repli sur défaut")

    _cache = dict(DEFAULT_SOURCES)
    _cache_time = now
    return _cache


def _to_source(name: str, entry: dict) -> Source:
    subfolder = entry.get("subfolder", "") or ""
    folder = str((Path(SOURCES_MOUNT) / subfolder).resolve()) if subfolder else str(Path(SOURCES_MOUNT).resolve())
    return Source(
        name=name,
        es_index=entry["es_index"],
        folder=folder,
        label=entry.get("label") or name,
        searchable=entry.get("searchable", True),
        collectable=entry.get("collectable", True),
        description=entry.get("description") or "",
        display_style=entry.get("display_style") or "default",
        ocr_enabled=entry.get("ocr_enabled", False),
    )


def get_sources() -> dict[str, Source]:
    """Retourne toutes les sources enregistrées, {name: Source}."""
    return {name: _to_source(name, entry) for name, entry in _raw_sources().items()}


def get_source(name: str) -> Source:
    """Retourne une source par son nom. Lève KeyError si inconnue —
    l'appelant doit décider explicitement du repli (ex: source par
    défaut) plutôt qu'échouer silencieusement sur un nom mal orthographié."""
    raw = _raw_sources()
    if name not in raw:
        raise KeyError(
            f"Source inconnue : '{name}'. Sources disponibles : {', '.join(raw.keys())}"
        )
    return _to_source(name, raw[name])


def _validate_name(name: str, label: str) -> None:
    if not _NAME_RE.match(name):
        raise ValueError(
            f"{label} invalide : '{name}' — attendu : lettres minuscules, "
            f"chiffres, '-' ou '_', commençant par une lettre/chiffre."
        )


def _validate_subfolder(subfolder: str) -> str:
    """Vérifie que le sous-dossier résout bien SOUS SOURCES_MOUNT (pas de
    traversée de chemin type '../..') et retourne sa forme normalisée."""
    mount_root = Path(SOURCES_MOUNT).resolve()
    candidate = (mount_root / subfolder).resolve()
    if candidate != mount_root and mount_root not in candidate.parents:
        raise ValueError(
            f"Sous-dossier invalide : '{subfolder}' sort de SOURCES_MOUNT ({mount_root})"
        )
    return str(candidate.relative_to(mount_root)) if candidate != mount_root else ""


def _read_write(mutate) -> dict:
    client = _get_redis_client()
    if client is None:
        raise RuntimeError(
            "Redis injoignable — impossible d'enregistrer la configuration. "
            "Vérifiez que le service redis tourne (docker compose ps redis)."
        )
    raw = client.get(SOURCES_KEY)
    sources = json.loads(raw) if raw else dict(DEFAULT_SOURCES)
    sources.setdefault(DEFAULT_SOURCE_NAME, dict(DEFAULT_SOURCES[DEFAULT_SOURCE_NAME]))

    mutate(sources)

    client.set(SOURCES_KEY, json.dumps(sources))
    global _cache, _cache_time
    _cache = sources
    _cache_time = time.time()
    return sources


def add_source(
    name: str, es_index: str, subfolder: str | None = None, label: str | None = None,
    searchable: bool = True, collectable: bool = True, description: str | None = None,
    ocr_enabled: bool = False,
) -> dict:
    """
    Enregistre une nouvelle source (ou met à jour une source existante du
    même nom). `subfolder` défaut au nom de la source elle-même — créer
    `$SOURCES_ROOT/<name>` sur l'hôte avant (ou après) cet appel, l'ordre
    n'a pas d'importance pour le registre, seul le premier scan/passage
    watcher a besoin que le dossier existe réellement sur disque.

    ATTENTION : cette fonction REMPLACE entièrement l'entrée existante
    (pas de fusion partielle) — un appelant qui réenregistre une source
    déjà configurée doit relire `searchable`/`collectable`/`ocr_enabled`
    au préalable (voir /admin/all-sources et /admin/file-sources) et les
    repasser explicitement, sous peine de réinitialiser ces bascules à
    leur valeur par défaut (True/True/False).
    """
    _validate_name(name, "Nom de source")
    _validate_name(es_index, "Nom d'index Elasticsearch")
    subfolder = _validate_subfolder(subfolder if subfolder is not None else name)

    def mutate(sources):
        for other_name, other in sources.items():
            if other_name != name and other.get("es_index") == es_index:
                raise ValueError(
                    f"L'index '{es_index}' est déjà utilisé par la source '{other_name}'."
                )
        sources[name] = {
            "subfolder":   subfolder,
            "es_index":    es_index,
            "label":       label or name,
            "searchable":  searchable,
            "collectable": collectable,
            "description": description or "",
            "ocr_enabled": ocr_enabled,
        }

    return _read_write(mutate)


def set_searchable(name: str, searchable: bool) -> dict:
    """
    Active/désactive la RECHERCHE pour une source, sans toucher à
    l'ingestion : watcher/worker/producer continuent de surveiller et
    d'indexer cette source normalement, seuls ses documents cessent
    d'apparaître dans /search (docsearch-api). Utile pour mettre une
    source en pause côté recherche (ex: données en cours de validation)
    sans interrompre l'indexation en arrière-plan.
    """
    def mutate(sources):
        if name not in sources:
            raise KeyError(
                f"Source inconnue : '{name}'. Sources disponibles : {', '.join(sources.keys())}"
            )
        sources[name]["searchable"] = searchable

    return _read_write(mutate)


def set_collectable(name: str, collectable: bool) -> dict:
    """
    Active/désactive l'ajout DES DOCUMENTS de cette source à une
    collection ("Mes collections", voir saved_collections.py) — sans
    effet sur l'ingestion ni sur la recherche elle-même : la source
    reste indexée et cherchable normalement, seule l'action "Ajouter à
    une collection" devient refusée (403) pour ses documents (voir
    add_collection_document() dans search_api.py).
    """
    def mutate(sources):
        if name not in sources:
            raise KeyError(
                f"Source inconnue : '{name}'. Sources disponibles : {', '.join(sources.keys())}"
            )
        sources[name]["collectable"] = collectable

    return _read_write(mutate)


def set_ocr_enabled(name: str, ocr_enabled: bool) -> dict:
    """
    Active/désactive l'OCR (Tesseract via Tika) pour cette source — sans
    effet sur les documents déjà indexés (pas de réextraction
    automatique : un fichier déjà indexé sans OCR ne redevient cherchable
    par son contenu OCRisé qu'au prochain passage watcher/scan qui le
    modifie, ou via une réindexation explicite de la source).
    """
    def mutate(sources):
        if name not in sources:
            raise KeyError(
                f"Source inconnue : '{name}'. Sources disponibles : {', '.join(sources.keys())}"
            )
        sources[name]["ocr_enabled"] = ocr_enabled

    return _read_write(mutate)


def set_display_style(name: str, display_style: str) -> dict:
    """
    Change le style d'affichage des résultats de cette source dans
    l'interface de recherche (voir DISPLAY_STYLES) — n'affecte ni
    l'ingestion ni la recherche elle-même, purement une préférence de
    présentation résolue côté index.html via GET /searchable-sources.
    """
    if display_style not in DISPLAY_STYLES:
        raise ValueError(
            f"Style d'affichage invalide : '{display_style}'. "
            f"Valeurs possibles : {', '.join(sorted(DISPLAY_STYLES))}"
        )

    def mutate(sources):
        if name not in sources:
            raise KeyError(
                f"Source inconnue : '{name}'. Sources disponibles : {', '.join(sources.keys())}"
            )
        sources[name]["display_style"] = display_style

    return _read_write(mutate)


def remove_source(name: str) -> dict:
    """
    Retire une source du registre — le watcher arrête d'observer son
    dossier, producer/scan ne peuvent plus la cibler par ce nom. NE
    SUPPRIME NI l'index Elasticsearch ni les documents déjà indexés
    (cohérent avec exclude-path : purge_path reste l'outil explicite,
    destructif et confirmé, pour nettoyer l'existant).
    """
    if name == DEFAULT_SOURCE_NAME:
        raise ValueError(f"Impossible de retirer la source par défaut ('{DEFAULT_SOURCE_NAME}').")

    def mutate(sources):
        sources.pop(name, None)

    return _read_write(mutate)


def set_label(name: str, label: str) -> dict:
    """
    Modifie le LIBELLÉ d'affichage d'une source, sans toucher à son nom
    (clé de registre), son index ES ni son dossier — contrairement à
    l'ancien rename_source(), le nom qui identifie la source dans le
    registre et dans le champ "source" des documents déjà indexés ne
    change jamais, donc aucune répercussion sur l'index ES n'est
    nécessaire ici.
    """
    if not label.strip():
        raise ValueError("Le libellé ne peut pas être vide.")

    def mutate(sources):
        if name not in sources:
            raise KeyError(f"Source inconnue : '{name}'")
        sources[name]["label"] = label.strip()

    return _read_write(mutate)


def set_description(name: str, description: str) -> dict:
    """Modifie la description d'une source (texte libre, affiché dans
    l'admin — n'affecte ni l'ingestion ni la recherche)."""

    def mutate(sources):
        if name not in sources:
            raise KeyError(f"Source inconnue : '{name}'")
        sources[name]["description"] = description.strip()

    return _read_write(mutate)
