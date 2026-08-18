# sources.py — Vue générique des registres de sources
#
# Les trois registres de DocSearch déclarent des objets différents —
# `Source` porte un dossier et un drapeau OCR, `SqlSource` une requête et
# un mapping de colonnes, `WebSource` un index de crawl intermédiaire —
# mais SEPT attributs leur sont communs, et ce sont exactement ceux dont
# dépendent les décisions d'accès : name, es_index, label, description,
# searchable, collectable, allowed_groups.
#
# Ce module ne connaît AUCUN de ces trois registres. Il reçoit une
# correspondance {type: module exposant get_sources()} et en tire une vue
# uniforme. C'est ce qui lui permet de vivre dans le contrat partagé,
# vendorisé à l'identique dans plusieurs dépôts, et d'accueillir un
# quatrième type ("plugin:<nom>", lot 1) sans que rien ici ne change.
#
# ⚠️  Pourquoi ça compte pour la sécurité. « Quelles sources cet
# utilisateur peut-il atteindre » était écrit SIX fois dans docsearch-api
# — quatre boucles dans search_api.py, deux dans search_query.py, dont
# deux portant le commentaire « Identique à … — voir l'avertissement de
# cohérence en tête de fichier ». Une divergence entre la copie que lit
# /search et celle que lit le worker d'alertes ne se voit nulle part :
# elle fait notifier une alerte sur une source que l'écran n'affiche
# plus, ou l'inverse. Le premier invariant du plan (« rien ne contourne
# l'ACL ») tient parce qu'il existe UN endroit qui décide, pas six.
#
# Ce module est volontairement sans dépendance : ni Redis, ni
# Elasticsearch, ni FastAPI. Les entrées/sorties restent chez l'appelant,
# ce qui rend ces règles testables sans aucun service — et importables
# depuis un worker de fond qui n'a pas à charger l'API pour savoir ce
# qu'une source est.

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field

# Types de source connus du cœur. Un module complémentaire s'annoncera
# sous "plugin:<nom>" (lot 1) : la liste ci-dessous ne sert donc PAS à
# valider un type reçu, seulement à nommer les trois natifs là où le code
# a besoin de les citer.
TYPES_NATIFS = ("file", "sql", "web")


@dataclass(frozen=True)
class SourceEntry:
    """Ce que toute source expose, quel que soit son type."""

    name: str
    type: str
    es_index: str
    label: str
    description: str
    searchable: bool
    collectable: bool
    allowed_groups: tuple[str, ...]
    # Ordre demandé par la source quand l'utilisateur n'a rien choisi
    # (contrat 0.8.0). Seules les sources de module en déclarent un ; les
    # trois registres natifs n'ont pas l'attribut et retombent donc sur
    # la pertinence, c'est-à-dire sur leur comportement d'avant.
    tri_defaut: str
    # L'objet du registre d'origine, pour les rares usages qui ont besoin
    # d'un attribut propre au type (le mapping de colonnes d'une source
    # SQL, le dossier surveillé d'une source fichier). Hors comparaison et
    # hors repr : deux entrées décrivant la même source doivent rester
    # égales, et un message d'erreur ne doit pas déballer tout un objet de
    # registre.
    native: object = field(default=None, compare=False, repr=False)


def entry(type_: str, name: str, source: object) -> SourceEntry:
    """Normalise un objet de registre en `SourceEntry`.

    Lecture par `getattr` avec valeur de repli plutôt que par accès
    direct : les trois dataclasses ont ces attributs aujourd'hui, mais un
    type de source à venir peut n'en déclarer qu'une partie, et un
    `AttributeError` levé ici ferait échouer l'énumération ENTIÈRE —
    donc, pour `searchable_names()`, une recherche qui ne rend plus rien
    à personne.

    ⚠️  Les deux replis ne sont pas symétriques, et c'est délibéré :
    `searchable` retombe sur **False** (une source dont on ne sait pas si
    elle est cherchable ne l'est pas), `collectable` sur **True** (ce
    n'est pas un contrôle d'accès, seulement le droit d'épingler un
    document dans une collection — et c'est le défaut des trois
    registres).
    """
    return SourceEntry(
        name=name,
        type=type_,
        es_index=getattr(source, "es_index", "") or "",
        label=getattr(source, "label", "") or "",
        description=getattr(source, "description", "") or "",
        searchable=bool(getattr(source, "searchable", False)),
        collectable=bool(getattr(source, "collectable", True)),
        allowed_groups=tuple(getattr(source, "allowed_groups", ()) or ()),
        # Repli sur la pertinence, comme les deux autres booléens ci-dessus
        # : une source qui ne dit rien ne réordonne rien.
        tri_defaut=getattr(source, "tri_defaut", "") or "_score",
        native=source,
    )


