# erreurs.py — Erreurs du contrat
#
# Une seule exception, et volontairement une seule : tout ce que ce
# contrat refuse se refuse pour la même raison — le message reçu ou la
# déclaration faite ne respecte pas le contrat. Le détail est dans le
# texte, pas dans la hiérarchie de classes.
#
# Hérite de ValueError : un appelant qui ne connaît pas ce module (un
# script d'administration, un test) l'attrape sans avoir à l'importer.


class ContratInvalide(ValueError):
    """Message, document ou déclaration non conforme au contrat.

    ⚠️  Cette exception n'est JAMAIS fatale côté worker : un message
    invalide se journalise et se saute, il n'arrête pas la consommation
    du topic. Un module complémentaire fautif ne doit pas pouvoir
    bloquer l'indexation des autres."""
