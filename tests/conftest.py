# tests/conftest.py — Fixtures communes
#
# docsearch-api n'avait aucun test : la CI ne faisait que `ruff` et
# `docker build`. Ce répertoire naît avec l'authentification, parce qu'un
# contrôle d'accès sans test est une intention, pas un mécanisme.
#
# Trois principes, repris de charlie/app-api-auth :
#
# 1. **Pas de test qui ne teste que des mocks.** Les tests LDAP tapent le
#    VRAI annuaire de dev de cette VM (~/ldap-test-stack) et se sautent
#    proprement s'il est injoignable ; les tests de session tapent le VRAI
#    Redis. Un fournisseur intégralement bouchonné ne prouverait rien.
# 2. **Ne jamais salir l'environnement partagé.** Les clés Redis créées
#    sont toutes sous `docsearch:auth:` et effacées avant ET après chaque
#    test — jamais un `FLUSHDB`, ce Redis porte la configuration de
#    l'installation de dev.
# 3. **Les clés RS256 sont éphémères**, générées dans un répertoire
#    temporaire : aucun test ne dépend d'une clé déployée, et aucun ne
#    laisse traîner de clé privée.
#
# ⚠️  auth/config.py lit l'environnement À L'IMPORT, comme tout le reste de
# docsearch-api. Les tests ne modifient donc pas os.environ (trop tard),
# mais les attributs du module `config` — ce que fait la fixture
# `env_auth`. Seule exception, et elle confirme la règle : le
# os.environ.setdefault("REDIS_HOST", ...) plus bas, qui n'est pas dans un
# test mais dans ce fichier, AVANT le premier import de `auth`.

import os
import sys
import time
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

# Redis sur la boucle locale par défaut, AVANT l'import de `auth` ci-dessous.
#
# auth/store.py lit REDIS_HOST à l'import et retombe sur "redis" — le nom
# d'hôte du réseau de conteneurs, qui ne résout pas quand les tests
# tournent sur la VM. Chaque get_client() payait alors ~3,5 s d'échec de
# résolution DNS, sans mise en cache du refus, et la fixture
# `clean_auth_keys` en appelle deux par test (avant et après) : ~7 s de
# setup/teardown sur CHAQUE test, soit une vingtaine de minutes pour la
# suite au lieu d'une minute.
#
# Plus grave que la lenteur : Redis injoignable faisait SAUTER les ~29
# tests marqués `requires_redis`, donc toute la couverture des sessions et
# de la configuration, en silence — une suite verte qui ne prouvait pas ce
# qu'elle prétendait.
#
# setdefault et non affectation : la CI et les conteneurs, qui définissent
# déjà REDIS_HOST, gardent la main.
os.environ.setdefault("REDIS_HOST", "localhost")

# Import APRÈS l'ajout de `app/` au chemin, forcément : les modules de
# l'API sont à plat, pas dans un paquet installable (COPY app/ . dans le
# Dockerfile).
from auth import accounts, config, directory, events, store, tokens  # noqa: E402

# Annuaire de dev de la VM (~/ldap-test-stack) : OpenLDAP, base
# dc=docsearch,dc=test, port 389 EN CLAIR — LDAPS/636 et STARTTLS n'y sont
# pas configurés. D'où la dérogation ci-dessous, qui n'est acceptable que
# parce que cet annuaire ne contient que des comptes de test.
#
# ⚠️  Les DEUX mots de passe viennent de l'environnement et n'ont pas de
# valeur par défaut. Ils sont sans valeur hors de cette VM — un conteneur
# jetable, en clair, sur un réseau de test — mais les écrire ici ferait
# entrer un mot de passe de bind dans le dépôt, ce que la convention du
# projet interdit sans exception, et ce que les HOWTO respectent déjà
# (« mot de passe : voir userPassword dans bootstrap-ldifs/03-users.ldif,
# pas reproduit ici »). Absents, les tests annuaire se sautent — comme si
# l'annuaire était arrêté.
#
#   export DOCSEARCH_TEST_LDAP_BIND_PASSWORD=...   # cn=admin, voir docker-compose.yml
#   export DOCSEARCH_TEST_LDAP_USER_PASSWORD=...   # alice.admin / bob.user, voir 03-users.ldif
LDAP_DEV = {
    "LDAP_ENABLED": True,
    "LDAP_HOST": "127.0.0.1",
    "LDAP_PORT": 389,
    "LDAP_USE_SSL": False,
    "LDAP_USE_STARTTLS": False,
    "LDAP_ALLOW_PLAINTEXT_INSECURE": True,
    "LDAP_BINDDN": "cn=admin,dc=docsearch,dc=test",
    "LDAP_PASS": os.getenv("DOCSEARCH_TEST_LDAP_BIND_PASSWORD", ""),
    "LDAP_BASE": "dc=docsearch,dc=test",
    "LDAP_USER_SEARCH_BASE": "ou=people,dc=docsearch,dc=test",
    "ACCESS_GROUP": "docsearch-users",
    "ADMIN_GROUP": "docsearch-admins",
}

