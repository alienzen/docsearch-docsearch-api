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

import re
import time

import pytest

import cluster_status
import search_api
import search_log
import sql_sources_config
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
def test_replie_les_accents_de_l_historique(journal):
    """Personne ne pose d'accent dans une barre de recherche : « marche »
    doit retrouver « marché de travaux », et « marché » aussi."""
    sans = [e["query"] for e in user_history.matching_queries(journal, MOI, "marche", 5)]
    avec = [e["query"] for e in user_history.matching_queries(journal, MOI, "marché", 5)]
    assert "marché de travaux" in sans
    assert "marché de travaux" in avec


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
                # Ce qu'une source SQL peut déclarer en facette : le
                # `keyword` qui se suggère, le `boolean` qui en a le droit
                # mais ne se suggère pas, et le `text` qui n'a rien à faire
                # là mais qu'une configuration périmée peut désigner (voir
                # champs_agregables).
                "bureau": {"type": "keyword"},
                "actif": {"type": "boolean"},
                "resume": {"type": "text"},
                # Sept facettes pour un plafond de six — voir le test du
                # plafond, qui a besoin d'un champ de trop.
                **{f"facette_{i}": {"type": "keyword"} for i in range(1, 8)},
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
            "bureau": "Paris",
            "actif": True,
            "resume": "Paris et sa région",
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
            "bureau": "Parme Secret",
            "acl": {"public": False, "groups": ["finance"]},
        },
    )
    # Un troisième auteur commençant lui aussi par « Du », dans un bureau
    # qui commence pareil : de quoi vérifier que le tour de rôle laisse une
    # place à la facette même quand les auteurs pourraient tout prendre.
    es.index(
        index=INDEX_CORPUS,
        id="tour-de-role",
        document={
            "author": "Dupont Martin",
            "keywords": ["budget"],
            "source": "sonde",
            "bureau": "Dunkerque",
            "acl": {"public": True, "groups": []},
        },
    )
    es.index(
        index=INDEX_CORPUS,
        id="plafond",
        document={
            "source": "sonde",
            **{f"facette_{i}": "Zurich" for i in range(1, 8)},
            "acl": {"public": True, "groups": []},
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
    # Un auteur accentué, et un second qui le porte en NOM et l'emporte
    # au nombre de documents : de quoi vérifier que le repli d'accents
    # sert aussi bien à sélectionner qu'à classer.
    es.index(
        index=INDEX_CORPUS,
        id="accent",
        document={
            "author": "Émilie Dubois",
            "keywords": ["procédure"],
            "source": "sonde",
            "acl": {"public": True, "groups": []},
        },
    )
    for identifiant in ("accent-tri-1", "accent-tri-2"):
        es.index(
            index=INDEX_CORPUS,
            id=identifiant,
            document={
                "author": "Jean Émilie",
                "keywords": ["procédure"],
                "source": "sonde",
                "acl": {"public": True, "groups": []},
            },
        )
    es.indices.refresh(index=INDEX_CORPUS)
    # `field_caps` lit l'état du cluster, qui SUIT la création de l'index
    # au lieu de la précéder — sur cette VM, un index neuf met parfois
    # plusieurs secondes à se déclarer (voir DELAI_ES). Sans cette
    # attente, champs_agregables() ne voit aucun champ et n'en propose
    # aucun : c'est son contrat (dégrader en silence plutôt que priver
    # l'utilisateur des auteurs), mais un test ne doit pas courir contre.
    # Observé une fois sur une quinzaine d'exécutions avant cette attente.
    for _ in range(int(DELAI_ES / 0.2)):
        if "facette_7" in es.field_caps(index=INDEX_CORPUS, fields=["facette_7"])["fields"]:
            break
        time.sleep(0.2)
    # Le relevé de types est mémorisé une minute (TTL_TYPES) : un index
    # jetable recréé sous le même nom entre deux exécutions verrait sinon
    # les types de l'exécution précédente.
    user_history._types_connus.clear()
    yield es
    user_history._types_connus.clear()
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


@requiert_es
def test_replie_les_accents_du_corpus(corpus):
    """Dans les deux sens, et sans toucher au terme rendu : c'est celui
    de l'index, accent compris, puisqu'il servira de filtre exact."""
    sans = user_history.corpus_terms(corpus, INDEX_CORPUS, _filtre_acl([]), "emilie", 10)
    avec = user_history.corpus_terms(corpus, INDEX_CORPUS, _filtre_acl([]), "Émilie", 10)
    assert "Émilie Dubois" in [p["text"] for p in sans]
    assert "Émilie Dubois" in [p["text"] for p in avec]


@requiert_es
def test_replie_les_accents_des_mots_cles(corpus):
    propositions = user_history.corpus_terms(corpus, INDEX_CORPUS, _filtre_acl([]), "procedure", 10)
    assert [p["text"] for p in propositions if p["kind"] == "keyword"] == ["procédure"]


@requiert_es
def test_le_repli_vaut_aussi_pour_le_classement(corpus):
    """« Jean Émilie » porte deux documents contre un à « Émilie
    Dubois » : sans repli au reclassement, la saisie non accentuée ne
    reconnaîtrait aucun début de terme et laisserait l'ordre d'ES."""
    propositions = user_history.corpus_terms(corpus, INDEX_CORPUS, _filtre_acl([]), "emilie", 10)
    auteurs = [p["text"] for p in propositions if p["kind"] == "author"]
    assert auteurs == ["Émilie Dubois", "Jean Émilie"]


# ── 3 bis. Facettes personnalisées du corpus ─────────────────
#
# Mêmes agrégations que les auteurs et les mots-clés, dans la MÊME
# requête, mais sur des champs que la CONFIGURATION désigne — d'où deux
# risques que les champs fixes n'avaient pas : un champ qu'Elasticsearch
# ne sait pas agréger (qui ferait échouer la requête entière, auteurs
# compris), et un nombre de champs qui ne dépend plus du code.

BUREAU = {"bureau": "Bureau"}


@requiert_es
def test_propose_les_valeurs_d_une_facette_personnalisee(corpus):
    """`field` et `label` en plus du texte : l'interface doit savoir
    QUELLE facette cocher, ce qu'un texte seul ne dit pas."""
    propositions = user_history.corpus_terms(
        corpus, INDEX_CORPUS, _filtre_acl([]), "paris", 10, champs_custom=BUREAU,
    )
    assert propositions == [
        {"text": "Paris", "kind": "custom", "count": 1, "field": "bureau", "label": "Bureau"}
    ]


@requiert_es
def test_les_valeurs_de_facette_respectent_les_droits(corpus):
    """Une agrégation de facette fuit exactement comme celle des auteurs :
    « Parme Secret » ne vit que dans le document réservé au groupe
    « finance »."""
    visibles = user_history.corpus_terms(
        corpus, INDEX_CORPUS, _filtre_acl([]), "parme", 10, champs_custom=BUREAU,
    )
    assert visibles == []

    avec_droits = user_history.corpus_terms(
        corpus, INDEX_CORPUS, _filtre_acl(["finance"]), "parme", 10, champs_custom=BUREAU,
    )
    assert "Parme Secret" in [p["text"] for p in avec_droits]


@requiert_es
def test_ne_retient_que_les_champs_reellement_agregables(corpus):
    """Le juge est le moteur et non le type déclaré en configuration : un
    index survit à sa reconfiguration, et c'est ce qu'il PORTE qui décide.
    `resume` est en `text`, `actif` en `boolean`, `absent` n'existe pas."""
    retenus = user_history.champs_agregables(
        corpus, INDEX_CORPUS, ["bureau", "resume", "actif", "absent"],
    )
    assert retenus == frozenset({"bureau"})


@requiert_es
def test_une_agregation_regex_sur_un_champ_text_echoue_vraiment(corpus):
    """Ce que le garde-fou précédent évite, vérifié contre le vrai moteur
    plutôt que supposé : la requête échoue ENTIÈREMENT, elle ne se
    contente pas de rendre zéro bucket pour ce champ-là."""
    from elasticsearch import BadRequestError

    with pytest.raises(BadRequestError):
        corpus.search(
            index=INDEX_CORPUS,
            size=0,
            aggs={"x": {"terms": {"field": "resume", "include": "par.*"}}},
        )


@requiert_es
def test_un_champ_mal_type_ne_prive_pas_des_auteurs(corpus):
    """La conséquence de l'échec ci-dessus, et la raison d'être du
    garde-fou : une facette mal typée ne doit pas emporter avec elle les
    auteurs et les mots-clés, qui n'y sont pour rien."""
    propositions = user_history.corpus_terms(
        corpus, INDEX_CORPUS, _filtre_acl([]), "du", 10,
        champs_custom={"resume": "Résumé", "bureau": "Bureau"},
    )
    assert "Durand Public" in [p["text"] for p in propositions]
    assert "Dunkerque" in [p["text"] for p in propositions]
    assert all(p.get("field") != "resume" for p in propositions)


@requiert_es
def test_le_tour_de_role_laisse_une_place_a_la_facette(corpus):
    """Deux lignes seulement, et trois auteurs commençant par « Du » : par
    concaténation — l'ordre d'avant le 2026-08-13 — les auteurs prenaient
    toute la place et aucune facette n'était jamais visible."""
    propositions = user_history.corpus_terms(
        corpus, INDEX_CORPUS, _filtre_acl([]), "du", 2, champs_custom=BUREAU,
    )
    assert [p["kind"] for p in propositions] == ["author", "custom"]
    assert propositions[1]["text"] == "Dunkerque"


@requiert_es
def test_le_nombre_de_facettes_agregees_est_plafonne(corpus):
    """Sept facettes déclarées, six agrégées (MAX_CHAMPS_CUSTOM) : le
    coût se paie à chaque frappe, et rien n'empêche une configuration d'en
    déclarer quinze. La septième est écartée, pas tronquée en aval."""
    champs = {f"facette_{i}": f"Facette {i}" for i in range(1, 8)}
    propositions = user_history.corpus_terms(
        corpus, INDEX_CORPUS, _filtre_acl([]), "zurich", 10, champs_custom=champs,
    )
    assert [p["text"] for p in propositions] == ["Zurich"] * 6
    assert "facette_7" not in {p["field"] for p in propositions}


# ── 3 ter. Quelles facettes ont le droit d'être proposées ────
#
# Tri fait sur la CONFIGURATION (search_api._suggestable_custom_facets),
# avant toute requête : inutile d'interroger le moteur sur un champ dont
# on sait déjà qu'il n'a rien à faire dans cette liste. Ce que la
# configuration ne peut PAS dire — le type réellement en place dans les
# index — est vérifié plus haut, par champs_agregables().
#
# Le registre des sources et les groupes de l'appelant sont remplacés, et
# rien d'autre : ce sont les deux entrées de la fonction testée, pas sa
# logique (même principe qu'en tête de test_acces_sources.py). Des
# sources pour de vrai supposeraient d'écrire dans le Redis de
# configuration de l'installation de dev, que ces tests ne salissent
# jamais.

def _champ(es_field: str, es_type: str = "keyword", facet: bool = True, libelle=None):
    return sql_sources_config.FieldMapping(
        column=es_field, es_field=es_field, es_type=es_type,
        facet=facet, facet_label=libelle,
    )


def _source_sql(nom: str, *champs, groupes: tuple = ()):
    return sql_sources_config.SqlSource(
        name=nom, db_type="postgresql", connection_ref="DSN_SONDE",
        query="SELECT 1", id_column="id", es_index=f"idx_{nom}",
        poll_interval_seconds=300, fields=champs, allowed_groups=groupes,
    )


@pytest.fixture
def sources_sql(monkeypatch):
    def _poser(*sources, groupes=("docsearch-users",)):
        registre = {s.name: s for s in sources}
        monkeypatch.setattr(sql_sources_config, "get_sources", lambda: registre)
        monkeypatch.setattr(sql_sources_config, "get_source", lambda nom: registre[nom])
        monkeypatch.setattr(search_api, "get_effective_groups", lambda username: list(groupes))
    return _poser


def test_une_facette_keyword_est_proposable(sources_sql):
    sources_sql(_source_sql("agents", _champ("bureau", libelle="Bureau")))
    assert search_api._suggestable_custom_facets(MOI) == {"bureau": "Bureau"}


def test_une_facette_booleenne_n_est_pas_proposable(sources_sql):
    """`boolean` est un type de facette légitime — la sidebar l'affiche —
    mais l'`include` d'une agrégation `terms` est une expression
    régulière, qu'Elasticsearch refuse hors des champs textuels. Et
    proposer « true » sous une barre de recherche n'aiderait personne."""
    sources_sql(_source_sql("agents", _champ("actif", es_type="boolean", libelle="Actif")))
    assert search_api._suggestable_custom_facets(MOI) == {}


def test_un_champ_deja_propose_ne_l_est_pas_deux_fois(sources_sql):
    """Une source SQL a le droit de mapper une colonne sur `author`, que
    le volet fixe propose déjà : l'agréger une seconde fois afficherait
    chaque auteur deux fois, sous deux libellés."""
    sources_sql(_source_sql("agents", _champ("author", libelle="Rédacteur")))
    assert search_api._suggestable_custom_facets(MOI) == {}


def test_un_champ_declare_sous_deux_types_est_ecarte(sources_sql):
    """Deux sources, un seul nom de champ, deux mappings sur l'alias :
    l'agrégation échouerait, et emporterait tout le volet corpus. Le
    conflit ne dépend pas de qui regarde — la source fautive est ici
    invisible pour l'appelant, et pèse quand même."""
    sources_sql(
        _source_sql("agents", _champ("bureau", libelle="Bureau")),
        _source_sql("notes", _champ("bureau", es_type="text", facet=False), groupes=("dsi",)),
    )
    assert search_api._suggestable_custom_facets(MOI) == {}


def test_une_facette_d_une_source_interdite_reste_cachee(sources_sql):
    """Le seul NOM d'une facette décrit le schéma de sa source — « Motif
    de la sanction » en dit long — et cette source est cachée partout
    ailleurs à qui n'a pas le groupe."""
    sources_sql(_source_sql("rh", _champ("motif", libelle="Motif"), groupes=("rh",)))
    assert search_api._suggestable_custom_facets(MOI) == {}


# ── 4. Échappement du préfixe ────────────────────────────────

# Tête de l'expression produite par regex_prefixe() : le match a le droit
# de commencer après un séparateur interne au terme. Écrite en toutes
# lettres plutôt qu'importée du module — un test qui relit la constante
# qu'il vérifie ne vérifie plus rien.
DEBUT_DE_MOT = r"(.*[ ,;:/'\.\-])?"


def _correspond(saisie: str, terme: str) -> bool:
    """Ce que l'expression VEUT DIRE, sans passer par Elasticsearch.

    L'`include` doit couvrir le terme entier, d'où `fullmatch`. Les
    constructions employées — classes de caractères, `.*`, groupe
    optionnel — ont le même sens dans le module `re` que chez Lucene, ce
    qui permet de vérifier le sens de l'expression même quand le moteur
    est absent. Que Lucene, lui, l'ACCEPTE, reste vérifié plus bas contre
    le vrai moteur : ces deux tests ne se remplacent pas.
    """
    return re.fullmatch(user_history.regex_prefixe(saisie), terme) is not None


def test_regex_insensible_a_la_casse():
    assert _correspond("du", "Dubois")
    assert _correspond("DU", "dubois")


def test_regex_insensible_aux_accents_dans_les_deux_sens():
    """Les classes sont construites depuis le latin étendu (voir
    _VARIANTES) : la saisie accentuée trouve le terme qui ne l'est pas,
    et réciproquement."""
    assert _correspond("emi", "Émilie Dubois")
    assert _correspond("ÉMI", "Emilie Dubois")
    assert _correspond("françois", "Francois Ledoux")
    assert _correspond("francois", "François Ledoux")


def test_regex_ne_confond_pas_deux_lettres_distinctes():
    """Le repli rapproche les variantes d'une même lettre, pas les
    lettres entre elles — sans quoi il rendrait n'importe quoi."""
    assert not _correspond("emi", "Amélie Dubois")
    assert not _correspond("du", "Bubois")


def test_regex_echappe_les_caracteres_reserves():
    """Sans échappement, ce `.*` collé dans la barre de recherche ferait
    balayer tout le dictionnaire de termes de l'index. Aucune lettre
    ici, donc aucune classe : l'expression s'écrit en toutes lettres."""
    assert user_history.regex_prefixe(".*") == DEBUT_DE_MOT + "\\.\\*.*"
    assert "\\(" in user_history.regex_prefixe("a(b")


def test_regex_borne_la_saisie():
    """Un collage de 500 caractères ne doit pas produire 500 classes :
    au-delà de MAX_PREFIXE, la saisie est tronquée."""
    assert user_history.regex_prefixe("a" * 500) == user_history.regex_prefixe("a" * 60)


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
