# ui_config.py — Bascules de fonctionnalités d'interface, suspendables
# depuis l'admin (distinct d'engagement_config.py, dédié à la mesure de
# satisfaction — celui-ci couvre des éléments d'interface sans rapport,
# comme la visibilité du lien "Assistant IA").
#
# Propre à docsearch-api (pas de copie dans docsearch-ingestion) : ces
# réglages ne concernent que l'interface de recherche et l'API qui la
# sert, aucun processus d'ingestion n'en a besoin.
#
# Même principe que engagement_config.py (clé Redis unique en JSON,
# cache local, repli sur des valeurs par défaut si Redis est injoignable,
# booléens purs validés côté FastAPI avant d'arriver ici).

import os
import json
import time
import logging

logger = logging.getLogger(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
UI_CONFIG_KEY = "docsearch:config:ui"
UI_CONFIG_CACHE_TTL = int(os.getenv("UI_CONFIG_CACHE_TTL", "10"))

DEFAULT_UI_CONFIG = {
    "chat_enabled":        True,   # lien "Assistant IA" dans l'en-tête de recherche
    "footer_enabled":      True,   # pied de page des pages "recherche" (index.html, help.html)
    "footer_enabled_admin": True,   # pied de page des pages "administration" (admin.html,
                                    # stats.html, admin-help.html) — bascule séparée de
                                    # footer_enabled, même principe que les paires
                                    # show_current_user_enabled/_admin ci-dessous.
    "admin_links_enabled": True,   # liens "Administration"/"Statistiques" (en-tête + pied de page) —
                                    # combiné en ET logique avec /is-admin côté index.html : un
                                    # utilisateur admin ne les voit QUE si ce flag est aussi actif.
    "export_enabled":      True,   # boutons "Exporter en XLSX/DOCX" des résultats de recherche —
                                    # ne bloque que l'affichage côté index.html ; POST /search/export
                                    # reste refusé côté API si ce flag est désactivé (voir search_api.py).
    "help_enabled":        True,   # lien "❓ Aide" dans l'en-tête de recherche + raccourci clavier "?" —
                                    # ne bloque pas l'accès direct à /help.html (page statique sans
                                    # donnée sensible), seulement sa mise en avant dans l'UI.
    "collections_enabled": True,   # sélection de documents + collections personnelles ("📋 Mes
                                    # collections") — désactivé, TOUTES les routes /collections
                                    # renvoient 403 (voir search_api.py) : les collections déjà
                                    # créées restent dans leur index ES, simplement inaccessibles
                                    # tant que le flag est désactivé.
    "custom_keywords_enabled": True,   # ajout/retrait de mots-clés personnalisés sur les documents
                                        # de type fichier (voir custom_keywords.py) — désactivé, les
                                        # routes POST/DELETE /document/{id}/keywords renvoient 403.
                                        # Les surcharges déjà enregistrées restent appliquées par le
                                        # pipeline d'ingestion (docsearch-ingestion ne consulte jamais
                                        # ce flag — seule la création/modification depuis l'UI est
                                        # concernée, pas la réapplication à l'indexation).
    "alerts_enabled":      True,   # alertes sur recherches sauvegardées ("🔔 Alertes" dans
                                    # l'en-tête + bascule par recherche, voir saved_searches.py/
                                    # alert_worker.py) — désactivé, les routes /alerts* et
                                    # PATCH /saved-searches/{id}/alert renvoient 403. Le worker
                                    # d'arrière-plan continue de tourner mais ne fait plus rien
                                    # d'utile (aucune alerte n'est plus activable), et les
                                    # notifications déjà déposées restent lisibles nulle part
                                    # tant que le flag est désactivé (l'UI qui les affiche est
                                    # elle-même masquée).
    "sort_enabled":        True,   # sélecteur "Trier par" au-dessus des résultats de recherche —
                                    # purement une préférence d'affichage, pas de contrôle d'accès
                                    # associé côté API (contrairement à export/collections/alerts) :
                                    # désactivé, la recherche reste triée par pertinence par défaut.
    "show_current_user_enabled": True,   # badge "Connecté : <utilisateur> · <groupes>" dans l'en-tête
                                    # de la page de RECHERCHE (voir index.html:current-user) —
                                    # purement un affichage, aucun contrôle d'accès associé : le
                                    # contrôle d'accès réel se fait via access_auth.py/ACCESS_GROUP,
                                    # pas ici. Indépendant de show_current_user_enabled_admin
                                    # ci-dessous, qui couvre le même badge sur admin.html.
    "show_current_user_groups_enabled": True,   # inclut " · <groupes>" dans ce même badge (recherche) —
                                    # indépendant de show_current_user_enabled (qui masque le badge
                                    # entier) : permet d'afficher juste "Connecté : <utilisateur>"
                                    # sans exposer l'appartenance aux groupes LDAP. Sans effet si
                                    # l'utilisateur n'a aucun groupe (rien à masquer).
    "show_current_user_enabled_admin": True,   # même badge que ci-dessus, mais sur admin.html —
                                    # bascule séparée : l'admin peut par exemple le garder visible en
                                    # administration (utile pour savoir qui a fait quoi) tout en le
                                    # masquant sur la page de recherche, ou l'inverse.
    "show_current_user_groups_enabled_admin": True,   # inclut " · <groupes>" dans le badge d'admin.html —
                                    # indépendant de show_current_user_groups_enabled (recherche) et
                                    # de show_current_user_enabled_admin (qui masque le badge entier).
    "theme": "default",   # thème visuel des pages "recherche" (index.html, help.html) — voir
                            # theme-search.css : chaque valeur correspond à un bloc
                            # ":root[data-theme=...]" qui redéfinit les mêmes variables CSS. Une
                            # seule valeur pour toute l'installation (pas de préférence par
                            # utilisateur), indépendante de theme_admin ci-dessous.
    "theme_admin": "default",   # même principe que theme, mais pour les pages "administration"
                            # (admin.html, stats.html, admin-help.html) et theme-admin.css —
                            # permet par exemple un thème sombre en administration sans l'imposer
                            # aux utilisateurs de la recherche, ou l'inverse.
}

THEMES = ["default", "dark", "slate", "contrast", "red", "green"]

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
                f"[ui_config] Redis injoignable ({e}) — "
                f"repli sur la configuration par défaut (tout activé)."
            )
            _redis_unavailable_logged = True
        _redis_client = None
        return None