# Comptes de l'annuaire de dev — voir bootstrap-ldifs/03-users.ldif.
ALICE = "alice.admin"      # docsearch-admins + docsearch-users
BOB = "bob.user"           # docsearch-users
LDAP_PASSWORD = os.getenv("DOCSEARCH_TEST_LDAP_USER_PASSWORD", "")


def pytest_configure(config):
    config.addinivalue_line("markers", "requires_ldap: exige l'annuaire de dev (~/ldap-test-stack)")
    config.addinivalue_line("markers", "requires_redis: exige un Redis joignable")
    config.addinivalue_line("markers", "requires_elasticsearch: exige un Elasticsearch joignable")
    config.addinivalue_line("markers", "requires_kerberos: exige un KDC — aucun sur cette VM")


def _redis_reachable() -> bool:
    store.reset_client()
    return store.get_client() is not None


def _elasticsearch_reachable() -> bool:
    import httpx

    import cluster_status
    try:
        return httpx.get(f"{cluster_status.ES_HOST}/", timeout=2).status_code == 200
    except Exception:
        return False


def _ldap_reachable() -> bool:
    import socket
    try:
        with socket.create_connection((LDAP_DEV["LDAP_HOST"], LDAP_DEV["LDAP_PORT"]), timeout=2):
            return True
    except OSError:
        return False


def _raison_de_sauter(marqueur: str) -> str | None:
    """Raison de sauter les tests portant `marqueur`, ou None s'ils peuvent
    tourner."""
    if marqueur == "requires_redis":
        return None if _redis_reachable() else "Redis injoignable"

    if marqueur == "requires_elasticsearch":
        return None if _elasticsearch_reachable() else "Elasticsearch injoignable"

    if marqueur == "requires_ldap":
        if not (LDAP_DEV["LDAP_PASS"] and LDAP_PASSWORD):
            return (
                "Mots de passe de l'annuaire de test absents — exporter "
                "DOCSEARCH_TEST_LDAP_BIND_PASSWORD et "
                "DOCSEARCH_TEST_LDAP_USER_PASSWORD (voir l'en-tête de ce fichier)"
            )
        if not _ldap_reachable():
            return "Annuaire de dev injoignable (~/ldap-test-stack)"
        return None

    if marqueur == "requires_kerberos":
        return "Aucun KDC sur cette VM — voir PLAN-AUTH-SSO.md, « À trancher »"

    raise AssertionError(f"Marqueur de dépendance inconnu : {marqueur}")


# Ordre significatif : c'est la PREMIÈRE dépendance manquante qui donne sa
# raison au saut, comme le faisait la cascade de `if` qui précédait.
MARQUEURS_DE_DEPENDANCE = (
    "requires_redis",
    "requires_elasticsearch",
    "requires_ldap",
    "requires_kerberos",
)


def pytest_collection_modifyitems(items):
    """Marque « à sauter », dès la collecte, les tests dont la dépendance
    n'est pas là.

    ⚠️  À la COLLECTE, et non dans une fixture autouse comme jusqu'au
    2026-08-13 : une fixture, quoi qu'elle fasse, est de portée `function`,
    donc construite APRÈS les fixtures de portée `module` ou `session` du
    test. Or ce sont justement celles-là qui ouvrent les connexions —
    l'`es` de test_zero_resultat.py, par exemple. Le saut arrivait trop
    tard : la connexion avait déjà échoué, et pytest comptait une ERREUR
    de fixture là où on avait écrit un saut. 48 tests sur 7 fichiers
    étaient dans ce cas dès qu'Elasticsearch manquait, ce qui rendait la
    suite inutilisable sans lui — et rouge en CI, qui ne le démarrait pas.
    `pytest_collection_modifyitems` s'exécute avant toute fixture, quelle
    que soit sa portée.

    Effet de bord bienvenu : chaque dépendance n'est sondée qu'UNE fois par
    exécution, et seulement si un test collecté la réclame. La fixture, elle,
    rouvrait une connexion à chaque test — 29 allers-retours pour le seul
    `requires_redis`.
    """
    raisons: dict[str, str | None] = {}
    for item in items:
        for marqueur in MARQUEURS_DE_DEPENDANCE:
            if item.get_closest_marker(marqueur) is None:
                continue
            if marqueur not in raisons:
                raisons[marqueur] = _raison_de_sauter(marqueur)
            if raisons[marqueur] is not None:
                item.add_marker(pytest.mark.skip(reason=raisons[marqueur]))
                break


