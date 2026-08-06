#!/usr/bin/env python3
"""Rétro-remplissage des groupes sur les journaux déjà enregistrés.

    docker exec docsearch-api python3 backfill_groups.py          # simulation
    docker exec docsearch-api python3 backfill_groups.py --apply  # écriture

OPÉRATION EXCEPTIONNELLE, à ne pas transformer en tâche récurrente.

⚠️ SÉMANTIQUE — à comprendre avant de lancer. Les groupes sont normalement
capturés À L'ÉCRITURE (voir search_log.py), ce qui fige l'appartenance
telle qu'elle était au moment de l'événement. Ce script fait l'inverse :
il applique l'appartenance D'AUJOURD'HUI à des événements passés. Un
utilisateur ayant changé de service verra donc ses anciennes recherches
recomptées dans son service actuel.

C'est acceptable pour amorcer les statistiques après l'ajout du champ —
sans quoi l'historique reste des mois durant dans un lot « non
renseigné » qui écrase tous les autres. Ce ne l'est pas comme routine :
rejoué régulièrement, il réécrirait l'histoire à chaque mouvement de
personnel.

Garde-fous :
  - simulation par défaut, comme la purge d'index ;
  - ne touche QUE les documents dépourvus de `groups` — une valeur
    capturée à l'écriture fait foi et n'est jamais écrasée ;
  - sur les suggestions, ne traite que celles portant un `username`.
    Les suggestions ANONYMES sont laissées intactes : leur attacher un
    groupe percerait l'anonymat que leur auteur a choisi (voir
    suggestion_log.py).
"""

import os
import sys

from elasticsearch import Elasticsearch

from auth.directory import get_effective_groups

ES_HOST = os.getenv("ES_HOST", "http://localhost:9200")

# Index à traiter, et faut-il exiger un nom d'utilisateur pour agir.
# `exigence_username` n'est vrai que pour les suggestions, dont
# l'anonymat est le principe.
INDEX = [
    (os.getenv("SEARCH_LOG_INDEX", "search_logs"), False),
    (os.getenv("NPS_LOG_INDEX", "nps_logs"), False),
    (os.getenv("SUGGESTION_LOG_INDEX", "suggestions"), True),
]

SANS_GROUPES = {"bool": {"must_not": [{"exists": {"field": "groups"}}]}}


def _champ_agregeable(es: Elasticsearch, index: str, champ: str) -> str:
    """Nom du champ utilisable en agrégation.

    L'index `suggestions` déployé porte un `username` de type `text` — il
    précède la déclaration en `keyword` du module, et Elasticsearch ne
    change jamais le type d'un champ existant. Une agrégation `terms`
    dessus échoue (« Fielddata is disabled »), mais le sous-champ
    `.keyword` créé par le mapping dynamique fait l'affaire.
    """
    try:
        mapping = es.indices.get_mapping(index=index)
        props = list(mapping.values())[0]["mappings"]["properties"]
        info = props.get(champ, {})
        if info.get("type") == "text" and "keyword" in info.get("fields", {}):
            return f"{champ}.keyword"
    except Exception:
        pass
    return champ


def _filtre(exige_username: bool) -> dict:
    """Documents à compléter : sans groupes, et — pour les suggestions —
    portant un nom d'utilisateur."""
    clauses = [SANS_GROUPES]
    if exige_username:
        clauses.append({"exists": {"field": "username"}})
    return {"bool": {"filter": clauses}}


def traiter(es: Elasticsearch, index: str, exige_username: bool, appliquer: bool) -> None:
    if not es.indices.exists(index=index):
        print(f"  {index:16} index absent, rien à faire")
        return

    filtre = _filtre(exige_username)
    total = es.count(index=index, query=filtre)["count"]
    if not total:
        print(f"  {index:16} aucun document à compléter")
        return

    # Les utilisateurs concernés, pas les documents : une seule résolution
    # LDAP par personne, quel qu'en soit le nombre d'événements.
    champ = _champ_agregeable(es, index, "username")
    res = es.search(
        index=index,
        size=0,
        query=filtre,
        aggs={"users": {"terms": {"field": champ, "size": 10000}}},
    )
    utilisateurs = [b["key"] for b in res["aggregations"]["users"]["buckets"]]

    print(f"  {index:16} {total} document(s), {len(utilisateurs)} utilisateur(s)")

    modifies = sans_groupe = 0
    for username in utilisateurs:
        groupes = get_effective_groups(username)
        if not groupes:
            # Un utilisateur sans groupe resterait de toute façon dans le
            # lot « non renseigné » : écrire une liste vide n'apporte
            # rien et masquerait le fait qu'on n'a rien trouvé.
            sans_groupe += 1
            continue
        if not appliquer:
            n = es.count(
                index=index,
                query={"bool": {"filter": [filtre, {"term": {champ: username}}]}},
            )["count"]
            modifies += n
            continue
        r = es.update_by_query(
            index=index,
            query={"bool": {"filter": [filtre, {"term": {champ: username}}]}},
            script={
                "source": "ctx._source.groups = params.g",
                "params": {"g": groupes},
            },
            refresh=True,
            conflicts="proceed",
        )
        modifies += r["updated"]

    verbe = "à compléter" if not appliquer else "complété(s)"
    print(f"  {'':16} → {modifies} document(s) {verbe}"
          + (f", {sans_groupe} utilisateur(s) sans groupe LDAP" if sans_groupe else ""))


def main() -> int:
    appliquer = "--apply" in sys.argv
    es = Elasticsearch(ES_HOST)

    print("Rétro-remplissage des groupes" + ("" if appliquer else "  [SIMULATION — rien n'est écrit]"))
    print("⚠️  Applique l'appartenance LDAP D'AUJOURD'HUI à des événements passés.\n")

    for index, exige_username in INDEX:
        traiter(es, index, exige_username, appliquer)

    if not appliquer:
        print("\nRelancer avec --apply pour écrire.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
