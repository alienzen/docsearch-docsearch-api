# auth/directory.py — Annuaire LDAP / Active Directory
#
# Remplace ldap_resolver.py, qui avait trois défauts sur lesquels reposait
# pourtant tout le contrôle d'accès :
#
#   1. le filtre de recherche était construit en f-string, sans
#      escape_filter_chars — un identifiant contrôlé par l'appelant
#      (l'en-tête X-User, à l'époque cru sur parole) permettait d'injecter
#      un filtre LDAP arbitraire, donc de choisir les groupes qu'on
#      obtenait ;
#   2. aucun TLS, aucune validation de certificat, aucun receive_timeout —
#      un annuaire lent bloquait un worker sans limite ;
#   3. un lru_cache sans expiration : une exclusion de groupe mettait un
#      redémarrage de l'API à être prise en compte.
#
# La structure suit charlie/app-api-auth/app/ldap_client.py, qui fait mieux
# sur les trois points. Ce qui est PROPRE à DocSearch et n'existe pas
# là-bas : `get_user_groups`, qui rend des noms courts de groupes (CN) et
# non des DN, parce que c'est cette forme que porte le champ ACL des
# documents indexés ; et `get_effective_groups`, qui y ajoute les groupes
# d'un éventuel compte de secours local.
#
# Deux régimes d'erreur, à ne jamais confondre (c'est l'invariant 6 du
# plan) : DirectoryAuthError = identifiants invalides (401, message
# générique), DirectoryUnavailableError = annuaire injoignable (503).

import logging
import ssl
import time
from dataclasses import dataclass, field

from ldap3 import ALL, SUBTREE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException, LDAPSocketOpenError
from ldap3.utils.conv import escape_filter_chars

from auth import accounts, config

logger = logging.getLogger(__name__)

# Ré-exporté : plusieurs modules testent `LDAP_ENABLED` avant d'appeler.
LDAP_ENABLED = config.LDAP_ENABLED


class DirectoryAuthError(Exception):
    """Identifiant inconnu ou mot de passe faux — volontairement
    indistinguables. C'est à l'appelant HTTP d'en faire un 401 au message
    générique unique."""


class DirectoryUnavailableError(Exception):
    """Annuaire injoignable, timeout, ou configuration TLS refusée.

    Jamais présenté comme « identifiants invalides » : ce serait faire
    passer une panne pour une faute de frappe, et personne ne diagnostique
    un annuaire arrêté à travers un message d'erreur de connexion."""


@dataclass
class DirectoryUser:
    dn: str
    uid: str
    cn: str
    mail: str | None
    group_dns: list[str] = field(default_factory=list)

    @property
    def groups(self) -> list[str]:
        """Noms courts, minuscules — la forme attendue par le filtrage ACL."""
        return [cn_from_dn(dn) for dn in self.group_dns]


def cn_from_dn(dn: str) -> str:
    """« cn=docsearch-admins,ou=groups,dc=… » → « docsearch-admins ».

    Tolère un nom déjà court (un annuaire qui rendrait des CN bruts dans
    memberOf) : la première composante d'une chaîne sans « = » est
    elle-même."""
    first = str(dn).split(",")[0]
    return first.split("=", 1)[-1].strip().lower()


# ── Connexion ────────────────────────────────────────────────