def get_config() -> dict:
    """Retourne {chat_enabled} — cache local, sinon Redis, sinon défaut
    (tout activé)."""
    global _cache, _cache_time

    now = time.time()
    if _cache and (now - _cache_time) < UI_CONFIG_CACHE_TTL:
        return _cache

    client = _get_redis_client()
    if client is not None:
        try:
            raw = client.get(UI_CONFIG_KEY)
            if raw:
                # Fusion avec les défauts : un nouveau flag ajouté après
                # coup et absent de Redis retombe sur sa valeur par
                # défaut plutôt que de faire disparaître la clé.
                merged = dict(DEFAULT_UI_CONFIG)
                merged.update(json.loads(raw))
                _cache = merged
                _cache_time = now
                return _cache
        except Exception as e:
            logger.warning(f"[ui_config] Erreur lecture Redis : {e} — repli sur défaut")

    _cache = dict(DEFAULT_UI_CONFIG)
    _cache_time = now
    return _cache


def _persist(key: str, value) -> dict:
    """Lit la config actuelle depuis Redis, applique un changement et
    réenregistre — logique partagée par set_param()/set_theme()."""
    client = _get_redis_client()
    if client is None:
        raise RuntimeError(
            "Redis injoignable — impossible d'enregistrer la configuration. "
            "Vérifiez que le service redis tourne (docker compose ps redis)."
        )

    raw = client.get(UI_CONFIG_KEY)
    config = dict(DEFAULT_UI_CONFIG)
    if raw:
        config.update(json.loads(raw))
    config[key] = value

    client.set(UI_CONFIG_KEY, json.dumps(config))

    global _cache, _cache_time
    _cache = config
    _cache_time = time.time()

    return config


def set_param(key: str, value: bool) -> dict:
    """Modifie un flag (ex: chat_enabled) et le persiste immédiatement
    dans Redis."""
    if key not in DEFAULT_UI_CONFIG:
        raise ValueError(
            f"Paramètre inconnu : '{key}'. Valeurs possibles : "
            f"{', '.join(DEFAULT_UI_CONFIG.keys())}"
        )
    return _persist(key, bool(value))


def set_theme(theme: str, key: str = "theme") -> dict:
    """Modifie le thème visuel (voir THEMES) — "theme" (recherche) ou
    "theme_admin" (administration) — et le persiste immédiatement dans
    Redis."""
    if key not in ("theme", "theme_admin"):
        raise ValueError(f"Champ de thème inconnu : '{key}'.")
    if theme not in THEMES:
        raise ValueError(
            f"Thème inconnu : '{theme}'. Valeurs possibles : {', '.join(THEMES)}"
        )
    return _persist(key, theme)
