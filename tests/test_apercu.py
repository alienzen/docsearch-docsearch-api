# tests/test_apercu.py — L'aperçu montre-t-il les documents océrisés ?
#
# Une image n'entre dans l'index que si son extension a été activée pour
# la source ET que l'OCR y est activé (voir filetype_config.py, où
# jpg/png/tiff sont livrés désactivés, et indexer.py::_ocr_headers) : un
# .jpg trouvé dans l'index est donc, par construction, un document
# océrisé. Son texte était cherchable depuis toujours, mais l'aperçu
# répondait 415 — alors que le lien « Voir l'aperçu » s'affichait sur sa
# carte de résultat (ResultCard.vue ne regarde que le type de la SOURCE).
#
# Le point qui mérite un test plutôt qu'une relecture est le TIFF :
# format de sortie courant des scanners, donc précisément ce qu'on
# océrise, mais qu'aucun navigateur n'affiche. Renvoyé en `image/tiff`,
# il serait TÉLÉCHARGÉ au lieu d'être affiché — un aperçu qui n'en est
# pas, et un défaut qu'aucune erreur ne signale. Il passe donc par
# LibreOffice, comme les fichiers Office, et c'est cette conversion
# réelle qui est exécutée ici : la bouchonner ne prouverait rien, elle
# est tout l'enjeu.
#
# `get_document` est en revanche remplacé : ce qu'il fait (relecture ES
# et contrôle d'ACL) a ses propres tests (test_acces_sources.py), et
# l'aperçu n'y ajoute rien — il l'appelle AVANT tout accès au fichier,
# et c'est cet ordre-là qui est vérifié ici.

import asyncio
import shutil
import struct

import pytest
from fastapi import HTTPException

import search_api


def _tiff_minimal() -> bytes:
    """Un vrai TIFF de 2×2 pixels en niveaux de gris, non compressé.

    Fabriqué ici plutôt que déposé en fichier binaire dans le dépôt :
    126 octets lisibles valent mieux qu'un blob opaque, et rien
    n'installe Pillow pour l'occasion (docsearch-api n'en dépend pas).
    LibreOffice l'importe comme n'importe quel scan.
    """
    pixels = bytes([0, 128, 200, 255])
    entrees = [
        (256, 3, 1, 2),  # ImageWidth
        (257, 3, 1, 2),  # ImageLength
        (258, 3, 1, 8),  # BitsPerSample
        (259, 3, 1, 1),  # Compression : aucune
        (262, 3, 1, 1),  # PhotometricInterpretation : BlackIsZero
        (273, 4, 1, 8),  # StripOffsets : les pixels suivent l'en-tête
        (277, 3, 1, 1),  # SamplesPerPixel
        (278, 3, 1, 2),  # RowsPerStrip
        (279, 4, 1, 4),  # StripByteCounts
    ]
    donnees = struct.pack("<2sHI", b"II", 42, 8 + len(pixels)) + pixels
    donnees += struct.pack("<H", len(entrees))
    for tag, type_, nombre, valeur in entrees:
        donnees += struct.pack("<HHI", tag, type_, nombre) + (
            # Un SHORT tient dans les 4 octets du champ de valeur, cadré
            # à gauche ; un LONG les occupe entièrement.
            struct.pack("<HH", valeur, 0) if type_ == 3 else struct.pack("<I", valeur)
        )
    return donnees + struct.pack("<I", 0)


@pytest.fixture
def document(monkeypatch, tmp_path):
    """Fabrique un fichier et fait répondre `get_document` en
    conséquence. Renvoie la réponse de l'aperçu pour ce fichier."""
    def apercu(nom: str, contenu: bytes = b"contenu"):
        chemin = tmp_path / nom
        chemin.write_bytes(contenu)
        monkeypatch.setattr(search_api, "get_document", lambda doc_id, user: {
            "filepath": str(chemin), "extension": chemin.suffix.lower(),
        })
        return search_api.preview_document("doc-1", user="bob.user")
    return apercu


def _corps(reponse) -> bytes:
    """Vide le flux d'une StreamingResponse — asynchrone même quand on
    lui passe un itérateur ordinaire (Starlette l'y bascule)."""
    async def lire():
        return b"".join([morceau async for morceau in reponse.body_iterator])
    return asyncio.run(lire())


# ── Les images servies telles quelles ────────────────────────

@pytest.mark.parametrize("nom, type_attendu", [
    ("scan.jpg",  "image/jpeg"),
    ("scan.jpeg", "image/jpeg"),
    ("scan.png",  "image/png"),
    ("scan.gif",  "image/gif"),
    ("scan.webp", "image/webp"),
    ("scan.bmp",  "image/bmp"),
])
def test_une_image_est_servie_telle_quelle(document, nom, type_attendu):
    """Aucune conversion pour ces formats : le navigateur les affiche
    nativement, et l'image d'origine reste plus lisible que la même
    réencapsulée. Le type MIME compte autant que le fichier — c'est lui
    qui décide entre afficher et télécharger."""
    reponse = document(nom)
    assert reponse.media_type == type_attendu
    assert reponse.path.endswith(nom)


# ── Le TIFF, qu'aucun navigateur n'affiche ───────────────────

@pytest.mark.parametrize("nom", ["scan.tif", "scan.tiff"])
def test_un_tiff_est_converti_en_pdf(document, nom):
    if shutil.which("libreoffice") is None:
        pytest.skip("LibreOffice absent — présent dans l'image de l'API")

    reponse = document(nom, _tiff_minimal())

    assert reponse.media_type == "application/pdf"
    # `inline` et non `attachment` : tout ce travail viserait à côté si
    # le navigateur téléchargeait quand même le résultat.
    assert reponse.headers["content-disposition"].startswith("inline")
    # La conversion a réellement produit un PDF, pas un fichier vide ni
    # le TIFF recopié : c'est la signature qui le dit.
    assert _corps(reponse).startswith(b"%PDF-")


# ── Ce qui ne change pas ─────────────────────────────────────

def test_un_pdf_reste_servi_directement(document):
    assert document("rapport.pdf").media_type == "application/pdf"


def test_un_format_non_previsualisable_reste_refuse(document):
    """L'ouverture aux images ne devait pas ouvrir à tout : une archive
    n'a rien à montrer, et le 415 reste le bon signal."""
    with pytest.raises(HTTPException) as erreur:
        document("archive.zip")
    assert erreur.value.status_code == 415


def test_un_fichier_absent_donne_404_avant_toute_conversion(monkeypatch):
    """L'ordre importe : chercher à convertir un fichier disparu ferait
    échouer LibreOffice au bout de 30 s, là où le disque répond tout de
    suite."""
    monkeypatch.setattr(search_api, "get_document", lambda doc_id, user: {
        "filepath": "/introuvable/scan.tiff", "extension": ".tiff",
    })
    with pytest.raises(HTTPException) as erreur:
        search_api.preview_document("doc-1", user="bob.user")
    assert erreur.value.status_code == 404
