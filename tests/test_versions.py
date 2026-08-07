"""L'identité de la livraison affichée en administration.

Ce que ce fichier protège n'est pas un calcul — `check_versions` en fait
à peine — mais des noms de champs : `docsearch-ingestion` écrit
`version`/`commit`/`build_date` dans le battement de cœur du watcher, et
`docsearch-api` les relit. Rien dans le typage ne rattrape un renommage,
et le symptôme serait une ligne « Ingestion » silencieusement absente du
panneau — exactement l'information qu'on y était venu chercher.

⚠️  Portée : ces tests couvrent le CÔTÉ API du contrat. Le côté ingestion
échappe à cette suite, qui ne voit pas l'autre dépôt — il ne tient qu'au
fait que `version.py` y est une copie conforme. Un renommage fait dans
docsearch-ingestion seul ne serait rattrapé par aucun test ici.

Aucun bouchon ici, et c'est le point : `check_versions` est une fonction
pure qui prend le dict rendu par `check_watcher_heartbeat` et rend le
bloc `versions`. Les entrées des tests reproduisent donc la forme EXACTE
de ce dict, y compris ses cas dégradés (watcher muet, Redis injoignable),
plutôt que d'imiter Redis pour n'observer que l'imitation.
"""

import cluster_status
import version


def _battement(**champs) -> dict:
    """Un battement vivant, tel que `check_watcher_heartbeat` le rend."""
    return {"alive": True, "last_seen_seconds_ago": 2.0, **champs}


def test_le_bloc_api_est_celui_du_module_version():
    """L'API se décrit elle-même : pas de recopie de champs au passage,
    qui serait une seconde source de vérité à maintenir."""
    versions = cluster_status.check_versions(_battement())

    assert versions["api"] == version.infos()


def test_le_champ_version_de_l_api_n_est_jamais_vide():
    """`version.py` retombe sur le fichier VERSION quand les --build-arg
    manquent : le panneau affiche alors une version juste, seule
    l'estampille de build est perdue. Un « inconnu » ici signifierait que
    ce repli ne fonctionne plus."""
    api = cluster_status.check_versions(_battement())["api"]

    assert api["version"] and api["version"] != "inconnu"


def test_l_ingestion_reprend_les_champs_du_battement():
    versions = cluster_status.check_versions(_battement(
        version="2.2.0", commit="a1b2c3d", build_date="2026-08-07T17:00:00+02:00",
    ))

    assert versions["ingestion"] == {
        "version":    "2.2.0",
        "commit":     "a1b2c3d",
        "build_date": "2026-08-07T17:00:00+02:00",
        # Le watcher est un singleton d'ingest-1 (install-units.sh,
        # --with-singletons) : la provenance est affichée pour que
        # personne ne lise cette ligne comme l'état des trois machines
        # d'ingestion.
        "source":     "watcher (ingest-1)",
    }


def test_les_champs_ecrits_par_le_watcher_sont_ceux_que_l_api_relit():
    """Les deux moitiés du contrat, côté API, doivent s'accorder.

    Le watcher déverse `version.infos()` dans le battement — le module y
    étant une copie conforme de celui-ci. Ce que `check_versions` en
    ressort doit donc être exactement ce qu'`infos()` produit ici :
    renommer un champ dans `version.py` sans toucher à `check_versions`
    (ou l'inverse) casse ce test, au lieu de vider une ligne du panneau
    sans rien dire.

    Ce que ce test ne peut PAS voir, faute d'accès à l'autre dépôt : un
    renommage fait dans docsearch-ingestion seul.
    """
    ecrits = version.infos()
    relus = cluster_status.check_versions(_battement(**ecrits))["ingestion"]

    assert {cle: relus[cle] for cle in ecrits} == ecrits


def test_pas_de_ligne_ingestion_sans_battement_versionne():
    """Battement laissé par un watcher antérieur à 2.2.0, qui n'écrivait
    que `ts`. Mieux vaut pas de ligne qu'une ligne « ? » : l'absence est
    déjà signalée par l'état « watcher silencieux » juste au-dessus."""
    versions = cluster_status.check_versions(_battement())

    assert "ingestion" not in versions
    assert "api" in versions


def test_watcher_muet_ou_redis_injoignable():
    """Les deux formes dégradées que `check_watcher_heartbeat` peut
    rendre. Aucune ne doit faire lever `check_versions` : le panneau
    d'administration afficherait alors une erreur globale au lieu de
    l'état des composants, qui est justement ce qu'on consulte quand
    quelque chose ne va pas."""
    muet = {"alive": False, "reason": "Aucun battement reçu (watcher jamais démarré, ou Redis vidé)"}
    en_erreur = {"alive": False, "error": "Error 111 connecting to redis:6379."}

    for degrade in (muet, en_erreur):
        versions = cluster_status.check_versions(degrade)
        assert "ingestion" not in versions
        assert versions["api"] == version.infos()


def test_une_version_vide_ne_cree_pas_de_ligne():
    """Cas limite d'une image construite avec `DOCSEARCH_VERSION=` : le
    champ voyage jusqu'ici, vide. Une ligne « Ingestion : » sans valeur
    serait pire que pas de ligne."""
    versions = cluster_status.check_versions(_battement(version="", commit="a1b2c3d"))

    assert "ingestion" not in versions
