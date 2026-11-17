"""Медиа-индекс профиля: сбор, извлечение, лексический поиск, приватность.

Тест изолирован от живого профиля: каталоги данных и настройки файлового
индекса подменяются, векторы не строятся (Gemma/Qdrant здесь не нужны).
Проверяем то, что легко сломать при интеграции:

1. документы (PDF), картинки, аудио и видео попадают в индекс своими типами;
2. текст PDF извлекается и находится обычным поиском Search All;
3. карточка медиа-файла открывается через get_article_content;
4. файл вне выбранных папок не читается и не отдаётся;
5. при выключенном типе его записи не попадают в выдачу.
"""
import os
import sys
import time
import types
import wave

import pytest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

import module_flags
from modules import file_index as fi
from modules import media_index as mi


@pytest.fixture()
def profile(tmp_path, monkeypatch):
    root = tmp_path / "profile"
    folder = tmp_path / "library"
    folder.mkdir(parents=True)

    monkeypatch.setattr(mi, "_profile_data", lambda pid: str(root))
    monkeypatch.setattr(fi, "_dir", lambda pid: str(root / "file_index"))
    monkeypatch.setattr(fi, "get_settings", lambda pid: {
        "enabled": True, "vector": False, "folders": [str(folder)]})
    monkeypatch.setattr(mi, "_index_vectors", lambda pid, items, st: None)
    monkeypatch.setattr(mi, "_delete_kind", lambda pid, kind: False)
    monkeypatch.setattr(module_flags, "enabled_for",
                        lambda module, profile_id="": module in ("media_index", "file_index"))
    return root, folder


def _make_pdf(path):
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Dog runs on the beach. Quick brown fox.")
    doc.save(str(path))
    doc.close()


def _make_png(path):
    pytest.importorskip("PIL.Image")
    from PIL import Image
    Image.new("RGB", (64, 48), (30, 120, 200)).save(str(path))


def _make_wav(path):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 800)


def _build(pid="media_test"):
    # update_settings мог уже запустить пересборку — второй запуск не нужен.
    if not mi._state(pid).get("running"):
        mi.start_reindex(pid)
    for _ in range(300):
        if not mi._state(pid).get("running"):
            break
        time.sleep(0.05)
    return mi.get_items(pid)


def test_index_extracts_and_searches(profile):
    root, folder = profile
    _make_pdf(folder / "собака_на_пляже.pdf")
    _make_png(folder / "собака_фото.png")
    _make_wav(folder / "собака_голос.wav")
    (folder / "собака_клип.mp4").write_bytes(b"not a real video")

    mi.update_settings("media_test", {
        "documents": True, "images": True, "audio": True, "video": True,
        "ocr": False, "sync": False})
    items = _build()

    kinds = {it["kind"] for it in items}
    assert kinds == {"document", "image", "audio", "video"}
    doc = next(it for it in items if it["kind"] == "document")
    assert "dog runs" in doc["text"]

    import search_all_sources as sas
    found = sas.search_media("media_test", "собака")
    types = {r["type"] for r in found}
    assert "document" in types
    assert all(r["score"] > 0 for r in found)
    doc_res = next(r for r in found if r["type"] == "document")
    assert doc_res["url"].startswith("/api/media-index/file?path=")

    # Совпадение в тексте, а не в имени файла: запрос по содержимому PDF.
    by_text = sas.search_media("media_test", "brown fox")
    assert any(r["type"] == "document" for r in by_text)

    text = sas.get_article_content(doc_res["url"], "document", "media_test")
    assert "Dog runs" in text


def test_outside_folder_is_not_readable(profile, tmp_path):
    root, folder = profile
    _make_pdf(folder / "внутри.pdf")
    outside = tmp_path / "outside.pdf"
    _make_pdf(outside)
    mi.update_settings("media_test", {"documents": True, "sync": False})
    _build()

    assert mi.read_indexed("media_test", str(outside)) == ""
    assert mi.is_indexed("media_test", str(outside)) is False
    inside = str(folder / "внутри.pdf")
    assert "Dog runs" in mi.read_indexed("media_test", inside)
    assert mi.is_indexed("media_test", inside) is True


def test_disabled_kind_disappears(profile):
    root, folder = profile
    _make_pdf(folder / "документ.pdf")
    _make_png(folder / "картинка.png")
    mi.update_settings("media_test", {"documents": True, "images": True,
                                      "ocr": False, "sync": False})
    assert {it["kind"] for it in _build()} == {"document", "image"}

    mi.update_settings("media_test", {"images": False})
    items = _build()
    assert {it["kind"] for it in items} == {"document"}


