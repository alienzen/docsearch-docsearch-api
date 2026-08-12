# auth/router.py — Les routes /auth/*
#
# Toute connexion, quelle que soit la porte empruntée (formulaire annuaire,
# compte de secours, ticket Kerberos), passe par `_open_session` : un point
# de passage unique, pour qu'aucun chemin ne puisse sauter le contrôle
# d'accès, la pose des cookies ou la ligne d'audit. C'est ce qui a rendu
# l'ajout du SSO additif plutôt qu'invasif.
#
# Régimes d'erreur, constants sur toutes les routes :
#   401  identifiants refusés — message générique unique, toujours le même
#   403  authentifié mais pas membre du groupe requis
#   429  trop de tentatives
#   501  SSO Kerberos désactivé
#   503  annuaire, Redis, keytab ou clés de signature indisponibles
#
# Un 503 n'est JAMAIS présenté comme un 401 : une panne qui ressemble à un
# mot de passe faux envoie chercher au mauvais endroit, et c'est ainsi qu'un
# keytab manquant reste introuvable pendant une journée.

import base64
import binascii
import logging
from datetime import datetime, timezone

import jwt as pyjwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from auth import (
    accounts,
    config,
    deps,
    events,
    kerberos,
    providers,
    sessions,
    store,
    tokens,
)
from auth.base import (
    AuthenticationError,
    AuthProviderUnavailableError,
    LoginCredentials,
    ResolvedIdentity,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["authentification"])

# Message unique, quelle que soit la cause réelle (identifiant inconnu, mot
# de passe faux, compte désactivé). Toute variation dirait à qui essaie
# lesquels des identifiants présentés existent.
GENERIC_AUTH_ERROR = "Identifiant ou mot de passe incorrect."


class LoginRequest(BaseModel):
    identifiant: str = Field(min_length=1, max_length=256)
    # Borne haute par défense en profondeur : Argon2id est coûteux par
    # construction, une chaîne de plusieurs mégaoctets en ferait un déni de
    # service à un seul appel.
    mot_de_passe: str = Field(min_length=1, max_length=1024)


class SessionResponse(BaseModel):
    user: str
    display_name: str
    email: str | None = None
    groups: list[str] = []
    is_admin: bool = False
    auth_method: str
    expires_in: int


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "")


def _user_agent(request: Request) -> str:
    return request.headers.get("user-agent", "")


# ── Cookies ──────────────────────────────────────────────────

#: Une seule fois par processus : c'est une erreur de configuration, elle ne
#: se corrige pas d'elle-même et la répéter à chaque connexion noierait le
#: reste du journal.
_incoherence_cookie_signalee = False


def _verifier_coherence_cookie(request: Request) -> None:
    """Avertit quand `COOKIE_SECURE=true` sur une installation servie en HTTP.

    Le symptôme, sinon, est parfaitement muet et prête à contresens : la
    connexion RÉUSSIT (200, cookies posés), puis le navigateur refuse de
    renvoyer un cookie `Secure` sur du clair, et la page suivante renvoie au
    formulaire. On croit à un échec d'authentification, à une session qui
    ne tient pas, à un bug du front — alors qu'il s'agit d'un drapeau et
    d'un schéma d'URL qui ne s'accordent pas.

    Arbitré le 2026-08-06 : `false` sur la VM de développement (recette en
    clair sur le port 8090), `true` en production (accès HTTPS par le
    reverse-proxy). C'est ce que livrent respectivement les deux
    `.env.example`.
    """
    global _incoherence_cookie_signalee
    if _incoherence_cookie_signalee or not config.COOKIE_SECURE:
        return

    # X-Forwarded-Proto d'abord : derrière le reverse-proxy TLS, l'API voit
    # du HTTP en interne alors que le navigateur, lui, est bien en HTTPS.
    schema = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    if not schema:
        schema = request.url.scheme
    if schema == "https":
        return

    _incoherence_cookie_signalee = True
    logger.warning(
        "[auth] COOKIE_SECURE=true mais cette requête est arrivée en %s : le "
        "navigateur ACCEPTERA la connexion puis refusera de renvoyer le "
        "cookie, et chaque page renverra au formulaire. Poser "
        "COOKIE_SECURE=false pour une recette en clair, ou servir "
        "l'application en HTTPS (et transmettre X-Forwarded-Proto depuis le "
        "reverse-proxy).",
        schema or "clair",
    )


