# plugins.py — Modèle d'une source portée par un module complémentaire
#
# Une « source plugin » = un connecteur tiers qui pousse des documents
# DÉJÀ EXTRAITS sur le topic Kafka du cœur, lequel les indexe (voir
# documents.py pour le format des messages). Elle se déclare comme les
# autres sources — nom, index ES, libellé, groupes autorisés — plus deux
# choses qui lui sont propres :
#
#   `plugin`     le nom du module autorisé à pousser sur cette source.
#                Un message annonçant un autre module est refusé : sans
#                ça, n'importe quel connecteur installé pourrait écrire
#                dans la source d'un autre.
#   `acl_policy` la politique d'ACL, DÉCLARÉE PAR L'ADMINISTRATEUR et
#                jamais par le module. C'est l'invariant 2 du plan : un
#                module qui choisirait ses propres droits pourrait
#                publier n'importe quoi à tout le monde.
#
# Ce module est pur : il valide et modélise, il ne lit ni Redis ni
# Elasticsearch. Le stockage du registre vit dans les dépôts
# consommateurs (plugin_sources_config.py), la validation est ici pour
# qu'elle soit la même à l'enregistrement (manage.sh, côté ingestion) et
# à la lecture (docsearch-api).

import re
from dataclasses import dataclass, field

from .erreurs import ContratInvalide

# ── Politiques d'ACL ─────────────────────────────────────────
#   public   tous les documents de la source sont publics
#   groupes  ACL fixe, posée par l'administrateur (acl_groups)
#   fournie  le module fournit users/groups par document, VALIDÉS contre
#            une liste blanche (acl_principaux) ; `public` fourni par le
#            module est toujours ignoré, quelle que soit la politique.
POLITIQUES_ACL = ("public", "groupes", "fournie")

# Types ES autorisés pour un champ supplémentaire — même liste que les
# sources SQL, pour la même raison : ce sont ceux dont la recherche
# fédérée sait quoi faire (filtre, facette, tri).
TYPES_ES = ("keyword", "text", "long", "double", "date", "boolean")

# Tris qu'une source peut demander par défaut (`tri_defaut`). Ce sont les
# champs du schéma COMMUN, plus la pertinence — volontairement pas les
# champs supplémentaires de la source.
#
# La raison est côté consommateur : trier sur un champ que tous les index
# ne mappent pas exige d'en connaître le type ES, pour poser
# l'`unmapped_type` sans lequel Elasticsearch fait échouer les shards des
# index qui ne le portent pas (docsearch-api, _clause_de_tri). Ces
# cinq-là ont un type connu de tous les consommateurs ; un champ déclaré
# par un module obligerait chacun à résoudre le registre pour le trouver.
# Refusé explicitement plutôt qu'accepté puis ignoré, tant que ce besoin
# ne s'est pas présenté.
TRIS_POSSIBLES = ("_score", "date_modified", "date_created", "filename", "size")

# Nom de source/index/module : alphanumérique + tiret/underscore, jamais
# vide — même contrainte que les trois registres natifs, pour la même
# raison (un nom mal formé finirait comme composant d'une clé Redis ou
# d'un nom d'index ES).
_NOM_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
# Nom de champ ES : on autorise en plus le point, qui sert aux champs
# imbriqués — mais PAS en tête (un champ ".x" est ingérable côté ES).
_CHAMP_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")

# Champs du schéma DocSearch commun : un champ supplémentaire ne peut pas
# les redéfinir, sinon un module réécrirait le sens d'un champ dont toute
# la recherche fédérée dépend — `source` et `acl` en particulier.
CHAMPS_RESERVES = frozenset({
    "filename", "filepath", "extension", "type", "source", "content",
    "title", "author", "keywords", "date_created", "date_modified",
    "indexed_at", "doc_hash", "content_sha256", "size", "acl",
    "folder", "folder_top", "run_id",
})


