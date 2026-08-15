# auth/config.py — Réglages de l'authentification
#
# Même style que le reste de docsearch-api : des constantes de module lues
# dans l'environnement au démarrage, pas d'objet Settings. Un seul endroit
# où chercher « d'où vient cette valeur ».
#
# Les variables LDAP_* historiques (LDAP_HOST, LDAP_BASE, LDAP_BINDDN,
# LDAP_PASS, LDAP_ENABLED) gardent leur nom et leur sens : une installation
# existante n'a rien à renommer. Ce qui s'ajoute autour d'elles (TLS,
# timeouts, filtres) a des défauts sûrs.

import os


def _nettoyer(valeur: str) -> str:
    """Retire les espaces ET les guillemets entourants.

    systemd ne fait AUCUNE substitution ni déquotage dans un
    `EnvironmentFile` : `COOKIE_SECURE="true"` transmet au conteneur la
    chaîne `"true"`, guillemets compris. Sans ce nettoyage, elle ne vaut
    pas `true`, et le réglage est **silencieusement inversé** — un cookie
    de session posé sans `Secure` sur une installation qu'on croit
    protégée, sans une ligne dans les journaux pour le dire. Le piège est
    déjà signalé en tête de `quadlet/common/docsearch.env.example` ; s'y
    fier seul revient à faire dépendre une propriété de sécurité de la
    relecture d'un commentaire.

    Constaté le 2026-08-06 : `COOKIE_SECURE` posé dans le fichier, mais le
    service continuait de poser des cookies sans `Secure`.
    """
    return (valeur or "").strip().strip("\"'").strip()


def _flag(name: str, default: str = "false") -> bool:
    return _nettoyer(os.getenv(name, default)).lower() == "true"


def _int(name: str, default: int) -> int:
    """Entier, jamais un flottant : ldap3 2.9.1 casse sur Python 3.14 quand
    connect_timeout et receive_timeout sont tous deux passés en float
    (struct.error au fond de son code socket). L'image de l'API est en 3.12
    mais l'hôte de développement en 3.14 — le piège se déclencherait à
    l'exécution des tests, pas en production, donc au pire endroit."""
    raw = _nettoyer(os.getenv(name, ""))
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


# ── Environnement ────────────────────────────────────────────
# "production" est le seul mot qui verrouille les contournements de
# développement (voir guardrails.py). Toute autre valeur — vide comprise —
# vaut « développement », de sorte qu'un déploiement de production qui
# oublierait de le déclarer soit simplement moins strict, jamais cassé.
API_ENV = _nettoyer(os.getenv("API_ENV", "development")).lower()
IS_PRODUCTION = API_ENV == "production"


# ── Annuaire LDAP / Active Directory ─────────────────────────
LDAP_ENABLED = _flag("LDAP_ENABLED")

# Historiquement une URL complète ("ldap://dc01.domaine.gouv.fr"), parfois un
# simple nom d'hôte. Les deux formes restent acceptées : le schéma, quand il
# est présent, détermine le chiffrement et le port par défaut.
LDAP_HOST_RAW = os.getenv("LDAP_HOST", "").strip()

_scheme, _, _rest = LDAP_HOST_RAW.partition("://")
if not _rest:
    _scheme, _rest = "", LDAP_HOST_RAW
_host, _, _port = _rest.partition(":")

LDAP_HOST = _host.strip("/")
# LDAP_USE_SSL tranche en dernier ressort ; sans lui, "ldaps://" suffit.
LDAP_USE_SSL = _flag("LDAP_USE_SSL", "true" if _scheme == "ldaps" else "false")
LDAP_PORT = _int("LDAP_PORT", int(_port) if _port.isdigit() else (636 if LDAP_USE_SSL else 389))

LDAP_BASE = os.getenv("LDAP_BASE", "").strip()
LDAP_BINDDN = os.getenv("LDAP_BINDDN", "").strip()
LDAP_PASS = os.getenv("LDAP_PASS", "")

LDAP_USER_SEARCH_BASE = os.getenv("LDAP_USER_SEARCH_BASE", "").strip() or LDAP_BASE
# Compatible OpenLDAP (uid) ET Active Directory (sAMAccountName) sans
# configuration : c'est le filtre de charlie/app-api-auth, éprouvé contre
# l'annuaire de dev de cette VM. {username} est TOUJOURS échappé avant
# formatage (voir directory.py) — ne jamais construire ce filtre ailleurs.
LDAP_USER_FILTER_TEMPLATE = os.getenv(
    "LDAP_USER_FILTER_TEMPLATE", "(|(uid={username})(sAMAccountName={username}))"
).strip()

# Repli quand l'annuaire n'expose pas memberOf sur l'entrée utilisateur
# (OpenLDAP sans overlay memberof). Vide = pas de recherche inverse.
LDAP_GROUP_SEARCH_BASE = os.getenv("LDAP_GROUP_SEARCH_BASE", "").strip()
LDAP_GROUP_FILTER_TEMPLATE = os.getenv(
    "LDAP_GROUP_FILTER_TEMPLATE", "(|(member={user_dn})(uniqueMember={user_dn}))"
).strip()

LDAP_CONNECT_TIMEOUT_SECONDS = _int("LDAP_CONNECT_TIMEOUT_SECONDS", 5)
LDAP_RECEIVE_TIMEOUT_SECONDS = _int("LDAP_RECEIVE_TIMEOUT_SECONDS", 10)

# Le cache de groupes de ldap_resolver.py était un lru_cache sans expiration :
# une exclusion de groupe mettait un redémarrage de l'API à être prise en
# compte. TTL court — la résolution est sur le chemin de CHAQUE recherche,
# elle ne peut pas taper l'annuaire à chaque requête, mais elle ne peut pas
# non plus figer les droits.
LDAP_GROUP_CACHE_TTL_SECONDS = _int("LDAP_GROUP_CACHE_TTL_SECONDS", 60)

