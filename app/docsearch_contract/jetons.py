# jetons.py — Ce qu'un module complémentaire doit vérifier d'une session
#
# Un module qui expose des écrans ou des routes sous /ext/<nom>/ reçoit
# les requêtes du navigateur, cookie de session compris. Il doit donc
# savoir QUI parle — et il le fait sans jamais toucher à l'annuaire ni au
# magasin de sessions : l'API publie ses clés publiques
# (GET /auth/.well-known/jwks.json), le module vérifie la signature.
# C'est l'invariant 3 du plan, et c'est aussi ce qui permet à un module
# d'être écrit dans n'importe quel langage : il lui faut une
# bibliothèque JWT, rien de plus.
#
# Ce module ne vérifie PAS la signature : ça demande une bibliothèque
# cryptographique, et le contrat n'a aucune dépendance (voir le README).
# Il porte les constantes du dialogue et le contrôle des REVENDICATIONS,
# c'est-à-dire tout ce qu'on oublie de vérifier quand on n'a que
# « décoder le jeton » en tête :
#
#   - le type de jeton : un jeton de RAFRAÎCHISSEMENT présenté comme un
#     jeton d'accès est le contournement classique. L'API le contrôle
#     (auth/tokens.py::decode_token), un module qui l'oublierait
#     accepterait un jeton à durée de vie bien plus longue ;
#   - l'émetteur et l'audience : sans eux, un jeton signé par la même
#     autorité pour un AUTRE service est accepté ;
#   - l'expiration, que la plupart des bibliothèques vérifient — mais
#     seulement si on ne leur a pas demandé de s'en abstenir.
#
# ⚠️  Vérifier la session ne suffit pas à lire des documents. Un module
# ne lit JAMAIS Elasticsearch : il rappelle l'API du cœur en portant le
# jeton de l'utilisateur, et l'ACL s'applique sans qu'il ait à la
# connaître (invariant 1).

from .erreurs import ContratInvalide

# Chemin du JWKS, relatif à la racine de l'API.
CHEMIN_JWKS = "/auth/.well-known/jwks.json"

# Nom du cookie qui porte le jeton d'accès (auth/config.py).
COOKIE_ACCES = "docsearch_access"

# Valeurs par défaut de l'API — surchargeables par JWT_ISSUER /
# JWT_AUDIENCE côté cœur, donc à traiter comme des DÉFAUTS et non comme
# des constantes gravées : un module qui les code en dur cassera sur une
# installation qui les a changées.
EMETTEUR_DEFAUT = "docsearch-api"
AUDIENCE_DEFAUT = "docsearch"

ALGORITHME = "RS256"

# Le seul type de jeton qu'un module doit accepter.
TYPE_JETON_ACCES = "access"


def verifier_revendications(payload: dict, *, emetteur: str = EMETTEUR_DEFAUT,
                            audience: str = AUDIENCE_DEFAUT) -> str:
    """Contrôle les revendications d'un jeton DÉJÀ vérifié
    cryptographiquement, et rend le nom d'utilisateur (`sub`).

    Lève `ContratInvalide` — que l'appelant traduit en 401. Ne jamais
    traduire en 500 : un jeton refusé n'est pas une panne du module.
    """
    if not isinstance(payload, dict):
        raise ContratInvalide("Charge utile de jeton illisible.")

    type_jeton = payload.get("token_type")
    if type_jeton != TYPE_JETON_ACCES:
        raise ContratInvalide(
            f"Type de jeton refusé : {type_jeton!r} (attendu {TYPE_JETON_ACCES!r}). "
            "Un jeton de rafraîchissement ne vaut pas une session."
        )

    iss = payload.get("iss")
    if iss != emetteur:
        raise ContratInvalide(f"Émetteur inattendu : {iss!r} (attendu {emetteur!r}).")

    # `aud` peut être une chaîne ou une liste, selon la bibliothèque qui a
    # décodé — les deux formes sont valides au sens de la RFC 7519.
    aud = payload.get("aud")
    audiences = aud if isinstance(aud, list) else [aud]
    if audience not in audiences:
        raise ContratInvalide(f"Audience inattendue : {aud!r} (attendue {audience!r}).")

    sub = payload.get("sub")
    if not sub or not isinstance(sub, str):
        raise ContratInvalide("Jeton sans identité (`sub`).")

    return sub
