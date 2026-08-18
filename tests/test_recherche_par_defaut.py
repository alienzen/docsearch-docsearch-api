# tests/test_recherche_par_defaut.py — Le réglage « recherche affichée
# par défaut » est-il réellement modifiable, et réellement conservé ?
#
# Ce que ces tests protègent :
#
# 1. Le trou entre les trois listes. Un réglage d'interface vit à TROIS
#    endroits : la valeur par défaut (ui_config.DEFAULT_UI_CONFIG), le
#    champ du corps de requête (search_api.UiConfigUpdate) et la ligne qui
#    l'applique dans POST /admin/ui-config. En oublier un ne casse rien
#    visiblement : la clé s'affiche dans l'administration, l'enregistrement
#    répond 200 — et la valeur saisie est simplement ignorée. Le premier
#    test compare donc les deux listes ENTIÈRES, pour ce réglage-ci comme
#    pour tous ceux à venir.
#
# 2. La persistance elle-même, contre le VRAI Redis (principe 1 de
#    conftest.py) : un bouchon ne prouverait que ma relecture du code.
#
# ⚠️ La clé écrite ici, `docsearch:config:ui`, porte la configuration de
# l'installation de dev — ce n'est pas une clé jetable comme
# `docsearch:auth:*`. Elle est donc relue AVANT et restaurée dans un
# `finally`, valeur d'origine comprise quand elle n'existait pas (principe
# 2 : ne jamais salir l'environnement partagé).

import pytest

import ui_config
from search_api import UiConfigUpdate


def test_tout_reglage_est_modifiable_par_la_route():
    """Chaque clé de DEFAULT_UI_CONFIG a son champ dans UiConfigUpdate."""
    manquants = set(ui_config.DEFAULT_UI_CONFIG) - set(UiConfigUpdate.model_fields)
    assert not manquants, (
        f"Réglages sans champ dans UiConfigUpdate, donc impossibles à modifier "
        f"depuis l'administration : {sorted(manquants)}"
    )


def test_la_recherche_par_defaut_est_vide_a_l_installation():
    """Vide = écran d'accueil habituel : installer la fonctionnalité ne
    doit rien changer tant qu'un administrateur n'y a pas touché."""
    assert ui_config.DEFAULT_UI_CONFIG["default_search"] == ""


@pytest.mark.requires_redis
def test_la_recherche_par_defaut_est_conservee():
    client = ui_config._get_redis_client()
    avant = client.get(ui_config.UI_CONFIG_KEY)
    try:
        ui_config.set_text("default_search", "source:RH note de service")

        # Relu depuis Redis, cache local vidé : c'est la PERSISTANCE qui
        # est en cause, pas la valeur qu'on vient de poser en mémoire.
        ui_config._cache = {}
        ui_config._cache_time = 0.0
        assert ui_config.get_config()["default_search"] == "source:RH note de service"

        # Et l'effacement doit ramener l'écran d'accueil, pas laisser la
        # valeur précédente derrière lui.
        ui_config.set_text("default_search", "")
        ui_config._cache = {}
        ui_config._cache_time = 0.0
        assert ui_config.get_config()["default_search"] == ""
    finally:
        if avant is None:
            client.delete(ui_config.UI_CONFIG_KEY)
        else:
            client.set(ui_config.UI_CONFIG_KEY, avant)
        ui_config._cache = {}
        ui_config._cache_time = 0.0


@pytest.mark.requires_redis
def test_une_valeur_demesuree_est_refusee():
    """Même garde-fou que les autres champs texte : Redis ne stocke pas
    n'importe quoi, même sur demande d'un administrateur."""
    with pytest.raises(ValueError):
        ui_config.set_text("default_search", "a" * (ui_config.MAX_TEXT_PARAM_LENGTH + 1))