def _set_session_cookies(response: Response, access: str, refresh: str) -> None:
    """Deux cookies httpOnly, et deux portées différentes.

    Le cookie d'accès porte sur tout le site : le auth_request de Nginx doit
    pouvoir le relayer sur n'importe quelle page. Le cookie de
    rafraîchissement, lui, ne vaut que pour /auth — il n'a aucune raison
    d'accompagner chaque recherche, et une fuite de journal de proxy en
    exposerait d'autant moins."""
    response.set_cookie(
        key=config.ACCESS_COOKIE_NAME, value=access, httponly=True,
        secure=config.COOKIE_SECURE, samesite=config.COOKIE_SAMESITE, path="/",
        max_age=config.JWT_ACCESS_TOKEN_TTL_MINUTES * 60,
    )
    response.set_cookie(
        key=config.REFRESH_COOKIE_NAME, value=refresh, httponly=True,
        secure=config.COOKIE_SECURE, samesite=config.COOKIE_SAMESITE, path="/auth",
        max_age=config.JWT_REFRESH_TOKEN_TTL_DAYS * 24 * 3600,
    )


def _clear_session_cookies(response: Response) -> None:
    response.delete_cookie(
        config.ACCESS_COOKIE_NAME, path="/",
        samesite=config.COOKIE_SAMESITE, secure=config.COOKIE_SECURE,
    )
    response.delete_cookie(
        config.REFRESH_COOKIE_NAME, path="/auth",
        samesite=config.COOKIE_SAMESITE, secure=config.COOKIE_SECURE,
    )


# ── Le point de passage unique ───────────────────────────────

def _open_session(
    response: Response,
    identity: ResolvedIdentity,
    *,
    auth_method: str,
    request: Request,
    simulated: bool = False,
) -> SessionResponse:
    """Contrôle d'accès, émission des jetons, cookies, journal.

    **Le contrôle d'ACCESS_GROUP est fait ici, à la connexion**, et pas
    seulement à chaque requête : quelqu'un qui n'a pas le droit d'utiliser
    DocSearch ne repart pas avec une session valide qu'il verrait échouer
    sur chaque page sans comprendre pourquoi."""
    login = identity.login

    if not config.ACCESS_AUTH_DISABLED:
        try:
            deps.require_access(login)
        except HTTPException as exc:
            events.record(
                identifier=login, outcome=events.ACCESS_DENIED, method=auth_method,
                ip=_client_ip(request), user_agent=_user_agent(request),
                detail=str(exc.detail), simulated=simulated,
            )
            raise

    try:
        access_token, expires_at = tokens.create_access_token(identity, auth_method=auth_method)
        refresh_token, jti, _ = tokens.create_refresh_token(login)
    except tokens.KeyLoadError as exc:
        logger.error("[auth] Émission impossible : %s", exc)
        raise HTTPException(status_code=503, detail=f"Authentification indisponible : {exc}") from exc

    try:
        sessions.store_refresh_token(
            jti,
            login=login,
            ttl_seconds=config.JWT_REFRESH_TOKEN_TTL_DAYS * 24 * 3600,
            auth_method=auth_method,
            display_name=identity.display_name,
            email=identity.email,
        )
    except store.StoreUnavailable as exc:
        # Sans magasin, le jeton de rafraîchissement serait signé mais
        # jamais révocable : une déconnexion ne déconnecterait rien. Refus
        # franc plutôt qu'une session dont on ne peut plus reprendre la clé.
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    _verifier_coherence_cookie(request)
    _set_session_cookies(response, access_token, refresh_token)

    events.record(
        identifier=login, outcome=events.SUCCESS, method=auth_method,
        ip=_client_ip(request), user_agent=_user_agent(request), simulated=simulated,
    )
    if simulated:
        logger.warning(
            "[auth] Session ouverte par le HARNAIS de développement (aucun ticket "
            "réel) pour %s — API_ENV=%s", login, config.API_ENV,
        )

    return SessionResponse(
        user=login,
        display_name=identity.display_name,
        email=identity.email,
        groups=deps.user_groups(login),
        is_admin=deps.is_admin(login),
        auth_method=auth_method,
        # Ce que le front attend pour programmer son renouvellement : la
        # durée restante du jeton d'accès, pas celle du refresh.
        expires_in=max(0, int((expires_at - _now()).total_seconds())),
    )


# ── Connexion par identifiant/mot de passe ───────────────────