@dataclass(frozen=True)
class ChampSupplementaire:
    """Champ propre à une source plugin, déclaré à l'enregistrement.

    Même rôle que le mapping de colonnes d'une source SQL : ce qui n'est
    pas déclaré ici n'a pas de place dans le mapping, et le document qui
    le porterait est refusé par Elasticsearch (`dynamic: strict`) plutôt
    que mappé au hasard."""

    nom: str
    es_type: str
    analyzer: str | None = None
    facet: bool = False
    facet_label: str | None = None


@dataclass(frozen=True)
class PluginSource:
    name: str
    plugin: str
    es_index: str
    acl_policy: str
    acl_groups: tuple[str, ...] = field(default_factory=tuple)
    acl_principaux: tuple[str, ...] = field(default_factory=tuple)
    fields: tuple[ChampSupplementaire, ...] = field(default_factory=tuple)
    label: str = ""
    searchable: bool = True
    collectable: bool = True
    description: str = ""
    allowed_groups: tuple[str, ...] = field(default_factory=tuple)
    # Ordre souhaité par la source quand l'utilisateur n'a rien choisi.
    # "_score" (la pertinence) est le défaut de la recherche fédérée : une
    # source qui ne dit rien ne change donc rien.
    tri_defaut: str = "_score"


def nom_valide(nom: str) -> bool:
    """Un nom de source/index/module est-il acceptable ?

    Exposé parce que les registres des dépôts consommateurs valident le
    nom de la SOURCE, qui n'est pas dans la déclaration (c'est la clé du
    registre) — sans quoi chacun recopierait l'expression régulière."""
    return bool(_NOM_RE.match(nom or ""))


def valider_declaration(entree: dict) -> dict:
    """Valide la déclaration d'une source plugin et rend le dict à
    stocker, normalisé. Lève `ContratInvalide` avec un message qui dit
    quoi corriger.

    Appelée à l'ENREGISTREMENT (une fois), pas à chaque lecture : le
    coût n'a pas d'importance, l'exhaustivité si.
    """
    for clef in ("plugin", "es_index", "acl_policy"):
        if not entree.get(clef):
            raise ContratInvalide(f"Champ obligatoire manquant : '{clef}'.")

    for clef in ("plugin", "es_index"):
        if not _NOM_RE.match(entree[clef]):
            raise ContratInvalide(
                f"'{clef}' invalide : '{entree[clef]}' — minuscules, chiffres, "
                "tiret et underscore, en commençant par une lettre ou un chiffre."
            )

    politique = entree["acl_policy"]
    if politique not in POLITIQUES_ACL:
        raise ContratInvalide(
            f"Politique d'ACL inconnue : '{politique}' — valeurs possibles : "
            f"{', '.join(POLITIQUES_ACL)}."
        )

    groupes = [g for g in (entree.get("acl_groups") or []) if g]
    principaux = [p for p in (entree.get("acl_principaux") or []) if p]

    if politique == "groupes" and not groupes:
        raise ContratInvalide(
            "La politique 'groupes' exige au moins un groupe (acl_groups) — sans "
            "lui, aucun document de cette source ne serait visible par personne."
        )
    if politique == "fournie" and not principaux:
        # Le cas le plus dangereux du lot, et il se referme ici : une
        # liste blanche vide se lirait naturellement comme « aucune
        # restriction », c'est-à-dire tout ouvert à ce que le module
        # décide. On l'interdit à l'enregistrement plutôt que d'avoir à
        # s'en souvenir à chaque document.
        raise ContratInvalide(
            "La politique 'fournie' exige une liste blanche de principaux "
            "(acl_principaux) : utilisateurs et groupes que le module a le droit "
            "de nommer. Une liste vide n'ouvre pas tout — elle est refusée."
        )
    if politique != "groupes" and groupes:
        raise ContratInvalide("acl_groups n'a de sens qu'avec la politique 'groupes'.")
    if politique != "fournie" and principaux:
        raise ContratInvalide("acl_principaux n'a de sens qu'avec la politique 'fournie'.")

    champs = _valider_champs(entree.get("fields") or [])

    tri = entree.get("tri_defaut") or "_score"
    if tri not in TRIS_POSSIBLES:
        raise ContratInvalide(
            f"Tri par défaut inconnu : '{tri}' — valeurs possibles : "
            f"{', '.join(TRIS_POSSIBLES)}. Un champ supplémentaire de la source "
            "n'est pas accepté ici : le consommateur doit connaître le type ES du "
            "champ trié pour ne pas casser la recherche fédérée."
        )

    return {
        "plugin":         entree["plugin"],
        "es_index":       entree["es_index"],
        "acl_policy":     politique,
        "acl_groups":     groupes,
        "acl_principaux": principaux,
        "fields":         champs,
        "label":          entree.get("label") or "",
        "description":    entree.get("description") or "",
        "searchable":     bool(entree.get("searchable", True)),
        "collectable":    bool(entree.get("collectable", True)),
        "allowed_groups": list(entree.get("allowed_groups") or []),
        "tri_defaut":     tri,
    }


