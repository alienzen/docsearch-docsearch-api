# tests/test_source_plugin_tri.py — Le tri déclaré par un module doit
# arriver JUSQU'AU registre.
#
# Défaut constaté le 2026-08-18, en dev, après le déploiement complet du
# contrat 0.8.0 : le manifeste du module RSS déclarait bien
# « tri_defaut: date_modified », l'API savait le résoudre, l'interface
# savait l'afficher — et le registre contenait « _score ». Les résultats
# revenaient donc par pertinence, sans que rien ne signale l'écart.
#
# La cause tenait à une liste d'arguments : « manage.sh plugin install »
# appelle add_source() en nommant chaque champ du manifeste un par un, et
# `tri_defaut` n'en faisait pas partie. La valeur n'était pas rejetée,
# elle n'était jamais transmise — et valider_declaration() la ramenait
# alors à son défaut, qui est justement la pertinence.
#
# D'où ce fichier, qui garde le passage de bout en bout plutôt que la
# seule validation : le contrat était déjà testé, et il était déjà juste.

import pytest

import plugin_sources_config

BASE = {
    "name": "presse", "plugin": "rss", "es_index": "rss_presse",
    "acl_policy": "public",
}


@pytest.fixture
def registre(monkeypatch):
    """Remplace l'aller-retour Redis par un dictionnaire, et rend ce que
    add_source() a réellement voulu écrire."""
    ecrit = {}

    def _read_write(mutate):
        mutate(ecrit)
        return ecrit

    monkeypatch.setattr(plugin_sources_config, "_read_write", _read_write)
    # Le contrôle d'unicité d'index interroge les registres natifs, donc
    # Redis : hors sujet ici.
    monkeypatch.setattr(plugin_sources_config, "_verifier_index_libre",
                        lambda *a, **k: None)
    return ecrit


def test_le_tri_declare_arrive_au_registre(registre):
    """LE test de ce fichier : la forme exacte du défaut."""
    plugin_sources_config.add_source(**BASE, tri_defaut="date_modified")

    assert registre["presse"]["tri_defaut"] == "date_modified"


def test_sans_tri_declare_le_registre_porte_la_pertinence(registre):
    """Le défaut reste explicite dans l'entrée écrite, et non absent :
    c'est ce qui permet à la lecture de ne jamais avoir à deviner."""
    plugin_sources_config.add_source(**BASE)

    assert registre["presse"]["tri_defaut"] == "_score"


def test_un_tri_invalide_est_refuse_a_l_enregistrement(registre):
    """Refusé ICI plutôt qu'au moment de la recherche : une valeur que le
    cœur ne sait pas poser en clause de tri ferait échouer les shards des
    index qui ne portent pas le champ, donc la recherche fédérée entière
    — et pas seulement celle de cette source."""
    with pytest.raises(ValueError, match="Tri par défaut inconnu"):
        plugin_sources_config.add_source(**BASE, tri_defaut="popularite")

    assert registre == {}
