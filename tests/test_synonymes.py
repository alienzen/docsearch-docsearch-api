# tests/test_synonymes.py — Le thésaurus fait-il ce qu'il promet, et le
# fait-il À CHAUD ?
#
# Ce chantier repose sur trois propriétés d'Elasticsearch dont deux
# échouent EN SILENCE si on s'y prend mal — d'où des tests contre le vrai
# moteur, seul juge en la matière :
#
# 1. **L'ordre des filtres.** Les synonymes doivent passer AVANT le
#    stemmer : sinon l'expansion n'est pas racinisée alors que le texte
#    indexé l'a été, et la recherche ne trouve rien. Aucune erreur,
#    aucune trace : juste zéro résultat.
# 2. **Le rechargement à chaud.** Une règle ajoutée doit prendre effet
#    sans réindexer les documents — c'est toute la raison de mettre le
#    filtre dans un analyseur de RECHERCHE.
# 3. **La recherche entre guillemets ne s'élargit pas.** « terme exact »
#    veut dire exact ; l'utilisateur qui en tape ne s'attend pas à ce
#    qu'on élargisse sa requête.
# 4. **Ajouter une règle n'enlève jamais de résultats.** Lucene abandonne
#    la fuzziness sur les positions élargies par le thésaurus — sans
#    précaution, une règle bien écrite RÉDUIT le nombre de résultats du
#    terme qu'elle enrichit. Silencieusement, là encore, et à
#    contre-sens de tout ce qu'un administrateur peut imaginer.
#
# Le jeu de synonymes est celui de la sonde, jamais celui de
# l'installation (principe 2 de conftest.py) : les modules concernés sont
# détournés vers un identifiant dédié, supprimé avant et après.

import pytest

import cluster_status
import search_query
import synonyms

requiert_es = pytest.mark.requires_elasticsearch

INDEX_SONDE = "docsearch_test_sonde_synonymes"
JEU_SONDE = "docsearch_test_sonde_fr"
DELAI_ES = 60

# Repris à l'identique de docsearch-ingestion/app/indexer.py (ANALYSE) —
# c'est CETTE forme que les tests valident, ordre des filtres compris.
ANALYSE = {
    "analyzer": {
        "french": {
            "tokenizer": "standard",
            "filter": ["lowercase", "french_stop", "french_stemmer"],
        },
        "french_search": {
            "tokenizer": "standard",
            "filter": ["lowercase", "synonymes_fr", "french_stop", "french_stemmer"],
        },
        # Sans filtre de synonymes, à dessein : c'est là-dessus que porte
        # la branche de rattrapage orthographique de build_text_clause,
        # dont toute la raison d'être est d'échapper au thésaurus (voir
        # test_ajouter_une_regle_n_enleve_jamais_de_resultats).
        "exact": {
            "tokenizer": "standard",
            "filter": ["lowercase", "asciifolding"],
        },
    },
    "filter": {
        "french_stop": {"type": "stop", "stopwords": "_french_"},
        "french_stemmer": {"type": "stemmer", "language": "light_french"},
        "synonymes_fr": {
            "type": "synonym_graph",
            "synonyms_set": JEU_SONDE,
            "updateable": True,
        },
    },
}

# Le sous-champ `.exact` est peuplé À L'INDEXATION : il doit exister dès
# la création de l'index, contrairement à l'analyseur de RECHERCHE que la
# migration pose après coup. Un sous-champ ajouté plus tard resterait vide
# pour tous les documents déjà indexés — et la branche de rattrapage,
# muette, sans que rien ne le signale.
SOUS_CHAMP_EXACT = {"exact": {"type": "text", "analyzer": "exact"}}

MAPPING_CONTENT = {
    "content": {
        "type": "text",
        "analyzer": "french",
        "search_analyzer": "french_search",
        "search_quote_analyzer": "french",
        "fields": SOUS_CHAMP_EXACT,
    }
}


@pytest.fixture(scope="module")
def es():
    from elasticsearch import Elasticsearch

    client = Elasticsearch(cluster_status.ES_HOST, request_timeout=DELAI_ES, max_retries=0)
    yield client
    client.close()