def _valider_champs(champs: list) -> list[dict]:
    vus: set[str] = set()
    resultat = []
    for c in champs:
        nom = c.get("nom") or c.get("name")
        if not nom:
            raise ContratInvalide(f"Champ supplémentaire sans nom : {c}")
        if not _CHAMP_RE.match(nom):
            raise ContratInvalide(f"Nom de champ invalide : '{nom}'.")
        if nom in CHAMPS_RESERVES:
            raise ContratInvalide(
                f"'{nom}' est un champ du schéma DocSearch commun : un module ne "
                "peut pas le redéfinir. Choisir un autre nom."
            )
        if nom in vus:
            raise ContratInvalide(f"Champ déclaré deux fois : '{nom}'.")
        es_type = c.get("es_type")
        if es_type not in TYPES_ES:
            raise ContratInvalide(
                f"Type ES invalide pour '{nom}' : '{es_type}' — valeurs possibles : "
                f"{', '.join(TYPES_ES)}."
            )
        if c.get("analyzer") and es_type != "text":
            raise ContratInvalide(f"'analyzer' n'a de sens que pour es_type='text' ('{nom}').")
        if c.get("facet") and es_type != "keyword":
            # Même piège que les facettes SQL : une agrégation `terms` sur
            # un champ `text` n'a pas de doc_values, le shard échoue, et
            # _verifier_shards() refuse alors la recherche fédérée
            # ENTIÈRE — pas seulement cette source.
            raise ContratInvalide(
                f"Une facette exige es_type='keyword' ('{nom}' est '{es_type}') : "
                "une agrégation sur un champ 'text' fait échouer la recherche fédérée."
            )
        vus.add(nom)
        resultat.append({
            "nom": nom, "es_type": es_type,
            "analyzer": c.get("analyzer"),
            "facet": bool(c.get("facet", False)),
            "facet_label": c.get("facet_label"),
        })
    return resultat


def depuis_dict(name: str, entree: dict) -> PluginSource:
    """Reconstruit une `PluginSource` depuis sa forme stockée.

    Tolérant par nécessité — c'est le chemin de LECTURE, emprunté à
    chaque passage : une entrée écrite par une version antérieure du
    contrat doit continuer de se lire. Les clés inconnues sont ignorées,
    les manquantes prennent leur défaut."""
    champs = tuple(
        ChampSupplementaire(
            nom=c["nom"], es_type=c["es_type"], analyzer=c.get("analyzer"),
            facet=bool(c.get("facet", False)), facet_label=c.get("facet_label"),
        )
        for c in (entree.get("fields") or [])
    )
    return PluginSource(
        name=name,
        plugin=entree.get("plugin", ""),
        es_index=entree.get("es_index", ""),
        acl_policy=entree.get("acl_policy", "groupes"),
        acl_groups=tuple(entree.get("acl_groups") or ()),
        acl_principaux=tuple(entree.get("acl_principaux") or ()),
        fields=champs,
        label=entree.get("label", "") or "",
        searchable=bool(entree.get("searchable", True)),
        collectable=bool(entree.get("collectable", True)),
        description=entree.get("description", "") or "",
        allowed_groups=tuple(entree.get("allowed_groups") or ()),
        # Défaut sur une entrée écrite avant le contrat 0.8 : elle n'a pas
        # la clé, et la pertinence est bien ce qu'elle obtenait.
        tri_defaut=entree.get("tri_defaut") or "_score",
    )
