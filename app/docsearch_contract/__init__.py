# docsearch_contract — Contrat partagé entre les dépôts de DocSearch
#
# ⚠️  SOURCE DE VÉRITÉ : docsearch-infra/contract/. Les copies présentes
# dans les autres dépôts sont GÉNÉRÉES par « ./manage.sh sync-contract »
# et ne doivent jamais être modifiées sur place — un test de dérive les
# refuse (voir contract/README.md).
#
# Ce paquet ne contient que des règles SANS dépendance : ni Redis, ni
# Elasticsearch, ni FastAPI. Tout ce qui fait une entrée/sortie reste
# dans les dépôts consommateurs. C'est ce qui permet de le vendoriser
# partout sans rien tirer derrière lui, et de le tester sans lancer un
# seul service.

from .sources import (
    TYPES_NATIFS,
    SourceEntry,
    collectable_names,
    entry,
    find,
    iter_entries,
    searchable_entries,
    searchable_names,
    visible_to,
)
from .version import CONTRACT_VERSION

__all__ = [
    "CONTRACT_VERSION",
    "TYPES_NATIFS",
    "SourceEntry",
    "collectable_names",
    "entry",
    "find",
    "iter_entries",
    "searchable_entries",
    "searchable_names",
    "visible_to",
]
