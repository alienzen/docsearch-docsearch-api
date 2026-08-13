# tests/test_historique_utilisateur.py — L'historique personnel
# (GET /me/searches) et l'autocomplétion (GET /suggest) rendent-ils à
# chacun SES recherches, et rien de plus ?
#
# Ce que ces tests protègent, dans l'ordre d'importance :
#
# 1. **Le cloisonnement.** La donnée lue est le journal de recherche de
#    TOUTE l'installation : une requête mal filtrée montrerait à chacun ce
#    que cherchent ses collègues. C'est le premier test du fichier, et
#    celui qui justifie les autres.
# 2. **Le filtrage ACL des suggestions du corpus.** Une agrégation fuit
#    exactement autant qu'un résultat de recherche : suggérer le nom d'un
#    auteur trouvé dans un document interdit, c'est divulguer ce document.
# 3. **L'échappement du préfixe de saisie.** L'`include` d'une agrégation
#    `terms` est une expression régulière Lucene : une parenthèse tapée
#    dans la barre de recherche produirait une 400, et un `.*` collé
#    ferait balayer tout le dictionnaire de termes. Vérifié contre le VRAI
#    moteur — c'est lui qui accepte ou refuse, pas ma relecture.
#
# Elasticsearch est le vrai (principe 1 de conftest.py), sur des index
# jetables supprimés avant ET après (principe 2) : jamais `search_logs`
# ni les index de documents de l'installation de dev.
#
# ⚠️ Comme les autres modules qui créent un index jetable, ces tests
# exigent du disque : au-dessus du seuil haut d'allocation (90 %), un
# index neuf se crée en rouge et toute écriture échoue — voir l'en-tête
# de test_temps_recherche.py.

import time

import pytest

import cluster_status
import search_log
import user_history

requiert_es = pytest.mark.requires_elasticsearch

INDEX_JOURNAL = "docsearch_test_sonde_historique"
INDEX_CORPUS = "docsearch_test_sonde_suggestions"

# Même constat que les autres modules à index jetable sur cette VM : la
# création d'un index peut prendre une trentaine de secondes.
DELAI_ES = 60

MOI = "sonde.moi"
AUTRE = "sonde.autre"


@pytest.fixture(scope="module")
def es():
    from elasticsearch import Elasticsearch

    client = Elasticsearch(cluster_status.ES_HOST, request_timeout=DELAI_ES, max_retries=0)
    yield client
    client.close()


def _supprimer(es, index: str) -> None:
    if es.indices.exists(index=index):
        es.indices.delete(index=index)


def _journaliser(es, username: str, query: str) -> None:
    search_log.log_search(
        es,
        username=username,
        ip=None,
        query=query,
        search_in="all",
        source=None,
        total_results=1,
        result_files=[],
    )
    # Le journal est écrit sans `refresh` (c'est un journal, pas une base
    # de travail) : sans cette attente, l'agrégation qui suit ne verrait
    # rien et le test passerait au vert pour la mauvaise raison.
    time.sleep(0.05)


@pytest.fixture(scope="module")
def journal(es):
    """Un journal de recherche jetable, peuplé une fois pour tout le
    module. `_ensure_index` de search_log crée l'index au premier appel —
    c'est aussi ce qui est exercé ici."""
    _supprimer(es, INDEX_JOURNAL)
    precedent = search_log.SEARCH_LOG_INDEX
    search_log.SEARCH_LOG_INDEX = INDEX_JOURNAL
    search_log._index_ready = False

    _journaliser(es, MOI, "budget 2025")
    _journaliser(es, MOI, "marché de travaux")
    _journaliser(es, MOI, "budget 2025")          # doublon : une seule entrée attendue
    _journaliser(es, MOI, "BUDGET rectificatif")  # casse différente
    _journaliser(es, MOI, "")                     # filtres seuls, sans texte libre
    _journaliser(es, AUTRE, "dossier disciplinaire Martin")
    es.indices.refresh(index=INDEX_JOURNAL)

    yield es

    search_log.SEARCH_LOG_INDEX = precedent
    search_log._index_ready = False
    _supprimer(es, INDEX_JOURNAL)


