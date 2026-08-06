"""Le flux de connexion complet : session, révocation, rate limiting,
journal, garde-fous.

Ces tests tapent le VRAI Redis (celui de l'installation de dev) : les
sessions et les compteurs de tentatives n'ont aucun sens simulés. Les clés
créées vivent toutes sous `docsearch:auth:` et sont effacées avant et après
chaque test — jamais de FLUSHDB, ce Redis porte aussi la configuration de
l'installation.
"""

import pytest
from auth import accounts, config, events, sessions, store

MOT_DE_PASSE = "mot-de-passe-de-secours-1234"


@pytest.fixture
def secours(compte_secours, monkeypatch):
    """Un compte de secours administrateur, annuaire éteint — la situation
    exacte pour laquelle ces comptes existent."""
    monkeypatch.setattr(config, "LDAP_ENABLED", False)
    compte_secours("secours.admin", MOT_DE_PASSE, ["docsearch-users", "docsearch-admins"])
    return "secours.admin"


# ── Connexion et session ─────────────────────────────────────

@pytest.mark.requires_redis
def test_connexion_locale_ouvre_une_session(client, secours):
    reponse = client.post("/auth/login", json={"identifiant": secours, "mot_de_passe": MOT_DE_PASSE})
    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["user"] == secours
    assert corps["is_admin"] is True
    assert corps["auth_method"] == "local"
    assert config.ACCESS_COOKIE_NAME in reponse.cookies
    assert config.REFRESH_COOKIE_NAME in reponse.cookies


@pytest.mark.requires_redis
def test_la_session_ouvre_les_routes_admin(client, secours):
    client.post("/auth/login", json={"identifiant": secours, "mot_de_passe": MOT_DE_PASSE})
    assert client.get("/is-admin").json()["is_admin"] is True


@pytest.mark.requires_redis
def test_identifiant_insensible_a_la_casse(client, secours):
    reponse = client.post(
        "/auth/login", json={"identifiant": "Secours.Admin", "mot_de_passe": MOT_DE_PASSE},
    )
    assert reponse.status_code == 200
    assert reponse.json()["user"] == "secours.admin"


@pytest.mark.requires_redis
def test_mot_de_passe_faux_401_generique(client, secours):
    reponse = client.post("/auth/login", json={"identifiant": secours, "mot_de_passe": "faux"})
    assert reponse.status_code == 401
    assert reponse.json()["detail"] == "Identifiant ou mot de passe incorrect."


@pytest.mark.requires_redis
def test_compte_inconnu_repond_la_meme_chose(client, monkeypatch):
    """Aucune variation ne doit dire à qui essaie lesquels des identifiants
    présentés existent. Ici l'annuaire est éteint et le compte n'existe pas
    localement : le fournisseur retenu est l'annuaire, donc 503 — c'est la
    panne, pas un aveu. Avec l'annuaire joignable, le message est celui du
    test précédent, à l'identique."""
    monkeypatch.setattr(config, "LDAP_ENABLED", False)
    reponse = client.post("/auth/login", json={"identifiant": "inconnu", "mot_de_passe": "x"})
    assert reponse.status_code == 503


@pytest.mark.requires_redis
def test_acces_refuse_sans_le_groupe(client, compte_secours, monkeypatch):
    """Le contrôle d'ACCESS_GROUP a lieu à la CONNEXION : quelqu'un qui n'a
    pas le droit d'utiliser DocSearch ne repart pas avec une session valide
    qu'il verrait échouer sur chaque page sans comprendre pourquoi."""
    monkeypatch.setattr(config, "LDAP_ENABLED", False)
    compte_secours("sans.droits", MOT_DE_PASSE, ["un-autre-groupe"])
    reponse = client.post("/auth/login", json={"identifiant": "sans.droits", "mot_de_passe": MOT_DE_PASSE})
    assert reponse.status_code == 403
    assert config.ACCESS_COOKIE_NAME not in reponse.cookies


# ── Renouvellement et révocation ─────────────────────────────

@pytest.mark.requires_redis
def test_refresh_renouvelle_la_session(client, secours):
    client.post("/auth/login", json={"identifiant": secours, "mot_de_passe": MOT_DE_PASSE})
    assert client.post("/auth/refresh").status_code == 200


@pytest.mark.requires_redis
def test_deconnexion_revoque_reellement(client, secours):
    """LE test de la révocation : sans magasin de sessions, « se
    déconnecter » n'effacerait qu'un cookie que l'on pourrait recoller."""
    client.post("/auth/login", json={"identifiant": secours, "mot_de_passe": MOT_DE_PASSE})
    refresh = client.cookies.get(config.REFRESH_COOKIE_NAME)

    assert client.post("/auth/logout").status_code == 204

    client.cookies.set(config.REFRESH_COOKIE_NAME, refresh)
    assert client.post("/auth/refresh").status_code == 401


