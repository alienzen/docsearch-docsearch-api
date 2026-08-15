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

from .documents import (
    TYPES_MESSAGE,
    construire_document,
    doc_id_pour,
    valider_message,
    verifier_emetteur,
    version_compatible,
)
from .erreurs import ContratInvalide
from .manifeste import CAPACITES, RESSOURCES_DEFAUT, valider_manifeste
from .plugins import (
    POLITIQUES_ACL,
    ChampSupplementaire,
    PluginSource,
    depuis_dict,
    nom_valide,
    valider_declaration,
)
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
    "CAPACITES",
    "CONTRACT_VERSION",
    "POLITIQUES_ACL",
    "RESSOURCES_DEFAUT",
    "TYPES_MESSAGE",
    "TYPES_NATIFS",
    "ChampSupplementaire",
    "ContratInvalide",
    "PluginSource",
    "SourceEntry",
    "collectable_names",
    "construire_document",
    "depuis_dict",
    "doc_id_pour",
    "entry",
    "find",
    "iter_entries",
    "nom_valide",
    "searchable_entries",
    "searchable_names",
    "valider_declaration",
    "valider_manifeste",
    "valider_message",
    "verifier_emetteur",
    "version_compatible",
    "visible_to",
]
