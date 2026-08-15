# source_registries.py — Liaison entre les registres de ce dépôt et la
# vue générique du contrat partagé (docsearch_contract/sources.py).
#
# Tout le raisonnement — « quelles sources cet utilisateur peut-il
# atteindre », « laquelle porte ce nom », « laquelle accepte les
# collections » — vit dans le contrat, sans dépendance et testable sans
# service. Ce module-ci ne fait qu'UNE chose que le contrat ne peut pas
# faire : nommer les trois registres de docsearch-api.
#
# Pourquoi il existe plutôt qu'un appel direct au contrat depuis chaque
# fichier : la correspondance {type: module} serait alors recopiée dans
# search_api.py et dans search_query.py, et on aurait remplacé six
# copies d'une règle par deux copies d'une table. Les deux importent
# donc ce module, comme ils importent déjà field_sets() depuis un seul
# endroit.
#
# ⚠️  L'ORDRE de REGISTRES est significatif à un endroit et un seul :
# find() rend la première correspondance, donc une source fichier
# l'emporte sur une source SQL de même nom. C'est le comportement
# historique de _get_any_source(), conservé tel quel.

import file_sources_config
import plugin_sources_config
import sql_sources_config
import web_sources_config
from docsearch_contract import sources as contract_sources

REGISTRES = {
    "file":   file_sources_config,
    "sql":    sql_sources_config,
    "web":    web_sources_config,
    # Sources portées par un module complémentaire (lot 1). Le type reste
    # "plugin" au singulier quel que soit le module : c'est le champ
    # `plugin` de la source qui dit lequel, et la recherche fédérée n'a
    # aucune raison de distinguer deux modules l'un de l'autre.
    "plugin": plugin_sources_config,
}


def toutes_les_entrees():
    """Toutes les sources de tous les registres, normalisées — voir
    `docsearch_contract.sources.SourceEntry`."""
    return contract_sources.iter_entries(REGISTRES)


def entrees_cherchables(user_groups) -> list:
    """Sources que cet utilisateur peut atteindre (searchable ET groupes)."""
    return contract_sources.searchable_entries(REGISTRES, user_groups)


def noms_cherchables(user_groups) -> list[str]:
    return contract_sources.searchable_names(REGISTRES, user_groups)


def noms_collectables() -> set[str]:
    return contract_sources.collectable_names(REGISTRES)


def trouver(name: str):
    """Entrée portant ce nom, quel que soit son registre — None si
    absente partout.

    ⚠️  Jamais pour décider d'un ACCÈS : `None` veut dire « source
    inconnue », pas « aucune restriction ». Voir entrees_cherchables()."""
    return contract_sources.find(REGISTRES, name)


def visible_par(source, user_groups) -> bool:
    """Restriction par groupe AD/LDAP d'une source — accepte aussi bien
    une SourceEntry qu'un objet de registre."""
    return contract_sources.visible_to(source, user_groups)