@router.post("/login", response_model=SessionResponse)
def login(payload: LoginRequest, request: Request, response: Response) -> SessionResponse:
    """Connexion par formulaire.

    **Le client ne dit pas par quelle voie s'authentifier** : le serveur
    choisit, et l'existence d'un compte de secours local portant cet
    identifiant est le discriminant (voir providers.pick_provider). Un seul
    fournisseur est sollicité par tentative, sans aucun repli de l'un vers
    l'autre — ce repli masquerait les pannes d'annuaire, fuiterait la nature
    du compte et dédoublerait le compteur de tentatives."""
    identifier = accounts.normalize_login(payload.identifiant)
    ip = _client_ip(request)
    provider = providers.pick_provider(identifier)

    try:
        sessions.check_rate_limit(provider.name, identifier, ip)
    except sessions.RateLimitExceeded:
        events.record(
            identifier=identifier, outcome=events.RATE_LIMITED, method=provider.name,
            ip=ip, user_agent=_user_agent(request),
        )
        raise HTTPException(
            status_code=429,
            detail="Trop de tentatives de connexion. Réessayer dans quelques minutes.",
        ) from None

    try:
        identity = provider.authenticate(
            LoginCredentials(identifier=identifier, secret=payload.mot_de_passe)
        )
    except AuthenticationError:
        sessions.register_failed_attempt(provider.name, identifier, ip)
        events.record(
            identifier=identifier, outcome=events.INVALID_CREDENTIALS,
            method=provider.name, ip=ip, user_agent=_user_agent(request),
        )
        raise HTTPException(status_code=401, detail=GENERIC_AUTH_ERROR) from None
    except AuthProviderUnavailableError as exc:
        # Jamais un 401 : l'annuaire est en panne, les identifiants n'y sont
        # pour rien, et l'échec ne doit pas compter dans le rate limiting.
        #
        # ⚠️  Journalisé ICI, dans les logs du service, et PAS seulement dans
        # `login_events`. Le message rendu au client est volontairement
        # générique ; si le détail ne vivait que dans l'index ES, il
        # deviendrait introuvable exactement quand on en a besoin — un
        # cluster rouge, un disque plein ou un ES arrêté rendent ce journal
        # muet, et l'exploitant se retrouve devant un 503 sans cause.
        # Constaté sur l'installation de dev : « annuaire injoignable » sans
        # une seule ligne de log pour dire lequel des trois motifs.
        logger.warning(
            "[auth] Fournisseur %s indisponible pour %r : %s",
            provider.name, identifier, exc,
        )
        events.record(
            identifier=identifier, outcome=events.PROVIDER_UNAVAILABLE,
            method=provider.name, ip=ip, user_agent=_user_agent(request), detail=str(exc),
        )
        raise HTTPException(
            status_code=503,
            detail="Service d'authentification temporairement indisponible.",
        ) from exc

    sessions.reset_rate_limit(provider.name, identifier, ip)
    return _open_session(response, identity, auth_method=provider.name, request=request)


# ── Connexion automatique par ticket Kerberos ────────────────

def _sso_enabled() -> bool:
    """Interrupteur serveur, réglable à chaud depuis le panneau
    d'administration (runtime_config), désactivé par défaut.

    Sans cet interrupteur, un déploiement sans keytab répondrait un défi que
    personne ne peut relever, à chaque chargement de page."""
    import runtime_config
    return str(runtime_config.get_param("sso_kerberos_enabled", "false")).lower() == "true"


