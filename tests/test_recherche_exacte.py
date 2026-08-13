# tests/test_recherche_exacte.py — La recherche exacte tient-elle sa
# promesse, et rien qu'elle ?
#
# La promesse tient en une phrase : « les mots tels qu'écrits, aux accents
# et aux majuscules près ». Elle se décompose en quatre propriétés, dont
# aucune n'est vérifiable en lisant le code — elles dépendent entièrement
# de ce que le moteur fait de l'analyseur, d'où des tests contre le vrai
# Elasticsearch pour la partie analyse :
#
#   1. pas de racinisation — « délégations » ne répond pas à
#      « délégation », alors que la recherche ordinaire le fait ;
#   2. pas de mots vides — « état de l'art » garde ses mots outils, sans
#      quoi l'expression serait vidée avant même d'être cherchée ;
#   3. accents et casse ignorés — « Congrès », « congres » et « CONGRES »
#      sont une seule et même requête ;
#   4. pas de synonymes — un thésaurus qui élargit une recherche exacte la
#      rend inexacte.
#
# Les deux premières échouent EN SILENCE si l'analyseur est mal composé :
# un filtre en trop ne lève aucune erreur, il élargit simplement la
# recherche sans que rien ne le signale. C'est exactement le mode de
# défaillance décrit dans test_synonymes.py, et il justifie le même
# traitement.
#
# S'y ajoute une cinquième propriété, qui n'est pas de l'analyse mais de
# l'affichage : **une recherche exacte qui trouve un document doit en
# montrer l'extrait**. Elle a sa section plus bas, et elle a manqué au
# premier jet — la recherche trouvait, la carte de résultat n'affichait
# rien sous le titre. Même mode de défaillance silencieux que les deux
# premières : le hit revient sans fragment, pas en erreur.
#
# La construction de requête, elle, se teste sans moteur : ce qui s'y joue
# est un choix de champs et de clauses, pas un comportement d'indexation.

import pytest

import cluster_status
import search_query

requiert_es = pytest.mark.requires_elasticsearch

INDEX_SONDE = "docsearch_test_sonde_exacte"
DELAI_ES = 60

# Repris de docsearch-ingestion/app/indexer.py (ANALYSE / CHAMP_EXACT) —
# c'est CETTE composition que les tests valident, la liste de filtres
# comprise. Toute divergence entre les deux fichiers doit faire échouer
# quelque chose ici.
ANALYSE = {
    "analyzer": {
        "french": {
            "tokenizer": "standard",
            "filter": ["lowercase", "french_stop", "french_stemmer"],
        },
        "exact": {
            "tokenizer": "standard",
            "filter": ["lowercase", "asciifolding"],
        },
    },
    "filter": {
        "french_stop": {"type": "stop", "stopwords": "_french_"},
        "french_stemmer": {"type": "stemmer", "language": "light_french"},
    },
}

DOCUMENTS = [
    {"content": "Délégations de service public et Congrès annuel"},
    {"content": "État de l'art des systèmes documentaires"},
]


@pytest.fixture(scope="module")
def es():
    from elasticsearch import Elasticsearch

    client = Elasticsearch(cluster_status.ES_HOST, request_timeout=DELAI_ES, max_retries=0)
    yield client
    client.close()


def _attendre_index_utilisable(es) -> None:
    """Saute les tests si le shard de l'index de sonde ne s'active pas.

    `requires_elasticsearch` ne vérifie qu'une chose : le moteur répond en
    HTTP (voir _elasticsearch_reachable dans conftest.py). Un cluster peut
    répondre parfaitement tout en refusant d'allouer le moindre nouveau
    shard — c'est le cas dès que le disque dépasse le *high watermark*
    (90 % par défaut) : l'index se crée, la création est acquittée, et
    c'est l'indexation du premier document qui échoue une minute plus tard
    sur un `unavailable_shards_exception`.

    Sans cette attente, la suite ne montre pas « l'environnement est
    saturé » mais une pile d'erreurs de setup qui ressemblent à une
    régression de la recherche exacte. On distingue donc explicitement les
    deux, et on nettoie l'index à demi créé au passage — un shard non
    alloué de plus laisse le cluster en rouge pour tout le monde.
    """
    # Deux formes pour le même signal : le client lève sur le 408 que
    # renvoie l'attente expirée, mais rend le corps (avec `timed_out`)
    # dans d'autres versions. On traite les deux plutôt que de parier sur
    # l'une — un test d'environnement qui échoue sur la façon dont
    # l'échec est signalé n'aide personne.
    try:
        expire = es.cluster.health(
            index=INDEX_SONDE, wait_for_status="yellow", timeout="15s",
        ).get("timed_out", False)
    except Exception:
        expire = True

    if not expire:
        return

    es.indices.delete(index=INDEX_SONDE, ignore_unavailable=True)
    pytest.skip(
        "Elasticsearch répond mais n'alloue plus de shard — disque au-delà du "
        "high watermark (voir GET _cluster/allocation/explain)."
    )


