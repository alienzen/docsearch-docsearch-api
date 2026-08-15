# version.py — Version du contrat partagé
#
# Version sémantique, indépendante du fichier VERSION des dépôts (qui
# porte la version PRODUIT). Elle ne bouge que quand le contrat change :
#
#   correctif  — un comportement corrigé sans changer les signatures ;
#   mineure    — un ajout rétrocompatible (nouveau champ optionnel,
#                nouvelle fonction) ;
#   majeure    — tout ce qu'un consommateur existant ne supporterait pas
#                (champ retiré, sémantique modifiée).
#
# C'est ce numéro qu'un manifeste de module complémentaire déclarera
# (lot 2), et le seul moyen de refuser proprement un module écrit contre
# un contrat qui n'existe plus. Aujourd'hui il n'a qu'un consommateur
# (docsearch-api) et aucun producteur externe : il est en 0.x, donc sans
# promesse de stabilité tant que les lots 1 et 2 n'ont pas éprouvé la
# forme du contrat.

CONTRACT_VERSION = "0.1.0"