@router.get("/login/kerberos", response_model=SessionResponse)
def login_kerberos(request: Request, response: Response) -> SessionResponse:
    """Connexion automatique par ticket (SPNEGO/Negotiate).

    Un GET, parce que c'est le navigateur qui rejoue la requête tout seul
    après le défi, et qu'un corps de requête ne survit pas à ce rejeu.

    Cinq issues, et le front les distingue toutes sans configuration — la
    tentative EST la découverte :

    | Situation                        | Réponse                                   |
    |----------------------------------|-------------------------------------------|
    | SSO désactivé                    | 501                                        |
    | Pas d'en-tête Authorization      | 401 + WWW-Authenticate: Negotiate (défi)  |
    | Ticket invalide / realm refusé   | 401 SANS défi — redéfier ferait boucler   |
    | Keytab, gssapi ou annuaire KO    | 503                                        |
    | Ticket valide                    | 200 + cookies de session                   |
    """
    if not _sso_enabled():
        raise HTTPException(
            status_code=501,
            detail="La connexion automatique Kerberos n'est pas activée sur cette installation.",
        )

    ip = _client_ip(request)
    simulated = False
    return_token: bytes | None = None

    harness = kerberos.dev_harness_principal()
    if harness:
        # Court-circuite l'acceptation GSSAPI, et elle seule : tout le reste
        # du chemin (mapping, annuaire, contrôle d'accès, cookies, audit)
        # est exercé pour de bon.
        principal, simulated = harness, True
    else:
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("negotiate "):
            # Le défi. Pas de journal ici : ce n'est pas une tentative
            # échouée, c'est la première moitié normale du dialogue, et
            # l'écrire noierait le journal sous une ligne par chargement.
            return _challenge()

        try:
            spnego_token = base64.b64decode(header.split(" ", 1)[1], validate=True)
        except (binascii.Error, IndexError, ValueError):
            events.record(
                identifier="", outcome=events.INVALID_CREDENTIALS, method="kerberos",
                ip=ip, user_agent=_user_agent(request), detail="jeton SPNEGO illisible",
            )
            raise HTTPException(status_code=401, detail="Ticket Kerberos invalide.") from None

        # Premier étage de limitation : avant l'acceptation, seule l'IP est
        # imputable — le jeton n'a pas encore livré d'identité. Surtout pas
        # de clé constante ici : un seul poste mal configuré bloquerait
        # toute l'installation.
        try:
            sessions.check_rate_limit("kerberos", "", ip)
        except sessions.RateLimitExceeded:
            events.record(
                identifier="", outcome=events.RATE_LIMITED, method="kerberos",
                ip=ip, user_agent=_user_agent(request),
            )
            raise HTTPException(status_code=429, detail="Trop de tentatives.") from None

        try:
            principal, return_token = kerberos.accept_token(spnego_token)
        except AuthenticationError as exc:
            sessions.register_failed_attempt("kerberos", "", ip)
            events.record(
                identifier="", outcome=events.INVALID_CREDENTIALS, method="kerberos",
                ip=ip, user_agent=_user_agent(request), detail=str(exc),
            )
            # 401 SANS WWW-Authenticate : redéfier ferait reboucler le
            # navigateur sur un ticket qu'on vient de refuser.
            raise HTTPException(status_code=401, detail="Ticket Kerberos invalide.") from None
        except AuthProviderUnavailableError as exc:
            # Keytab absent/illisible, `gssapi` non installée : une panne
            # d'exploitation, et le message est le seul moyen de savoir
            # laquelle. Même raison que sur /auth/login : ne pas la confier
            # au seul journal Elasticsearch.
            logger.warning("[kerberos] Acceptation impossible : %s", exc)
            events.record(
                identifier="", outcome=events.PROVIDER_UNAVAILABLE, method="kerberos",
                ip=ip, user_agent=_user_agent(request), detail=str(exc),
            )
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        identity = kerberos.KERBEROS_PROVIDER.identity_from_principal(principal)
    except AuthenticationError as exc:
        sessions.register_failed_attempt("kerberos", principal, ip)
        events.record(
            identifier=principal, outcome=events.INVALID_CREDENTIALS, method="kerberos",
            ip=ip, user_agent=_user_agent(request), detail=str(exc), simulated=simulated,
        )
        raise HTTPException(status_code=401, detail="Ticket Kerberos invalide.") from None
    except AuthProviderUnavailableError as exc:
        logger.warning("[kerberos] Annuaire indisponible pour %r : %s", principal, exc)
        events.record(
            identifier=principal, outcome=events.PROVIDER_UNAVAILABLE, method="kerberos",
            ip=ip, user_agent=_user_agent(request), detail=str(exc), simulated=simulated,
        )
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    # Second étage : après l'acceptation, le principal est imputable.
    sessions.reset_rate_limit("kerberos", identity.login, ip)

    session = _open_session(
        response, identity, auth_method="kerberos", request=request, simulated=simulated,
    )
    if return_token:
        # Authentification MUTUELLE : prouve au navigateur que ce serveur
        # détient bien la clé du SPN.
        response.headers["WWW-Authenticate"] = (
            "Negotiate " + base64.b64encode(return_token).decode("ascii")
        )
    return session


def _challenge() -> Response:
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=401,
        content={"detail": "Négociation Kerberos requise."},
        headers={"WWW-Authenticate": "Negotiate"},
    )


# ── Session ──────────────────────────────────────────────────