@pytest.fixture(scope="module")
def index(es):
    """Index de sonde portant le même couple de champs que la production :
    `content` analysé en français, `content.exact` en analyseur exact."""
    if es.indices.exists(index=INDEX_SONDE):
        es.indices.delete(index=INDEX_SONDE)

    es.indices.create(
        index=INDEX_SONDE,
        # `wait_for_active_shards=0` : sans lui, la création BLOQUE 30 s
        # quand le disque dépasse le high watermark et qu'aucun shard neuf
        # ne peut être alloué. On rend la main tout de suite et c'est
        # _attendre_index_utilisable() qui tranche — un test
        # d'environnement doit conclure vite, pas faire patienter la suite
        # entière pour un verdict connu d'avance.
        wait_for_active_shards=0,
        settings={
            "number_of_shards": 1,
            # Zéro réplique : sur cette VM à un seul nœud, une réplique
            # reste éternellement non assignée et laisse le cluster en
            # « yellow » — un bruit dont les tests n'ont pas besoin.
            "number_of_replicas": 0,
            "analysis": ANALYSE,
        },
        mappings={
            "properties": {
                "content": {
                    "type": "text",
                    "analyzer": "french",
                    "fields": {"exact": {"type": "text", "analyzer": "exact"}},
                }
            }
        },
    )
    _attendre_index_utilisable(es)

    for document in DOCUMENTS:
        es.index(index=INDEX_SONDE, document=document, refresh=True)

    yield es

    es.indices.delete(index=INDEX_SONDE, ignore_unavailable=True)


def _trouve(es, requete: str, champ: str) -> int:
    """Nombre de documents trouvés pour `requete` sur `champ`.

    Sans `fuzziness` d'aucun côté : ce qui est mesuré ici est l'effet de
    l'ANALYSEUR, et la tolérance aux fautes le masquerait en rattrapant
    précisément les écarts qu'on veut voir subsister.
    """
    res = es.search(
        index=INDEX_SONDE,
        query={"multi_match": {"query": requete, "fields": [champ]}},
    )
    return res["hits"]["total"]["value"]


# ── L'analyseur : ce que le moteur fait vraiment ────────────────────

@requiert_es
def test_le_pluriel_ne_repond_plus_au_singulier(index):
    # La propriété centrale, et la seule raison d'être de tout le
    # chantier : « Délégations » est indexé, « délégation » ne doit PAS le
    # trouver en recherche exacte. La recherche ordinaire, elle, continue
    # de le faire — c'est la comparaison qui donne son sens au test, une
    # assertion isolée sur le champ exact passerait tout aussi bien si
    # l'index était vide.
    assert _trouve(index, "délégation", "content") == 1
    assert _trouve(index, "délégation", "content.exact") == 0
    assert _trouve(index, "délégations", "content.exact") == 1


@requiert_es
@pytest.mark.parametrize("requete", ["Congrès", "congres", "CONGRES", "congrès"])
def test_les_accents_et_la_casse_sont_ignores(index, requete):
    # « aux accents et aux majuscules près » : les quatre écritures sont
    # une seule requête. C'est ce que garantissent `lowercase` et
    # `asciifolding`, et c'est ce qui distingue cette recherche exacte
    # d'une comparaison littérale d'octets.
    assert _trouve(index, requete, "content.exact") == 1


@requiert_es
def test_les_mots_vides_sont_conserves(index):
    # « état de l'art » : sans mots vides, l'expression perd « de » et
    # « l' » et ne veut plus rien dire. Le filtre french_stop est donc
    # absent de l'analyseur exact — ce test le prouve par l'usage.
    res = index.search(
        index=INDEX_SONDE,
        query={"match_phrase": {"content.exact": "état de l'art"}},
    )
    assert res["hits"]["total"]["value"] == 1


def _jetons(es, analyseur: dict, texte: str) -> list[str]:
    """Jetons produits par un analyseur DÉCRIT EN LIGNE, sans index.

    Volontairement sans index : `_analyze` accepte une composition
    anonyme, ce qui n'alloue aucun shard et ne dépend donc pas de l'état
    du disque. Les tests qui suivent tournent ainsi même quand le cluster
    refuse toute nouvelle allocation — et ce sont eux qui portent la
    propriété centrale du chantier.
    """
    return [
        j["token"]
        for j in es.indices.analyze(**analyseur, text=texte)["tokens"]
    ]