def _build_server() -> Server:
    if config.LDAP_USE_SSL:
        tls = Tls(
            validate=ssl.CERT_REQUIRED,
            ca_certs_file=config.LDAP_CA_CERT_FILE or None,
        )
        return Server(
            config.LDAP_HOST,
            port=config.LDAP_PORT,
            use_ssl=True,
            tls=tls,
            get_info=ALL,
            connect_timeout=config.LDAP_CONNECT_TIMEOUT_SECONDS,
        )

    if not config.LDAP_ALLOW_PLAINTEXT_INSECURE:
        raise DirectoryUnavailableError(
            "Connexion LDAP en clair refusée : LDAPS est attendu "
            "(LDAP_HOST=ldaps://… ou LDAP_USE_SSL=true), sauf dérogation "
            "explicite LDAP_ALLOW_PLAINTEXT_INSECURE=true. Une installation "
            "existante qui bindait en clair doit poser ce drapeau "
            "sciemment — voir .env.example."
        )

    logger.warning(
        "[auth] LDAP_ALLOW_PLAINTEXT_INSECURE=true : connexion EN CLAIR vers "
        "%s:%s — le mot de passe du compte de service et ceux des "
        "utilisateurs transitent sans chiffrement.",
        config.LDAP_HOST, config.LDAP_PORT,
    )
    return Server(
        config.LDAP_HOST,
        port=config.LDAP_PORT,
        use_ssl=False,
        get_info=ALL,
        connect_timeout=config.LDAP_CONNECT_TIMEOUT_SECONDS,
    )


def _maybe_start_tls(conn: Connection) -> None:
    if config.LDAP_USE_STARTTLS and not config.LDAP_USE_SSL and not conn.start_tls():
        raise DirectoryUnavailableError("Échec STARTTLS vers le serveur LDAP.")


def _open_technical_connection() -> Connection:
    """Bind du compte de service, en lecture seule. Sert à CHERCHER, jamais
    à vérifier un mot de passe (c'est le rôle du second bind)."""
    server = _build_server()
    conn = Connection(
        server,
        user=config.LDAP_BINDDN or None,
        password=config.LDAP_PASS or None,
        receive_timeout=config.LDAP_RECEIVE_TIMEOUT_SECONDS,
        auto_bind=False,
    )
    try:
        _maybe_start_tls(conn)
        if not conn.bind():
            raise DirectoryUnavailableError(f"Échec du bind technique LDAP : {conn.result}")
    except LDAPSocketOpenError as exc:
        raise DirectoryUnavailableError(f"LDAP injoignable : {exc}") from exc
    except LDAPException as exc:
        raise DirectoryUnavailableError(f"Erreur LDAP (bind technique) : {exc}") from exc
    return conn


def _attr(entry, name: str) -> str | None:
    if name not in entry:
        return None
    value = entry[name].value
    return str(value) if value else None


def build_user_filter(username: str) -> str:
    """Filtre de recherche d'un utilisateur.

    Fonction séparée et PURE parce que c'est le point qu'il faut pouvoir
    tester sans annuaire : `escape_filter_chars` AVANT formatage est la
    ligne qui manquait à ldap_resolver.py. Sans elle, un identifiant
    contenant `*)(uid=*` sort du filtre prévu et choisit l'entrée qu'il
    veut — et jusqu'ici cet identifiant venait d'un en-tête HTTP contrôlé
    par l'appelant."""
    return config.LDAP_USER_FILTER_TEMPLATE.format(username=escape_filter_chars(username))


def _search_user(conn: Connection, username: str):
    search_filter = build_user_filter(username)
    base = config.LDAP_USER_SEARCH_BASE
    try:
        conn.search(
            search_base=base,
            search_filter=search_filter,
            search_scope=SUBTREE,
            attributes=["cn", "mail", "memberOf", "uid", "sAMAccountName"],
            time_limit=config.LDAP_RECEIVE_TIMEOUT_SECONDS or 5,
        )
    except LDAPException as exc:
        raise DirectoryUnavailableError(f"Erreur LDAP (recherche utilisateur) : {exc}") from exc

    if len(conn.entries) == 0:
        return None
    if len(conn.entries) > 1:
        logger.warning(
            "[auth] Plusieurs entrées annuaire pour un même identifiant "
            "(base=%s) : la première est retenue.", base,
        )
    entry = conn.entries[0]
    return entry.entry_dn, entry