@router.post("/refresh", response_model=SessionResponse)
def refresh(request: Request, response: Response) -> SessionResponse:
    """Réémet un jeton d'accès à partir du cookie de rafraîchissement.

    Le jeton doit être signé ET présent dans le magasin : un jeton révoqué
    par une déconnexion est refusé même s'il est parfaitement valide
    cryptographiquement. C'est la seule chose qui rend une déconnexion
    réelle.

    Ne touche PAS à l'annuaire : c'est le chemin le plus fréquent du
    service, et l'instantané d'identité mémorisé à la connexion suffit. Les
    GROUPES, eux, sont relus (via `_open_session` → `require_access`) — une
    exclusion prend donc effet au plus tard au renouvellement suivant."""
    raw = request.cookies.get(config.REFRESH_COOKIE_NAME)
    if not raw:
        raise HTTPException(status_code=401, detail="Session absente.")

    try:
        claims = tokens.decode_token(raw, expected_type=tokens.REFRESH_TOKEN_TYPE)
    except tokens.KeyLoadError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except pyjwt.InvalidTokenError:
        _clear_session_cookies(response)
        raise HTTPException(status_code=401, detail="Session expirée.") from None

    try:
        stored = sessions.get_refresh_session(claims["jti"])
    except store.StoreUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if stored is None:
        _clear_session_cookies(response)
        raise HTTPException(status_code=401, detail="Session révoquée.")

    identity = ResolvedIdentity(
        login=stored["login"],
        display_name=stored.get("display_name") or stored["login"],
        email=stored.get("email") or None,
    )
    session = _open_session(
        response, identity,
        auth_method=stored.get("auth_method", "ldap"), request=request,
    )

    # Révocation de l'ancien jeton APRÈS l'ouverture du nouveau, et pas
    # avant : `_open_session` peut échouer (annuaire injoignable pendant
    # une seconde, exclusion de groupe, Redis coupé). Révoquer d'abord
    # ferait payer une déconnexion définitive à une panne passagère, alors
    # que la session en cours était parfaitement valide.
    #
    # Le jeton présenté ne vaut donc plus rien une fois consommé — ce qui
    # borne la fenêtre d'exploitation d'un cookie intercepté — mais la
    # fenêtre où les deux existent ensemble se compte en microsecondes.
    sessions.revoke_refresh_token(claims["jti"])
    return session


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response) -> None:
    """Révoque la session et efface les cookies. Toujours 204, même sans
    session : « se déconnecter » ne doit jamais échouer, et un code
    différent selon qu'une session existait dirait à qui essaie si le
    cookie présenté était valide."""
    raw = request.cookies.get(config.REFRESH_COOKIE_NAME)
    if raw:
        try:
            claims = tokens.decode_token(raw, expected_type=tokens.REFRESH_TOKEN_TYPE)
            sessions.revoke_refresh_token(claims["jti"])
        except (pyjwt.InvalidTokenError, tokens.KeyLoadError, store.StoreUnavailable, KeyError):
            pass
    _clear_session_cookies(response)


@router.get("/me", response_model=SessionResponse)
def me(request: Request, user: str = Depends(deps.current_user)) -> SessionResponse:
    groups = deps.user_groups(user)
    return SessionResponse(
        user=user,
        display_name=user,
        groups=groups,
        is_admin=deps.is_admin(user),
        auth_method="",
        expires_in=config.JWT_ACCESS_TOKEN_TTL_MINUTES * 60,
    )


# ── Cibles internes du auth_request de Nginx ─────────────────
# Contrat inchangé : seul le code HTTP compte, le corps est ignoré par
# Nginx. Ce qui change, c'est la source de l'identité — le cookie relayé
# par la sous-requête, et non plus un en-tête X-User posé par le client.

@router.get("/check-access", include_in_schema=False)
def check_access(user: str = Depends(deps.require_access)) -> dict:
    return {"user": user}


@router.get("/check-admin", include_in_schema=False)
def check_admin(request: Request) -> dict:
    """Garde /admin.html AVANT que la page ne soit servie.

    Toujours 401 en cas de refus, jamais 403 — contrairement à
    require_admin(), utilisée telle quelle par les routes /admin/* pour
    leurs réponses JSON. Cette cible-ci ne sert qu'à Nginx et doit bloquer
    la page à l'identique, qu'on soit anonyme ou simplement pas membre du
    groupe d'administration : le navigateur y arrive par une navigation, il
    n'y a pas d'interface pour afficher un 403 utilement."""
    try:
        user = deps.require_admin(deps.current_user(request, request.headers.get("x-user")))
    except HTTPException as exc:
        if exc.status_code == 503:
            raise
        raise HTTPException(status_code=401, detail="Accès refusé.") from exc
    return {"user": user}


@router.get("/.well-known/jwks.json")
def jwks() -> dict:
    """Clé publique au format JWKS (RFC 7517).

    Aucun consommateur externe aujourd'hui — l'API valide ses propres
    jetons. Publiée quand même : c'est ce qui permettra à un second service
    de les vérifier sans qu'on ait à lui confier de secret."""
    try:
        return tokens.build_jwks()
    except tokens.KeyLoadError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