@requiert_es
def test_l_analyseur_exact_ne_produit_aucun_radical(es):
    # Vérification directe de la composition de l'analyseur, et la plus
    # parlante : `_analyze` dit ce que le moteur fait RÉELLEMENT du texte,
    # là où un test de recherche n'en montre que la conséquence. Si
    # quelqu'un ajoute un stemmer par symétrie avec l'analyseur français,
    # c'est ici que ça se voit en clair.
    exact = {
        "tokenizer": ANALYSE["analyzer"]["exact"]["tokenizer"],
        "filter": ["lowercase", "asciifolding"],
    }
    francais = {
        "tokenizer": "standard",
        "filter": [
            "lowercase",
            {"type": "stop", "stopwords": "_french_"},
            {"type": "stemmer", "language": "light_french"},
        ],
    }
    texte = "Délégations de Congrès CONGRES congres"

    # Le pluriel survit, les mots vides restent, accents et casse sont
    # repliés sur une seule forme.
    assert _jetons(es, exact, texte) == [
        "delegations", "de", "congres", "congres", "congres",
    ]
    # La comparaison donne son sens au test : l'analyseur français, lui,
    # racinise (« deleg ») et supprime « de ». C'est précisément ce que la
    # recherche exacte doit cesser de faire.
    assert _jetons(es, francais, texte) == ["deleg", "congr", "congr", "congr"]


# ── L'extrait affiché : ce que l'utilisateur voit ───────────────────
#
# Trouver un document sans pouvoir montrer POURQUOI il est là ne sert à
# rien : l'extrait est la seule chose qui rattache un résultat à ce qui a
# été tapé. Ces tests exigent le moteur pour la même raison que les
# précédents — le surlignage est un comportement d'Elasticsearch, pas une
# ligne de code à relire.


def _extraits(es, requete: str, exact: bool) -> list[str]:
    """Fragments rendus pour `requete`, avec la configuration de
    surlignage de /search.

    Le champ surligné et ses options viennent de search_api, jamais d'une
    copie locale : c'est le couplage clause/champ qui est testé ici, et le
    recopier ici testerait la copie. Les balises, elles, sont réduites à
    « << >> » pour la lisibilité des assertions — celles de production
    (`<mark class="highlight">`) sont l'affaire du frontend, qui a ses
    propres tests (utils/highlight.spec.ts).
    """
    import search_api

    champ, options = search_api._config_surlignage(exact)
    res = es.search(
        index=INDEX_SONDE,
        query=search_query.build_text_clause(requete, "all", exact),
        highlight={"fields": {champ: options}, "pre_tags": ["<<"], "post_tags": [">>"]},
    )
    return [fragment for h in res["hits"]["hits"] for fragment in search_api._extraits(h)]


@requiert_es
def test_la_recherche_exacte_rend_l_extrait_du_document_trouve(index):
    # LE test du chantier, et celui qui manquait : « Délégations » est
    # trouvé — ça, c'était déjà vrai — et l'extrait le MONTRE.
    #
    # La comparaison ci-dessous donne son sens à l'assertion, et rejoue
    # exactement ce que faisait le code précédent : surligner `content`,
    # analysé en français, pour une clause portant sur `content.exact`.
    # Le document est bien trouvé (total = 1) mais AUCUN fragment ne
    # revient — les termes extraits de la requête sont les formes exactes
    # (« delegations ») et les jetons du champ sont racinisés (« deleg »),
    # donc le surligneur ne reconnaît aucun passage. Ce n'était pas un
    # extrait sans marques qui s'affichait, c'était rien du tout.
    assert _extraits(index, "délégations", exact=True) == [
        "<<Délégations>> de service public et Congrès annuel",
    ]

    ancien = index.search(
        index=INDEX_SONDE,
        query=search_query.build_text_clause("délégations", "all", exact=True),
        highlight={"fields": {"content": {"require_field_match": False}}},
    )
    assert ancien["hits"]["total"]["value"] == 1
    assert ancien["hits"]["hits"][0].get("highlight") is None