def _resolve_group_dns(conn: Connection, user_dn: str, entry) -> list[str]:
    if "memberOf" in entry and entry["memberOf"].values:
        return [str(v) for v in entry["memberOf"].values]

    # Repli pour un annuaire qui n'expose pas memberOf (OpenLDAP sans
    # overlay memberof) : recherche inverse groupe → membre.
    if not config.LDAP_GROUP_SEARCH_BASE:
        return []

    safe_dn = escape_filter_chars(user_dn)
    try:
        conn.search(
            search_base=config.LDAP_GROUP_SEARCH_BASE,
            search_filter=config.LDAP_GROUP_FILTER_TEMPLATE.format(user_dn=safe_dn),
            search_scope=SUBTREE,
            attributes=["cn"],
            time_limit=config.LDAP_RECEIVE_TIMEOUT_SECONDS or 5,
        )
    except LDAPException as exc:
        raise DirectoryUnavailableError(f"Erreur LDAP (recherche groupes) : {exc}") from exc
    return [e.entry_dn for e in conn.entries]


# ── Recherche et authentification ────────────────────────────

def lookup_user(username: str) -> DirectoryUser:
    """Cherche une entrée annuaire SANS vérifier de mot de passe.

    Deux appelants, pour des raisons opposées : `authenticate()` juste en
    dessous, qui enchaîne avec le bind utilisateur ; et le fournisseur
    Kerberos, pour qui il n'y a justement rien à vérifier — le ticket a
    déjà prouvé l'identité, l'annuaire n'est plus qu'une source
    d'attributs. C'est l'invariant « Kerberos authentifie, l'annuaire
    renseigne » : si cette fonction se mettait à vérifier quoi que ce soit,
    elle sortirait de son rôle."""
    if not config.LDAP_ENABLED:
        raise DirectoryUnavailableError(
            "LDAP_ENABLED=false : aucune identité ne peut être résolue dans "
            "l'annuaire."
        )

    conn = _open_technical_connection()
    try:
        found = _search_user(conn, username)
        if found is None:
            raise DirectoryAuthError("Identifiant ou mot de passe incorrect.")
        user_dn, entry = found
        group_dns = _resolve_group_dns(conn, user_dn, entry)
        cn = _attr(entry, "cn") or username
        mail = _attr(entry, "mail")
    finally:
        try:
            conn.unbind()
        except LDAPException:
            pass

    return DirectoryUser(dn=user_dn, uid=username, cn=cn, mail=mail, group_dns=group_dns)


def authenticate(username: str, password: str) -> DirectoryUser:
    """Bind technique pour trouver le DN, puis second bind avec le mot de
    passe fourni — c'est la SEULE vérification du mot de passe, qui n'est
    jamais stocké, jamais journalisé, jamais transmis au-delà."""
    if not username or not password:
        # Un bind à mot de passe vide est un « unauthenticated bind » LDAP,
        # qui peut réussir sans rien vérifier. Toujours un échec ici.
        raise DirectoryAuthError("Identifiant ou mot de passe incorrect.")

    found = lookup_user(username)

    server = _build_server()
    user_conn = Connection(
        server,
        user=found.dn,
        password=password,
        receive_timeout=config.LDAP_RECEIVE_TIMEOUT_SECONDS,
        auto_bind=False,
    )
    bound = False
    try:
        _maybe_start_tls(user_conn)
        bound = user_conn.bind()
    except LDAPSocketOpenError as exc:
        raise DirectoryUnavailableError(f"LDAP injoignable : {exc}") from exc
    except LDAPException:
        bound = False
    finally:
        try:
            user_conn.unbind()
        except LDAPException:
            pass

    if not bound:
        raise DirectoryAuthError("Identifiant ou mot de passe incorrect.")

    # Le cache de groupes est réamorcé au login : la personne vient de
    # prouver son identité, c'est le meilleur moment pour rafraîchir ses
    # droits sans coût supplémentaire.
    _cache_put(accounts.normalize_login(username), found.groups)
    return found


