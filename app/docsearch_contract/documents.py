# documents.py — Contrat des messages poussés par un module complémentaire
#
# Un module complémentaire ne parle jamais à Elasticsearch (invariant 1
# du plan). Il publie des messages JSON sur le topic Kafka
# `documents-ready`, qu'un worker du cœur valide puis indexe. Ce module
# décrit ces messages et construit le document final — sans rien savoir
# de Kafka ni d'ES, pour rester testable sans service.
#
# ── L'enveloppe ──────────────────────────────────────────────
#
#   {"contract_version": "0.2.0",   version du contrat visée
#    "plugin":  "jira",             module émetteur
#    "source":  "tickets",          source déclarée dans le registre
#    "run_id":  "2026-08-15T09:00:00Z-3f2a",   identifiant de PASSE
#    "type":    "document" | "delete" | "run_end",
#    "document": {...}  |  "doc_id": "..."}
#
# ── Pourquoi un run_id ───────────────────────────────────────
#
# C'est ce qui permet de SUPPRIMER. Les connecteurs SQL et web relisent
# leur source en entier à chaque passage et réconcilient par différence
# d'identifiants ; un module complémentaire, lui, pousse — le cœur ne
# peut pas deviner ce qu'il n'a pas reçu. Chaque passe porte donc un
# `run_id`, écrit sur chaque document, et le message `run_end` déclenche
# la purge des documents de cette source restés sur une passe
# antérieure. Sans ce mécanisme, aucun module ne saurait supprimer, et
# personne ne s'en apercevrait avant des mois.
#
# ── Ce que le module ne décide PAS ───────────────────────────
#
# L'index de destination (déduit du registre), le nom de la source
# (vérifié contre le registre), l'ACL (politique déclarée par
# l'administrateur), et `indexed_at`. Tout le reste vient de lui.

import hashlib
from datetime import datetime, timezone

from .erreurs import ContratInvalide
from .plugins import PluginSource
from .version import CONTRACT_VERSION

TYPES_MESSAGE = ("document", "delete", "run_end")

# Longueur maximale d'un identifiant fourni par un module. Il ne finit
# pas tel quel dans ES (on en prend l'empreinte), mais il est journalisé
# et comparé : un identifiant d'un mégaoctet n'a aucune raison d'être.
MAX_LONGUEUR_IDENTIFIANT = 512


def version_compatible(declaree: str) -> bool:
    """Le contrat déclaré par un module est-il servi par cette version ?

    Tant que la majeure est 0, la forme du contrat n'est pas figée : on
    exige `majeure.mineure` identiques. À partir de 1.0, la majeure
    suffira — c'est le sens d'une version sémantique, et c'est aussi
    l'engagement qu'on ne peut pas prendre avant que les lots 1 et 2
    aient éprouvé la forme.
    """
    try:
        maj_d, min_d, _ = str(declaree).split(".")
        maj_c, min_c, _ = CONTRACT_VERSION.split(".")
    except (ValueError, AttributeError):
        return False
    if maj_c == "0":
        return (maj_d, min_d) == (maj_c, min_c)
    return maj_d == maj_c


def valider_message(message) -> dict:
    """Valide l'enveloppe d'un message et la rend normalisée.

    Ne valide PAS le contenu du document (voir construire_document) :
    l'enveloppe seule suffit à décider si le message concerne une source
    connue et un module autorisé, et c'est ce contrôle-là qui protège.
    """
    if not isinstance(message, dict):
        raise ContratInvalide(f"Message attendu sous forme d'objet JSON, reçu {type(message).__name__}.")

    version = message.get("contract_version")
    if not version_compatible(version):
        raise ContratInvalide(
            f"Version de contrat incompatible : '{version}' (le cœur sert {CONTRACT_VERSION}). "
            "Le module doit être mis à jour, ou le cœur."
        )

    type_ = message.get("type")
    if type_ not in TYPES_MESSAGE:
        raise ContratInvalide(
            f"Type de message inconnu : '{type_}' — valeurs possibles : {', '.join(TYPES_MESSAGE)}."
        )

    for clef in ("plugin", "source", "run_id"):
        valeur = message.get(clef)
        if not valeur or not isinstance(valeur, str):
            raise ContratInvalide(f"Champ d'enveloppe obligatoire manquant ou non textuel : '{clef}'.")

    if type_ == "document" and not isinstance(message.get("document"), dict):
        raise ContratInvalide("Message de type 'document' sans objet 'document'.")
    if type_ == "delete" and not message.get("doc_id"):
        raise ContratInvalide("Message de type 'delete' sans 'doc_id'.")

    return {
        "contract_version": version,
        "plugin":  message["plugin"],
        "source":  message["source"],
        "run_id":  message["run_id"],
        "type":    type_,
        "document": message.get("document"),
        "doc_id":   message.get("doc_id"),
    }


def verifier_emetteur(source: PluginSource, message: dict) -> None:
    """Le module qui pousse est-il celui à qui cette source appartient ?

    Sans ce contrôle, tout module installé pourrait écrire dans la
    source d'un autre — y compris dans une source dont la politique
    d'ACL est plus permissive que la sienne. C'est le seul endroit qui
    l'empêche : Kafka n'a pas de notion d'émetteur digne de confiance
    ici, c'est le registre qui fait foi.
    """
    if message["plugin"] != source.plugin:
        raise ContratInvalide(
            f"Le module '{message['plugin']}' pousse sur la source '{source.name}', "
            f"qui appartient à '{source.plugin}'. Message refusé."
        )


