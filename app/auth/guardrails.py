# auth/guardrails.py — Ce qui empêche un contournement de dev d'atteindre la prod
#
# DocSearch a cinq interrupteurs qui, chacun, suffisent à donner l'identité
# de son choix à n'importe qui. Ils sont indispensables : sans eux, aucune
# recette n'est possible sans annuaire ni KDC. Le défaut qu'ils avaient
# jusqu'ici est qu'ils ne coûtaient rien à oublier — ADMIN_AUTH_DISABLED
# journalisait un avertissement et servait quand même le panneau
# d'administration à un anonyme.
#
# Deux régimes, et la différence est tout le sujet :
#
#   API_ENV=production  → l'API REFUSE DE DÉMARRER. Le module lève à
#                         l'import, donc uvicorn ne sert pas une requête.
#   sinon               → encadré d'avertissement au démarrage, plus une
#                         ligne de log à chaque usage (voir deps.py).
#
# Ne jamais « assouplir » le premier régime en le transformant en
# avertissement : un déploiement qui démarre est un déploiement dont
# personne ne relit les logs.

import logging

from auth import config

logger = logging.getLogger(__name__)


class InsecureProductionConfig(RuntimeError):
    """Un contournement de développement est armé alors que API_ENV=production."""


#: Ce que chaque interrupteur ouvre. Le message compte autant que le
#: contrôle : c'est lui que lira quelqu'un dont le déploiement refuse de
#: démarrer, sans forcément savoir ce que la variable fait.
_BYPASSES: dict[str, str] = {
    "ACCESS_AUTH_DISABLED": "l'accès à toutes les pages sans aucune authentification",
    "ADMIN_AUTH_DISABLED": "le panneau d'administration sans aucune authentification",
    "TRUST_X_USER_HEADER": "l'usurpation d'identité par simple en-tête HTTP X-User",
    "DEV_USER": "une identité par défaut pour toute requête non authentifiée",
    "KERBEROS_DEV_PRINCIPAL": "l'ouverture d'une session Kerberos sans le moindre ticket",
}


def _armed() -> list[tuple[str, str]]:
    """Relit `config` À CHAQUE APPEL, plutôt que de figer une table à
    l'import : c'est ce qui rend `enforce()` réellement idempotente, et
    testable sans recharger le module."""
    return [
        (name, opens)
        for name, opens in _BYPASSES.items()
        if bool(getattr(config, name, False))
    ]


def _banner(lines: list[str]) -> str:
    width = max(len(line) for line in lines) + 2
    top = "╔" + "═" * width + "╗"
    bottom = "╚" + "═" * width + "╝"
    body = "\n".join("║ " + line.ljust(width - 1) + "║" for line in lines)
    return f"\n{top}\n{body}\n{bottom}"


def enforce() -> None:
    """Appelée à l'import (voir tout en bas). Idempotente, donc appelable
    aussi depuis un test qui vient de modifier l'environnement."""
    armed = _armed()
    if not armed:
        return

    if config.IS_PRODUCTION:
        detail = " ; ".join(f"{name} (ouvre {opens})" for name, opens in armed)
        raise InsecureProductionConfig(
            f"API_ENV=production avec {len(armed)} contournement(s) de "
            f"développement armé(s) : {detail}. "
            "L'API refuse de démarrer plutôt que de les ignorer — retirer ces "
            "variables de l'environnement, ou basculer API_ENV si cette "
            "installation n'est pas une production."
        )

    lines = [
        "⚠️   AUTHENTIFICATION PARTIELLEMENT DÉSACTIVÉE",
        f"API_ENV={config.API_ENV or '(vide)'} — contournements de développement actifs :",
        "",
    ]
    lines += [f"  · {name} → {opens}" for name, opens in armed]
    lines += [
        "",
        "Réservé aux tests locaux. En production (API_ENV=production),",
        "l'API refuserait de démarrer avec cette configuration.",
    ]
    logger.warning(_banner(lines))


enforce()
