# tests/test_sources_sql_mapping.py — Le registre des sources SQL doit
# refuser un mapping qui ne peut pas tenir dans Elasticsearch.
#
# Deux entrées visant le même `es_field` sont ambiguës de bout en bout :
# sql_indexer.py construit le mapping ET le document par écrasement
# successif, donc la dernière gagne — mais seulement à la CRÉATION de
# l'index, ES refusant ensuite de changer le type d'un champ existant. Le
# type déclaré et le type réel divergent alors sans que rien ne le dise.
#
# Ce n'est pas une hypothèse : le 2026-08-13, la source « agents »
# déclarait « titre -> title (text) » puis « titre -> title (keyword,
# facette) ». Le contrôle de facette voyait « keyword » et laissait
# passer ; l'agrégation frappait un champ `text` ; 13 shards sur 14
# échouaient ; la recherche fédérée annonçait 0 résultat sur 23 000
# documents (voir tests/test_resultats_partiels.py, l'autre moitié du
# garde-fou).
#
# Aucune dépendance externe : la validation est une fonction pure,
# appelée avant le moindre accès à Redis — y compris par add_source(),
# ce que vérifie le dernier test.

import pytest

import sql_sources_config


def _mapping_valide() -> list[dict]:
    return [
        {"column": "id",    "es_field": "id",      "es_type": "keyword"},
        {"column": "titre", "es_field": "title",   "es_type": "text", "analyzer": "french"},
        {"column": "bureau", "es_field": "bureau", "es_type": "keyword", "facet": True},
    ]


def test_un_mapping_sans_collision_passe():
    """Le chemin nominal, pour que les tests suivants prouvent bien le
    refus et non une erreur de forme."""
    fields = _mapping_valide()

    assert sql_sources_config._validate_fields(fields, id_column="id") == fields


def test_deux_colonnes_vers_le_meme_champ_es_sont_refusees():
    """LE test de ce fichier : la forme exacte qui a mis la recherche à
    zéro."""
    fields = _mapping_valide() + [
        {"column": "titre", "es_field": "title", "es_type": "keyword", "facet": True},
    ]

    with pytest.raises(ValueError) as erreur:
        sql_sources_config._validate_fields(fields, id_column="id")

    message = str(erreur.value)
    # Le message doit nommer le champ ET les deux types : sans eux,
    # l'administrateur ne sait pas laquelle des deux lignes supprimer.
    assert "title" in message
    assert "text" in message and "keyword" in message


def test_la_collision_est_refusee_meme_sans_facette():
    """Le défaut n'est pas « une facette de trop » mais « un champ mappé
    deux fois » : le refus ne doit pas dépendre de l'attribut facet, qui
    n'existe que côté docsearch-api (la copie docsearch-ingestion,
    elle, n'en a aucune notion)."""
    fields = [
        {"column": "id",     "es_field": "id",    "es_type": "keyword"},
        {"column": "nom",    "es_field": "titre", "es_type": "text"},
        {"column": "prenom", "es_field": "titre", "es_type": "text"},
    ]

    with pytest.raises(ValueError, match="titre"):
        sql_sources_config._validate_fields(fields, id_column="id")


def test_une_facette_sur_un_champ_text_reste_refusee():
    """Le contrôle voisin, qui ne dit la vérité que depuis que les
    collisions sont écartées — un `text` déclaré tel quel n'est toujours
    pas agrégeable."""
    fields = [
        {"column": "id",    "es_field": "id",    "es_type": "keyword"},
        {"column": "titre", "es_field": "title", "es_type": "text", "facet": True},
    ]

    with pytest.raises(ValueError, match="facette"):
        sql_sources_config._validate_fields(fields, id_column="id")


def test_add_source_refuse_avant_d_ecrire_dans_redis():
    """La garantie qui compte pour l'administration : POST
    /admin/sql-sources traduit ce ValueError en 400 (voir
    admin_add_sql_source), et le registre n'est pas modifié.

    Aucun marqueur `requires_redis` : si la validation laissait passer,
    ce test écrirait dans le Redis de l'installation de dev — l'échec
    serait donc visible, et bruyant, plutôt que silencieux."""
    with pytest.raises(ValueError, match="mappé deux fois"):
        sql_sources_config.add_source(
            name="sonde_collision_test",
            db_type="postgresql",
            connection_ref="CLIENTS_PG_DSN",
            query="SELECT id, titre FROM t",
            id_column="id",
            es_index="sonde_collision_test",
            fields=[
                {"column": "id",    "es_field": "id",    "es_type": "keyword"},
                {"column": "titre", "es_field": "title", "es_type": "text"},
                {"column": "titre", "es_field": "title", "es_type": "keyword", "facet": True},
            ],
        )

    assert "sonde_collision_test" not in sql_sources_config.get_sources()
