# admin_scan.py — Déclenchement d'un scan d'indexation et purge, côté API
#
# Duplique le strict nécessaire de docsearch-ingestion/producer.py et
# indexer.py (impossible d'importer un autre dépôt dans l'architecture
# multi-dépôts). Permet de déclencher un scan ou une purge depuis le
# panneau d'administration SANS accès Docker : l'API publie simplement
# sur Kafka comme le ferait producer.py, et les workers déjà actifs
# consomment ces messages normalement — aucune élévation de privilège
# nécessaire côté API.
#
# Seule la DÉTECTION d'archive est dupliquée ici (pas l'extraction,
# qui reste faite par les workers) : l'API n'a pas besoin de connaître
# le contenu d'une archive, seulement de savoir qu'il s'agit d'une
# archive pour la publier telle quelle sur Kafka.

import os
import json
import logging
from pathlib import Path

from elasticsearch import Elasticsearch
from elasticsearch.helpers import scan as es_scan, bulk as es_bulk
from kafka import KafkaProducer
from kafka.errors import KafkaError

from filetype_config import is_allowed
from path_filter import is_path_allowed, is_dir_excluded, matches_pattern

logger = logging.getLogger(__name__)

ES_HOST         = os.getenv("ES_HOST", "http://localhost:9200")
ES_INDEX        = os.getenv("ES_INDEX", "documents")
DOCS_FOLDER     = os.getenv("DOCS_FOLDER", "/documents")
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "kafka:9092")
KAFKA_TOPIC     = os.getenv("KAFKA_TOPIC", "documents-to-index")

# Client ES dédié à ce module (pas de dépendance circulaire avec
# search_api.py — chaque module qui a besoin d'ES crée son propre
# client léger, la connexion réelle n'est établie qu'à la 1ère requête).
_es = Elasticsearch(ES_HOST, retry_on_timeout=True, max_retries=3, request_timeout=60)


def _archive_kind(path: Path) -> str | None:
    """
    Détection minimale — voir docsearch-ingestion/archive_extractor.py
    (archive_kind) pour la logique de référence, dupliquée ici à
    l'identique. Ne PAS utiliser path.suffix pour les extensions
    composées : Path("x.tar.gz").suffix vaut ".gz", pas ".tar.gz".
    """
    name = path.name.lower()
    if name.endswith(".tar.gz"):  return "tar.gz"
    if name.endswith(".tar.bz2"): return "tar.bz2"
    if name.endswith(".tar.xz"):  return "tar.xz"
    if name.endswith(".tgz"):     return "tgz"
    if name.endswith(".tbz2"):    return "tbz2"
    if name.endswith(".txz"):     return "txz"
    if name.endswith(".tar"):     return "tar"
    if name.endswith(".zip"):     return "zip"
    if name.endswith(".7z"):      return "7z"
    return None


def _is_archive(path: Path) -> bool:
    return _archive_kind(path) is not None


def _is_excluded_name(filename: str) -> bool:
    """Fichiers temporaires — voir docsearch-ingestion/indexer.py:is_excluded."""
    return filename.startswith("~") or filename.startswith(".~")


def trigger_scan(subfolder: str | None = None) -> dict:
    """
    Publie sur Kafka les fichiers d'un sous-dossier de DOCS_FOLDER (ou
    de DOCS_FOLDER entier si `subfolder` est None) — équivalent de
    producer.py, déclenchable depuis le panneau d'administration.
    Le travail réel (extraction Tika, indexation ES) est fait par les
    workers déjà actifs, pas par l'API elle-même.
    """
    docs_root = Path(DOCS_FOLDER).resolve()

    if subfolder:
        candidate = Path(subfolder) if os.path.isabs(subfolder) else docs_root / subfolder
        candidate = candidate.resolve()
        if docs_root != candidate and docs_root not in candidate.parents:
            raise ValueError(f"'{candidate}' est en dehors de DOCS_FOLDER ({docs_root})")
        if not candidate.is_dir():
            raise ValueError(f"Dossier introuvable : {candidate}")
        target = candidate
    else:
        target = docs_root

    producer = KafkaProducer(
        bootstrap_servers=KAFKA_BOOTSTRAP,
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        acks=1, retries=3, linger_ms=20, batch_size=32768,
    )

    published, skipped = 0, 0
    try:
        for root, dirs, files in os.walk(target):
            rel_root = os.path.relpath(root, docs_root)
            if rel_root == ".":
                rel_root = ""

            dirs[:] = [
                d for d in dirs
                if not is_dir_excluded(f"{rel_root}/{d}" if rel_root else d)
            ]

            for filename in files:
                filepath = os.path.join(root, filename)
                path = Path(filepath)
                rel_file = f"{rel_root}/{filename}" if rel_root else filename

                if _is_excluded_name(path.name):
                    skipped += 1
                    continue

                allowed, _ = is_path_allowed(rel_file)
                if not allowed:
                    skipped += 1
                    continue

                extension = path.suffix.lower()
                archive = _is_archive(path)

                try:
                    size = path.stat().st_size
                except OSError:
                    skipped += 1
                    continue

                check_key = _archive_kind(path) if archive else extension
                ok, _ = is_allowed(check_key, size)
                if not ok:
                    skipped += 1
                    continue

                message = {
                    "filepath":   str(path.resolve()),
                    "extension":  extension,
                    "is_archive": archive,
                }
                try:
                    producer.send(KAFKA_TOPIC, value=message)
                    published += 1
                except KafkaError as e:
                    logger.error(f"Erreur publication [{filepath}] : {e}")
    finally:
        producer.flush(timeout=30)
        producer.close()

    return {"published": published, "skipped": skipped, "target": str(target)}


def _relative_candidate(filepath: str) -> str:
    """Voir docsearch-ingestion/indexer.py:_relative_candidates
    (version à une seule valeur, cohérente avec le comportement archive)."""
    docs_root = str(Path(DOCS_FOLDER).resolve())
    archive_part = filepath.split("::", 1)[0]
    try:
        return str(Path(archive_part).resolve().relative_to(docs_root))
    except ValueError:
        return archive_part


def purge_path(pattern: str, dry_run: bool = True) -> int:
    """
    Équivalent de docsearch-ingestion/indexer.py:purge_path(), dupliqué
    ici pour permettre le déclenchement depuis le panneau
    d'administration. Voir ce fichier pour la documentation complète
    du comportement (scan/scroll ES, gestion des membres d'archive).
    """
    to_delete = []
    matched = 0

    for hit in es_scan(
        _es, index=ES_INDEX,
        query={"query": {"match_all": {}}},
        _source=["filepath"],
    ):
        filepath = hit["_source"].get("filepath", "")
        if not filepath:
            continue
        rel = _relative_candidate(filepath)
        if matches_pattern(rel, pattern):
            matched += 1
            if not dry_run:
                to_delete.append({
                    "_op_type": "delete",
                    "_index":   ES_INDEX,
                    "_id":      hit["_id"],
                })

    if to_delete:
        ok, errors = es_bulk(_es, to_delete, raise_on_error=False)
        if errors:
            logger.error(f"[purge_path] {len(errors)} erreur(s) de suppression")
        _es.indices.refresh(index=ES_INDEX)

    return matched
