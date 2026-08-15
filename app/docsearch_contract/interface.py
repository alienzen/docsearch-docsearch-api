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
#   nav   une entrée dans le menu de l'interface, qui mène à une adresse
#         du module (donc sous /ext/<nom>/)
#
# Trois autres accroches sont prévues par le plan — `result_action`
# (bouton sur une carte de résultat), `admin_panel` (panneau décrit en
# champs) et `page` (écran entier en iframe). Elles ne sont PAS servies :
# les déclarer ferait s'installer un module qui annonce des éléments que
# rien n'affiche. Elles suivront le même chemin que `nav`, qui l'a
# défriché.

import re

from .erreurs import ContratInvalide

ACCROCHES = ("nav",)

# Une entrée de menu ne peut viser QUE le module qui la déclare. Sans ce
# contrôle, un module poserait dans le menu de tout le monde un lien vers
# n'importe où — /admin.html, ou un site extérieur.
_CHEMIN_RE = re.compile(r"^/ext/[a-z0-9][a-z0-9_-]*/[a-zA-Z0-9._~/-]*$")
# Icônes DSFR : le cœur ne rend que ce vocabulaire, et une classe libre
# permettrait d'injecter n'importe quel nom de classe dans le rendu.
_ICONE_RE = re.compile(r"^fr-icon-[a-z0-9-]+$")

LONGUEUR_MAX_LIBELLE = 40


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

    return {"nav": resultat}