@pytest.fixture(scope="session")
def rsa_keys(tmp_path_factory):
    """Paire RS256 éphémère. Le script scripts/generer-cles.py fait la même
    chose pour de vrai ; on ne l'appelle pas ici pour ne pas dépendre de son
    chemin de sortie par défaut (/etc/docsearch)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    dossier = tmp_path_factory.mktemp("jwt")
    # 2048 et non 3072 : ces clés ne protègent rien et sont régénérées à
    # chaque session de test — autant ne pas payer la génération.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    privee = dossier / "private.pem"
    privee.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    publique = dossier / "public.pem"
    publique.write_bytes(key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    return {"private": str(privee), "public": str(publique), "kid": "test-kid"}


@pytest.fixture
def env_auth(monkeypatch, rsa_keys):
    """Configuration d'authentification complète et cohérente.

    Patche les attributs du module `config` plutôt que os.environ : le
    module les a lus à l'import, bien avant qu'un test ne s'exécute."""
    monkeypatch.setattr(config, "JWT_PRIVATE_KEY_PATH", rsa_keys["private"])
    monkeypatch.setattr(config, "JWT_PUBLIC_KEY_PATH", rsa_keys["public"])
    monkeypatch.setattr(config, "JWT_ACTIVE_KID", rsa_keys["kid"])
    monkeypatch.setattr(config, "IS_PRODUCTION", False)
    monkeypatch.setattr(config, "API_ENV", "test")
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    monkeypatch.setattr(config, "TRUST_X_USER_HEADER", False)
    monkeypatch.setattr(config, "DEV_USER", "")
    monkeypatch.setattr(config, "KERBEROS_DEV_PRINCIPAL", "")
    monkeypatch.setattr(config, "ACCESS_AUTH_DISABLED", False)
    monkeypatch.setattr(config, "ADMIN_AUTH_DISABLED", False)
    # Les groupes d'autorisation font partie d'une configuration COHÉRENTE :
    # vides, le contrôle d'accès refuse tout le monde (refus par défaut), et
    # tous les tests de connexion échoueraient en 403 pour une raison qui
    # n'a rien à voir avec ce qu'ils vérifient.
    monkeypatch.setattr(config, "ACCESS_GROUP", "docsearch-users")
    monkeypatch.setattr(config, "ADMIN_GROUP", "docsearch-admins")
    tokens.reset_keys()
    directory.invalidate_cache()
    yield
    tokens.reset_keys()
    directory.invalidate_cache()


@pytest.fixture
def env_ldap(monkeypatch, env_auth):
    """Ajoute l'annuaire de dev à la configuration."""
    for key, value in LDAP_DEV.items():
        monkeypatch.setattr(config, key, value)
    monkeypatch.setattr(directory, "LDAP_ENABLED", True)
    directory.invalidate_cache()
    yield


@pytest.fixture(autouse=True)
def clean_auth_keys():
    """Efface les clés `docsearch:auth:*` avant et après chaque test.

    Jamais de FLUSHDB : ce Redis porte aussi la configuration de
    l'installation de développement (sources, réglages à chaud, recherches
    enregistrées)."""
    def _purge():
        client = store.get_client()
        if client is None:
            return
        keys = list(client.scan_iter(match=f"{store.KEY_PREFIX}*", count=200))
        if keys:
            client.delete(*keys)

    _purge()
    yield
    _purge()


@pytest.fixture(autouse=True)
def journal_hors_ligne(monkeypatch):
    """Le journal des connexions n'écrit PAS dans l'Elasticsearch partagé.

    Corrige un défaut de la première version de ces tests : `_open_session`
    appelle `events.record`, qui indexait pour de bon dans le cluster de
    l'installation de développement — la suite y a créé l'index
    `login_events` et l'a rempli de connexions fictives. Même principe que
    pour Redis : on tape le vrai service quand c'est lui qu'on teste, jamais
    en passant.

    Les deux tests qui portent SUR le journal remplacent eux-mêmes
    `events._client` dans leur corps, donc après cette fixture : ce sont eux
    qui gagnent."""
    class _FauxEs:
        def __init__(self):
            self.documents: list[dict] = []

        def index(self, index, document):  # noqa: A002 — signature d'Elasticsearch
            self.documents.append(document)

    faux = _FauxEs()
    monkeypatch.setattr(events, "_client", lambda: faux)
    # Court-circuite _ensure_index : aucun index n'est créé nulle part.
    # (Une fenêtre de vérification qui vient de s'ouvrir vaut « déjà
    # vérifié » — voir events.INDEX_CHECK_TTL_SECONDS.)
    monkeypatch.setattr(events, "_index_verifie_a", time.monotonic())
    return faux


@pytest.fixture
def client(env_auth):
    """Client HTTP sur l'application complète.

    Importé tardivement : search_api tire tout le reste de l'API, et une
    erreur d'import y ferait échouer même les tests qui n'en dépendent
    pas."""
    import search_api
    from fastapi.testclient import TestClient

    return TestClient(search_api.app, raise_server_exceptions=False)


@pytest.fixture
def compte_secours():
    """Crée un compte de secours local, et le retire à la fin."""
    created = []

    def _create(login: str, password: str, groups: list[str]):
        accounts.set_account(login, password=password, groups=groups)
        created.append(login)
        return login

    yield _create
    for login in created:
        try:
            accounts.delete_account(login)
        except Exception:
            pass
