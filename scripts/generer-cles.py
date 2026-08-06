#!/usr/bin/env python3
"""Génère la paire de clés RS256 qui signe les jetons de session.

Les clés ne sont JAMAIS dans le dépôt git ni dans l'image : elles vivent
sur l'hôte, montées en lecture seule dans le conteneur (voir l'unité
Quadlet docsearch-api.container). Même régime que tout autre secret.

    python3 scripts/generer-cles.py                      # → /etc/docsearch/jwt
    python3 scripts/generer-cles.py --sortie /tmp/cles   # ailleurs

Rejouer la commande crée une NOUVELLE paire sous un nouvel identifiant de
clé (`kid`) sans écraser l'ancienne : c'est ce qui rendra une rotation
possible le jour où elle sera implémentée. Basculer revient à changer
JWT_ACTIVE_KID puis à redémarrer l'API — toutes les sessions signées avec
l'ancienne clé sont alors refusées, il faut donc le faire hors heures
ouvrées ou accepter que chacun se reconnecte.
"""

import argparse
import os
import sys
import uuid
from pathlib import Path

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
except ImportError:
    sys.exit(
        "Le paquet `cryptography` est absent. Passer par l'image de l'API :\n"
        "  sudo podman run --rm -v /etc/docsearch/jwt:/etc/docsearch/jwt:Z \\\n"
        "       localhost/docsearch/api:latest python scripts/generer-cles.py"
    )

DEFAUT = "/etc/docsearch/jwt"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sortie", default=DEFAUT, help=f"répertoire de destination (défaut : {DEFAUT})")
    parser.add_argument("--taille", type=int, default=3072, help="taille de la clé RSA (défaut : 3072)")
    args = parser.parse_args()

    kid = uuid.uuid4().hex[:12]
    dossier = Path(args.sortie) / kid
    dossier.mkdir(parents=True, exist_ok=True)
    # 0700 sur le dossier, 0600 sur la clé privée. ⚠️ En Podman rootless,
    # ces permissions sont interprétées avec l'UID MAPPÉ dans le conteneur :
    # une clé lisible sur l'hôte peut rester illisible dedans.
    os.chmod(dossier, 0o700)

    cle = rsa.generate_private_key(public_exponent=65537, key_size=args.taille)

    privee = dossier / "private.pem"
    privee.write_bytes(cle.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    os.chmod(privee, 0o600)

    publique = dossier / "public.pem"
    publique.write_bytes(cle.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    os.chmod(publique, 0o644)

    print(f"Paire RS256 générée dans {dossier}\n")
    print("À reporter dans l'environnement de l'API (docsearch.env) :\n")
    print(f"JWT_ACTIVE_KID={kid}")
    print(f"JWT_PRIVATE_KEY_PATH={privee}")
    print(f"JWT_PUBLIC_KEY_PATH={publique}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
