#!/usr/bin/env python3
"""Tests for the Reader full-text search endpoint
(card: reader-polish — requirement #2).

Locks in the ``GET /api/reader/search?q=<query>`` contract:

  * Returns matching **books** with their summary fields.
  * Each result carries a ``matches`` list of ``{page, snippet}`` for
    pages whose extracted text contains the query.
  * Books whose **title** or **author** match the query are returned
    even with zero content matches, and sort first.
  * Snippets are context windows centred on the first hit per page,
    collapsed to a single line, and ellipsised when truncated.
  * An empty/whitespace query yields an empty result set (no 500).

Standalone (no pytest dependency)::

    python3 tests/test_reader_search.py

Or via pytest::

    pytest tests/test_reader_search.py
"""

import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import zipfile
from urllib.parse import parse_qs, urlencode

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The reader server lives at the repo root (reader_server.py). Older
# tests referenced a stale "projects/reader/reader_server.py" path from
# before the repo was reorganised; resolve the real file at import time
# so the suite keeps working regardless of layout.
_candidates = [
    os.path.join(REPO, "reader_server.py"),
    os.path.join(REPO, "projects", "reader", "reader_server.py"),
]
READER_SERVER = next((p for p in _candidates if os.path.isfile(p)), _candidates[0])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load_reader(tmp_data_dir):
    """Import reader_server.py with DATA_DIR redirected to a temp dir."""
    spec = importlib.util.spec_from_file_location("reader_server_under_test",
                                                  READER_SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.DATA_DIR = tmp_data_dir
    mod.BOOKS_DIR = os.path.join(tmp_data_dir, "books")
    mod.COVERS_DIR = os.path.join(tmp_data_dir, "covers")
    mod.LIBRARY_FILE = os.path.join(tmp_data_dir, "library.json")
    mod.ensure_dirs()
    return mod


def _make_epub(path, title, author, body):
    """Build a minimal valid EPUB with one chapter of *body* text."""
    from PIL import Image
    img = io.BytesIO()
    Image.new("RGB", (50, 80), (1, 2, 3)).save(img, "PNG")
    cover_bytes = img.getvalue()
    ch = (
        "<?xml version='1.0' encoding='utf-8'?>"
        "<html xmlns='http://www.w3.org/1999/xhtml'>"
        "<head><title>{t}</title></head>"
        "<body><h1>{t}</h1><p>{b}</p></body></html>"
    ).format(t=title, b=body)
    opf = (
        "<?xml version='1.0' encoding='utf-8'?>"
        "<package xmlns='http://www.idpf.org/2007/opf' version='3.0' "
        "unique-identifier='b'>"
        "<metadata xmlns:dc='http://purl.org/dc/elements/1.1/'>"
        "<dc:identifier id='b'>x</dc:identifier>"
        "<dc:title>{t}</dc:title><dc:creator>{a}</dc:creator></metadata>"
        "<manifest><item id='c' href='cover.png' media-type='image/png' "
        "properties='cover-image'/>"
        "<item id='h' href='ch.xhtml' media-type='application/xhtml+xml'/>"
        "</manifest><spine><itemref idref='h'/></spine></package>"
    ).format(t=title, a=author)
    container = (
        "<?xml version='1.0'?><container xmlns='urn:oasis:names:tc:opendoc"
        "ument:xmlns:container'><rootfiles><rootfile full-path='OEBPS/"
        "content.opf' media-type='application/oebps-package+xml'/>"
        "</rootfiles></container>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip",
                   compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/ch.xhtml", ch)
        z.writestr("OEBPS/cover.png", cover_bytes)


def _run_search(rs, query):
    """Invoke Handler._search directly and return the JSON payload."""
    h = rs.Handler.__new__(rs.Handler)
    captured = {}

    def fake_send(payload, status=200):
        captured["payload"] = payload
        captured["status"] = status

    h._send_json = fake_send
    qs = parse_qs(urlencode({"q": query}))
    h._search(qs)
    return captured["payload"]


def _import_two_books(rs, tmp):
    e1 = os.path.join(tmp, "lore.epub")
    _make_epub(e1, "Dragon Lore", "Ada Lovelace",
               "The dragon soared over the misty mountains at dawn.")
    e2 = os.path.join(tmp, "cooking.epub")
    _make_epub(e2, "Cooking Basics", "Bob Chef",
               "Whisk the eggs gently. The dragon fruit is optional.")
    rs.import_file(e1)
    rs.import_file(e2)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_content_match_returns_page_and_snippet():
    tmp = tempfile.mkdtemp()
    try:
        rs = _load_reader(tmp)
        _import_two_books(rs, tmp)
        data = _run_search(rs, "dragon")
        assert data["query"] == "dragon"
        assert data["count"] == 2
        titles = [r["title"] for r in data["results"]]
        assert "Dragon Lore" in titles
        assert "Cooking Basics" in titles
        for r in data["results"]:
            assert r["match_count"] >= 1
            m = r["matches"][0]
            assert isinstance(m["page"], int) and m["page"] >= 1
            assert "dragon" in m["snippet"].lower(), \
                "snippet must contain the query term"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_title_match_floats_to_top_even_without_content_hit():
    tmp = tempfile.mkdtemp()
    try:
        rs = _load_reader(tmp)
        _import_two_books(rs, tmp)
        # "Lore" appears in a title but not in body text.
        data = _run_search(rs, "lore")
        top = data["results"][0]
        assert top["title"] == "Dragon Lore"
        assert top["title_match"] is True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_author_match_returned_without_content_hit():
    tmp = tempfile.mkdtemp()
    try:
        rs = _load_reader(tmp)
        _import_two_books(rs, tmp)
        data = _run_search(rs, "lovelace")
        titles = [r["title"] for r in data["results"]]
        assert "Dragon Lore" in titles
        r = next(r for r in data["results"] if r["title"] == "Dragon Lore")
        assert r["author_match"] is True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_match_returns_empty_results():
    tmp = tempfile.mkdtemp()
    try:
        rs = _load_reader(tmp)
        _import_two_books(rs, tmp)
        data = _run_search(rs, "zzznotfound")
        assert data["count"] == 0
        assert data["results"] == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_empty_query_returns_empty_results_not_error():
    tmp = tempfile.mkdtemp()
    try:
        rs = _load_reader(tmp)
        _import_two_books(rs, tmp)
        for q in ("", "   "):
            data = _run_search(rs, q)
            assert data["count"] == 0
            assert data["results"] == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_snippet_is_single_line_and_ellipsised_when_truncated():
    tmp = tempfile.mkdtemp()
    try:
        rs = _load_reader(tmp)
        # A body long enough that the snippet window truncates on both sides.
        body = ("word " * 400) + " needle " + ("word " * 400)
        e = os.path.join(tmp, "long.epub")
        _make_epub(e, "Long Book", "Author", body)
        rs.import_file(e)
        data = _run_search(rs, "needle")
        assert data["count"] == 1
        m = data["results"][0]["matches"][0]
        assert "needle" in m["snippet"]
        assert "\n" not in m["snippet"], "snippet must be single-line"
        # Truncated on at least one side → starts or ends with an ellipsis.
        assert m["snippet"].startswith("…") or m["snippet"].endswith("…"), \
            "truncated snippet should be ellipsised"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------
def _run(tests):
    failures = 0
    for fn in tests:
        name = fn.__name__
        try:
            fn()
            print("  PASS  " + name)
        except Exception as e:
            failures += 1
            import traceback
            print("  FAIL  " + name + ": " + repr(e))
            traceback.print_exc()
    print("\n%d/%d passed" % (len(tests) - failures, len(tests)))
    return failures


if __name__ == "__main__":
    tests = [
        test_content_match_returns_page_and_snippet,
        test_title_match_floats_to_top_even_without_content_hit,
        test_author_match_returned_without_content_hit,
        test_no_match_returns_empty_results,
        test_empty_query_returns_empty_results_not_error,
        test_snippet_is_single_line_and_ellipsised_when_truncated,
    ]
    sys.exit(1 if _run(tests) else 0)
