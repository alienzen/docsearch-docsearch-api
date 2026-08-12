# synonyms.py — Thésaurus métier
#
# Sur un corpus administratif, c'est le levier de pertinence le plus
# fort : sigles internes, noms de code de projet, ancien et nouveau nom
# d'un service. « DRH » et « direction des ressources humaines » désignent
# la même chose pour tout le monde sauf pour le moteur.
#
# Le jeu de règles vit dans Elasticsearch (API `_synonyms`, index système
# dédié) et non dans un fichier de l'image : il se modifie depuis le
# panneau d'administration, sans reconstruction d'image, sans redémarrage
# et sans accès réseau — compatible avec la production isolée.
#
# ⚠️ Trois propriétés, éprouvées contre le moteur avant d'écrire ce
# module (ES 9.4.3), dont deux échouent EN SILENCE si on s'y prend
# autrement — voir ANALYSE dans docsearch-ingestion/app/indexer.py :
#
# 1. Le filtre de synonymes doit passer AVANT le stemmer.
# 2. `updateable: true` n'est accepté que dans un analyseur de RECHERCHE,
#    ce qui est précisément ce qui permet de modifier le thésaurus sans
#    réindexer les documents.
# 3. Une modification est prise en compte à chaud : Elasticsearch
#    recharge lui-même les analyseurs des index qui référencent le jeu.
#
# Le champ `content` porte en plus un `search_quote_analyzer` SANS
# synonymes : une recherche entre guillemets veut dire exacte, et
# l'utilisateur qui en tape ne s'attend pas à ce qu'on élargisse sa
# requête.

import logging
import os
import re

logger = logging.getLogger(__name__)

SYNONYMS_SET = os.getenv("SYNONYMS_SET", "docsearch_fr")

# Une règle Solr/Lucene est soit « a, b, c » (équivalence : chaque terme
# en appelle les autres), soit « a, b => c » (réécriture, à sens unique).
# La seconde forme est refusée ici : elle SUPPRIME les termes d'origine à
# la recherche, ce qui surprend tout le monde et se règle très mal depuis
# une interface d'administration. L'équivalence suffit au besoin réel.
_FLECHE = re.compile(r"=>")

MAX_REGLES = 2000
MAX_LONGUEUR = 512


class RegleInvalide(ValueError):
    """Message destiné à l'administrateur, affiché tel quel."""


def _identifiant(regle: str) -> str:
    """Identifiant stable dérivé du premier terme — c'est lui qui permet
    de modifier ou supprimer une règle sans réécrire tout le jeu."""
    premier = regle.split(",")[0].strip().casefold()
    nettoye = re.sub(r"[^a-z0-9]+", "_", premier).strip("_")
    return nettoye or "regle"


def valider(regle: str) -> str:
    regle = " ".join(regle.split())
    if not regle:
        raise RegleInvalide("Règle vide.")
    if len(regle) > MAX_LONGUEUR:
        raise RegleInvalide(f"Règle trop longue (maximum {MAX_LONGUEUR} caractères).")
    if _FLECHE.search(regle):
        raise RegleInvalide(
            "La forme « a => b » n'est pas acceptée : elle remplace les termes "
            "d'origine au lieu de les compléter. Écrire « a, b » pour que les "
            "deux se trouvent l'un l'autre."
        )
    termes = [t.strip() for t in regle.split(",")]
    if len([t for t in termes if t]) < 2:
        raise RegleInvalide(
            "Une règle relie au moins deux termes, séparés par une virgule — "
            "par exemple « DRH, direction des ressources humaines »."
        )
    return ", ".join(t for t in termes if t)


def lister(es) -> list[dict]:
    """Les règles du jeu, ou une liste vide s'il n'existe pas encore.

    Un jeu inexistant n'est PAS une erreur : c'est l'état d'une
    installation où personne n'a encore écrit de synonyme, et les index
    qui le référencent fonctionnent normalement (vérifié — le filtre se
    comporte comme vide)."""
    try:
        res = es.synonyms.get_synonym(id=SYNONYMS_SET)
    except Exception as e:
        if "resource_not_found" in str(e).lower() or "404" in str(e):
            return []
        raise
    return [
        {"id": regle["id"], "regle": regle["synonyms"]}
        for regle in res.get("synonyms_set", [])
    ]


def _ecrire(es, regles: list[dict]) -> dict:
    """Réécrit le jeu entier. L'API `_synonyms` recharge d'elle-même les
    analyseurs des index concernés, et rapporte le nombre de shards
    rechargés — qu'on remonte tel quel : c'est la seule preuve que la
    modification est réellement en vigueur."""
    if len(regles) > MAX_REGLES:
        raise RegleInvalide(f"Trop de règles (maximum {MAX_REGLES}).")
    # Un jeu vide est accepté et vaut « aucun synonyme » : c'est ce qui
    # permet de retirer la dernière règle sans avoir à supprimer le jeu,
    # ce qu'Elasticsearch refuse dès qu'un index le référence.
    res = es.synonyms.put_synonym(
        id=SYNONYMS_SET,
        synonyms_set=[{"id": r["id"], "synonyms": r["regle"]} for r in regles],
    )
    details = res.get("reload_analyzers_details", {}).get("_shards", {})
    return {
        "regles": regles,
        "shards_recharges": details.get("successful", 0),
        "shards_en_echec": details.get("failed", 0),
    }


def ajouter(es, regle: str) -> dict:
    regle = valider(regle)
    identifiant = _identifiant(regle)
    regles = [r for r in lister(es) if r["id"] != identifiant]
    regles.append({"id": identifiant, "regle": regle})
    return _ecrire(es, regles)


def supprimer(es, identifiant: str) -> dict:
    """Retire une règle — y compris la dernière.

    ⚠️ On réécrit alors un jeu VIDE, on ne supprime pas le jeu :
    Elasticsearch refuse (400) de supprimer un jeu référencé par un
    index, et tous nos index de documents le référencent en permanence.
    Un jeu vide, lui, est parfaitement accepté et se comporte comme
    l'absence de synonymes. Les deux comportements ont été vérifiés
    contre le moteur, dans cet ordre — le premier a d'abord fait échouer
    un test."""
    regles = lister(es)
    restantes = [r for r in regles if r["id"] != identifiant]
    if len(restantes) == len(regles):
        raise RegleInvalide(f"Règle « {identifiant} » introuvable.")
    return _ecrire(es, restantes)


def tester(es, index: str, texte: str) -> dict:
    """Ce que le moteur comprend RÉELLEMENT d'une requête, une fois les
    synonymes appliqués.

    Sans cette vue, personne ne peut savoir si une règle est prise en
    compte : une règle mal placée ne produit aucune erreur, seulement une
    recherche qui ne trouve rien de plus qu'avant.
    """
    res = es.indices.analyze(index=index, analyzer="french_search", text=texte)
    return {"jetons": [jeton["token"] for jeton in res.get("tokens", [])]}