@pytest.fixture(scope="module")
def thesaurus(es, monkeypatch_module=None):
    """Index de sonde migré comme le ferait `./manage.sh migrer-synonymes`,
    et jeu de synonymes dédié."""
    precedent = synonyms.SYNONYMS_SET
    synonyms.SYNONYMS_SET = JEU_SONDE

    if es.indices.exists(index=INDEX_SONDE):
        es.indices.delete(index=INDEX_SONDE)
    try:
        es.synonyms.delete_synonym(id=JEU_SONDE)
    except Exception:
        pass

    # Volontairement créé SANS l'analyseur de recherche, puis migré plus
    # bas : c'est le chemin des installations déjà en service, le seul
    # qu'une installation neuve ne rencontre jamais.
    es.indices.create(
        index=INDEX_SONDE,
        settings={
            "number_of_shards": 1,
            "number_of_replicas": 0,
            "analysis": {
                "analyzer": {
                    "french": ANALYSE["analyzer"]["french"],
                    "exact":  ANALYSE["analyzer"]["exact"],
                },
                "filter": {k: v for k, v in ANALYSE["filter"].items() if k != "synonymes_fr"},
            },
        },
        mappings={"properties": {"content": {
            "type": "text", "analyzer": "french", "fields": SOUS_CHAMP_EXACT,
        }}},
    )
    for contenu in (
        "note de la direction des ressources humaines",
        "arrêté portant délégation de signature",
        # Faute de frappe, et pas n'importe laquelle : « déléagation » se
        # racinise en `deleag`, à UNE correction de `deleg`. C'est donc
        # exactement le document que l'ancienne clause trouvait par sa
        # fuzziness, et que l'ajout d'une règle faisait disparaître — le
        # cas qui donnait MOINS de résultats après enrichissement du
        # thésaurus. Une faute plus grossière (« délégatoin » → `delegatoin`)
        # ne prouverait rien : l'ancienne clause ne la trouvait pas non plus.
        "arrêté portant déléagation de signature",
        # Ne partage aucun mot avec les autres tests du fichier : la sonde
        # est partagée, et un « drh » de plus ici ferait mentir leurs
        # comptages.
        "note de mutation interne",
    ):
        es.index(index=INDEX_SONDE, document={"content": contenu}, refresh=True)

    yield es

    synonyms.SYNONYMS_SET = precedent
    es.indices.delete(index=INDEX_SONDE)
    try:
        es.synonyms.delete_synonym(id=JEU_SONDE)
    except Exception:
        pass


def _migrer(es) -> None:
    """Ce que fait migrer_analyse() côté ingestion : fermer, poser
    l'analyseur, rouvrir, basculer le champ."""
    es.indices.close(index=INDEX_SONDE)
    try:
        es.indices.put_settings(index=INDEX_SONDE, settings={"analysis": ANALYSE})
    finally:
        es.indices.open(index=INDEX_SONDE)
    es.indices.put_mapping(index=INDEX_SONDE, properties=MAPPING_CONTENT)


def _trouve(es, requete: dict) -> int:
    es.indices.refresh(index=INDEX_SONDE)
    return es.search(index=INDEX_SONDE, query=requete)["hits"]["total"]["value"]


# ── Validation des règles ────────────────────────────────────

def test_refuse_la_forme_a_vers_b():
    """« a => b » remplace les termes d'origine au lieu de les compléter :
    surprenant pour tout le monde, et très mal réglable depuis une
    interface d'administration."""
    with pytest.raises(synonyms.RegleInvalide):
        synonyms.valider("drh => direction des ressources humaines")


def test_refuse_une_regle_a_un_seul_terme():
    with pytest.raises(synonyms.RegleInvalide):
        synonyms.valider("drh")


def test_normalise_les_espaces():
    assert synonyms.valider("  drh ,   direction   des ressources humaines ") == (
        "drh, direction des ressources humaines"
    )


# ── Identité d'une règle ─────────────────────────────────────