def visible_to(source, user_groups) -> bool:
    """Une source dont `allowed_groups` est vide est visible par tout le
    monde (comportement historique, antérieur à cette restriction) ;
    sinon il faut être membre d'au moins un des groupes listés.

    Accepte aussi bien une `SourceEntry` qu'un objet de registre : les
    deux portent `allowed_groups`, et les appelants ont les deux en main.

    Orthogonal à l'ACL par document (`build_acl_filter`) : ceci masque
    une source en bloc, celle-là filtre les documents individuels d'une
    source par ailleurs visible.
    """
    allowed = getattr(source, "allowed_groups", ()) or ()
    return not allowed or any(g in allowed for g in user_groups)


def iter_entries(registries: Mapping[str, object]) -> Iterator[SourceEntry]:
    """Toutes les sources de tous les registres, normalisées.

    L'ordre est celui de `registries` puis celui d'insertion dans chaque
    registre. Il n'a pas de sens métier, mais il est STABLE, et deux
    appelants qui comparent leurs listes doivent obtenir le même ordre.
    """
    for type_, registry in registries.items():
        for name, source in registry.get_sources().items():
            yield entry(type_, name, source)


def searchable_entries(registries: Mapping[str, object], user_groups) -> list[SourceEntry]:
    """Sources que CET utilisateur peut chercher — deux restrictions
    indépendantes, et il faut passer les deux :

      - `searchable` : une source peut continuer d'être indexée
        normalement tout en étant retirée de la consultation ;
      - `allowed_groups` : visibilité réservée aux membres d'un groupe
        AD/LDAP, vide = aucune restriction.

    Cette liste est LA définition de « ce que cet utilisateur peut
    atteindre ». Tous les chemins d'accès s'y réfèrent : la recherche
    (fédérée ou non), l'accès direct par identifiant de document,
    l'aperçu, les suggestions, la vérification des alertes. Les deux
    doivent le rester — une restriction que seule la recherche applique
    n'est pas une restriction, juste un tri par défaut.
    """
    return [e for e in iter_entries(registries) if e.searchable and visible_to(e, user_groups)]


def searchable_names(registries: Mapping[str, object], user_groups) -> list[str]:
    """Noms seuls de `searchable_entries()` — la forme attendue par un
    filtre Elasticsearch `{"terms": {"source": [...]}}`."""
    return [e.name for e in searchable_entries(registries, user_groups)]


def collectable_names(registries: Mapping[str, object]) -> set[str]:
    """Sources dont les documents peuvent être ajoutés à une collection.

    Indépendant de `searchable` : une source peut rester cherchable
    normalement tout en étant exclue des collections. Un `set` parce que
    seul le test d'appartenance importe, jamais l'ordre — et indépendant
    de l'utilisateur, parce que le droit de VOIR un document est déjà
    tranché ailleurs.
    """
    return {e.name for e in iter_entries(registries) if e.collectable}


def find(registries: Mapping[str, object], name: str) -> SourceEntry | None:
    """Cherche `name` dans tous les registres et rend la PREMIÈRE
    correspondance, dans l'ordre de `registries` — `None` si absente
    partout.

    ⚠️  À ne jamais employer pour décider d'un ACCÈS : un nom absent des
    registres rend `None`, ce qui se lit trop facilement comme « aucune
    restriction » alors que ça veut dire « source inconnue ». Pour un
    accès, c'est `searchable_entries()` qui fait foi.
    """
    for e in iter_entries(registries):
        if e.name == name:
            return e
    return None