# Dérogation explicite : bind en clair (port 389). Reste autorisée en
# production — beaucoup d'annuaires internes n'exposent pas LDAPS, et en
# faire une erreur fatale couperait l'application au lieu de la sécuriser —
# mais elle est journalisée en WARNING à chaque connexion, et le défaut de
# .env.example est false.
LDAP_ALLOW_PLAINTEXT_INSECURE = _flag("LDAP_ALLOW_PLAINTEXT_INSECURE")
LDAP_CA_CERT_FILE = os.getenv("LDAP_CA_CERT_FILE", "").strip()
LDAP_USE_STARTTLS = _flag("LDAP_USE_STARTTLS")


# ── Groupes d'autorisation ───────────────────────────────────
# Inchangés : c'est tout le modèle d'autorisation de DocSearch.
ACCESS_GROUP = os.getenv("ACCESS_GROUP", "").strip().lower()
ADMIN_GROUP = os.getenv("ADMIN_GROUP", "").strip().lower()


# ── Jetons ───────────────────────────────────────────────────
JWT_ISSUER = os.getenv("JWT_ISSUER", "docsearch-api").strip()
# Audience unique : un seul service consomme ces jetons. La liste
# d'audiences de Charlie n'a pas d'objet ici.
JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", "docsearch").strip()
JWT_PRIVATE_KEY_PATH = os.getenv("JWT_PRIVATE_KEY_PATH", "").strip()
JWT_PUBLIC_KEY_PATH = os.getenv("JWT_PUBLIC_KEY_PATH", "").strip()
JWT_ACTIVE_KID = os.getenv("JWT_ACTIVE_KID", "").strip()
JWT_ACCESS_TOKEN_TTL_MINUTES = _int("JWT_ACCESS_TOKEN_TTL_MINUTES", 15)
JWT_REFRESH_TOKEN_TTL_DAYS = _int("JWT_REFRESH_TOKEN_TTL_DAYS", 7)

# Fenêtre pendant laquelle un jeton de rafraîchissement DÉJÀ TOURNÉ reste
# accepté, sans rouvrir de session (voir auth/sessions.py). Elle éteint la
# course entre onglets : deux clients du même navigateur qui présentent le
# même jeton à quelques secondes d'écart — cas ordinaire dès que deux
# onglets se réveillent ensemble — n'en déconnectaient qu'un, et le
# déconnectaient VRAIMENT.
#
# 0 restaure le comportement strict (un jeton, un usage, le second est
# refusé). Mesuré sur l'installation de dev : une douzaine de rotations
# concurrentes en huit jours, toutes à moins de 20 secondes d'écart.
REFRESH_ROTATION_GRACE_SECONDS = _int("REFRESH_ROTATION_GRACE_SECONDS", 30)


# ── Cookies de session ───────────────────────────────────────
ACCESS_COOKIE_NAME = "docsearch_access"
REFRESH_COOKIE_NAME = "docsearch_refresh"
# Secure par défaut : la production est en HTTPS (voir le reverse-proxy).
# Se met à false pour la recette en clair sur le port 8090.
COOKIE_SECURE = _flag("COOKIE_SECURE", "true")
# "strict" protège le mieux ; "lax" est nécessaire si l'on arrive sur
# DocSearch depuis un lien collé dans un mail ou un portail intranet (avec
# strict, la toute première requête part sans cookie et renvoie au
# formulaire). Voir PLAN-AUTH-SSO.md, « À trancher ».
COOKIE_SAMESITE = _nettoyer(os.getenv("COOKIE_SAMESITE", "strict")).lower()


# ── Rate limiting ────────────────────────────────────────────
RATE_LIMIT_MAX_ATTEMPTS = _int("RATE_LIMIT_MAX_ATTEMPTS", 5)
RATE_LIMIT_WINDOW_SECONDS = _int("RATE_LIMIT_WINDOW_SECONDS", 15 * 60)


# ── Journal des connexions ───────────────────────────────────
LOGIN_EVENTS_INDEX = os.getenv("LOGIN_EVENTS_INDEX", "login_events").strip()


# ── Kerberos / SPNEGO ────────────────────────────────────────
# L'interrupteur fonctionnel (sso_kerberos.enabled) vit dans la
# configuration à chaud, côté Redis (voir runtime_config.py) : il se règle
# depuis le panneau d'administration. Ce qui est ici est ce qui ne peut PAS
# se changer à chaud — des chemins de fichiers et un nom de realm.
KERBEROS_REALM = os.getenv("KERBEROS_REALM", "").strip()
KERBEROS_KEYTAB = os.getenv("KERBEROS_KEYTAB", "").strip()
KERBEROS_SPN = os.getenv("KERBEROS_SPN", "").strip()


# ── Contournements de développement ──────────────────────────
# Tous verrouillés en production par guardrails.py, importé au démarrage.
# Aucun d'eux ne doit être lu ailleurs qu'à travers deps.py / kerberos.py :
# c'est ce qui garantit qu'ils n'ont qu'un seul point d'effet.
DEV_USER = os.getenv("DEV_USER", "").strip()
TRUST_X_USER_HEADER = _flag("TRUST_X_USER_HEADER")
KERBEROS_DEV_PRINCIPAL = os.getenv("KERBEROS_DEV_PRINCIPAL", "").strip()
ACCESS_AUTH_DISABLED = _flag("ACCESS_AUTH_DISABLED")
ADMIN_AUTH_DISABLED = _flag("ADMIN_AUTH_DISABLED")
