# interface.py — Ce qu'un module peut ajouter à l'écran
#
# Un module complémentaire ne livre PAS de JavaScript dans le paquet de
# l'interface. Il déclare des points d'accroche dans un vocabulaire fixe,
# et le cœur les rend avec ses propres composants DSFR. Deux raisons, et
# elles ne sont pas négociables (§3 du plan) :
#
#   - sur un service de l'État, du JS tiers injecté dans la page de
#     recherche ferait porter au cœur la conformité RGAA de code qu'il
#     n'écrit pas ;
#   - une XSS dans cette page vaut contournement d'ACL côté navigateur :
#     la session y est, et l'API répond à qui la porte.
#
# ── Vocabulaire ──────────────────────────────────────────────
#
#   nav          une entrée dans le menu de l'interface, qui mène à une
#                adresse du module (donc sous /ext/<nom>/)
#   admin_panel  des réglages TYPÉS, rendus par le cœur dans l'écran
#                d'administration — le module n'envoie aucun formulaire,
#                il décrit ce qu'il veut régler et le cœur dessine
#   result_action un lien sur chaque carte de résultat, qui porte
#                l'identifiant du document au module
#   page         un écran entier du module, encadré par l'interface du
#                produit
#
# Le vocabulaire est désormais complet : les quatre accroches du §3 sont
# servies.
#
# ── Comment un réglage atteint le module ─────────────────────
#
# Par VARIABLE D'ENVIRONNEMENT, à la (re)génération de son unité, donc
# au redémarrage — arbitré le 2026-08-16. Depuis l'isolement réseau, un
# module ne voit ni Redis ni Elasticsearch : il ne peut pas lire lui-même
# ce qu'un administrateur vient de régler. Les deux autres voies
# envisagées demandaient soit une identité de module à concevoir et à
# garder (route interrogée par le module), soit des réglages
# machine-locaux alors que tout le reste de la configuration est commun à
# la grappe (fichier monté).
#
# ⚠️  Le nom de la variable est PRÉFIXÉ (`DOCSEARCH_OPT_<CLÉ>`), et ce
# n'est pas cosmétique : sans préfixe, un module déclarant un réglage
# nommé `kafka_bootstrap` ou `docsearch_api_url` réécrirait la
# configuration que le cœur lui impose.

import re

from .erreurs import ContratInvalide

ACCROCHES = ("nav", "admin_panel", "result_action", "page")

# Une entrée de menu ne peut viser QUE le module qui la déclare. Sans ce
# contrôle, un module poserait dans le menu de tout le monde un lien vers
# n'importe où — /admin.html, ou un site extérieur.
_CHEMIN_RE = re.compile(r"^/ext/[a-z0-9][a-z0-9_-]*/[a-zA-Z0-9._~/-]*$")
# Icônes DSFR : le cœur ne rend que ce vocabulaire, et une classe libre
# permettrait d'injecter n'importe quel nom de classe dans le rendu.
_ICONE_RE = re.compile(r"^fr-icon-[a-z0-9-]+$")

LONGUEUR_MAX_LIBELLE = 40

# ── Réglages d'un panneau d'administration ───────────────────
TYPES_REGLAGE = ("booleen", "texte", "liste")
# Clé d'un réglage : minuscules, chiffres, underscore. Elle devient le
# suffixe d'une variable d'environnement, d'où l'absence de tiret.
_CLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
PREFIXE_VARIABLE = "DOCSEARCH_OPT_"
# Bornes : un panneau est rendu dans un écran partagé, et sa valeur finit
# dans une unité systemd. Ni l'un ni l'autre n'accepte l'illimité.
MAX_REGLAGES = 20
LONGUEUR_MAX_VALEUR = 500

# Actions posées sur chaque carte de résultat — voir _valider_actions().
MAX_ACTIONS_RESULTAT = 3


def variable_de(cle: str) -> str:
    """Nom de la variable d'environnement portant ce réglage."""
    return f"{PREFIXE_VARIABLE}{cle.upper()}"


