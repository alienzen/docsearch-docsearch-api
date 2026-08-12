# pinned.py — Résultats épinglés (« best bets »)
#
# Sur les quelques requêtes que tout le monde tape — « congés »,
# « télétravail », « note de frais » — le bon document est connu
# d'avance. Le laisser au hasard du classement, c'est laisser chacun le
# chercher tous les mois.
#
# Registre Redis, comme le reste de la configuration à chaud : une clé
# JSON {requête normalisée: [doc_id, ...]}. Pas d'index ES — ce sont
# quelques dizaines de lignes que l'administration modifie à la main, pas
# une donnée à chercher.
#
# ⚠️ DEUX RÈGLES, et elles ne se négocient pas :
#
# 1. **Un document épinglé n'échappe pas à l'ACL.** Il est relu à travers
#    exactement le même filtre que le reste de la recherche : celui qui
#    n'a pas le droit de le voir ne le voit pas, épinglé ou non. Un
#    épinglage est une mise en avant, jamais une autorisation.
# 2. **L'utilisateur doit savoir que le classement a été forcé.**
#    L'interface l'affiche sous une mention explicite. Un classement
#    modifié en silence est une mauvaise surprise le jour où quelqu'un
#    s'en aperçoit — et ce jour arrive.

import json
import logging
import os
import unicodedata

logger = logging.getLogger(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
PINNED_KEY = "docsearch:config:pinned"

# Au-delà, ce n'est plus une mise en avant mais un classement parallèle,
# que personne ne tient à jour.
MAX_REQUETES = 500
MAX_PAR_REQUETE = 5

_redis_client = None
_redis_indisponible_signale = False


def _get_redis_client():
    global _redis_client, _redis_indisponible_signale
    if _redis_client is not None:
        return _redis_client
    try:
        import redis
        _redis_client = redis.Redis(
            host=REDIS_HOST, port=REDIS_PORT, decode_responses=True, socket_timeout=2
        )
        _redis_client.ping()
        return _redis_client
    except Exception as e:
        if not _redis_indisponible_signale:
            logger.warning(f"[epingles] Redis injoignable ({e}) — aucun épinglage appliqué")
            _redis_indisponible_signale = True
        _redis_client = None
        return None


def normaliser(requete: str) -> str:
    """Forme de comparaison d'une requête.

    Minuscules, accents repliés, espaces réduits : « Congés », « conges »
    et «  CONGÉS  » désignent la même intention, et personne ne pensera à
    épingler les trois. Le repli d'accents se fait par décomposition
    Unicode plutôt que par une table de correspondance, qui oublierait
    toujours un caractère.
    """
    sans_accent = "".join(
        c for c in unicodedata.normalize("NFD", requete)
        if unicodedata.category(c) != "Mn"
    )
    return " ".join(sans_accent.casefold().split())


def _charger() -> dict:
    client = _get_redis_client()
    if client is None:
        return {}
    try:
        brut = client.get(PINNED_KEY)
        return json.loads(brut) if brut else {}
    except Exception as e:
        logger.warning(f"[epingles] Lecture impossible ({e})")
        return {}


def _ecrire(registre: dict) -> dict:
    client = _get_redis_client()
    if client is None:
        raise RuntimeError("Redis injoignable — épinglage non enregistré.")
    client.set(PINNED_KEY, json.dumps(registre, ensure_ascii=False))
    return registre


def lister() -> list[dict]:
    """Le registre, trié pour que l'écran d'administration ne réordonne
    pas ses lignes à chaque rechargement."""
    return [
        {"requete": requete, "documents": documents}
        for requete, documents in sorted(_charger().items())
    ]


def definir(requete: str, documents: list[str]) -> list[dict]:
    """Remplace les épinglages d'une requête. Une liste vide les retire —
    c'est le même geste que « supprimer la règle », et ça évite deux
    chemins pour un seul résultat."""
    cle = normaliser(requete)
    if not cle:
        raise ValueError("Requête vide.")
    documents = [d.strip() for d in documents if d.strip()][:MAX_PAR_REQUETE]

    registre = _charger()
    if not documents:
        registre.pop(cle, None)
    else:
        if cle not in registre and len(registre) >= MAX_REQUETES:
            raise ValueError(f"Trop de requêtes épinglées (maximum {MAX_REQUETES}).")
        registre[cle] = documents
    _ecrire(registre)
    return lister()


def pour_requete(requete: str) -> list[str]:
    """Identifiants épinglés pour cette requête, dans l'ordre voulu par
    l'administration. Liste vide si rien n'est épinglé — le cas courant,
    et le moins cher : une lecture Redis mise en cache par le client."""
    if not requete:
        return []
    return _charger().get(normaliser(requete), [])