def doc_id_pour(source_name: str, identifiant: str) -> str:
    """Identifiant ES d'un document poussé.

    Empreinte de « source::identifiant » et non de l'identifiant seul :
    deux sources peuvent légitimement employer le même identifiant
    (« 1234 »), et elles partagent l'alias de recherche fédérée. MD5
    comme partout ailleurs dans le produit (indexer.py, web_indexer.py)
    — c'est une clé de répartition, pas une empreinte de sécurité, d'où
    `usedforsecurity=False`.
    """
    return hashlib.md5(f"{source_name}::{identifiant}".encode(), usedforsecurity=False).hexdigest()


def _acl_du_document(source: PluginSource, charge: dict) -> dict:
    """ACL finale d'un document, selon la politique de la SOURCE.

    ⚠️  `acl.public` proposé par un module est ignoré dans les trois
    politiques, sans exception. C'est le seul champ dont l'acceptation
    naïve ouvrirait tout le corpus : `build_acl_filter()` côté API rend
    visible à TOUT LE MONDE un document portant `acl.public: true`.
    """
    if source.acl_policy == "public":
        return {"public": True}

    if source.acl_policy == "groupes":
        return {"public": False, "groups": list(source.acl_groups)}

    # Politique "fournie" : on ne garde que les principaux figurant dans
    # la liste blanche. Un principal écarté l'est bruyamment (le worker
    # journalise), parce qu'une ACL silencieusement rétrécie donne un
    # document introuvable sans rien à quoi le rattacher.
    fournie = charge.get("acl") or {}
    autorises = set(source.acl_principaux)
    users  = [u for u in (fournie.get("users") or [])  if u in autorises]
    groups = [g for g in (fournie.get("groups") or []) if g in autorises]
    refuses = sorted(
        (set(fournie.get("users") or []) | set(fournie.get("groups") or [])) - autorises
    )

    if not users and not groups:
        raise ContratInvalide(
            "Aucun principal autorisé dans l'ACL fournie"
            + (f" (refusés : {', '.join(refuses)})" if refuses else " (aucun principal fourni)")
            + f" — la liste blanche de '{source.name}' est : {', '.join(source.acl_principaux)}. "
            "Document refusé plutôt qu'indexé invisible."
        )

    acl = {"public": False, "users": users, "groups": groups}
    if refuses:
        acl["_refuses"] = refuses   # retiré par construire_document, sert au journal
    return acl


def construire_document(source: PluginSource, charge: dict, run_id: str,
                        horodatage: str | None = None) -> tuple[str, dict, list[str]]:
    """Document Elasticsearch final à partir de la charge d'un module.

    Rend `(doc_id, document, principaux_refuses)`. Lève `ContratInvalide`
    si la charge ne permet pas de construire un document indexable —
    identifiant absent, champ non déclaré, ACL vide après filtrage.
    """
    identifiant = charge.get("id")
    if not identifiant or not isinstance(identifiant, str):
        raise ContratInvalide("Document sans 'id' textuel : impossible de lui donner une identité stable.")
    if len(identifiant) > MAX_LONGUEUR_IDENTIFIANT:
        raise ContratInvalide(
            f"'id' trop long ({len(identifiant)} caractères, maximum {MAX_LONGUEUR_IDENTIFIANT})."
        )

    declares = {c.nom for c in source.fields}
    supplementaires = charge.get("extra") or {}
    if not isinstance(supplementaires, dict):
        raise ContratInvalide("'extra' doit être un objet.")
    inconnus = sorted(set(supplementaires) - declares)
    if inconnus:
        # Refusé ICI et pas laissé à Elasticsearch : `dynamic: strict`
        # rejetterait aussi le document, mais dans un message d'erreur de
        # bulk noyé dans un lot, sans dire quel module ni quelle source.
        raise ContratInvalide(
            f"Champ(s) non déclaré(s) dans le registre : {', '.join(inconnus)} — "
            f"déclarés pour '{source.name}' : {', '.join(sorted(declares)) or '(aucun)'}."
        )

    acl = _acl_du_document(source, charge)
    refuses = acl.pop("_refuses", [])

    document = {
        # `filename`/`filepath` : mêmes noms que les trois autres chemins
        # d'indexation, pour que la carte de résultat, les facettes et le
        # tri de la recherche fédérée traitent ce document comme les
        # autres, sans cas particulier côté interface.
        "filename":      charge.get("filename") or charge.get("title") or identifiant,
        # Repli quand le module ne donne pas d'URL. Le séparateur n'est
        # PAS « :: » : c'est la convention des membres d'archive
        # (« archive.zip::membre »), et l'interface s'en sert pour poser
        # « Extrait d'une archive » sur la carte de résultat — mention
        # fausse et déroutante sur un document poussé par un module.
        # Constaté à l'écran le 2026-08-16, sur le module d'exemple.
        "filepath":      charge.get("url") or f"plugin:{source.name}/{identifiant}",
        "extension":     charge.get("extension") or "",
        "type":          "plugin",
        "source":        source.name,
        "content":       charge.get("content") or "",
        "title":         charge.get("title") or "",
        "author":        charge.get("author") or "",
        "keywords":      list(charge.get("keywords") or []),
        "date_created":  charge.get("date_created"),
        "date_modified": charge.get("date_modified"),
        "indexed_at":    horodatage or datetime.now(timezone.utc).isoformat(),
        # Passe qui a produit ce document — c'est lui que la
        # réconciliation compare (voir l'en-tête de ce module).
        "run_id":        run_id,
        "acl":           acl,
    }
    document.update(supplementaires)

    # Les dates absentes sont retirées plutôt qu'envoyées à null : ES
    # accepte le null, mais un champ date à null se trie et se facette
    # différemment d'un champ absent, et les trois autres chemins
    # d'indexation omettent le champ.
    for clef in ("date_created", "date_modified"):
        if document[clef] is None:
            del document[clef]

    return doc_id_pour(source.name, identifiant), document, refuses