def test_semantic_searches_image_collection(profile, monkeypatch):
    """Настройка картинок называется "images", а тип — "image": векторная
    выдача обязана запрашивать коллекцию изображений, не теряя её по ключу."""
    mi.update_settings("media_test", {"documents": False, "images": True,
                                      "audio": False, "video": False, "sync": False})
    queried = []

    class _Point:
        payload = {"kind": "image", "item_id": "x.png", "path": "x.png",
                   "title": "x"}
        score = 0.5

    class _Client:
        def query_points(self, collection_name, query, limit):
            queried.append(collection_name)
            return types.SimpleNamespace(points=[_Point()])

    fake_qm = types.SimpleNamespace(
        get_client=lambda: _Client(),
        ensure_collection=lambda name, dim=None: True)
    monkeypatch.setattr(mi, "search_ready", lambda pid: True)
    monkeypatch.setattr(mi, "_qm", lambda: fake_qm)
    from modules import gemma_client
    monkeypatch.setattr(gemma_client, "available", lambda: True)
    monkeypatch.setattr(gemma_client, "ensure_worker", lambda: True)
    monkeypatch.setattr(gemma_client, "embed_text",
                        lambda q, prompt="SearchQuery": [0.1] * 768)

    out = mi.search_semantic("media_test", "собака", 5)
    assert queried == [mi.collection("media_test", "image")]
    assert out and out[0]["type"] == "image"


def test_worker_batches_images_one_vector_each(monkeypatch):
    """Один dict {"image": [...]} модель считает одним сэмплом; батч обязан
    вернуть по вектору на каждый файл, иначе пачка из 8 даёт 1 вектор."""
    import numpy as np
    import gemma_worker.worker as worker

    class _Model:
        def __init__(self):
            self.calls = []

        def encode(self, data, **kwargs):
            self.calls.append(data)
            return np.ones((len(data), 768), dtype="float32")

    fake = _Model()
    monkeypatch.setattr(worker, "_load", lambda: fake)
    out = worker._encode_dict("image", ["a.png", "b.png", "c.png"], 8)
    assert len(out) == 3
    assert isinstance(fake.calls[0], list)
    assert all(isinstance(item, dict) and "image" in item
               for item in fake.calls[0])


def test_flag_off_hides_media(profile, monkeypatch):
    root, folder = profile
    _make_pdf(folder / "документ.pdf")
    mi.update_settings("media_test", {"documents": True, "sync": False})
    _build()

    monkeypatch.setattr(module_flags, "enabled_for",
                        lambda module, profile_id="": False)
    import search_all_sources as sas
    assert sas.search_media("media_test", "собака") == []
    assert sas.get_article_content(
        "/api/media-index/file?path=" + str(folder / "документ.pdf").replace("\\", "/"),
        "document", "media_test") == ""


FB2 = """<?xml version="1.0" encoding="utf-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">
  <description><title-info><book-title>Собака</book-title></title-info></description>
  <body><section><p>Dog runs on the beach. Quick brown fox.</p></section></body>
</FictionBook>
"""


def test_zst_logical_extension():
    from modules import media_extract as mx
    assert mx.logical_ext("a/b.docx.zst") == ".docx"
    assert mx.logical_ext("a/B.PDF.ZST") == ".pdf"
    assert mx.kind_for("a/b.pdf.zst") == "document"
    assert mx.kind_for("a/b.png.zst") == "image"


def test_compressed_document_is_indexed(profile):
    """Компрессия файлового менеджера оставляет .zst: тип и текст должны читаться."""
    root, folder = profile
    import compression_module as cz
    src = folder / "собака.fb2"
    src.write_text(FB2, encoding="utf-8")
    packed = cz.compress_bytes(src.read_bytes())
    src.unlink()
    zst = folder / "собака.fb2.zst"
    zst.write_bytes(packed)

    mi.update_settings("media_test", {"documents": True, "sync": False})
    items = _build()

    doc = next(it for it in items if it["kind"] == "document")
    assert "dog runs" in doc["text"]
    assert doc["title"] == "собака"
    assert doc["ext"] == ".fb2"
    assert mi.is_indexed("media_test", str(zst)) is True
    assert "Quick brown fox" in mi.read_indexed("media_test", str(zst))

    import search_all_sources as sas
    found = sas.search_media("media_test", "собака")
    assert any(r["type"] == "document" for r in found)
