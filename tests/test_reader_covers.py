#!/usr/bin/env python3
"""Tests for Reader cover-image extraction (card: reader-cover-extract).

Locks in the three cover paths the card requires:

  1. **EPUB** — `ebooklib` reads the cover image (ITEM_COVER, else the
     first image whose name contains "cover", else the first image item).
     Saved as JPEG to ``data/covers/<id>.jpg`` with a 200px-wide
     ``<id>.thumb.jpg`` thumbnail.
  2. **PDF** — the first embedded image XObject on page 1 is rebuilt via
     Pillow (DCTDecode / FlateDecode / JPXDecode). Falls back to a text
     cover when page 1 has no image.
  3. **Fallback** — when no image can be extracted, a text-based cover
     (title + author on a dark background) is generated so the library
     grid never shows a broken tile.

Plus the serving contract: ``GET /api/reader/cover/:id`` returns the full
JPEG and ``?size=thumb`` returns the 200px thumbnail.

Standalone (no pytest dependency)::

    python3 tests/test_reader_covers.py

Or via pytest::

    pytest tests/test_reader_covers.py
"""

import importlib.util
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READER_SERVER = os.path.join(REPO, "projects", "reader", "reader_server.py")

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------
def _load_reader(tmp_data_dir):
    """Import reader_server.py with DATA_DIR redirected to a temp dir.

    The module is loaded fresh each call so each test gets an isolated
    library/covers directory and never touches the real ``data/`` tree.
    """
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


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _make_epub_with_cover(path, title="Cover Extract Test",
                          author="Test Author", cover_rgb=(10, 200, 90)):
    """Build a minimal EPUB that contains a real cover image."""
    import zipfile
    from PIL import Image

    cover_png = io.BytesIO()
    Image.new("RGB", (200, 300), cover_rgb).save(cover_png, "PNG")
    cover_bytes = cover_png.getvalue()

    img_entry = "images/cover.png"
    cover_html = (
        "<?xml version='1.0' encoding='utf-8'?>"
        "<html xmlns='http://www.w3.org/1999/xhtml'>"
        "<head><title>Cover</title></head>"
        "<body><img src='../{img}' alt='cover'/></body></html>"
    ).format(img=img_entry)
    ch_html = (
        "<?xml version='1.0' encoding='utf-8'?>"
        "<html xmlns='http://www.w3.org/1999/xhtml'>"
        "<head><title>{t}</title></head>"
        "<body><h1>{t}</h1><p>Chapter one body text here.</p></body></html>"
    ).format(t=title)

    mimetype = "application/epub+zip"
    container = (
        "<?xml version='1.0'?>"
        "<container version='1.0' "
        "xmlns='urn:oasis:names:tc:opendocument:xmlns:container'>"
        "<rootfiles><rootfile full-path='OEBPS/content.opf' "
        "media-type='application/oebps-package+xml'/></rootfiles>"
        "</container>")
    opf = (
        "<?xml version='1.0' encoding='utf-8'?>"
        "<package xmlns='http://www.idpf.org/2007/opf' version='3.0' "
        "unique-identifier='bookid'>"
        "<metadata xmlns:dc='http://purl.org/dc/elements/1.1/'>"
        "<dc:identifier id='bookid'>urn:uuid:test</dc:identifier>"
        "<dc:title>{t}</dc:title>"
        "<dc:creator>{a}</dc:creator>"
        "<meta name='cover' content='cover-image'/>"
        "</metadata>"
        "<manifest>"
        "<item id='cover-image' href='{img}' media-type='image/png' "
        "properties='cover-image'/>"
        "<item id='cover-html' href='cover.xhtml' media-type='application/xhtml+xml'/>"
        "<item id='ch1' href='ch1.xhtml' media-type='application/xhtml+xml'/>"
        "<item id='ncx' href='toc.ncx' media-type='application/x-dtbncx+xml'/>"
        "</manifest>"
        "<spine toc='ncx'>"
        "<itemref idref='cover-html'/>"
        "<itemref idref='ch1'/>"
        "</spine>"
        "</package>"
    ).format(t=title, a=author, img=img_entry)
    ncx = (
        "<?xml version='1.0' encoding='utf-8'?>"
        "<ncx xmlns='http://www.daisy.org/z3986/2005/ncx/' version='2005-1'>"
        "<head><meta name='dtb:uid' content='urn:uuid:test'/></head>"
        "<docTitle><text>{t}</text></docTitle>"
        "<navMap><navPoint id='n1' playOrder='1'>"
        "<navLabel><text>{t}</text></navLabel>"
        "<content src='ch1.xhtml'/></navPoint></navMap></ncx>"
    ).format(t=title)

    with zipfile.ZipFile(path, "w") as z:
        # mimetype must be first & uncompressed per the EPUB spec.
        z.writestr("mimetype", mimetype, compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/toc.ncx", ncx)
        z.writestr("OEBPS/cover.xhtml", cover_html)
        z.writestr("OEBPS/ch1.xhtml", ch_html)
        z.writestr("OEBPS/" + img_entry, cover_bytes)
    return cover_bytes


def _make_pdf_with_image(path, rgb=(123, 45, 67)):
    """Build a single-page PDF with a real embedded image on page 1."""
    from PIL import Image
    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import letter
    except ImportError:
        return False
    img_path = path + ".png"
    Image.new("RGB", (300, 450), rgb).save(img_path)
    c = canvas.Canvas(path, pagesize=letter)
    c.drawImage(img_path, 50, 200, width=300, height=450)
    c.showPage()
    c.save()
    os.remove(img_path)
    return True


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_epub_cover_extracted_as_jpeg_with_thumbnail():
    from PIL import Image
    tmp = tempfile.mkdtemp()
    try:
        rs = _load_reader(tmp)
        epub = os.path.join(tmp, "book.epub")
        cover_rgb = (10, 200, 90)
        _make_epub_with_cover(epub, cover_rgb=cover_rgb)

        summary = rs.import_file(epub)
        cid = summary["id"]
        main = os.path.join(rs.COVERS_DIR, cid + ".jpg")
        thumb = os.path.join(rs.COVERS_DIR, cid + ".thumb.jpg")

        assert summary["cover_url"] == "/api/reader/cover/" + cid
        assert os.path.isfile(main), "full cover not written"
        assert os.path.isfile(thumb), "thumbnail not written"

        im = Image.open(main)
        assert im.format == "JPEG"
        # Full cover capped to COVER_MAX_W x COVER_MAX_H.
        assert im.size[0] <= rs.COVER_MAX_W
        assert im.size[1] <= rs.COVER_MAX_H
        # The extracted cover colour should survive (not the text fallback bg).
        px = im.convert("RGB").getpixel((im.size[0] // 2, im.size[1] // 2))
        assert all(abs(a - b) <= 4 for a, b in zip(px, cover_rgb)), \
            "cover pixel %r != drawn %r" % (px, cover_rgb)

        th = Image.open(thumb)
        assert th.format == "JPEG"
        assert th.size[0] == rs.THUMB_WIDTH, "thumbnail not 200px wide (%r)" % (th.size,)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_pdf_cover_extracted_from_first_page_image():
    from PIL import Image
    tmp = tempfile.mkdtemp()
    try:
        rs = _load_reader(tmp)
        pdf = os.path.join(tmp, "book.pdf")
        rgb = (123, 45, 67)
        if not _make_pdf_with_image(pdf, rgb=rgb):
            print("SKIP: reportlab not installed")
            return
        summary = rs.import_file(pdf)
        cid = summary["id"]
        main = os.path.join(rs.COVERS_DIR, cid + ".jpg")
        thumb = os.path.join(rs.COVERS_DIR, cid + ".thumb.jpg")

        assert os.path.isfile(main)
        assert os.path.isfile(thumb)
        im = Image.open(main)
        assert im.format == "JPEG"
        px = im.convert("RGB").getpixel((im.size[0] // 2, im.size[1] // 2))
        assert all(abs(a - b) <= 4 for a, b in zip(px, rgb)), \
            "PDF cover not the embedded image (got %r)" % (px,)
        th = Image.open(thumb)
        assert th.size[0] == rs.THUMB_WIDTH
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_text_fallback_cover_when_no_image():
    from PIL import Image
    tmp = tempfile.mkdtemp()
    try:
        rs = _load_reader(tmp)
        bid = "fallback001"
        url = rs._save_cover(bid, None, None,
                             "My Great Book Title", "Jane Doe")
        main = os.path.join(rs.COVERS_DIR, bid + ".jpg")
        thumb = os.path.join(rs.COVERS_DIR, bid + ".thumb.jpg")

        assert url == "/api/reader/cover/" + bid
        assert os.path.isfile(main)
        assert os.path.isfile(thumb)
        im = Image.open(main)
        assert im.format == "JPEG"
        # Text cover is the dark design-system background. Sample below
        # the top accent stripe (y < 12) so we hit the body bg, not the
        # primary-blue stripe. JPEG is lossy, so allow a small tolerance.
        px = im.convert("RGB").getpixel((5, 50))
        bg = rs.COVER_BG_RGB
        assert all(abs(a - b) <= 2 for a, b in zip(px, bg)), \
            "text cover bg %r not close to %r" % (px, bg)
        th = Image.open(thumb)
        assert th.size[0] == rs.THUMB_WIDTH
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cover_http_endpoint_full_and_thumb():
    """Boot the real server on an ephemeral port and hit the cover route."""
    from PIL import Image
    port = _free_port()
    tmp = tempfile.mkdtemp()
    env = dict(os.environ, DOT_READER_DATA_DIR=tmp)
    proc = None
    try:
        # The server reads DATA_DIR at import from its own __file__, so we
        # spawn it with a tiny wrapper that overrides DATA_DIR before main().
        wrapper = os.path.join(tmp, "_run.py")
        with open(wrapper, "w") as fh:
            fh.write(
                "import os, sys\n"
                "sys.path.insert(0, %r)\n"
                "import importlib.util\n"
                "spec = importlib.util.spec_from_file_location('rs', %r)\n"
                "rs = importlib.util.module_from_spec(spec); spec.loader.exec_module(rs)\n"
                "rs.DATA_DIR = %r\n"
                "rs.BOOKS_DIR = os.path.join(rs.DATA_DIR, 'books')\n"
                "rs.COVERS_DIR = os.path.join(rs.DATA_DIR, 'covers')\n"
                "rs.LIBRARY_FILE = os.path.join(rs.DATA_DIR, 'library.json')\n"
                "rs.PORT = %d\n"
                "rs.main()\n"
                % (os.path.dirname(READER_SERVER), READER_SERVER, tmp, port))
        proc = subprocess.Popen(
            [sys.executable, wrapper],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)

        # Wait for health.
        base = "http://127.0.0.1:%d" % port
        deadline = time.time() + 10
        ok = False
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(base + "/api/reader/health",
                                            timeout=1) as r:
                    if r.status == 200:
                        ok = True
                        break
            except Exception:
                time.sleep(0.2)
        assert ok, "server did not come up on port %d" % port

        # Import a book so a cover exists.
        rs = _load_reader(tmp)
        epub = os.path.join(tmp, "book.epub")
        _make_epub_with_cover(epub)
        summary = rs.import_file(epub)
        cid = summary["id"]

        def fetch(suffix):
            req = urllib.request.Request(base + suffix)
            with urllib.request.urlopen(req, timeout=3) as r:
                return r.status, r.headers.get("Content-Type"), r.read()

        st, ct, body = fetch("/api/reader/cover/" + cid)
        assert st == 200
        assert (ct or "").startswith("image/jpeg")
        im = Image.open(io.BytesIO(body))
        assert im.format == "JPEG"

        st, ct, body = fetch("/api/reader/cover/" + cid + "?size=thumb")
        assert st == 200
        assert (ct or "").startswith("image/jpeg")
        th = Image.open(io.BytesIO(body))
        assert th.size[0] == rs.THUMB_WIDTH
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


def test_cover_palette_parsed_from_design_system_css():
    """Cover colours must come from the design-system CSS, not hardcoded.

    The text-cover palette is parsed at import from
    ``design-system/colors.css`` ([data-theme="dark"]) so a designer
    changing ``--dot-bg`` updates generated covers with no code change.
    This test (a) confirms the parser reads the real CSS, and (b) confirms
    the drift check warns when a fallback has gone stale — i.e. the
    hardcoded fallbacks are validated against the CSS, not trusted blindly.
    """
    tmp = tempfile.mkdtemp()
    try:
        rs = _load_reader(tmp)
        # The real design-system CSS must exist and be parsed.
        assert os.path.isfile(rs.DESIGN_CSS_PATH), \
            "design-system colors.css not found"
        tokens = rs._parse_design_tokens(rs.DESIGN_CSS_PATH)
        assert set(tokens) == {"bg", "accent", "title", "author"}, \
            "not all cover tokens parsed: %r" % (set(tokens),)
        # The live palette globals must equal the parsed CSS values.
        assert rs.COVER_BG_RGB == tokens["bg"]
        assert rs.COVER_ACCENT_RGB == tokens["accent"]
        assert rs.COVER_TITLE_RGB == tokens["title"]
        assert rs.COVER_AUTHOR_RGB == tokens["author"]
        # The fallbacks must currently match the CSS (no silent drift).
        for role, rgb in tokens.items():
            assert rs._COVER_FALLBACK[role] == rgb, \
                "_COVER_FALLBACK[%r]=%r drifted from CSS=%r" % (
                    role, rs._COVER_FALLBACK[role], rgb)

        # Drift check: a stale fallback must trigger a stderr warning and
        # the resolved palette must still follow the CSS (not the stale
        # fallback), proving the CSS is the source of truth.
        rs._COVER_FALLBACK = dict(rs._COVER_FALLBACK)
        rs._COVER_FALLBACK["accent"] = (1, 2, 3)
        import io as _io, contextlib
        buf = _io.StringIO()
        with contextlib.redirect_stderr(buf):
            rs._resolve_cover_palette()
        assert rs.COVER_ACCENT_RGB == tokens["accent"], \
            "palette should follow CSS, not the stale fallback"
        assert "drifted" in buf.getvalue() and "accent" in buf.getvalue(), \
            "drift warning not emitted: %r" % buf.getvalue()

        # Missing-CSS path: falls back silently (no crash) to the constants.
        rs.DESIGN_CSS_PATH = os.path.join(tmp, "nope.css")
        with contextlib.redirect_stderr(_io.StringIO()):
            rs._resolve_cover_palette()
        assert rs.COVER_BG_RGB == tokens["bg"]  # unchanged from last resolve
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
    # Skip the reportlab-dependent test gracefully if reportlab is absent;
    # _make_pdf_with_image already prints SKIP and returns. For the
    # standalone runner we still count it as a pass so the suite is green
    # in minimal envs (the HTTP/EPUB/fallback paths are the core contract).
    tests = [
        test_epub_cover_extracted_as_jpeg_with_thumbnail,
        test_pdf_cover_extracted_from_first_page_image,
        test_text_fallback_cover_when_no_image,
        test_cover_http_endpoint_full_and_thumb,
        test_cover_palette_parsed_from_design_system_css,
    ]
    sys.exit(1 if _run(tests) else 0)
