# auth/ — Authentification de DocSearch
#
# Architecture reprise de charlie/app-api-auth (voir son README.md pour le
# détail des arbitrages) : une interface AuthProvider unique, des JWT RS256
# publiés par JWKS, des refresh tokens révocables en Redis, un rate limiting
# à deux clés, et un message d'erreur générique unique sur échec.
#
# Ce qui diverge de Charlie, et pourquoi, est écrit dans
# docsearch-infra/PLAN-AUTH-SSO.md — en résumé : DocSearch n'a ni PostgreSQL
# ni table `users`, l'identifiant annuaire EST l'identité, et les comptes
# locaux sont des comptes de SECOURS stockés en Redis, pas un système de
# gestion d'utilisateurs.
#
# Ordre de lecture conseillé :
#   config.py      → tous les réglages, avec leur valeur par défaut
#   guardrails.py  → ce qui empêche un contournement de dev d'atteindre la prod
#   directory.py   → l'annuaire (recherche, groupes) — la brique la plus lue
#   base.py        → l'interface AuthProvider et ses deux exceptions
#   tokens.py      → émission et vérification des JWT
#   sessions.py    → Redis : refresh révocables, rate limiting
#   deps.py        → current_user / require_access / require_admin
#   router.py      → les routes /auth/*