@requiert_es
@pytest.mark.parametrize("requete", ["Congrès", "congres", "CONGRES", "congrès"])
def test_l_extrait_marque_le_mot_tel_qu_il_est_ecrit_dans_le_document(index, requete):
    # Pendant d'affichage de test_les_accents_et_la_casse_sont_ignores :
    # les quatre écritures trouvent le document, et l'extrait marque la
    # forme du DOCUMENT (« Congrès »), pas celle qui a été tapée. Sans
    # quoi l'utilisateur ne verrait pas ce qu'il a réellement trouvé.
    assert _extraits(index, requete, exact=True) == [
        "Délégations de service public et <<Congrès>> annuel",
    ]


@requiert_es
def test_une_phrase_exacte_est_marquee_d_un_seul_tenant(index):
    # Les deux dimensions se croisent aussi à l'affichage : une phrase
    # exacte doit être surlignée comme UNE expression, mots outils
    # compris, et non mot à mot. C'est ce que voit l'utilisateur qui a
    # cherché « état de l'art » entre guillemets.
    assert _extraits(index, '"état de l\'art"', exact=True) == [
        "<<État de l'art>> des systèmes documentaires",
    ]


@requiert_es
def test_la_recherche_ordinaire_garde_son_extrait(index):
    # Garde-fou de non-régression : le champ surligné suit le mode, donc
    # la recherche ORDINAIRE doit continuer de surligner `content`. Une
    # correction qui basculerait tout le monde sur `content.exact` ferait
    # disparaître l'extrait de « délégation » → « Délégations », que la
    # racinisation rattrape et que l'exact, lui, ne rattrape pas.
    assert _extraits(index, "délégation", exact=False) == [
        "<<Délégations>> de service public et Congrès annuel",
    ]


def test_le_champ_surligne_suit_les_champs_interroges():
    # Sans moteur : le couplage lui-même. Les options ne dépendent PAS du
    # mode — en particulier max_analyzed_offset, dont l'absence fait
    # échouer tous les shards portant un document trop long et rend la
    # recherche entière vide.
    import search_api

    exact, options_exact = search_api._config_surlignage(True)
    ordinaire, options_ordinaire = search_api._config_surlignage(False)

    assert (exact, ordinaire) == ("content.exact", "content")
    assert options_exact == options_ordinaire
    assert options_exact["max_analyzed_offset"] == 1000000
    # `require_field_match` n'a plus à être forcé : il ne servait qu'à
    # tenter de faire surligner `content` par une clause visant
    # `content.exact`. Le laisser à False ferait surligner dans le corps
    # les termes trouvés via le titre ou l'auteur.
    assert "require_field_match" not in options_exact


def test_les_fragments_se_lisent_quel_que_soit_le_champ_surligne():
    # ES range les fragments sous le nom du champ surligné. Les lire sous
    # une clé écrite en dur ne ferait que déplacer le défaut d'un cran :
    # la liste serait vide dans l'un des deux modes.
    import search_api

    assert search_api._extraits({"highlight": {"content": ["a"]}}) == ["a"]
    assert search_api._extraits({"highlight": {"content.exact": ["b"]}}) == ["b"]
    # Un hit sans surlignage n'est pas une erreur : le document peut avoir
    # été trouvé par son titre ou son nom de fichier.
    assert search_api._extraits({}) == []


# ── La construction de requête : sans moteur ────────────────────────

def test_les_champs_exacts_portent_les_memes_poids():
    # Les poids disent qu'un mot trouvé dans un titre compte plus que dans
    # le corps ; ça ne dépend pas de la façon dont le texte est analysé.
    # Un jeu de poids qui ne s'appliquerait qu'à la recherche ordinaire
    # ferait diverger les deux classements sans que rien ne le signale.
    ordinaire = search_query.field_sets()
    exact = search_query.field_sets(exact=True)

    # Le poids se lit après « ^ » : on le retire avant de vérifier le
    # suffixe, sinon `title.exact^4` ne « finit » pas par `.exact` et
    # l'assertion passerait pour de mauvaises raisons.
    assert all(champ.split("^")[0].endswith(".exact") for champ in exact["all"])
    assert [c.split("^")[1] for c in ordinaire["all"] if "^" in c] == [
        c.split("^")[1] for c in exact["all"] if "^" in c
    ]
    # `author.text` est le sous-champ analysé de la recherche ordinaire ;
    # son équivalent exact est `author.exact`, PAS `author.text.exact` —
    # Elasticsearch refuse un sous-champ de sous-champ.
    assert exact["author"] == ["author.exact"]
    assert "author.exact" in exact["all"]


