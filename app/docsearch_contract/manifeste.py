# manifeste.py — Déclaration d'un module complémentaire installable
#
# Un module complémentaire se livre sous forme d'une archive contenant
# CE manifeste et l'image du conteneur. `./manage.sh plugin install` le
# valide AVANT de charger quoi que ce soit — c'est tout l'intérêt de le
# porter dans un fichier séparé plutôt qu'en étiquette OCI de l'image :
# un manifeste refusé ne laisse rien derrière lui.
#
#   {"nom": "jira",
#    "version": "1.2.0",              version du MODULE
#    "contract_version": "0.3.0",     version du contrat visée
#    "image": "registre.interne/docsearch-plugins/jira:1.2.0",
#    "description": "Tickets Jira",
#    "capacites": ["ingestion"],
#    "secrets": ["jira-token"],
#    "ressources": {"cpus": "1.0", "memoire": "512m"},
#    "sources": [{"nom": "tickets", "es_index": "tickets_jira",
#                 "acl_policy": "groupes", "acl_groups": ["DL-SUPPORT"]}]}
#
# ── Ce que le manifeste ne peut pas faire ────────────────────
#
# Déclarer à quel module appartient une source : c'est `nom` qui le dit,
# injecté par la validation. Un manifeste ne peut donc pas revendiquer la
# source d'un autre module, ni s'en attribuer une déjà installée — c'est
# `manage.sh plugin install` qui refuse le nom déjà pris.
#
# Il ne peut pas non plus porter un secret : seulement le NOM d'un secret
# podman que l'administrateur aura créé. Même principe que
# `connection_ref` des sources SQL, qui nomme une variable
# d'environnement plutôt que de transporter un DSN.

import re

from .documents import version_compatible
from .erreurs import ContratInvalide
from .plugins import valider_declaration
from .version import CONTRACT_VERSION

# Capacités qu'un module peut demander. Fermée à dessein : une capacité
# inconnue est refusée à l'installation plutôt qu'ignorée, sans quoi un
# module écrit contre une version future du cœur s'installerait sans
# bruit et ne ferait qu'une partie de ce qu'il annonce.
#
#   ingestion    pousser des documents sur le topic `documents-ready`
#   service_web  exposer des routes sous /ext/<nom>/, servies par le
#                proxy ; exige `port`, celui que le module écoute dans
#                son conteneur
CAPACITES = ("ingestion", "service_web")

_NOM_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
# Référence d'image : on exige une ÉTIQUETTE explicite et on refuse
# `latest`. La production reçoit ses images par transfert manuel
# (HOWTO-deploiement-hors-ligne.md) ; une étiquette flottante rend
# impossible de savoir quelle version transférer, et fait diverger les
# machines selon la date du dernier chargement.
_IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*:[a-zA-Z0-9._-]+$")

# Bornes de ressources. Ce ne sont pas des optimisations : un module
# tiers qui part en boucle ne doit pas emporter la machine d'ingestion.
RESSOURCES_DEFAUT = {"cpus": "1.0", "memoire": "512m"}
_CPUS_RE = re.compile(r"^\d+(\.\d+)?$")
_MEMOIRE_RE = re.compile(r"^\d+[kmg]$")

CLES_CONNUES = frozenset({
    "nom", "version", "contract_version", "image", "description",
    "auteur", "capacites", "secrets", "ressources", "sources", "port",
})

# Ports que le proxy accepte de servir. Fermé plutôt qu'ouvert : le
# module écoute dans SON conteneur, sur le réseau de la pile, et un port
# libre est vite un port qui recouvre un service du cœur.
PORT_MIN, PORT_MAX = 1024, 65535


def _exiger(manifeste: dict, clef: str) -> str:
    valeur = manifeste.get(clef)
    if not valeur or not isinstance(valeur, str):
        raise ContratInvalide(f"Champ obligatoire manquant ou non textuel : '{clef}'.")
    return valeur


