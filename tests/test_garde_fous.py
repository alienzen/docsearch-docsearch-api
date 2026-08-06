"""Les garde-fous : ce qui empêche un contournement de dev d'atteindre la
production.

DocSearch a cinq interrupteurs qui, chacun, suffisent à donner l'identité
de son choix à n'importe qui. Ils sont indispensables — sans eux, aucune
recette n'est possible sans annuaire ni KDC. Leur défaut, jusqu'ici, était
de ne rien coûter à oublier.
"""

import pytest
from auth import config, guardrails


@pytest.fixture
def _restaure():
    """guardrails lit `config` au moment de l'appel : les tests modifient
    donc le module, et le remettent d'aplomb."""
    avant = {nom: getattr(config, nom) for nom in (
        "IS_PRODUCTION", "ACCESS_AUTH_DISABLED", "ADMIN_AUTH_DISABLED",
        "TRUST_X_USER_HEADER", "DEV_USER", "KERBEROS_DEV_PRINCIPAL",
    )}
    yield
    for nom, valeur in avant.items():
        setattr(config, nom, valeur)


@pytest.mark.parametrize("variable,valeur", [
    ("ACCESS_AUTH_DISABLED", True),
    ("ADMIN_AUTH_DISABLED", True),
    ("TRUST_X_USER_HEADER", True),
    ("DEV_USER", "alice.admin"),
    ("KERBEROS_DEV_PRINCIPAL", "alice.admin@DOCSEARCH.TEST"),
])
def test_production_refuse_de_demarrer(variable, valeur, _restaure):
    """Le point qui n'est pas négociable : l'API LÈVE, elle n'avertit pas.
    Un déploiement qui démarre est un déploiement dont personne ne relit
    les logs."""
    config.IS_PRODUCTION = True
    setattr(config, variable, valeur)

    with pytest.raises(guardrails.InsecureProductionConfig, match=variable):
        guardrails.enforce()


def test_hors_production_avertit_seulement(_restaure, caplog):
    config.IS_PRODUCTION = False
    config.TRUST_X_USER_HEADER = True

    guardrails.enforce()  # ne lève pas
    assert "TRUST_X_USER_HEADER" in caplog.text


def test_production_propre_ne_dit_rien(_restaure, caplog):
    config.IS_PRODUCTION = True
    for nom in ("ACCESS_AUTH_DISABLED", "ADMIN_AUTH_DISABLED", "TRUST_X_USER_HEADER"):
        setattr(config, nom, False)
    config.DEV_USER = ""
    config.KERBEROS_DEV_PRINCIPAL = ""

    guardrails.enforce()
    assert not caplog.text


def test_le_message_dit_quoi_faire(_restaure):
    config.IS_PRODUCTION = True
    config.DEV_USER = "alice.admin"

    with pytest.raises(guardrails.InsecureProductionConfig) as capture:
        guardrails.enforce()
    message = str(capture.value)
    assert "refuse de démarrer" in message
    assert "API_ENV" in message