@pytest.mark.requires_redis
def test_un_refresh_ne_sert_qu_une_fois(client, secours):
    """La session précédente est remplacée à chaque renouvellement : un
    cookie intercepté ne vaut que jusqu'au prochain usage légitime."""
    client.post("/auth/login", json={"identifiant": secours, "mot_de_passe": MOT_DE_PASSE})
    premier = client.cookies.get(config.REFRESH_COOKIE_NAME)
    client.post("/auth/refresh")

    client.cookies.set(config.REFRESH_COOKIE_NAME, premier)
    assert client.post("/auth/refresh").status_code == 401


@pytest.mark.requires_redis
def test_deconnexion_sans_session_reussit(client):
    """Toujours 204 : un code différent selon qu'une session existait
    dirait à qui essaie si le cookie présenté était valide."""
    assert client.post("/auth/logout").status_code == 204


@pytest.mark.requires_redis
def test_revocation_de_toutes_les_sessions(client, secours):
    client.post("/auth/login", json={"identifiant": secours, "mot_de_passe": MOT_DE_PASSE})
    assert sessions.revoke_all_for_login(secours) == 1
    assert client.post("/auth/refresh").status_code == 401


# ── Rate limiting ────────────────────────────────────────────

@pytest.mark.requires_redis
def test_trop_de_tentatives_bloque(client, secours, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_MAX_ATTEMPTS", 3)
    for _ in range(3):
        assert client.post("/auth/login", json={"identifiant": secours, "mot_de_passe": "faux"}).status_code == 401
    reponse = client.post("/auth/login", json={"identifiant": secours, "mot_de_passe": "faux"})
    assert reponse.status_code == 429


@pytest.mark.requires_redis
def test_une_connexion_reussie_remet_le_compteur_a_zero(client, secours, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_MAX_ATTEMPTS", 3)
    client.post("/auth/login", json={"identifiant": secours, "mot_de_passe": "faux"})
    client.post("/auth/login", json={"identifiant": secours, "mot_de_passe": MOT_DE_PASSE})
    for _ in range(2):
        assert client.post("/auth/login", json={"identifiant": secours, "mot_de_passe": "faux"}).status_code == 401


@pytest.mark.requires_redis
def test_la_fenetre_est_ancree_au_premier_echec(monkeypatch):
    """`expire(..., nx=True)` : sans lui, un attaquant assez lent
    repousserait indéfiniment sa propre échéance et ne serait jamais
    bloqué."""
    sessions.register_failed_attempt("local", "quelqu-un", "10.0.0.1")
    client_redis = store.require_client()
    cle = f"{store.KEY_PREFIX}rl:local:user:quelqu-un"
    premier_ttl = client_redis.ttl(cle)
    sessions.register_failed_attempt("local", "quelqu-un", "10.0.0.1")
    assert client_redis.ttl(cle) <= premier_ttl


# ── Journal des connexions ───────────────────────────────────

def test_le_journal_ne_porte_jamais_de_mot_de_passe(monkeypatch):
    """Le document indexé est inspecté champ par champ. Un `**payload` posé
    un jour par commodité y ferait entrer le mot de passe sans que personne
    ne le remarque."""
    captures = []

    class FauxEs:
        def index(self, **kwargs):
            captures.append(kwargs["document"])

    monkeypatch.setattr(events, "_client", lambda: FauxEs())
    monkeypatch.setattr(events, "_ensure_index", lambda es: None)

    events.record(
        identifier="alice.admin", outcome=events.INVALID_CREDENTIALS,
        method="local", ip="10.0.0.1", user_agent="curl", detail="mot de passe refusé",
    )

    document = captures[0]
    assert set(document) == {
        "timestamp", "identifier", "outcome", "method", "ip", "user_agent", "detail", "simulated",
    }
    serialise = str(document).lower()
    for interdit in ("password", "mot_de_passe", "secret", "hash"):
        assert interdit not in serialise


def test_le_journal_n_interrompt_jamais_une_connexion(monkeypatch):
    """Une panne d'Elasticsearch ne doit ni faire échouer une connexion
    légitime, ni faire réussir une connexion refusée."""
    def _explose():
        raise RuntimeError("ES injoignable")

    monkeypatch.setattr(events, "_client", _explose)
    events.record(identifier="alice.admin", outcome=events.SUCCESS, method="ldap")


# ── Comptes de secours ───────────────────────────────────────

@pytest.mark.requires_redis
def test_un_compte_desactive_ne_se_connecte_pas(client, monkeypatch):
    monkeypatch.setattr(config, "LDAP_ENABLED", False)
    accounts.set_account(
        "gele", password=MOT_DE_PASSE, groups=["docsearch-users"], disabled=True,
    )
    assert accounts.verify_password("gele", MOT_DE_PASSE) is None


@pytest.mark.requires_redis
def test_la_liste_des_comptes_ne_rend_aucun_hachage(compte_secours):
    compte_secours("secours.liste", MOT_DE_PASSE, ["docsearch-users"])
    for compte in accounts.list_accounts():
        assert "password_hash" not in compte