def test_deux_regles_de_meme_premier_terme_ont_des_identifiants_distincts():
    """L'identifiant ne dérivait que du premier terme : « DRH, service du
    personnel » écrasait alors « DRH, direction des ressources humaines »
    EN SILENCE, sans erreur ni trace."""
    assert synonyms._identifiant("drh, direction des ressources humaines") != (
        synonyms._identifiant("drh, service du personnel")
    )


def test_deux_premiers_termes_voisins_ne_se_confondent_pas():
    """Le nettoyage du préfixe lisible ramène tout à [a-z0-9_] : deux
    premiers termes distincts s'y écrasaient (« ressources humaines » et
    « ressources-humaines » donnaient un seul « ressources_humaines »).
    Ce sont pourtant deux jetons distincts pour le moteur."""
    assert synonyms._identifiant("ressources humaines, rh") != (
        synonyms._identifiant("ressources-humaines, rh")
    )


def test_l_identifiant_ignore_l_ordre_et_la_casse():
    """L'équivalence est symétrique : réécrire une règle dans un autre
    ordre remplace la précédente au lieu de la doubler."""
    assert synonyms._identifiant("drh, service du personnel") == (
        synonyms._identifiant("Service du Personnel, DRH")
    )


def test_l_identifiant_distingue_les_accents():
    """Le filtre de synonymes ne voit que `lowercase` — pas de repli
    d'accents. « congés » et « conges » y sont deux termes aux effets
    distincts, et confondre les deux règles en ferait disparaître une."""
    assert synonyms._identifiant("congés, vacances") != (
        synonyms._identifiant("conges, vacances")
    )


# ── Le moteur ────────────────────────────────────────────────

@requiert_es
def test_la_migration_active_les_synonymes_sans_reindexer(thesaurus):
    """L'index existe déjà et ses documents ne bougent pas : seul
    l'analyseur DE RECHERCHE change."""
    es = thesaurus
    assert _trouve(es, {"match": {"content": "drh"}}) == 0

    _migrer(es)
    synonyms.ajouter(es, "drh, direction des ressources humaines")

    assert _trouve(es, {"match": {"content": "drh"}}) == 1


@requiert_es
def test_une_regle_ajoutee_prend_effet_a_chaud(thesaurus):
    """Ni réindexation, ni fermeture d'index, ni redémarrage : c'est
    toute la raison d'un filtre `updateable` en analyseur de recherche."""
    es = thesaurus
    assert _trouve(es, {"match": {"content": "ressources humaines de la boîte"}}) >= 0

    synonyms.ajouter(es, "rh, ressources humaines")
    assert _trouve(es, {"match": {"content": "rh"}}) == 1


@requiert_es
def test_ajouter_une_regle_n_enleve_jamais_de_resultats(thesaurus):
    """LE contre-sens du thésaurus, et la raison d'être des deux branches
    de build_text_clause : une règle bien écrite FAISAIT CHUTER le nombre
    de résultats du terme qu'elle enrichit.

    Lucene n'applique la fuzziness que dans `newTermQuery`, appelé pour
    les positions à jeton unique ; une position élargie par le thésaurus
    passe par `newSynonymQuery`, qui construit une `SynonymQuery` de
    termes bruts. La règle échangeait donc, en silence, la tolérance aux
    fautes contre l'expansion — sur l'index de la VM de dev, « congés »
    perdait 5330 documents pour en gagner 2, et l'administrateur n'avait
    aucun moyen de relier la chute à la règle qu'il venait d'écrire.

    Ni l'ordre des filtres ni les jetons produits n'y étaient pour quoi
    que ce soit : le panneau de test du thésaurus affichait `drh, cong`,
    parfaitement corrects. C'est la CLAUSE qui devait changer, pas
    l'analyse — d'où la branche floue portée par les sous-champs
    `.exact`, seuls champs hors de portée du thésaurus.
    """
    es = thesaurus
    _migrer(es)

    def chercher(mot: str) -> set[str]:
        es.indices.refresh(index=INDEX_SONDE)
        reponse = es.search(
            index=INDEX_SONDE, query=search_query.build_text_clause(mot, "all"),
        )
        return {hit["_source"]["content"] for hit in reponse["hits"]["hits"]}

    avant = chercher("délégation")
    assert any("déléagation" in contenu for contenu in avant), (
        "la faute de frappe doit être rattrapée AVANT la règle — sans quoi "
        "le test ne prouve rien de ce qu'il prétend prouver"
    )

    synonyms.ajouter(es, "délégation, mutation")
    apres = chercher("délégation")

    # Le point du test : une règle AJOUTE, elle ne retire pas.
    assert avant <= apres
    assert any("mutation" in contenu for contenu in apres)


