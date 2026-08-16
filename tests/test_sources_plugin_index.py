# tests/test_sources_plugin_index.py — Deux sources ne peuvent pas
# partager un index Elasticsearch.
#
# Ce n'est pas une règle de nommage. `plugin_indexer.reconcilier()`
# supprime, à chaque `run_end`, tout document de `source.es_index` qui ne
# porte pas le `run_id` de la passe qui vient de finir — et il filtre sur
# l'INDEX, jamais sur la source. Deux sources dans le même index se
# suppriment donc mutuellement leurs documents à chaque passe, jusqu'à ce
# que le garde-fou des 50 % (RECONCILE_MAX_DELETE_RATIO) bloque la
# réconciliation définitivement : plus rien n'est jamais nettoyé, et le
# journal ressemble à une panne du module.
#
# Rien ne l'empêchait : les registres fichiers et SQL vérifiaient déjà
# l'unicité de leur index — sql_sources_config.add_source va jusqu'à
# vérifier contre les sources fichiers — mais le registre des sources de
# modules, arrivé après, ne le faisait pas du tout. Un manifeste pouvait
# donc déclarer deux sources sur le même index, et « add-plugin-source »
# marcher sur l'index d'une source native.
#
# Les quatre familles de sources partagent l'alias de recherche fédérée :
# le contrôle porte donc sur les quatre registres.

import pytest

import plugin_sources_config


def _source(es_index: str, plugin: str = "jira") -> dict:
    """Entrée brute du registre, telle que _read_write la manipule."""
    return {
        "plugin": plugin, "es_index": es_index, "acl_policy": "groupes",
        "acl_groups": ["DL-SUPPORT"], "acl_principaux": [], "fields": [],
        "label": "", "searchable": True, "collectable": True,
        "allowed_groups": [],
    }


class _SourceNative:
    """Ce que get_sources() rend dans les trois registres natifs : un objet
    porteur d'`es_index`, et de `crawl_index` pour les sources web."""

    def __init__(self, es_index: str, crawl_index: str = ""):
        self.es_index = es_index
        self.crawl_index = crawl_index


@pytest.fixture
def sans_sources_natives(monkeypatch):
    """Aucune source native, pour que les tests d'index entre modules ne
    dépendent pas du contenu du Redis de développement."""
    for module in ("file_sources_config", "sql_sources_config", "web_sources_config"):
        monkeypatch.setattr(f"{module}.get_sources", dict, raising=True)


def test_deux_sources_de_modules_sur_le_meme_index_refusees():
    """LE test de ce fichier : la forme exacte qui vidait un index à
    chaque passe.

    Aucun accès à Redis — le contrôle entre sources de modules précède
    les trois autres registres, et lève avant de les interroger."""
    existantes = {"tickets": _source("tickets_jira")}

    with pytest.raises(ValueError, match="déjà utilisé par la source plugin 'tickets'"):
        plugin_sources_config._verifier_index_libre(
            "commentaires", "tickets_jira", existantes,
        )


def test_une_source_conserve_son_propre_index(sans_sources_natives):
    """Réinstaller un module réenregistre ses sources à l'identique : une
    source qui retrouve SON index n'est pas un conflit, sans quoi aucune
    mise à jour de module ne passerait."""
    existantes = {"tickets": _source("tickets_jira")}

    plugin_sources_config._verifier_index_libre("tickets", "tickets_jira", existantes)


def test_index_d_une_source_native_refuse(monkeypatch):
    """Le cas que « add-plugin-source » laissait passer : un module qui
    vise l'index d'une source fichier. Les documents du module y
    entreraient avec un mapping étranger, et la réconciliation du module
    supprimerait les documents de la source fichier."""
    monkeypatch.setattr(
        "file_sources_config.get_sources", lambda: {"finance": _SourceNative("finance_docs")},
    )
    monkeypatch.setattr("sql_sources_config.get_sources", dict)
    monkeypatch.setattr("web_sources_config.get_sources", dict)

    with pytest.raises(ValueError, match="source fichier 'finance'"):
        plugin_sources_config._verifier_index_libre("tickets", "finance_docs", {})


def test_index_de_crawl_d_une_source_web_refuse(monkeypatch):
    """Une source web occupe DEUX index : celui que le crawler écrit et
    celui que web_indexer.py en dérive. Le second est le plus évident, le
    premier est celui qu'on oublie — y écrire depuis un module casserait
    la transformation de l'un vers l'autre."""
    monkeypatch.setattr("file_sources_config.get_sources", dict)
    monkeypatch.setattr("sql_sources_config.get_sources", dict)
    monkeypatch.setattr(
        "web_sources_config.get_sources",
        lambda: {"decisions": _SourceNative("cc_decisions", crawl_index="cc_decisions_raw")},
    )

    with pytest.raises(ValueError, match="source web 'decisions'"):
        plugin_sources_config._verifier_index_libre("tickets", "cc_decisions_raw", {})


@pytest.mark.requires_redis
def test_add_source_refuse_avant_d_ecrire_dans_redis(monkeypatch):
    """La garantie qui compte pour l'exploitation : « manage.sh
    add-plugin-source » et « plugin install » passent tous deux par
    add_source(), et le registre n'est pas modifié quand l'index est
    pris.

    Le contrôle est fait pendant la mutation, donc après la lecture de
    Redis mais avant l'écriture — d'où le marqueur, contrairement au test
    équivalent des sources SQL dont la validation est purement locale."""
    monkeypatch.setattr(
        "file_sources_config.get_sources",
        lambda: {"finance": _SourceNative("sonde_index_pris_test")},
    )
    monkeypatch.setattr("sql_sources_config.get_sources", dict)
    monkeypatch.setattr("web_sources_config.get_sources", dict)

    with pytest.raises(ValueError, match="déjà utilisé"):
        plugin_sources_config.add_source(
            name="sonde_collision_plugin_test",
            plugin="jira",
            es_index="sonde_index_pris_test",
            acl_policy="groupes",
            acl_groups=["DL-SUPPORT"],
        )

    assert "sonde_collision_plugin_test" not in plugin_sources_config.get_sources()