# ── 1. Cloisonnement ─────────────────────────────────────────

@requiert_es
def test_ne_rend_que_mes_recherches(journal):
    """Le test qui compte : rien de ce qu'un autre a cherché ne doit
    sortir. Une requête de dossier disciplinaire nomme quelqu'un."""
    textes = [entree["query"] for entree in user_history.recent_queries(journal, MOI, 20)]
    assert "dossier disciplinaire Martin" not in textes
    assert user_history.recent_queries(journal, AUTRE, 20)[0]["query"] == (
        "dossier disciplinaire Martin"
    )


@requiert_es
def test_dedoublonne_et_compte_les_occurrences(journal):
    entrees = {e["query"]: e for e in user_history.recent_queries(journal, MOI, 20)}
    assert entrees["budget 2025"]["count"] == 2
    assert entrees["marché de travaux"]["count"] == 1


@requiert_es
def test_la_plus_recente_en_premier(journal):
    textes = [e["query"] for e in user_history.recent_queries(journal, MOI, 20)]
    assert textes[0] == "BUDGET rectificatif"


@requiert_es
def test_ecarte_les_recherches_sans_texte_libre(journal):
    """Une recherche par filtres seuls s'afficherait comme une ligne vide,
    et l'historique ne porte pas de quoi la rejouer."""
    assert "" not in [e["query"] for e in user_history.recent_queries(journal, MOI, 20)]


# ── 2. Autocomplétion sur l'historique ───────────────────────

@requiert_es
def test_propose_le_prefixe_sans_tenir_compte_de_la_casse(journal):
    textes = [e["query"] for e in user_history.matching_queries(journal, MOI, "budg", 5)]
    assert set(textes) == {"budget 2025", "BUDGET rectificatif"}


@requiert_es
def test_les_correspondances_en_debut_passent_devant(journal):
    """« marché » commence « marché de travaux » ; « travaux » ne fait que
    s'y trouver. Les deux sont utiles, dans cet ordre."""
    debut = user_history.matching_queries(journal, MOI, "marché", 5)
    ailleurs = user_history.matching_queries(journal, MOI, "travaux", 5)
    assert debut[0]["query"] == "marché de travaux"
    assert ailleurs[0]["query"] == "marché de travaux"


@requiert_es
def test_ne_propose_pas_ce_qui_est_deja_tape(journal):
    """Proposer à l'identique la saisie en cours n'aide personne."""
    textes = [e["query"] for e in user_history.matching_queries(journal, MOI, "budget 2025", 5)]
    assert "budget 2025" not in textes


# ── 3. Suggestions du corpus, et leur filtrage ───────────────

@pytest.fixture(scope="module")
def corpus(es):
    """Deux documents : un public, un réservé au groupe « finance ».
    Mapping réduit à ce dont l'agrégation a besoin, mais des MÊMES types
    que l'index réel (voir create_index dans docsearch-ingestion)."""
    _supprimer(es, INDEX_CORPUS)
    es.indices.create(
        index=INDEX_CORPUS,
        mappings={
            "properties": {
                "author": {"type": "keyword"},
                "keywords": {"type": "keyword"},
                "source": {"type": "keyword"},
                "acl": {
                    "properties": {
                        "public": {"type": "boolean"},
                        "groups": {"type": "keyword"},
                    }
                },
            }
        },
    )
    es.index(
        index=INDEX_CORPUS,
        id="public",
        document={
            "author": "Durand Public",
            "keywords": ["budget"],
            "source": "sonde",
            "acl": {"public": True, "groups": []},
        },
    )
    es.index(
        index=INDEX_CORPUS,
        id="prive",
        document={
            "author": "Duchemin Secret",
            "keywords": ["budget"],
            "source": "sonde",
            "acl": {"public": False, "groups": ["finance"]},
        },
    )
    # Deux documents pour que « Marc Durand » l'emporte sur « Durand
    # Public » au nombre de documents, qui est l'ordre rendu par
    # l'agrégation — et que le reclassement de corpus_terms ait donc
    # quelque chose à inverser.
    for identifiant in ("tri-1", "tri-2"):
        es.index(
            index=INDEX_CORPUS,
            id=identifiant,
            document={
                "author": "Marc Durand",
                "keywords": ["budget"],
                "source": "sonde",
                "acl": {"public": True, "groups": []},
            },
        )
    es.indices.refresh(index=INDEX_CORPUS)
    yield es
    _supprimer(es, INDEX_CORPUS)