@requiert_es
def test_l_expansion_passe_par_le_stemmer(thesaurus):
    """LE piège de ce chantier : avec le filtre de synonymes APRÈS le
    stemmer, l'expansion resterait non racinisée face à un index racinisé
    — zéro résultat, sans la moindre erreur. Les jetons rendus ici le
    prouvent : « ressources » ressort raciné."""
    jetons = synonyms.tester(thesaurus, INDEX_SONDE, "drh")["jetons"]

    assert "drh" in jetons
    assert "ressources" not in jetons   # la forme brute ne doit PAS sortir
    assert any(jeton.startswith("resourc") or jeton.startswith("ressourc") for jeton in jetons)


@requiert_es
def test_la_recherche_entre_guillemets_ne_s_elargit_pas(thesaurus):
    """« terme exact » veut dire exact : le search_quote_analyzer du champ
    n'a pas de synonymes."""
    es = thesaurus
    # Sans guillemets, le synonyme trouve le document…
    assert _trouve(es, {"match": {"content": "drh"}}) == 1
    # …avec, la phrase est prise au pied de la lettre.
    assert _trouve(es, {"multi_match": {"query": "drh", "fields": ["content"], "type": "phrase"}}) == 0


@requiert_es
def test_deux_regles_partageant_leur_premier_terme_cohabitent(thesaurus):
    """La règle ajoutée en second ne remplace plus la première, et le
    moteur cumule bien les deux expansions — ce qu'il faisait déjà quand
    le terme commun n'était pas en tête."""
    es = thesaurus
    synonyms.ajouter(es, "drh, service du personnel")

    regles = {r["regle"] for r in synonyms.lister(es)}
    assert "drh, direction des ressources humaines" in regles
    assert "drh, service du personnel" in regles

    # Les deux expansions sortent ensemble, racinisées : « personnel »
    # ressort en « personel », d'où le préfixe court.
    jetons = synonyms.tester(es, INDEX_SONDE, "drh")["jetons"]
    assert any(jeton.startswith("person") for jeton in jetons)
    assert any(jeton.startswith("resourc") or jeton.startswith("ressourc") for jeton in jetons)
    assert _trouve(es, {"match": {"content": "drh"}}) == 1


@requiert_es
def test_reecrire_une_regle_dans_un_autre_ordre_ne_la_duplique_pas(thesaurus):
    """Le dédoublonnage porte sur les termes, pas sur l'identifiant : une
    règle réécrite dans un autre ordre remplace la précédente."""
    es = thesaurus
    synonyms.ajouter(es, "Service du personnel, DRH")

    equivalentes = [
        r for r in synonyms.lister(es)
        if synonyms._canonique(r["regle"]) == synonyms._canonique("drh, service du personnel")
    ]
    assert len(equivalentes) == 1
    assert equivalentes[0]["regle"] == "Service du personnel, DRH"


@requiert_es
def test_supprimer_la_derniere_regle_ne_casse_pas_la_recherche(thesaurus):
    """Retirer la dernière règle laisse un jeu VIDE, et surtout pas un jeu
    supprimé : Elasticsearch refuse (400) de supprimer un jeu référencé
    par un index, et tous nos index de documents le référencent. Un jeu
    vide se comporte comme l'absence de synonymes, l'index continuant de
    fonctionner normalement."""
    es = thesaurus
    for regle in synonyms.lister(es):
        synonyms.supprimer(es, regle["id"])

    assert synonyms.lister(es) == []
    assert _trouve(es, {"match": {"content": "direction"}}) == 1
    assert _trouve(es, {"match": {"content": "drh"}}) == 0
