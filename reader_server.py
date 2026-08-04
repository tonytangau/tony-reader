#!/usr/bin/env python3
"""Dot Reader — EPUB/PDF import + reading API.

A single-file HTTP server (stdlib only at the transport layer) that lets the
Reader UI import books, list a library, and read pages of extracted text.

Runs on **:8081** (the main Dot server lives on :8080) so it can be developed
and restarted independently.

Routes
------
  POST /api/reader/import        body: {"path": "<abs path to .epub/.pdf>"}
                                  -> parses metadata + full text, stores JSON
  GET  /api/reader/library        -> all books (title, author, cover, pages,
                                     last position)
  GET  /api/reader/book/:id?page=N -> page N content (default page=1)
  GET  /api/reader/search?q=kw     -> full-text search across all books;
                                     returns matching books + page numbers +
                                     context snippets
  GET  /api/reader/cover/:id       -> raw cover image bytes (?size=thumb
                                     for the 200px library thumbnail; a
                                     text-based fallback is generated on
                                     demand if none exists)
  GET  /api/reader/health          -> {"status":"ok"}

Storage layout (all under ~/dot/data/)
------------------------------------------------------
  library.json            index of all books (id -> summary)
  books/<id>.json         full book record: metadata + pages[]
  covers/<id>.jpg           full-size cover (JPEG)
  covers/<id>.thumb.jpg    200px-wide thumbnail for the grid

Page chunking: extracted plain text is split every ~3000 characters on the
nearest paragraph/word boundary so pages read naturally.

Dependencies: stdlib + ebooklib (EPUB) + PyPDF2 (PDF). The two parser deps
are imported lazily inside the import handler so the server boots even before
they're installed (and a missing dep produces a clean 500 instead of a crash
on startup).
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import mimetypes
import os
import re
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
HOST = "127.0.0.1"
PORT = 8081

DATA_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data")
STATIC_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "reader")
BOOKS_DIR = os.path.join(DATA_DIR, "books")
COVERS_DIR = os.path.join(DATA_DIR, "covers")
LIBRARY_FILE = os.path.join(DATA_DIR, "library.json")

# Page chunking target. Pages are cut near this character count on the
# nearest paragraph/line break so a page never splits mid-sentence.
PAGE_SIZE = 3000

# Max import body size (path string) — generous cap to avoid abuse.
MAX_BODY = 1 << 20  # 1 MiB
# Max size for a raw file upload (an EPUB/PDF streamed straight from the
# browser file picker). Books are usually a few MB; allow headroom.
UPLOAD_MAX_BODY = 100 << 20  # 100 MiB

# Full-text search tuning.
SEARCH_MAX_PER_BOOK = 8      # max matching pages returned per book
SEARCH_SNIPPET = 160         # context chars around each match

# Cover image tuning.
COVER_MAX_W = 1000           # full-size cover capped to this width
COVER_MAX_H = 1500
THUMB_WIDTH = 200            # library-grid thumbnail width

# ---------------------------------------------------------------------------
# Text-cover palette (RGB tuples).
# ---------------------------------------------------------------------------
# The cover is rendered server-side via Pillow, so it can't read CSS custom
# properties directly. Instead the *real* colours are parsed at startup from
# the Dot design system DARK theme (design-system/colors.css,
# [data-theme="dark"]) by :func:`_resolve_cover_palette` — that makes the CSS
# file the single source of truth, so a designer tweaking `--dot-bg` in the
# stylesheet automatically updates generated covers with no code change.
#
# The ``_COVER_FALLBACK_*`` constants below are only used when the CSS file
# can't be read (e.g. running the module from a copied location without the
# repo). On startup we compare the parsed CSS values against these fallbacks
# and warn on stderr if they've drifted, so a stale fallback never silently
# produces off-brand covers.
# ---------------------------------------------------------------------------
DESIGN_CSS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "design-system", "colors.css")

# Which design-system token each cover colour is bound to.
_COVER_TOKEN_MAP = {
    "bg":     "--dot-bg",             # canvas behind title/author
    "accent": "--dot-primary",        # top accent stripe
    "title":  "--dot-text-primary",   # title text
    "author": "--dot-text-secondary", # author text
}

# Last-resort fallbacks (used only if the CSS file is unreadable). Kept in
# sync with design-system/colors.css [data-theme="dark"] by the drift check
# in :func:`_resolve_cover_palette`.
_COVER_FALLBACK = {
    "bg":     (0x16, 0x16, 0x17),   # #161617
    "accent": (0x4f, 0x70, 0xd2),   # #4f70d2
    "title":  (0xe9, 0xe9, 0xec),   # #e9e9ec
    "author": (0x9a, 0x9c, 0xa3),   # #9a9ca3
}

# RGBA→RGB flatten matte. Pure black is correct for dark-theme covers (the
# canvas itself is near-black), so it intentionally does not track a token.
COVER_FLATTEN_RGB = (0, 0, 0)

# Public palette — populated by _resolve_cover_palette() at import time.
# Callers use COVER_BG_RGB / COVER_ACCENT_RGB / COVER_TITLE_RGB /
# COVER_AUTHOR_RGB; these are assigned below.
COVER_BG_RGB = _COVER_FALLBACK["bg"]
COVER_ACCENT_RGB = _COVER_FALLBACK["accent"]
COVER_TITLE_RGB = _COVER_FALLBACK["title"]
COVER_AUTHOR_RGB = _COVER_FALLBACK["author"]


def _parse_design_tokens(css_path: str) -> dict[str, tuple[int, int, int]]:
    """Parse ``--dot-*`` colour custom properties from the DARK theme block.

    Scans ``design-system/colors.css`` for the ``[data-theme="dark"]`` rule
    and extracts hex colour values for the tokens in
    :data:`_COVER_TOKEN_MAP`. Returns ``{role: (r, g, b)}`` for every token
    found; missing/unparseable tokens are simply absent from the result so
    the caller can fall back per-token.
    """
    try:
        with open(css_path, "r", encoding="utf-8") as fh:
            css = fh.read()
    except OSError:
        return {}
    # Isolate the dark-theme rule body. The file declares it as
    # `:root[data-theme="dark"],\n[data-theme="dark"] { ... }`.
    m = re.search(r'\[data-theme\s*=\s*"dark"\s*\]\s*\{([^}]*)\}',
                  css, re.DOTALL)
    if not m:
        return {}
    body = m.group(1)
    out: dict[str, tuple[int, int, int]] = {}
    for role, token in _COVER_TOKEN_MAP.items():
        # Match `--dot-bg: #161617;` (tolerate surrounding whitespace and
        # trailing comments). Only solid 3/6-digit hex is accepted; rgba()
        # and other formats are skipped (none of the bound tokens use them).
        vm = re.search(re.escape(token) + r'\s*:\s*(#[0-9a-fA-F]{3,6})',
                       body)
        if not vm:
            continue
        hexval = vm.group(1).lstrip("#")
        if len(hexval) == 3:
            hexval = "".join(c * 2 for c in hexval)
        try:
            out[role] = (int(hexval[0:2], 16),
                         int(hexval[2:4], 16),
                         int(hexval[4:6], 16))
        except ValueError:
            continue
    return out


def _resolve_cover_palette() -> None:
    """Populate the COVER_*_RGB globals from the design-system CSS.

    Reads :data:`DESIGN_CSS_PATH`, parses the dark-theme tokens, and assigns
    them to the module-level :data:`COVER_BG_RGB` / :data:`COVER_ACCENT_RGB` /
    :data:`COVER_TITLE_RGB` / :data:`COVER_AUTHOR_RGB`. When the CSS is
    unreadable or a token is missing, the corresponding
    :data:`_COVER_FALLBACK` value is kept. A stderr warning is emitted for
    any token whose parsed value disagrees with its fallback, so a stale
    fallback is surfaced immediately rather than silently producing
    off-brand covers.
    """
    global COVER_BG_RGB, COVER_ACCENT_RGB, COVER_TITLE_RGB, COVER_AUTHOR_RGB
    parsed = _parse_design_tokens(DESIGN_CSS_PATH)
    if not parsed:
        if not os.path.isfile(DESIGN_CSS_PATH):
            sys.stderr.write(
                "[reader] warning: design-system CSS not found at %s; "
                "using fallback cover palette\n" % DESIGN_CSS_PATH)
        return
    attr_by_role = {
        "bg": "COVER_BG_RGB",
        "accent": "COVER_ACCENT_RGB",
        "title": "COVER_TITLE_RGB",
        "author": "COVER_AUTHOR_RGB",
    }
    for role, rgb in parsed.items():
        fb = _COVER_FALLBACK.get(role)
        if fb is not None and rgb != fb:
            sys.stderr.write(
                "[reader] warning: cover palette token %s drifted from "
                "fallback (css=%s fallback=%s) — update _COVER_FALLBACK\n"
                % (role, rgb, fb))
    COVER_BG_RGB = parsed.get("bg", _COVER_FALLBACK["bg"])
    COVER_ACCENT_RGB = parsed.get("accent", _COVER_FALLBACK["accent"])
    COVER_TITLE_RGB = parsed.get("title", _COVER_FALLBACK["title"])
    COVER_AUTHOR_RGB = parsed.get("author", _COVER_FALLBACK["author"])


# Bind the palette from the design system at import time so the constants
# used by _make_text_cover_image() always reflect the current CSS.
_resolve_cover_palette()


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------
def ensure_dirs() -> None:
    for d in (DATA_DIR, BOOKS_DIR, COVERS_DIR):
        os.makedirs(d, exist_ok=True)


def load_library() -> dict:
    """Return the library index as {book_id: summary_dict}."""
    if not os.path.isfile(LIBRARY_FILE):
        return {}
    try:
        with open(LIBRARY_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}


def save_library(lib: dict) -> None:
    ensure_dirs()
    tmp = LIBRARY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(lib, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, LIBRARY_FILE)


def book_path(book_id: str) -> str:
    return os.path.join(BOOKS_DIR, book_id + ".json")


def load_book(book_id: str) -> dict | None:
    fpath = book_path(book_id)
    if not os.path.isfile(fpath):
        return None
    with open(fpath, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_book(book: dict) -> None:
    ensure_dirs()
    fpath = book_path(book["id"])
    tmp = fpath + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(book, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, fpath)


def cover_path(book_id: str) -> str | None:
    """Find an existing full-size cover file for a book (any extension).

    Skips ``<id>.thumb.<ext>`` thumbnail files so the main cover is
    returned, not its tiny sibling. Prefers ``.jpg``/``.jpeg`` (the format
    :func:`_save_cover` writes) so a legacy ``.png`` placeholder never
    shadows a real JPEG cover.
    """
    if not os.path.isdir(COVERS_DIR):
        return None
    matches: list[str] = []
    for name in os.listdir(COVERS_DIR):
        base, ext = os.path.splitext(name)
        if base == book_id:
            matches.append(os.path.join(COVERS_DIR, name))
    if not matches:
        return None
    # Prefer JPEG (the canonical cover format written by _save_cover).
    for fpath in matches:
        if os.path.splitext(fpath)[1].lower() in (".jpg", ".jpeg"):
            return fpath
    return matches[0]


def cover_thumb_path(book_id: str) -> str | None:
    """Find an existing 200px thumbnail for a book."""
    if not os.path.isdir(COVERS_DIR):
        return None
    thumb = os.path.join(COVERS_DIR, book_id + ".thumb.jpg")
    return thumb if os.path.isfile(thumb) else None


def _remove_cover_files(book_id: str) -> None:
    """Delete any existing cover + thumbnail for *book_id* (re-import)."""
    if not os.path.isdir(COVERS_DIR):
        return
    for name in os.listdir(COVERS_DIR):
        base, _ = os.path.splitext(name)
        if base == book_id or base == book_id + ".thumb":
            try:
                os.remove(os.path.join(COVERS_DIR, name))
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Cover image processing (Pillow)
# ---------------------------------------------------------------------------
def _load_font(size: int):
    """Load a TTF font for text-cover generation, falling back to the
    PIL default bitmap font if no system TTF is available."""
    from PIL import ImageFont  # noqa: WPS433
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ):
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _wrap_text(draw, text, font, max_width: int, max_lines: int = 4) -> list[str]:
    """Greedy word-wrap of *text* for *font* within *max_width* pixels."""
    words = str(text).split()
    lines: list[str] = []
    cur = ""
    for w in words:
        trial = (cur + " " + w).strip()
        bbox = draw.textbbox((0, 0), trial, font=font)
        if (bbox[2] - bbox[0]) <= max_width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
            if len(lines) >= max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) >= max_lines:
        # Truncate last line with an ellipsis if we ran out of room.
        last = lines[-1]
        while last and draw.textbbox((0, 0), last + "…", font=font)[2] > max_width:
            last = last[:-1]
        lines[-1] = (last + "…") if last else "…"
    return lines


def _make_text_cover_image(title: str, author: str):
    """Render a simple text-based cover: title + author on a dark bg.

    Used as the fallback when no image can be extracted, so every book in
    the library grid has a visible cover tile.
    """
    from PIL import Image, ImageDraw  # noqa: WPS433
    W, H = 600, 900
    img = Image.new("RGB", (W, H), COVER_BG_RGB)
    draw = ImageDraw.Draw(img)
    # Top accent stripe for a touch of visual identity (primary blue).
    draw.rectangle([0, 0, W, 12], fill=COVER_ACCENT_RGB)
    title_font = _load_font(72)
    author_font = _load_font(36)
    wrapped = _wrap_text(draw, title or "Untitled", title_font, W - 80)
    line_h = 84
    y = H // 2 - (len(wrapped) * line_h) // 2
    for line in wrapped:
        bbox = draw.textbbox((0, 0), line, font=title_font)
        draw.text(((W - (bbox[2] - bbox[0])) // 2, y), line,
                  font=title_font, fill=COVER_TITLE_RGB)
        y += line_h
    # Author below the title block.
    author = author or "Unknown"
    bbox = draw.textbbox((0, 0), author, font=author_font)
    draw.text(((W - (bbox[2] - bbox[0])) // 2, y + 40), author,
              font=author_font, fill=COVER_AUTHOR_RGB)
    return img


def _save_cover(book_id: str, raw: bytes | None, mime: str | None,
                title: str = "", author: str = "") -> str:
    """Persist a cover image for *book_id*.

    Normalises *raw* image bytes (from EPUB/PDF extraction) to JPEG via
    Pillow, writes a full-size cover to ``covers/<id>.jpg`` (capped to
    ``COVER_MAX_W``×``COVER_MAX_H``) and a 200px-wide thumbnail to
    ``covers/<id>.thumb.jpg``. If *raw* is missing or can't be decoded,
    a text-based fallback cover (title + author on a dark background) is
    generated instead so every book has a cover.

    Always returns the cover URL — every book gets a cover.
    """
    import io  # noqa: WPS433
    from PIL import Image  # noqa: WPS433

    ensure_dirs()
    _remove_cover_files(book_id)

    img = None
    if raw:
        try:
            img = Image.open(io.BytesIO(raw))
            img.load()
        except Exception:
            img = None
    if img is None:
        img = _make_text_cover_image(title or "Untitled",
                                     author or "Unknown")

    # Flatten transparency onto black, then force RGB for JPEG.
    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", img.size, COVER_FLATTEN_RGB)
        bg.paste(img, mask=img.split()[-1])
        img = bg
    elif img.mode != "RGB":
        try:
            img = img.convert("RGB")
        except Exception:
            img = _make_text_cover_image(title or "Untitled",
                                         author or "Unknown")

    main = img.copy()
    main.thumbnail((COVER_MAX_W, COVER_MAX_H))
    main.save(os.path.join(COVERS_DIR, book_id + ".jpg"),
              "JPEG", quality=85)

    thumb = img.copy()
    thumb.thumbnail((THUMB_WIDTH, THUMB_WIDTH * 3))
    thumb.save(os.path.join(COVERS_DIR, book_id + ".thumb.jpg"),
               "JPEG", quality=80)

    return "/api/reader/cover/" + book_id


def backfill_covers(force: bool = False) -> int:
    """Ensure every book in the library has a proper JPEG cover.

    Books imported before cover generation landed either have no cover
    file at all (``cover_url`` is null) or a legacy placeholder (e.g. a
    tiny 1×1 ``.png`` from old test fixtures). Both leave the library
    grid looking broken, so this walks the library index and regenerates
    a real cover for any book missing a ``<id>.jpg``.

    When the original source file (``source_path``) still exists, the
    cover is re-extracted from it so we get the real artwork rather than
    the text fallback. Otherwise the text fallback (title + author on a
    dark background) is generated from the stored metadata.

    Returns the number of covers (re)generated. Idempotent: books that
    already have a ``<id>.jpg`` are skipped unless *force* is True. Safe
    to run on every startup.
    """
    lib = load_library()
    if not lib:
        return 0
    regenerated = 0
    dirty = False
    for book_id, summary in lib.items():
        jpg_path = os.path.join(COVERS_DIR, book_id + ".jpg")
        if not force and os.path.isfile(jpg_path):
            # Already has a proper JPEG cover; just make sure the URL is set.
            if not summary.get("cover_url"):
                summary["cover_url"] = "/api/reader/cover/" + book_id
                dirty = True
            continue
        book = load_book(book_id)
        if book is None:
            continue
        cover_bytes = None
        cover_mime = None
        source_path = book.get("source_path")
        if source_path and os.path.isfile(source_path):
            try:
                ext = os.path.splitext(source_path)[1].lower()
                if ext == ".epub":
                    parsed = parse_epub(source_path)
                    cover_bytes = parsed.get("cover_bytes")
                    cover_mime = parsed.get("cover_mime")
                elif ext == ".pdf":
                    parsed = parse_pdf(source_path)
                    cover_bytes = parsed.get("cover_bytes")
                    cover_mime = parsed.get("cover_mime")
            except Exception:
                # Source unreadable / deps missing — fall back to text cover.
                cover_bytes = None
        try:
            url = _save_cover(book_id, cover_bytes, cover_mime,
                              book.get("title") or "Untitled",
                              book.get("author") or "Unknown")
        except Exception:
            continue
        summary["cover_url"] = url
        # Keep the full book record in sync too.
        book["cover_url"] = url
        save_book(book)
        dirty = True
        regenerated += 1
    if dirty:
        save_library(lib)
    return regenerated


# ---------------------------------------------------------------------------
# Text chunking — every ~PAGE_SIZE chars, snapped to a paragraph/word break
# ---------------------------------------------------------------------------
def chunk_pages_with_offsets(text: str, size: int = PAGE_SIZE):
    """Split *text* into page-sized chunks, tracking each chunk's byte
    range in the original (newline-normalised) text.

    Returns a list of ``(page_text, start_offset, end_offset)`` tuples.
    Cuts on paragraph boundaries first, then line breaks, then word breaks,
    so each page is a natural reading unit that stays close to *size* chars.
    Pages may be slightly over *size* when a single paragraph is longer than
    the target (rare for normal prose).

    The offsets refer to positions in the normalised *text* (after CRLF → LF)
    so callers can map an arbitrary character offset (e.g. the start of a
    TOC target document) onto a page index.
    """
    if not text:
        return []
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    pages: list[tuple[str, int, int]] = []
    i = 0
    n = len(text)
    while i < n:
        # Take a window starting at PAGE_SIZE past i, then look backwards
        # for the best split point within the last ~500 chars.
        end = min(i + size, n)
        if end >= n:
            chunk = text[i:n].strip()
            if chunk:
                pages.append((chunk, i, n))
            break
        window = text[max(i, end - 500):end]
        # Prefer paragraph break, then newline, then space.
        cut = -1
        sep_len = 1
        for pat, slen in (("\n\n", 2), ("\n", 1), ("  ", 1), (" ", 1)):
            idx = window.rfind(pat)
            if idx != -1:
                cut = idx
                sep_len = slen
                break
        if cut == -1:
            split_at = end
        else:
            split_at = max(i, end - 500) + cut + sep_len
        chunk = text[i:split_at].strip()
        if chunk:
            pages.append((chunk, i, split_at))
        i = split_at if split_at > i else end
    return pages


def chunk_pages(text: str, size: int = PAGE_SIZE) -> list[str]:
    """Split *text* into page-sized chunks (text only).

    Thin wrapper over :func:`chunk_pages_with_offsets` so the chunking
    algorithm has a single source of truth.
    """
    return [p[0] for p in chunk_pages_with_offsets(text, size)]


# ---------------------------------------------------------------------------
# EPUB parsing (ebooklib)
# ---------------------------------------------------------------------------
def parse_epub(path: str) -> dict:
    import ebooklib  # noqa: WPS433 (lazy import)
    from ebooklib import epub  # noqa: WPS433

    book = epub.read_epub(path, options={"ignore_ncx": True})

    title = (book.get_metadata("DC", "title") or [("Unknown",)])[0][0]
    author_meta = book.get_metadata("DC", "creator") or []
    authors = ", ".join(m[0] for m in author_meta) or "Unknown"

    # Cover image: look for a cover image item by name/property, else the
    # first image item.
    cover_bytes: bytes | None = None
    cover_mime: str | None = None
    cover_item = None
    for item in book.get_items_of_type(ebooklib.ITEM_COVER):
        cover_item = item
        break
    if cover_item is None:
        for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
            name = (item.get_name() or "").lower()
            if "cover" in name:
                cover_item = item
                break
    if cover_item is None:
        images = list(book.get_items_of_type(ebooklib.ITEM_IMAGE))
        if images:
            cover_item = images[0]
    if cover_item is not None:
        cover_bytes = cover_item.get_content()
        cover_mime = cover_item.media_type

    # Extract text from spine documents in reading order. We walk the
    # spine (not just all ITEM_DOCUMENT items) so chapter order matches
    # the author's intended flow, and we remember each document's name
    # (href) + start offset in the joined text so EPUB TOC entries can be
    # mapped onto a page number later.
    from ebooklib.epub import EpubHtml  # noqa: WPS433
    import lxml.html  # noqa: WPS433

    spine_items: list = []
    seen_ids: set[str] = set()
    for idref, _linear in (getattr(book, "spine", None) or []):
        item = book.get_item_with_id(idref)
        if item is None or idref in seen_ids:
            continue
        seen_ids.add(idref)
        spine_items.append(item)
    if not spine_items:
        # Fallback: spine missing — use document items in declared order.
        spine_items = list(book.get_items_of_type(ebooklib.ITEM_DOCUMENT))

    doc_texts: list[tuple[str, str]] = []  # (href, normalised_text)
    for item in spine_items:
        try:
            raw = item.get_content().decode("utf-8", errors="replace")
        except Exception:
            doc_texts.append((item.get_name() or "", ""))
            continue
        try:
            tree = lxml.html.fromstring(raw)
        except Exception:
            # Strip tags crudely if the parser chokes.
            raw2 = re.sub(r"<[^>]+>", " ", raw)
            doc_texts.append((item.get_name() or "",
                              _normalize_whitespace(raw2)))
            continue
        # Remove script/style before extracting text.
        for bad in tree.iter("script", "style"):
            bad.drop_tree()
        txt = tree.text_content()
        doc_texts.append((item.get_name() or "",
                          _normalize_whitespace(txt)))

    # Join with paragraph breaks; track each non-empty doc's start offset
    # so TOC hrefs can be resolved to a character offset.
    parts: list[str] = []
    doc_offsets: list[tuple[str, int]] = []
    offset = 0
    for href, txt in doc_texts:
        if not txt or not txt.strip():
            continue
        if parts:
            offset += 2  # length of "\n\n" separator
        doc_offsets.append((href, offset))
        parts.append(txt)
        offset += len(txt)
    full_text = "\n\n".join(parts).strip()

    # Flatten the EPUB TOC (book.toc) into a simple list of
    # {title, href, depth}. ebooklib mixes Link and Section objects and
    # nests Sections' children as sub-lists, so we recurse.
    toc_raw = _flatten_epub_toc(getattr(book, "toc", None) or [])

    return {
        "title": str(title).strip() or "Unknown",
        "author": str(authors).strip() or "Unknown",
        "text": full_text,
        "cover_bytes": cover_bytes,
        "cover_mime": cover_mime,
        "doc_offsets": doc_offsets,
        "toc_raw": toc_raw,
    }


def _flatten_epub_toc(toc) -> list[dict]:
    """Flatten ebooklib's TOC tree into ``[{title, href, depth}]``.

    ebooklib represents the TOC as a list whose entries are either
    :class:`Link` (leaf) or :class:`Section` (which carries its own
    ``children`` list). Some EPUBs also embed bare sub-lists. We recurse
    through all three shapes so the result is a flat, depth-tagged list
    that's trivial to render in a sidebar.
    """
    from ebooklib.epub import Link, Section  # noqa: WPS433

    out: list[dict] = []

    def visit(it, depth: int) -> None:
        """Handle a single TOC node (Link, Section, or nested list)."""
        if isinstance(it, Section):
            title = (getattr(it, "title", None) or "").strip()
            href = (getattr(it, "href", None) or "").strip()
            if title:
                out.append({"title": title, "href": href, "depth": depth})
            # `children` may itself be a single Link/Section rather than a
            # list (ebooklib is inconsistent), so `visit` each child.
            kids = getattr(it, "children", None)
            if isinstance(kids, (list, tuple)):
                for k in kids:
                    visit(k, depth + 1)
            elif kids is not None:
                visit(kids, depth + 1)
        elif isinstance(it, Link):
            title = (getattr(it, "title", None)
                     or getattr(it, "href", None) or "").strip()
            href = (getattr(it, "href", None) or "").strip()
            if title:
                out.append({"title": title, "href": href, "depth": depth})
        elif isinstance(it, (list, tuple)):
            for k in it:
                visit(k, depth)

    # `toc` is normally a list, but some EPUBs expose a bare Link/Section
    # at the root — normalise to a list so we never try to iterate a Link.
    if isinstance(toc, (list, tuple)):
        for it in toc:
            visit(it, 0)
    elif toc is not None:
        visit(toc, 0)
    return out


def _normalize_whitespace(s: str) -> str:
    """Collapse runs of whitespace into single spaces but keep paragraph
    breaks (double newlines)."""
    # Protect paragraph breaks first.
    s = s.replace("\r", "\n")
    s = re.sub(r"\n[ \t]+", "\n", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# ---------------------------------------------------------------------------
# PDF parsing (PyPDF2)
# ---------------------------------------------------------------------------
def parse_pdf(path: str) -> dict:
    from PyPDF2 import PdfReader  # noqa: WPS433

    reader = PdfReader(path)
    try:
        info = reader.metadata or {}
        title = (getattr(info, "title", None) or "").strip() or "Unknown"
        author = (getattr(info, "author", None) or "").strip() or "Unknown"
    except Exception:
        title, author = "Unknown", "Unknown"

    parts: list[str] = []
    for page in reader.pages:
        try:
            txt = page.extract_text() or ""
        except Exception:
            txt = ""
        parts.append(_normalize_whitespace(txt))

    full_text = "\n\n".join(p for p in parts if p.strip()).strip()

    # Cover = first page rendered as an image is heavy; PyPDF2 can't render.
    # Instead, try to extract an embedded cover image from page 1's /XObject
    # images. If that fails, the UI shows a generated placeholder.
    cover_bytes: bytes | None = None
    cover_mime: str | None = None
    try:
        cover_bytes, cover_mime = _extract_pdf_first_image(reader)
    except Exception:
        pass

    return {
        "title": title,
        "author": author,
        "text": full_text,
        "cover_bytes": cover_bytes,
        "cover_mime": cover_mime,
        "doc_offsets": [],
        "toc_raw": [],
    }


def _extract_pdf_first_image(reader) -> tuple[bytes | None, str | None]:
    """Best-effort extraction of the first image XObject from page 1.

    Returns ``(jpeg_bytes, "image/jpeg")`` or ``(None, None)``. PyPDF2 can't
    render a page, but it *can* pull embedded image XObjects out of page 1's
    resources. Pillow then turns the raw image stream (JPEG, FlateDecode'd
    pixels, JPXDecode) into clean JPEG bytes — FlateDecode streams are raw
    pixel data, not a real image format, so Pillow is required to rebuild
    them from the XObject's width/height/colorspace.
    """
    import io  # noqa: WPS433
    from PIL import Image  # noqa: WPS433

    if not reader.pages:
        return None, None
    page = reader.pages[0]
    resources = page.get("/Resources")
    if resources is None:
        return None, None
    xobj = resources.get("/XObject")
    if xobj is None:
        return None, None
    xobj = xobj.get_object()
    for key in xobj:
        try:
            o = xobj[key].get_object()
        except Exception:
            continue
        if o.get("/Subtype") != "/Image":
            continue
        img = _pdf_xobject_to_pil(o)
        if img is None:
            continue
        buf = io.BytesIO()
        try:
            img.convert("RGB").save(buf, "JPEG", quality=85)
        except Exception:
            continue
        return buf.getvalue(), "image/jpeg"
    return None, None


def _pdf_xobject_to_pil(o):
    """Reconstruct a PIL Image from a PDF image XObject.

    Handles the common decoders (DCTDecode = JPEG, FlateDecode = raw pixels,
    JPXDecode = JPEG2000). For FlateDecode the pixel buffer is rebuilt from
    the XObject's Width/Height/BitsPerComponent/ColorSpace.
    """
    import io  # noqa: WPS433
    from PIL import Image  # noqa: WPS433

    filt = o.get("/Filter")
    filts = filt if isinstance(filt, list) else ([filt] if filt else [])
    filt_names = [str(f) for f in filts]
    data = o.get_data() or b""
    if not data:
        return None
    width = int(o.get("/Width") or 0)
    height = int(o.get("/Height") or 0)
    bpc = int(o.get("/BitsPerComponent") or 8)
    cs_raw = o.get("/ColorSpace")
    cs = str(cs_raw) if cs_raw is not None else ""

    # DCTDecode / JPXDecode are already-encoded image streams Pillow can
    # open directly (get_data() would strip the encoding, so use raw stream).
    if any("DCTDecode" in f for f in filt_names) or \
            any("JPXDecode" in f for f in filt_names):
        raw_stream = o.get_raw_data() if hasattr(o, "get_raw_data") else data
        for payload in (raw_stream, data):
            try:
                return Image.open(io.BytesIO(payload))
            except Exception:
                continue
        return None

    # FlateDecode / ASCII85Decode / etc.: get_data() fully decodes to raw
    # pixels — rebuild the image from width/height/colorspace.
    if width and height:
        if "Gray" in cs or cs == "/G":
            mode = "L"
        elif "CMYK" in cs:
            mode = "CMYK"
        elif "RGB" in cs:
            mode = "RGB"
        else:
            # Infer from buffer length.
            if len(data) >= width * height * 4:
                mode = "CMYK"
            elif len(data) >= width * height * 3:
                mode = "RGB"
            elif len(data) >= width * height:
                mode = "L"
            else:
                return None
        for trial_mode in (mode, "RGB", "L"):
            try:
                return Image.frombytes(trial_mode, (width, height), data)
            except Exception:
                continue
    return None


# ---------------------------------------------------------------------------
# Import orchestration
# ---------------------------------------------------------------------------
def import_file(path: str) -> dict:
    """Parse a book file, persist it, and return its summary record."""
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        raise FileNotFoundError("file not found: " + path)

    ext = os.path.splitext(path)[1].lower()
    if ext == ".epub":
        parsed = parse_epub(path)
    elif ext == ".pdf":
        parsed = parse_pdf(path)
    else:
        raise ValueError("unsupported file type: " + ext)

    pages_with_offsets = chunk_pages_with_offsets(parsed["text"], PAGE_SIZE)
    pages = [p[0] for p in pages_with_offsets]
    page_starts = [p[1] for p in pages_with_offsets]
    book_id = uuid.uuid4().hex[:12]

    # Resolve EPUB TOC entries (which reference source document hrefs)
    # onto 1-indexed page numbers in the chunked text. Each TOC href is
    # matched to a spine document's start offset, then we find the page
    # whose start offset is the largest not exceeding it.
    toc = _resolve_toc(parsed.get("toc_raw") or [],
                       parsed.get("doc_offsets") or [],
                       page_starts)

    # Persist cover image (extracted or generated fallback). Every book
    # gets a cover so the library grid never shows a broken tile: a
    # 200px-wide thumbnail is written alongside the full cover.
    cover_url = _save_cover(book_id, parsed.get("cover_bytes"),
                            parsed.get("cover_mime"),
                            parsed["title"], parsed["author"])

    now = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    book = {
        "id": book_id,
        "title": parsed["title"],
        "author": parsed["author"],
        "source_path": path,
        "source_format": ext.lstrip("."),
        "cover_url": cover_url,
        "page_count": len(pages),
        "char_count": len(parsed["text"]),
        "last_page": 1,            # 1-indexed reading position
        "last_position": 0.0,      # fraction read, 0.0–1.0
        "toc": toc,                # [{title, page, depth}] (EPUB only)
        "imported_at": now,
        "updated_at": now,
        "pages": pages,
    }
    save_book(book)

    # Update library index (summary without the full page text).
    lib = load_library()
    lib[book_id] = _summary(book)
    save_library(lib)
    return _summary(book)


def _summary(book: dict) -> dict:
    """Library-list projection of a full book record (no page text)."""
    return {
        "id": book["id"],
        "title": book["title"],
        "author": book["author"],
        "cover_url": book.get("cover_url"),
        "page_count": book["page_count"],
        "last_page": book.get("last_page", 1),
        "last_position": book.get("last_position", 0.0),
        "imported_at": book.get("imported_at"),
        "updated_at": book.get("updated_at"),
        "source_format": book.get("source_format"),
        "has_toc": bool(book.get("toc")),
    }


def _resolve_toc(toc_raw: list[dict],
                 doc_offsets: list[tuple[str, int]],
                 page_starts: list[int]) -> list[dict]:
    """Map EPUB TOC entries onto 1-indexed page numbers.

    ``toc_raw`` is the flattened ``[{title, href, depth}]`` list from
    :func:`_flatten_epub_toc`. ``doc_offsets`` is ``[(href, start_offset)]``
    for each spine document. Each TOC href (with any ``#fragment`` stripped)
    is matched against the spine hrefs; the matched document's start offset
    is then mapped to the page whose start offset is the largest not
    exceeding it. Unresolvable entries default to page 1.
    """
    if not toc_raw:
        return []
    # Build a lookup from a normalised href base to its start offset.
    # EPUB hrefs are sometimes relative, sometimes absolute paths — accept
    # both exact and suffix matches.
    offset_by_href: dict[str, int] = {}
    for href, off in doc_offsets:
        if not href:
            continue
        base = href.split("#", 1)[0]
        offset_by_href[base] = off
        # Also index by the trailing path segment for fuzzy matches.
        offset_by_href.setdefault(os.path.basename(base), off)

    resolved: list[dict] = []
    for entry in toc_raw:
        href = (entry.get("href") or "").split("#", 1)[0]
        target = None
        if href:
            target = offset_by_href.get(href)
            if target is None:
                target = offset_by_href.get(os.path.basename(href))
        page = 1
        if target is not None and page_starts:
            # Find the last page whose start offset <= target offset.
            for idx, start in enumerate(page_starts):
                if start <= target:
                    page = idx + 1
                else:
                    break
        resolved.append({
            "title": entry.get("title") or "(untitled)",
            "page": page,
            "depth": entry.get("depth", 0),
        })
    return resolved


# ---------------------------------------------------------------------------
# MIME types for static file serving
# ---------------------------------------------------------------------------
_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
    ".js":   "application/javascript; charset=utf-8",
    ".json": "application/json",
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg":  "image/svg+xml",
    ".ico":  "image/x-icon",
    ".woff2":"font/woff2",
    ".ttf":  "font/ttf",
}

# HTTP handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "DotReader/0.1"

    # -- plumbing ----------------------------------------------------------
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data: bytes, mime: str, status=200):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status, message):
        self._send_json({"error": message, "status": status}, status=status)

    def log_message(self, fmt, *args):
        sys.stderr.write("[reader] %s - %s\n" % (self.address_string(),
                                                 fmt % args))

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    # -- body reader -------------------------------------------------------
    def _read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}, None
        if length > MAX_BODY:
            return None, "request body too large"
        raw = self.rfile.read(length)
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return None, "invalid JSON: " + str(e)
        if not isinstance(obj, dict):
            return None, "JSON body must be an object"
        return obj, None

    # -- static file helper -------------------------------------------------
    def _serve_file(self, filepath: str) -> None:
        """Serve a static file with appropriate MIME type and caching."""
        if not os.path.isfile(filepath):
            return self._error(404, "not found: " + filepath)
        ext = os.path.splitext(filepath)[1].lower()
        mime = _MIME.get(ext, "application/octet-stream")
        try:
            with open(filepath, "rb") as fh:
                data = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            return self._error(500, "could not read: " + filepath)

    # -- routing -----------------------------------------------------------
    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if path == "/api/reader/import":
                return self._import()
            if path == "/api/reader/progress":
                return self._save_progress()
            return self._error(404, "unknown route: " + path)
        except FileNotFoundError as e:
            return self._error(404, str(e))
        except ValueError as e:
            return self._error(400, str(e))
        except ImportError as e:
            return self._error(500, "missing dependency: " + str(e))
        except Exception as e:  # pragma: no cover
            return self._error(500, "server error: " + str(e))

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)
        try:
            if path == "/api/reader/health":
                return self._send_json({"status": "ok"})
            if path == "/api/reader/library":
                return self._library()
            if path == "/api/reader/search":
                return self._search(qs)
            if path.startswith("/api/reader/book/"):
                return self._book(path, qs)
            if path.startswith("/api/reader/cover/"):
                return self._cover(path, qs)
            # Serve static files (frontend UI)
            if path == "/":
                path = "/index.html"
            static_path = os.path.join(STATIC_DIR, path.lstrip("/"))
            if os.path.isfile(static_path):
                return self._serve_file(static_path)
            return self._error(404, "unknown route: " + path)
        except FileNotFoundError as e:
            return self._error(404, str(e))
        except Exception as e:  # pragma: no cover
            return self._error(500, "server error: " + str(e))

    # -- route handlers ----------------------------------------------------
    def _import(self):
        ctype = (self.headers.get("Content-Type") or "").lower()
        # Two import modes:
        #   1) JSON {"path": "<abs path>"}  — local-file import (CLI/agent).
        #   2) Raw file upload — the browser file picker streams the actual
        #      EPUB/PDF bytes (browsers can't expose real paths). The
        #      filename travels in the X-Filename header or ?filename=.
        if ctype.startswith("application/json"):
            return self._import_by_path()
        return self._import_upload()

    def _import_by_path(self):
        body, err = self._read_json_body()
        if err:
            return self._error(400, err)
        path = (body.get("path") or "").strip()
        if not path:
            return self._error(400, "missing 'path' field")
        summary = import_file(path)
        return self._send_json(summary, status=201)

    def _import_upload(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return self._error(400, "empty upload body")
        if length > UPLOAD_MAX_BODY:
            return self._error(413, "upload too large (max %d bytes)"
                               % UPLOAD_MAX_BODY)
        raw = self.rfile.read(length)

        # Filename may arrive via header or query string.
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        filename = ((self.headers.get("X-Filename") or "")
                    or (qs.get("filename", [""])[0] or "")).strip()
        if not filename:
            return self._error(400, "missing filename (send X-Filename header "
                               "or ?filename=)")
        # Guard against path traversal in the supplied filename.
        filename = os.path.basename(filename)
        ext = os.path.splitext(filename)[1].lower()
        if ext not in (".epub", ".pdf"):
            return self._error(400, "unsupported file type: " + ext)

        # Spool to a temp file under DATA_DIR, import, then clean up.
        # Use a per-request unique name (pid alone collides under
        # ThreadingHTTPServer when two uploads of the same extension land
        # at once); uuid guarantees no collision on the temp path.
        ensure_dirs()
        tmp_name = "_upload_%s_%d%s" % (uuid.uuid4().hex, os.getpid(), ext)
        tmp_path = os.path.join(DATA_DIR, tmp_name)
        try:
            with open(tmp_path, "wb") as fh:
                fh.write(raw)
            summary = import_file(tmp_path)
        finally:
            try:
                if os.path.isfile(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
        return self._send_json(summary, status=201)

    @staticmethod
    def _persist_progress(book: dict) -> None:
        """Save ``book`` and update the library index entry for its id."""
        save_book(book)
        lib = load_library()
        book_id = book["id"]
        if book_id in lib:
            lib[book_id] = _summary(book)
            save_library(lib)

    def _save_progress(self):
        """Persist reading position for a book.

        Body: {"bookId": "<id>", "page": <int>}. Updates last_page,
        last_position and updated_at on both the full book record and the
        library index. Idempotent and safe to call on every page turn.
        """
        body, err = self._read_json_body()
        if err:
            return self._error(400, err)
        book_id = (body.get("bookId") or body.get("book_id") or "").strip()
        if not book_id:
            return self._error(400, "missing 'bookId' field")
        try:
            page = int(body.get("page"))
        except (TypeError, ValueError):
            return self._error(400, "'page' must be an integer")
        book = load_book(book_id)
        if book is None:
            return self._error(404, "book not found: " + book_id)
        page_count = book.get("page_count", 0) or len(book.get("pages", []))
        if page < 1:
            page = 1
        if page_count and page > page_count:
            page = page_count
        book["last_page"] = page
        book["last_position"] = round(page / max(page_count, 1), 4)
        book["updated_at"] = (datetime.datetime.utcnow()
                              .isoformat(timespec="seconds") + "Z")
        self._persist_progress(book)
        return self._send_json({
            "bookId": book_id,
            "page": page,
            "page_count": page_count,
            "last_position": book["last_position"],
            "ok": True,
        })

    def _library(self):
        lib = load_library()
        # Return as a list, newest-first by imported_at.
        items = list(lib.values())
        items.sort(key=lambda b: b.get("imported_at") or "",
                   reverse=True)
        return self._send_json({"books": items, "count": len(items)})

    def _book(self, path, qs):
        # path looks like /api/reader/book/<id>
        book_id = path[len("/api/reader/book/"):]
        if not book_id:
            return self._error(400, "missing book id")
        book = load_book(book_id)
        if book is None:
            return self._error(404, "book not found: " + book_id)

        try:
            page = int((qs.get("page", ["1"])[0]))
        except ValueError:
            return self._error(400, "page must be an integer")
        if page < 1:
            page = 1
        pages = book.get("pages", [])
        page_count = len(pages)
        if page_count == 0:
            content = ""
        elif page > page_count:
            content = ""
        else:
            content = pages[page - 1]

        # Persist reading position when a valid page is requested.
        if 1 <= page <= page_count:
            book["last_page"] = page
            book["last_position"] = round(page / max(page_count, 1), 4)
            book["updated_at"] = (datetime.datetime.utcnow()
                                  .isoformat(timespec="seconds") + "Z")
            self._persist_progress(book)

        return self._send_json({
            "id": book["id"],
            "title": book["title"],
            "author": book["author"],
            "cover_url": book.get("cover_url"),
            "page": page,
            "page_count": page_count,
            "content": content,
            "last_position": book.get("last_position", 0.0),
            "toc": book.get("toc") or [],
        })

    def _cover(self, path, qs):
        """Serve a book's cover image.

        ``GET /api/reader/cover/<id>`` serves the full cover; append
        ``?size=thumb`` for the 200px-wide library-grid thumbnail. If no
        cover file exists yet (e.g. a book imported before cover
        generation landed), one is generated on demand from the book's
        title/author and cached — so this endpoint always succeeds for a
        valid book id.
        """
        book_id = path[len("/api/reader/cover/"):]
        if not book_id:
            return self._error(400, "missing book id")
        want_thumb = ((qs.get("size", [""])[0] or "").lower() == "thumb")
        fpath = (cover_thumb_path(book_id) if want_thumb
                 else cover_path(book_id))
        if fpath is None:
            # No cover file (book imported before cover generation, or
            # the cover file was deleted). Generate a fallback text cover
            # from the book's metadata on demand and cache it to disk.
            book = load_book(book_id)
            if book is None:
                return self._error(404, "no cover for book " + book_id)
            try:
                _save_cover(book_id, None, None,
                            book.get("title") or "Untitled",
                            book.get("author") or "Unknown")
            except Exception as e:  # pragma: no cover
                return self._error(500, "failed to generate cover: " + str(e))
            fpath = (cover_thumb_path(book_id) if want_thumb
                     else cover_path(book_id))
            if fpath is None:
                return self._error(404, "no cover for book " + book_id)
        with open(fpath, "rb") as fh:
            data = fh.read()
        # Covers are always written as JPEG by _save_cover; fall back to a
        # mime guess for any legacy files.
        mime = mimetypes.guess_type(fpath)[0] or "image/jpeg"
        return self._send_bytes(data, mime)

    def _search(self, qs):
        """Full-text search across every book in the library.

        ``GET /api/reader/search?q=<query>`` returns, for each matching
        book, the book summary plus a list of ``{page, snippet}`` matches
        (up to ``SEARCH_MAX_PER_BOOK`` pages). Books whose title or author
        also match the query are returned even with no content matches and
        are sorted first. Snippets are ~``SEARCH_SNIPPET`` chars centred on
        the first hit on a page.
        """
        raw_q = (qs.get("q", [""])[0] or "").strip()
        if not raw_q:
            return self._send_json(
                {"query": "", "results": [], "count": 0})
        q = raw_q.lower()
        lib = load_library()
        results = []
        for book_id, _summary_rec in lib.items():
            book = load_book(book_id)
            if book is None:
                continue
            title = book.get("title") or ""
            author = book.get("author") or ""
            title_match = q in title.lower()
            author_match = q in author.lower()
            pages = book.get("pages") or []
            matches = []
            for i, page_text in enumerate(pages):
                # Case-insensitive substring search; record the first hit
                # per page with a context snippet around it.
                idx = page_text.lower().find(q)
                if idx == -1:
                    continue
                half = SEARCH_SNIPPET // 2
                start = max(0, idx - half)
                end = min(len(page_text), idx + len(q) + half)
                snippet = page_text[start:end]
                if start > 0:
                    snippet = "…" + snippet
                if end < len(page_text):
                    snippet = snippet + "…"
                # Collapse newlines in the snippet for a tidy one-line preview.
                snippet = re.sub(r"\s+", " ", snippet).strip()
                matches.append({"page": i + 1, "snippet": snippet})
                if len(matches) >= SEARCH_MAX_PER_BOOK:
                    break
            if not (title_match or author_match or matches):
                continue
            results.append({
                "id": book_id,
                "title": title,
                "author": author,
                "cover_url": book.get("cover_url"),
                "page_count": book.get("page_count", 0),
                "last_page": book.get("last_page", 1),
                "source_format": book.get("source_format"),
                "title_match": title_match,
                "author_match": author_match,
                "match_count": len(matches),
                "matches": matches,
            })
        # Books with a title/author hit float to the top; within each tier,
        # more content matches rank higher.
        results.sort(
            key=lambda r: (not (r["title_match"] or r["author_match"]),
                           -r["match_count"]))
        return self._send_json({
            "query": raw_q,
            "results": results,
            "count": len(results),
        })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    ensure_dirs()
    # Self-heal: regenerate covers for any book imported before cover
    # generation landed (or whose cover file was deleted). Idempotent and
    # cheap — skips books that already have a proper <id>.jpg.
    try:
        n = backfill_covers()
        if n:
            sys.stderr.write("[reader] backfilled %d cover(s)\n" % n)
    except Exception as e:  # pragma: no cover
        sys.stderr.write("[reader] cover backfill failed: %s\n" % e)
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    sys.stderr.write("[reader] serving on http://%s:%d/  (data: %s)\n"
                     % (HOST, PORT, DATA_DIR))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\n[reader] shutting down\n")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