def valider_manifeste(manifeste) -> dict:
    """Valide un manifeste et rend sa forme normalisée.

    La compatibilité de version est celle de
    `documents.version_compatible()` — même règle que pour les messages,
    écrite une fois : tant que la majeure du contrat est 0, la mineure
    doit correspondre.
    """
    if not isinstance(manifeste, dict):
        raise ContratInvalide(
            f"Manifeste attendu sous forme d'objet JSON, reçu {type(manifeste).__name__}."
        )

    inconnues = sorted(set(manifeste) - CLES_CONNUES)
    if inconnues:
        # Refusé et non ignoré : une clé inconnue est presque toujours une
        # faute de frappe sur une clé qui comptait ("capacite" au lieu de
        # "capacites"), et l'ignorer produit un module qui s'installe en
        # faisant autre chose que ce que son auteur croit avoir écrit.
        raise ContratInvalide(
            f"Clé(s) inconnue(s) dans le manifeste : {', '.join(inconnues)} — "
            f"clés acceptées : {', '.join(sorted(CLES_CONNUES))}."
        )

    nom = _exiger(manifeste, "nom")
    if not _NOM_RE.match(nom):
        raise ContratInvalide(
            f"Nom de module invalide : '{nom}' — minuscules, chiffres, tiret et "
            "underscore, en commençant par une lettre ou un chiffre."
        )

    version = _exiger(manifeste, "version")
    if not _VERSION_RE.match(version):
        raise ContratInvalide(
            f"Version de module invalide : '{version}' — attendu majeure.mineure.correctif."
        )

    contrat = _exiger(manifeste, "contract_version")
    if not version_compatible(contrat):
        raise ContratInvalide(
            f"Le module vise le contrat {contrat}, ce cœur sert {CONTRACT_VERSION}. "
            "Mettre à jour le module, ou DocSearch."
        )

    image = _exiger(manifeste, "image")
    if not _IMAGE_RE.match(image):
        raise ContratInvalide(
            f"Référence d'image invalide : '{image}' — une étiquette explicite est "
            "exigée (registre/nom:version)."
        )
    if image.endswith(":latest"):
        raise ContratInvalide(
            "Étiquette 'latest' refusée : la production reçoit ses images par transfert "
            "manuel, une étiquette flottante rend impossible de savoir quelle version "
            "transférer et fait diverger les machines."
        )

    capacites = manifeste.get("capacites") or []
    if not isinstance(capacites, list) or not capacites:
        raise ContratInvalide(
            f"'capacites' doit lister ce que le module demande — valeurs possibles : "
            f"{', '.join(CAPACITES)}."
        )
    inconnues = [c for c in capacites if c not in CAPACITES]
    if inconnues:
        raise ContratInvalide(
            f"Capacité(s) non servie(s) par ce cœur : {', '.join(inconnues)} — "
            f"servies : {', '.join(CAPACITES)}. Une capacité annoncée mais non routée "
            "produirait un module à moitié installé."
        )

    secrets = manifeste.get("secrets") or []
    if not isinstance(secrets, list) or any(not isinstance(s, str) or not s for s in secrets):
        raise ContratInvalide("'secrets' doit être une liste de NOMS de secrets podman.")
    for s in secrets:
        if not _NOM_RE.match(s):
            raise ContratInvalide(f"Nom de secret invalide : '{s}'.")

    ressources = _valider_ressources(manifeste.get("ressources") or {})
    sources = _valider_sources(manifeste.get("sources") or [], nom)

    if "ingestion" in capacites and not sources:
        raise ContratInvalide(
            "Un module qui demande la capacité 'ingestion' doit déclarer au moins une "
            "source : sans elle, ce qu'il pousserait serait refusé par le worker."
        )

    port = manifeste.get("port")
    if "service_web" in capacites:
        if port is None:
            raise ContratInvalide(
                "Un module qui demande la capacité 'service_web' doit déclarer le 'port' "
                "qu'il écoute dans son conteneur : c'est vers lui que le proxy enverra "
                "/ext/<nom>/."
            )
        try:
            port = int(port)
        except (TypeError, ValueError):
            raise ContratInvalide(f"'port' doit être un entier, reçu {manifeste['port']!r}.") from None
        if not PORT_MIN <= port <= PORT_MAX:
            raise ContratInvalide(f"'port' hors bornes : {port} (attendu {PORT_MIN}–{PORT_MAX}).")
    elif port is not None:
        raise ContratInvalide("'port' n'a de sens qu'avec la capacité 'service_web'.")

    return {
        "port": port,
        "nom": nom,
        "version": version,
        "contract_version": contrat,
        "image": image,
        "description": manifeste.get("description") or "",
        "auteur": manifeste.get("auteur") or "",
        "capacites": list(capacites),
        "secrets": list(secrets),
        "ressources": ressources,
        "sources": sources,
    }


def _valider_ressources(ressources: dict) -> dict:
    if not isinstance(ressources, dict):
        raise ContratInvalide("'ressources' doit être un objet {cpus, memoire}.")
    resultat = {**RESSOURCES_DEFAUT, **{k: str(v) for k, v in ressources.items()}}
    inconnues = sorted(set(resultat) - set(RESSOURCES_DEFAUT))
    if inconnues:
        raise ContratInvalide(f"Ressource(s) inconnue(s) : {', '.join(inconnues)}.")
    if not _CPUS_RE.match(resultat["cpus"]):
        raise ContratInvalide(f"'cpus' invalide : '{resultat['cpus']}' — un nombre, ex. '1.5'.")
    if not _MEMOIRE_RE.match(resultat["memoire"]):
        raise ContratInvalide(
            f"'memoire' invalide : '{resultat['memoire']}' — un entier suivi de k, m ou g, ex. '512m'."
        )
    return resultat


def _valider_sources(sources, nom_module: str) -> list[dict]:
    if not isinstance(sources, list):
        raise ContratInvalide("'sources' doit être une liste.")
    resultat = []
    vus = set()
    for source in sources:
        if not isinstance(source, dict):
            raise ContratInvalide(f"Déclaration de source invalide : {source}")
        nom_source = source.get("nom")
        if not nom_source or not _NOM_RE.match(nom_source):
            raise ContratInvalide(f"Nom de source invalide dans le manifeste : '{nom_source}'.")
        if nom_source in vus:
            raise ContratInvalide(f"Source déclarée deux fois : '{nom_source}'.")
        vus.add(nom_source)

        if "plugin" in source:
            # Le propriétaire d'une source n'est pas négociable : c'est le
            # module qui la déclare. Laisser le manifeste le dire lui
            # permettrait de revendiquer la source d'un autre.
            raise ContratInvalide(
                f"La source '{nom_source}' ne peut pas déclarer 'plugin' : son module "
                f"est celui du manifeste ('{nom_module}')."
            )

        declaration = valider_declaration({
            **{k: v for k, v in source.items() if k != "nom"},
            "plugin": nom_module,
        })
        resultat.append({"nom": nom_source, **declaration})
    return resultat
