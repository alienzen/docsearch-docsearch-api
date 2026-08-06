"""Cohérence entre `COOKIE_SECURE` et le schéma d'accès.

Arbitré le 2026-08-06 : `false` sur la VM de développement (recette en
clair sur le port 8090), `true` en production (HTTPS par le
reverse-proxy). L'erreur inverse — `true` sur une installation servie en
clair — a un symptôme muet et trompeur : la connexion réussit, puis
chaque page renvoie au formulaire parce que le navigateur refuse de
renvoyer un cookie `Secure` sur du HTTP. D'où l'avertissement testé ici.
"""

import pytest

from auth import config, router


@pytest.fixture(autouse=True)
def _reinitialiser_avertissement():
    """L'avertissement n'est émis qu'une fois par processus : sans remise à
    zéro, le premier test le consommerait pour tous les autres."""
    router._incoherence_cookie_signalee = False
    yield
    router._incoherence_cookie_signalee = False


MOT_DE_PASSE = "mot-de-passe-de-secours-1234"


@pytest.fixture
def secours(compte_secours, monkeypatch):
    monkeypatch.setattr(config, "LDAP_ENABLED", False)
    compte_secours("secours.cookie", MOT_DE_PASSE, ["docsearch-users"])
    return "secours.cookie"


def _connexion(client, login, **kwargs):
    return client.post(
        "/auth/login",
        json={"identifiant": login, "mot_de_passe": MOT_DE_PASSE},
        **kwargs,
    )


@pytest.mark.requires_redis
def test_secure_sur_du_clair_avertit(client, secours, monkeypatch, caplog):
    monkeypatch.setattr(config, "COOKIE_SECURE", True)
    assert _connexion(client, secours).status_code == 200
    assert "COOKIE_SECURE=true" in caplog.text
    assert "renverra au formulaire" in caplog.text


@pytest.mark.requires_redis
def test_pas_d_avertissement_derriere_un_proxy_https(client, secours, monkeypatch, caplog):
    """Derrière le reverse-proxy TLS, l'API voit du HTTP en interne alors
    que le navigateur est bien en HTTPS : c'est X-Forwarded-Proto qui fait
    foi, sans quoi toute production correctement configurée avertirait à
    tort."""
    monkeypatch.setattr(config, "COOKIE_SECURE", True)
    reponse = _connexion(client, secours, headers={"X-Forwarded-Proto": "https"})
    assert reponse.status_code == 200
    assert "COOKIE_SECURE" not in caplog.text


@pytest.mark.requires_redis
def test_pas_d_avertissement_quand_le_drapeau_est_faux(client, secours, monkeypatch, caplog):
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    assert _connexion(client, secours).status_code == 200
    assert "COOKIE_SECURE" not in caplog.text


@pytest.mark.requires_redis
def test_le_drapeau_se_retrouve_sur_les_deux_cookies(client, secours, monkeypatch):
    """Les deux cookies doivent porter le même régime : un jeton d'accès
    protégé et un jeton de renouvellement en clair ne protégerait rien."""
    monkeypatch.setattr(config, "COOKIE_SECURE", True)
    reponse = _connexion(client, secours)
    entetes = [v for k, v in reponse.headers.items() if k.lower() == "set-cookie"]
    poses = " ".join(entetes) if entetes else str(reponse.headers.get("set-cookie", ""))
    assert poses.count("HttpOnly") >= 1
    for cookie in (config.ACCESS_COOKIE_NAME, config.REFRESH_COOKIE_NAME):
        assert cookie in poses


# ── Lecture de l'environnement ───────────────────────────────

@pytest.mark.parametrize("valeur", ['true', ' true ', '"true"', "'true'", '"true" '])
def test_guillemets_et_espaces_ne_neutralisent_pas_un_drapeau(monkeypatch, valeur):
    """systemd ne déquote rien dans un EnvironmentFile : `COOKIE_SECURE="true"`
    transmet la chaîne avec ses guillemets. Sans nettoyage, le réglage est
    silencieusement inversé — cookie de session sans `Secure` sur une
    installation qu'on croit protégée, et pas une ligne de journal pour le
    dire. Constaté sur l'installation de dev le 2026-08-06."""
    monkeypatch.setenv("UN_DRAPEAU_DE_TEST", valeur)
    assert config._flag("UN_DRAPEAU_DE_TEST") is True


@pytest.mark.parametrize("valeur", ['false', '"false"', 'FALSE', 'nimporte quoi', ''])
def test_ce_qui_ne_vaut_pas_true_reste_faux(monkeypatch, valeur):
    monkeypatch.setenv("UN_DRAPEAU_DE_TEST", valeur)
    assert config._flag("UN_DRAPEAU_DE_TEST") is False


def test_un_entier_entre_guillemets_reste_lisible(monkeypatch):
    monkeypatch.setenv("UN_ENTIER_DE_TEST", '"900"')
    assert config._int("UN_ENTIER_DE_TEST", 0) == 900


def test_un_index_supprime_est_recree_avec_ses_reglages(monkeypatch):
    """Le drapeau « vérifié une fois » laissait Elasticsearch recréer
    l'index tout seul — sans mapping et avec un réplica inallouable — dès
    qu'on le supprimait sous un processus vivant. Constaté le 2026-08-06."""
    from auth import events

    cree: list[dict] = []

    class _FauxIndices:
        def __init__(self, existe): self.existe = existe
        def exists(self, index): return self.existe
        def create(self, index, body): cree.append(body)

    class _FauxEs:
        def __init__(self, existe): self.indices = _FauxIndices(existe)
        def index(self, index, document): pass

    # Fenêtre de vérification expirée : l'index a disparu entre-temps.
    monkeypatch.setattr(events, "_index_verifie_a", 0.0)
    es = _FauxEs(existe=False)
    events._ensure_index(es)

    assert cree, "l'index doit être recréé, et par NOUS"
    assert cree[0] is events.INDEX_BODY
    assert cree[0]["settings"]["number_of_replicas"] == 0
    assert "identifier" in cree[0]["mappings"]["properties"]