def _filtre_acl(groupes: list[str]) -> list[dict]:
    """La forme de build_acl_filter(), réduite à ce que le corpus de
    sonde porte."""
    return [
        {
            "bool": {
                "should": [
                    {"term": {"acl.public": True}},
                    {"terms": {"acl.groups": groupes}},
                ],
                "minimum_should_match": 1,
            }
        }
    ]


@requiert_es
def test_les_suggestions_du_corpus_respectent_les_droits(corpus):
    """LE test de ce fichier avec le cloisonnement de l'historique : les
    deux auteurs commencent par « Du », un seul est visible."""
    visibles = user_history.corpus_terms(corpus, INDEX_CORPUS, _filtre_acl([]), "du", 10)
    textes = [proposition["text"] for proposition in visibles]
    assert "Durand Public" in textes
    assert "Duchemin Secret" not in textes

    avec_droits = user_history.corpus_terms(
        corpus, INDEX_CORPUS, _filtre_acl(["finance"]), "du", 10
    )
    assert "Duchemin Secret" in [proposition["text"] for proposition in avec_droits]


@requiert_es
def test_propose_les_mots_cles_avec_leur_nature(corpus):
    propositions = user_history.corpus_terms(corpus, INDEX_CORPUS, _filtre_acl([]), "bud", 10)
    mots_cles = [p for p in propositions if p["kind"] == "keyword"]
    assert [p["text"] for p in mots_cles] == ["budget"]


@requiert_es
def test_trouve_un_auteur_par_son_second_mot(corpus):
    """Le champ agrégé est un `keyword` : « Durand Public » y est un seul
    terme. Sans quoi taper un nom de famille ne proposerait personne,
    alors que la recherche, elle, trouve l'auteur (`author.text`)."""
    propositions = user_history.corpus_terms(corpus, INDEX_CORPUS, _filtre_acl([]), "public", 10)
    assert "Durand Public" in [p["text"] for p in propositions]


@requiert_es
def test_ne_propose_pas_sur_un_milieu_de_mot(corpus):
    """La contrepartie voulue : le match commence un mot, ou rien. Un
    `.*` de tête nu proposerait « Durand Public » sur « urand », qui
    n'est le début de rien."""
    assert user_history.corpus_terms(corpus, INDEX_CORPUS, _filtre_acl([]), "urand", 10) == []


@requiert_es
def test_le_second_mot_ne_contourne_pas_les_droits(corpus):
    """Élargir l'expression élargit ce qu'une agrégation peut divulguer :
    « Duchemin Secret » ne doit pas devenir visible par son second mot."""
    visibles = user_history.corpus_terms(corpus, INDEX_CORPUS, _filtre_acl([]), "secret", 10)
    assert visibles == []

    avec_droits = user_history.corpus_terms(
        corpus, INDEX_CORPUS, _filtre_acl(["finance"]), "secret", 10
    )
    assert "Duchemin Secret" in [p["text"] for p in avec_droits]


@requiert_es
def test_ce_qui_commence_par_la_saisie_passe_devant(corpus):
    """« Marc Durand » porte deux documents contre un à « Durand
    Public » : l'agrégation le rend donc en premier, et c'est le
    reclassement — pas ES — qui doit remettre dans l'ordre."""
    propositions = user_history.corpus_terms(corpus, INDEX_CORPUS, _filtre_acl([]), "durand", 10)
    auteurs = [p["text"] for p in propositions if p["kind"] == "author"]
    assert auteurs == ["Durand Public", "Marc Durand"]