def valider_interface(interface, nom_module: str) -> dict:
    """Valide le bloc `interface` d'un manifeste et rend sa forme
    normalisée. Lève `ContratInvalide`."""
    if not isinstance(interface, dict):
        raise ContratInvalide("'interface' doit être un objet.")

    inconnues = sorted(set(interface) - set(ACCROCHES))
    if inconnues:
        raise ContratInvalide(
            f"Accroche(s) d'interface non servie(s) : {', '.join(inconnues)} — "
            f"servies : {', '.join(ACCROCHES)}. Une accroche annoncée mais non "
            "rendue produirait un module qui promet un écran que rien n'affiche."
        )

    resultat_panneau = _valider_panneau(interface.get("admin_panel") or [])
    resultat_actions = _valider_actions(interface.get("result_action") or [], nom_module)
    resultat_pages = _valider_pages(interface.get("page") or [], nom_module)

    entrees = interface.get("nav") or []
    if not isinstance(entrees, list):
        raise ContratInvalide("'nav' doit être une liste d'entrées de menu.")

    resultat = []
    libelles = set()
    for entree in entrees:
        if not isinstance(entree, dict):
            raise ContratInvalide(f"Entrée de menu invalide : {entree}")

        libelle = (entree.get("libelle") or "").strip()
        if not libelle:
            raise ContratInvalide("Entrée de menu sans libellé.")
        if len(libelle) > LONGUEUR_MAX_LIBELLE:
            raise ContratInvalide(
                f"Libellé trop long ({len(libelle)} caractères, maximum "
                f"{LONGUEUR_MAX_LIBELLE}) : « {libelle[:50]}… »"
            )
        if libelle in libelles:
            raise ContratInvalide(f"Entrée de menu déclarée deux fois : « {libelle} »")
        libelles.add(libelle)

        chemin = entree.get("chemin") or ""
        if not _CHEMIN_RE.match(chemin):
            raise ContratInvalide(
                f"Chemin d'entrée de menu invalide : '{chemin}' — attendu une adresse "
                f"sous /ext/{nom_module}/."
            )
        # Le préfixe doit être CELUI du module : un module ne pose pas
        # d'entrée menant chez un autre.
        if not chemin.startswith(f"/ext/{nom_module}/"):
            raise ContratInvalide(
                f"L'entrée « {libelle} » vise '{chemin}', hors de /ext/{nom_module}/ : "
                "un module ne peut poser dans le menu qu'un lien vers lui-même."
            )

        icone = entree.get("icone")
        if icone is not None and not _ICONE_RE.match(icone):
            raise ContratInvalide(
                f"Icône inconnue : '{icone}' — attendu une classe DSFR 'fr-icon-…'."
            )

        resultat.append({"libelle": libelle, "chemin": chemin, "icone": icone})

    return {
        "nav": resultat,
        "admin_panel": resultat_panneau,
        "result_action": resultat_actions,
        "page": resultat_pages,
    }


def _valider_lien(entree: dict, nom_module: str, quoi: str, vus: set) -> dict:
    """Contrôles communs à une entrée de menu, une action de résultat et
    une page : un libellé borné, une icône du vocabulaire DSFR, et un
    chemin qui ne peut viser QUE le module qui le déclare."""
    libelle = (entree.get("libelle") or entree.get("titre") or "").strip()
    if not libelle:
        raise ContratInvalide(f"{quoi} sans libellé.")
    if len(libelle) > LONGUEUR_MAX_LIBELLE:
        raise ContratInvalide(
            f"{quoi} : libellé trop long ({len(libelle)} caractères, maximum "
            f"{LONGUEUR_MAX_LIBELLE})."
        )
    if libelle in vus:
        raise ContratInvalide(f"{quoi} déclaré deux fois : « {libelle} »")
    vus.add(libelle)

    chemin = entree.get("chemin") or ""
    if not _CHEMIN_RE.match(chemin):
        raise ContratInvalide(
            f"{quoi} : chemin invalide '{chemin}' — attendu une adresse sous "
            f"/ext/{nom_module}/."
        )
    if not chemin.startswith(f"/ext/{nom_module}/"):
        raise ContratInvalide(
            f"{quoi} « {libelle} » vise '{chemin}', hors de /ext/{nom_module}/ : "
            "un module ne peut pointer que vers lui-même."
        )

    icone = entree.get("icone")
    if icone is not None and not _ICONE_RE.match(icone):
        raise ContratInvalide(f"{quoi} : icône inconnue '{icone}' — attendu une classe DSFR.")

    return {"libelle": libelle, "chemin": chemin, "icone": icone}


def _valider_actions(actions, nom_module: str) -> list[dict]:
    """Actions posées sur CHAQUE carte de résultat.

    Bornées à trois : elles s'ajoutent aux boutons du produit sur une
    carte déjà dense, et une carte de résultat qui devient une barre
    d'outils ne sert plus à lire.

    ⚠️  Le cœur ajoute l'identifiant du document en paramètre `doc`. Le
    module doit RELIRE ce document par l'API, avec le cookie de
    l'utilisateur : recevoir un identifiant ne prouve pas que celui qui
    l'envoie a le droit de le lire."""
    if not isinstance(actions, list):
        raise ContratInvalide("'result_action' doit être une liste.")
    if len(actions) > MAX_ACTIONS_RESULTAT:
        raise ContratInvalide(
            f"{len(actions)} actions déclarées, maximum {MAX_ACTIONS_RESULTAT} — une "
            "carte de résultat sert à lire, pas à porter une barre d'outils."
        )
    vus: set = set()
    return [_valider_lien(a, nom_module, "Action de résultat", vus) for a in actions if isinstance(a, dict) or _refuser(a)]