# ── Groupes ──────────────────────────────────────────────────
# Cache à TTL court : la résolution est sur le chemin de CHAQUE recherche
# (filtrage ACL), elle ne peut pas taper l'annuaire à chaque requête — mais
# elle ne peut pas non plus figer les droits jusqu'au prochain redémarrage.

_cache: dict[str, tuple[float, list[str]]] = {}


def _cache_get(login: str) -> list[str] | None:
    entry = _cache.get(login)
    if entry is None:
        return None
    expires_at, groups = entry
    if expires_at < time.monotonic():
        _cache.pop(login, None)
        return None
    return groups


def _cache_put(login: str, groups: list[str]) -> None:
    _cache[login] = (time.monotonic() + config.LDAP_GROUP_CACHE_TTL_SECONDS, groups)


def invalidate_cache() -> None:
    """Vide le cache des groupes (appelé par l'administration après une
    modification de droits, et par les tests)."""
    _cache.clear()
    logger.info("[auth] Cache des groupes annuaire vidé.")


def get_user_groups(username: str, *, strict: bool = False) -> list[str]:
    """Groupes annuaire d'un utilisateur, en noms courts et en minuscules.

    **Les deux régimes ont chacun leur raison d'être, ne pas les unifier :**

    - `strict=False` (défaut) — annuaire injoignable ⇒ liste vide, erreur
      journalisée. C'est le contrat historique, et il est le bon sur le
      chemin de la RECHERCHE : le filtrage ACL devient alors plus
      restrictif, jamais plus permissif, et le worker d'alertes
      (`alert_worker.py`, qui rejoue des recherches sans session) continue
      de tourner au lieu de s'arrêter à la première panne.
    - `strict=True` — annuaire injoignable ⇒ DirectoryUnavailableError.
      Obligatoire sur le chemin de l'AUTORISATION : là, une liste vide se
      traduit par « accès réservé aux membres du groupe … », c'est-à-dire
      une panne déguisée en refus de droits, que personne ne diagnostique.
    """
    if not config.LDAP_ENABLED:
        # Pas une panne : une installation qui filtre uniquement sur les ACL
        # POSIX. Même en strict, il n'y a rien à signaler.
        return []

    login = accounts.normalize_login(username)
    if not login:
        return []

    cached = _cache_get(login)
    if cached is not None:
        return cached

    try:
        groups = lookup_user(login).groups
    except DirectoryAuthError:
        # Identifiant inconnu de l'annuaire : ce n'est pas une panne, c'est
        # une réponse. Mise en cache pour ne pas rejouer la recherche à
        # chaque requête d'un utilisateur qui n'y est pas (compte de
        # secours local, notamment).
        _cache_put(login, [])
        return []
    except DirectoryUnavailableError as exc:
        logger.error("[auth] Résolution des groupes impossible pour %s : %s", login, exc)
        if strict:
            raise
        return []

    _cache_put(login, groups)
    return groups


def get_effective_groups(login: str, *, strict: bool = False) -> list[str]:
    """Groupes effectifs : annuaire ∪ groupes du compte de secours local.

    **Point unique de vérité pour toute décision d'autorisation** — accès à
    l'application, accès au panneau d'administration, filtrage ACL des
    documents. Un second chemin de résolution finirait par diverger de
    celui-ci, et c'est toujours celui qui n'a pas été mis à jour qui décide.

    L'union, et non un choix entre les deux : c'est ce qui fait qu'un compte
    de secours reste opérant *quand l'annuaire est en panne* (l'annuaire
    rend alors une liste vide) sans cesser de l'être quand il fonctionne."""
    canonical = accounts.normalize_login(login)
    if not canonical:
        return []

    groups = list(get_user_groups(canonical, strict=strict))
    for group in accounts.get_account_groups(canonical):
        if group not in groups:
            groups.append(group)
    return groups