# ── 4. Échappement du préfixe ────────────────────────────────

# Tête de l'expression produite par regex_prefixe() : le match a le droit
# de commencer après un séparateur interne au terme. Écrite en toutes
# lettres plutôt qu'importée du module — un test qui relit la constante
# qu'il vérifie ne vérifie plus rien.
DEBUT_DE_MOT = r"(.*[ ,;:/'\.\-])?"


def test_regex_insensible_a_la_casse_et_ouverte_aux_deux_bouts():
    assert user_history.regex_prefixe("ab") == DEBUT_DE_MOT + "[aA][bB].*"


def test_regex_echappe_les_caracteres_reserves():
    """Sans échappement, ce `.*` collé dans la barre de recherche ferait
    balayer tout le dictionnaire de termes de l'index."""
    assert user_history.regex_prefixe(".*") == DEBUT_DE_MOT + "\\.\\*.*"
    assert user_history.regex_prefixe("a(b") == DEBUT_DE_MOT + "[aA]\\([bB].*"


def test_regex_borne_la_saisie():
    assert len(user_history.regex_prefixe("a" * 500)) < 500


@requiert_es
def test_elasticsearch_accepte_un_prefixe_hostile(corpus):
    """Le seul juge de l'échappement est le moteur : une expression mal
    échappée lui fait renvoyer une 400, que l'utilisateur verrait sous
    ses doigts en tapant une parenthèse."""
    for saisie in ['a(b', 'c[d', 'e"f', 'g\\h', '.*', 'i{2}', 'j~k']:
        assert user_history.corpus_terms(corpus, INDEX_CORPUS, _filtre_acl([]), saisie, 5) == []


# ── 5. Documents récemment consultés ─────────────────────────
#
# La donnée existe déjà : chaque clic sur un résultat est enregistré en
# `nested` sous la recherche qui l'a produit (voir POST /click). On ne
# collecte rien de neuf, on le rend à son auteur.

@pytest.fixture
def clics(es, journal):
    """Deux recherches de MOI portant des clics, une d'AUTRE."""
    es.index(index=INDEX_JOURNAL, document={
        "username": MOI, "query": "budget", "timestamp": "2026-08-10T09:00:00+00:00",
        "clicks": [
            {"doc_id": "doc-ancien", "position": 0, "timestamp": "2026-08-10T09:00:10+00:00"},
            {"doc_id": "doc-recent", "position": 1, "timestamp": "2026-08-10T09:00:20+00:00"},
        ],
    })
    es.index(index=INDEX_JOURNAL, document={
        "username": MOI, "query": "budget", "timestamp": "2026-08-11T09:00:00+00:00",
        "clicks": [
            {"doc_id": "doc-recent", "position": 0, "timestamp": "2026-08-11T09:00:05+00:00"},
        ],
    })
    es.index(index=INDEX_JOURNAL, document={
        "username": AUTRE, "query": "dossier", "timestamp": "2026-08-12T09:00:00+00:00",
        "clicks": [
            {"doc_id": "doc-de-lautre", "position": 0, "timestamp": "2026-08-12T09:00:05+00:00"},
        ],
    })
    es.indices.refresh(index=INDEX_JOURNAL)
    return es


@requiert_es
def test_les_documents_consultes_sont_dedoublonnes_et_recents_d_abord(clics):
    """Un document ouvert deux fois n'apparaît qu'une fois, à la date de
    la dernière consultation."""
    assert user_history.recent_documents(clics, MOI, 10) == ["doc-recent", "doc-ancien"]


@requiert_es
def test_les_consultations_des_autres_ne_remontent_pas(clics):
    """Même cloisonnement que l'historique de recherche : c'est le
    journal de toute l'installation qui est lu."""
    assert "doc-de-lautre" not in user_history.recent_documents(clics, MOI, 10)
    assert user_history.recent_documents(clics, AUTRE, 10) == ["doc-de-lautre"]