def _valider_pages(pages, nom_module: str) -> list[dict]:
    """Écrans entiers du module, encadrés par l'interface du produit.

    Une seule par module : le cœur n'a qu'une page hôte, et deux écrans
    se distinguent par leur adresse à l'intérieur du module, pas par deux
    déclarations."""
    if not isinstance(pages, list):
        raise ContratInvalide("'page' doit être une liste.")
    if len(pages) > 1:
        raise ContratInvalide(
            "Une seule page par module : deux écrans se distinguent par leur "
            "adresse à l'intérieur du module."
        )
    vus: set = set()
    return [_valider_lien(p, nom_module, "Page", vus) for p in pages if isinstance(p, dict) or _refuser(p)]


def _refuser(valeur):
    raise ContratInvalide(f"Déclaration invalide : {valeur!r}")


def _valider_panneau(reglages) -> list[dict]:
    """Valide les réglages déclarés par un module.

    Ce que le cœur rendra est un formulaire DSFR construit à partir de
    cette description — jamais du balisage venu du module. C'est tout
    l'intérêt d'un vocabulaire fermé : trois types, et le cœur sait les
    dessiner tous les trois de façon accessible."""
    if not isinstance(reglages, list):
        raise ContratInvalide("'admin_panel' doit être une liste de réglages.")
    if len(reglages) > MAX_REGLAGES:
        raise ContratInvalide(
            f"{len(reglages)} réglages déclarés, maximum {MAX_REGLAGES} — un panneau "
            "d'administration se lit, il ne se parcourt pas."
        )

    resultat = []
    vues = set()
    for reglage in reglages:
        if not isinstance(reglage, dict):
            raise ContratInvalide(f"Réglage invalide : {reglage}")

        cle = reglage.get("cle") or ""
        if not _CLE_RE.match(cle):
            raise ContratInvalide(
                f"Clé de réglage invalide : '{cle}' — minuscules, chiffres et "
                "underscore, en commençant par une lettre (elle devient le suffixe "
                "d'une variable d'environnement)."
            )
        if cle in vues:
            raise ContratInvalide(f"Réglage déclaré deux fois : '{cle}'.")
        vues.add(cle)

        type_ = reglage.get("type")
        if type_ not in TYPES_REGLAGE:
            raise ContratInvalide(
                f"Type de réglage inconnu pour '{cle}' : '{type_}' — valeurs "
                f"possibles : {', '.join(TYPES_REGLAGE)}."
            )

        libelle = (reglage.get("libelle") or "").strip()
        if not libelle:
            raise ContratInvalide(f"Réglage '{cle}' sans libellé.")
        if len(libelle) > LONGUEUR_MAX_LIBELLE:
            raise ContratInvalide(
                f"Libellé du réglage '{cle}' trop long ({len(libelle)} caractères, "
                f"maximum {LONGUEUR_MAX_LIBELLE})."
            )

        resultat.append({
            "cle": cle,
            "type": type_,
            "libelle": libelle,
            "aide": (reglage.get("aide") or "").strip() or None,
            "defaut": normaliser_valeur(type_, reglage.get("defaut"), cle),
            "variable": variable_de(cle),
        })
    return resultat


def normaliser_valeur(type_: str, valeur, cle: str) -> str:
    """Rend la valeur sous sa forme TEXTUELLE, celle qui ira dans la
    variable d'environnement — c'est la seule que systemd sache porter.

    Un module reçoit donc toujours du texte, y compris pour un booléen :
    « true » ou « false », jamais autre chose, pour qu'il n'ait pas à
    deviner la convention."""
    if type_ == "booleen":
        if valeur is None:
            return "false"
        if isinstance(valeur, bool):
            return "true" if valeur else "false"
        if str(valeur).lower() in ("true", "false"):
            return str(valeur).lower()
        raise ContratInvalide(f"Réglage '{cle}' : booléen attendu, reçu {valeur!r}.")

    if type_ == "liste":
        if valeur is None:
            return ""
        if isinstance(valeur, str):
            valeur = valeur.split(",")
        if not isinstance(valeur, list):
            raise ContratInvalide(f"Réglage '{cle}' : liste attendue, reçu {valeur!r}.")
        elements = [str(v).strip() for v in valeur if str(v).strip()]
        if any("," in e for e in elements):
            # La virgule est le séparateur : l'accepter DANS un élément
            # ferait relire deux valeurs là où le module en attend une.
            raise ContratInvalide(f"Réglage '{cle}' : une valeur de liste ne peut pas contenir de virgule.")
        texte = ",".join(elements)
    else:
        texte = "" if valeur is None else str(valeur)

    if len(texte) > LONGUEUR_MAX_VALEUR:
        raise ContratInvalide(
            f"Réglage '{cle}' : valeur trop longue ({len(texte)} caractères, "
            f"maximum {LONGUEUR_MAX_VALEUR})."
        )
    if "\n" in texte or "\r" in texte:
        # Une valeur multiligne casserait le fichier d'unité systemd, qui
        # est en clé=valeur par ligne.
        raise ContratInvalide(f"Réglage '{cle}' : une valeur ne peut pas contenir de saut de ligne.")
    return texte