def test_la_recherche_exacte_ne_tolere_aucune_faute():
    # La tolérance aux fautes rattraperait exactement les écarts que la
    # recherche exacte est censée conserver : les deux ne peuvent pas
    # coexister dans la même clause.
    clause = search_query.build_text_clause("délégation", "all", exact=True)
    assert "fuzziness" not in clause["multi_match"]
    assert clause["multi_match"]["fields"] == search_query.field_sets(exact=True)["all"]

    # La recherche ORDINAIRE, elle, tolère les fautes — mais dans une
    # branche séparée, portée par les sous-champs `.exact` : c'est le seul
    # endroit où la fuzziness survit au thésaurus (voir
    # build_text_clause). Les deux emplois de `.exact` ne se contredisent
    # pas : le mode exact interroge ces champs SANS fuzziness, la branche
    # de rattrapage les interroge AVEC.
    ordinaire = search_query.build_text_clause("délégation", "all", exact=False)
    branches = ordinaire["bool"]["should"]

    assert "fuzziness" not in branches[0]["multi_match"]
    assert branches[0]["multi_match"]["fields"] == search_query.field_sets()["all"]

    assert branches[1]["multi_match"]["fuzziness"] == search_query.FUZZINESS
    assert branches[1]["multi_match"]["fields"] == search_query.field_sets(exact=True)["all"]


def test_le_flou_ne_corrige_pas_les_mots_courts():
    """`AUTO` seul vaut `AUTO:3,6` : une correction dès 3 caractères, deux
    à partir de 6. Sur ce corpus, les deux seuils sont faux — « loi »
    appellerait `roi` et `lot`, et à deux corrections « délégation »
    appelle `dérogation`, `délation`, `allégation`. Le plafond n'est pas
    un réglage esthétique : c'est ce qui sépare une tolérance aux fautes
    d'un générateur de faux positifs."""
    seuil_bas, seuil_haut = search_query.FUZZINESS.removeprefix("AUTO:").split(",")

    assert int(seuil_bas) >= 5
    # Seuil haut hors d'atteinte de tout mot réel = jamais deux
    # corrections. C'est la façon d'écrire ce plafond avec `AUTO`, dont on
    # garde le premier seuil.
    assert int(seuil_haut) > 40


def test_guillemets_et_mode_exact_sont_independants():
    # Les deux dimensions se croisent : les guillemets disent « dans cet
    # ordre », le mode exact dit « tels qu'écrits ». Les quatre
    # combinaisons ont un sens, et confondre les deux notions est
    # l'erreur naturelle — d'où ce test.
    phrase_ordinaire = search_query.build_text_clause('"délégation de service"', "all")
    phrase_exacte = search_query.build_text_clause('"délégation de service"', "all", exact=True)

    for clause in (phrase_ordinaire, phrase_exacte):
        assert clause["multi_match"]["type"] == "phrase"
        assert clause["multi_match"]["query"] == "délégation de service"

    assert phrase_ordinaire["multi_match"]["fields"] == search_query.field_sets()["all"]
    assert phrase_exacte["multi_match"]["fields"] == search_query.field_sets(exact=True)["all"]


def test_une_requete_vide_matche_tout_quel_que_soit_le_mode():
    # Champ de recherche vide mais filtres actifs (syntaxe avancée seule) :
    # le mode exact ne doit pas transformer ce cas en « aucun résultat ».
    assert search_query.build_text_clause("", "all", exact=True) == {"match_all": {}}


def test_une_alerte_rejoue_le_mode_exact_de_la_recherche_enregistree(monkeypatch):
    # Une alerte doit notifier sur ce que la recherche affiche. Si le
    # critère `exact` se perdait à l'enregistrement ou à la relecture,
    # l'alerte notifierait BEAUCOUP plus large que la recherche qui l'a
    # créée — et l'utilisateur n'aurait aucun moyen de comprendre pourquoi.
    monkeypatch.setattr(search_query, "get_effective_groups", lambda _: [])
    monkeypatch.setattr(search_query, "_searchable_source_names", lambda _: ["documents"])

    exacte = search_query.build_query_clauses(
        {"query": "délégation", "exact": True}, "sonde",
    )["bool"]["must"][0]["multi_match"]
    ordinaire = search_query.build_query_clauses(
        {"query": "délégation"}, "sonde",
    )["bool"]["must"][0]

    assert "fuzziness" not in exacte
    assert exacte["fields"] == search_query.field_sets(exact=True)["all"]
    # Une alerte ordinaire porte les deux branches, thésaurus et
    # rattrapage : c'est ce que l'écran affiche, donc ce qu'elle doit
    # notifier.
    assert [b["multi_match"].get("fuzziness") for b in ordinaire["bool"]["should"]] == [
        None, search_query.FUZZINESS,
    ]
