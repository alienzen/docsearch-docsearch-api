#!/usr/bin/env python3
"""Comptes de secours locaux — création, liste, suppression.

CE N'EST PAS UNE GESTION D'UTILISATEURS. Ces comptes servent au seul cas
où l'annuaire est en panne : sans eux, DocSearch devient totalement
inaccessible, administration comprise, et il n'y a plus aucun moyen de
diagnostiquer quoi que ce soit depuis l'application.

Volontairement hors de l'API : aucune route HTTP ne crée de compte, aucune
n'en liste les hachages. Créer un compte suppose un accès au serveur.

    podman exec -it docsearch-api python scripts/gerer-comptes-locaux.py lister
    podman exec -it docsearch-api python scripts/gerer-comptes-locaux.py creer \\
        secours.admin --groupes docsearch-users,docsearch-admins
    podman exec -it docsearch-api python scripts/gerer-comptes-locaux.py supprimer secours.admin

⚠️  Les GROUPES sont obligatoires à la création, et ce n'est pas une
formalité : l'annuaire étant en panne au moment où ce compte sert, c'est la
seule chose qui dira que son porteur a le droit d'entrer et d'administrer.
Un compte sans groupe se ferait refuser par le contrôle d'accès qu'il est
censé contourner.
"""

import argparse
import getpass
import sys
from pathlib import Path

# Deux dispositions à servir, et la seconde était cassée :
#   dépôt  — scripts/ et app/ côte à côte, les modules sont dans app/ ;
#   image  — `COPY app/ .` met les modules à plat dans /app, et
#            `COPY scripts/ ./scripts/` place ce fichier dans /app/scripts.
# Viser « parent.parent / app » ne marchait donc QUE depuis le dépôt, alors
# que la docstring ci-dessus documente l'invocation par `podman exec`.
# Symptôme : ModuleNotFoundError: No module named 'auth', au moment précis
# où l'annuaire est en panne et où ce script est le dernier recours.
_RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(next(
    (c for c in (_RACINE / "app", _RACINE) if (c / "auth").is_dir()),
    _RACINE / "app",
)))

from auth import accounts


def _demander_mot_de_passe() -> str:
    """Jamais en argument de ligne de commande : il resterait dans
    l'historique du shell et dans la liste des processus."""
    premier = getpass.getpass("Mot de passe : ")
    if len(premier) < 12:
        sys.exit("Mot de passe trop court (12 caractères minimum pour un compte de secours).")
    if premier != getpass.getpass("Confirmation : "):
        sys.exit("Les deux saisies diffèrent.")
    return premier


def creer(args) -> int:
    groupes = [g.strip() for g in args.groupes.split(",") if g.strip()]
    if not groupes:
        sys.exit(
            "Aucun groupe : ce compte ne pourrait pas se connecter. "
            "Ex. --groupes docsearch-users,docsearch-admins"
        )
    login = accounts.set_account(
        args.login,
        password=_demander_mot_de_passe(),
        groups=groupes,
        display_name=args.nom or "",
        email=args.email or "",
    )
    print(f"Compte de secours « {login} » créé (groupes : {', '.join(groupes)}).")
    print("Rappel : la connexion par compte local est journalisée en WARNING à chaque usage.")
    return 0


def lister(_args) -> int:
    comptes = accounts.list_accounts()
    if not comptes:
        print("Aucun compte de secours.")
        return 0
    for compte in comptes:
        etat = "désactivé" if compte.get("disabled") else "actif"
        print(f"{compte['login']:<24} {etat:<10} groupes : {', '.join(compte.get('groups') or []) or '—'}")
    return 0


def supprimer(args) -> int:
    if accounts.delete_account(args.login):
        print(f"Compte « {args.login} » supprimé.")
        return 0
    print(f"Aucun compte « {args.login} ».")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sous = parser.add_subparsers(dest="commande", required=True)

    p_creer = sous.add_parser("creer", help="créer ou remplacer un compte de secours")
    p_creer.add_argument("login")
    p_creer.add_argument("--groupes", required=True, help="liste séparée par des virgules")
    p_creer.add_argument("--nom", help="nom affiché")
    p_creer.add_argument("--email")
    p_creer.set_defaults(func=creer)

    sous.add_parser("lister", help="lister les comptes (sans les hachages)").set_defaults(func=lister)

    p_suppr = sous.add_parser("supprimer", help="supprimer un compte")
    p_suppr.add_argument("login")
    p_suppr.set_defaults(func=supprimer)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
