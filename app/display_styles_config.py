# display_styles_config.py — Composition des gabarits d'affichage des
# résultats de recherche, éditable à chaud depuis l'admin.
#
# Propre à docsearch-api (pas de copie dans docsearch-ingestion, qui ne
# rend jamais rien) — distinct de {file,sql,web}_sources_config.py :
# DISPLAY_STYLES (ces trois modules) fixe la liste des NOMS de style
# disponibles pour assigner UNE source à un style ; ce module-ci définit
# ce que chaque style nommé affiche RÉELLEMENT (quels champs, dépliable ou
# non), pour TOUTES les sources qui l'utilisent à la fois.
#
# Volontairement pas de HTML libre éditable : une liste déclarative de
# champs choisis dans un catalogue fixe (ALLOWED_FIELDS). Le balisage HTML
# et l'échappement (escapeHtml() côté index.html) restent dans le code —
# un gabarit éditable en HTML brut serait un vecteur d'injection JS pour
# TOUS les utilisateurs de la recherche (contenu interpolé issu de
# métadonnées de fichiers non fiables, et/ou compte admin compromis).
#
# Même principe que ui_config.py (clé Redis unique en JSON, cache local,
# repli sur des valeurs par défaut si Redis injoignable, fusion avec les
# défauts pour qu'un style ou un champ ajouté après coup au catalogue
# retombe sur sa valeur par défaut si absent de Redis).

import os
import json
import time
import logging

logger = logging.getLogger(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
DISPLAY_STYLES_KEY = "docsearch:config:display_styles"
DISPLAY_STYLES_CACHE_TTL = int(os.getenv("DISPLAY_STYLES_CACHE_TTL", "10"))

# Champs de métadonnée qu'un gabarit peut choisir d'afficher — voir
# renderCard() côté index.html, qui interprète ces noms. "source",
# "acl_group" et "filepath" affichent des blocs déjà présents dans le
# gabarit historique (badge source, premier groupe ACL, ligne de chemin
# avec boutons copier) ; "snippet" est l'extrait surligné du contenu.
# "telephone" n'a de sens que pour des sources SQL qui mappent une colonne
# vers ce champ (ex: source "agents") — inoffensif pour les autres types
# de source, renderCard() n'affiche le bloc que si le résultat porte
# réellement une valeur (même motif que "folder"/"acl_group", absents sur
# la plupart des documents sans que ça ne casse rien).
ALLOWED_FIELDS = {"source", "author", "date_modified", "folder", "size", "acl_group", "filepath", "snippet", "telephone"}

# Mêmes 6 noms que DISPLAY_STYLES dans {file,sql,web}_sources_config.py —
# dupliqué ici volontairement (ce module ne dépend d'aucun des trois, et
# eux ne dépendent pas de celui-ci) plutôt que de créer un couplage entre
# des modules qui n'ont sinon aucune raison de se connaître.
DEFAULT_STYLE_DEFINITIONS = {
    "default":              {"fields": sorted(ALLOWED_FIELDS), "expandable": True},
    "compact":              {"fields": ["source"], "expandable": False},
    "minimal":              {"fields": [], "expandable": False},
    "dense":                {"fields": ["source", "author", "date_modified"], "expandable": False},
    "essentiel":            {"fields": ["author", "date_modified"], "expandable": True},
    "complet_sans_extrait": {"fields": sorted(ALLOWED_FIELDS - {"snippet"}), "expandable": True},
}

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
                f"[display_styles_config] Redis injoignable ({e}) — "
                f"repli sur la composition par défaut des gabarits."
            )
            _redis_unavailable_logged = True
        _redis_client = None
        return None


def _merge_with_defaults(stored: dict) -> dict:
    """Fusionne la configuration lue depuis Redis avec
    DEFAULT_STYLE_DEFINITIONS, style par style ET champ par champ à
    l'intérieur de chaque style — un style ou une clé ("fields"/
    "expandable") absente de Redis (ajoutée au catalogue après coup, ou
    jamais enregistrée) retombe sur sa valeur par défaut plutôt que de
    disparaître ou de faire échouer la fusion."""
    merged = {}
    for style, default in DEFAULT_STYLE_DEFINITIONS.items():
        entry = dict(default)
        entry.update(stored.get(style, {}))
        merged[style] = entry
    return merged


def get_style_definitions() -> dict:
    """Retourne {style_name: {fields: [...], expandable: bool}} pour les
    6 styles — cache local, sinon Redis, sinon défaut (comportement
    identique aux gabarits historiques renderCardDefault/renderCardCompact)."""
    global _cache, _cache_time

    now = time.time()
    if _cache and (now - _cache_time) < DISPLAY_STYLES_CACHE_TTL:
        return _cache

    client = _get_redis_client()
    if client is not None:
        try:
            raw = client.get(DISPLAY_STYLES_KEY)
            stored = json.loads(raw) if raw else {}
            _cache = _merge_with_defaults(stored)
            _cache_time = now
            return _cache
        except Exception as e:
            logger.warning(f"[display_styles_config] Erreur lecture Redis : {e} — repli sur défaut")

    _cache = dict(DEFAULT_STYLE_DEFINITIONS)
    _cache_time = now
    return _cache


def set_style_definition(style_name: str, fields: list[str], expandable: bool) -> dict:
    """Modifie la composition d'un style (quels champs, dépliable ou non)
    et la persiste immédiatement dans Redis. Lève ValueError si le style
    ou un champ demandé sont inconnus — mêmes garde-fous que
    set_display_style() dans les registres de sources, pour la même
    raison (éviter qu'une faute de frappe crée silencieusement une
    entrée jamais utilisée par aucune source)."""
    if style_name not in DEFAULT_STYLE_DEFINITIONS:
        raise ValueError(
            f"Style inconnu : '{style_name}'. Valeurs possibles : "
            f"{', '.join(sorted(DEFAULT_STYLE_DEFINITIONS.keys()))}"
        )
    unknown = set(fields) - ALLOWED_FIELDS
    if unknown:
        raise ValueError(
            f"Champ(s) invalide(s) : {', '.join(sorted(unknown))}. "
            f"Valeurs possibles : {', '.join(sorted(ALLOWED_FIELDS))}"
        )

    client = _get_redis_client()
    if client is None:
        raise RuntimeError(
            "Redis injoignable — impossible d'enregistrer la configuration. "
            "Vérifiez que le service redis tourne (docker compose ps redis)."
        )

    raw = client.get(DISPLAY_STYLES_KEY)
    stored = json.loads(raw) if raw else {}
    stored[style_name] = {"fields": fields, "expandable": bool(expandable)}
    client.set(DISPLAY_STYLES_KEY, json.dumps(stored))

    global _cache, _cache_time
    _cache = _merge_with_defaults(stored)
    _cache_time = time.time()

    return _cache
