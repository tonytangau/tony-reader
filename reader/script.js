/* ============================================================
   Dot Reader — Library view logic
   Pure vanilla JS. Talks to the Reader API
   (projects/reader/reader_server.py, default port 8081).

   API:
     GET  /api/reader/library              -> { books, count }
     GET  /api/reader/book/:id?page=N      -> page content (also persists pos)
                                               + toc[] for the book
     GET  /api/reader/search?q=keyword     -> full-text search across books
     GET  /api/reader/cover/:id            -> raw cover image
     POST /api/reader/import               -> JSON {path} OR raw file upload
                                               (filename via X-Filename / ?filename=)
     POST /api/reader/progress             -> { bookId, page } persist position

   Features:
     • Font family selector (serif / sans / mono), persisted in localStorage.
     • Full-text search across all books (page numbers + context snippets).
     • Desktop two-column layout: sidebar list + inline reading pane
       (>1200px); mobile single column with fullscreen reading overlay.
     • Keyboard shortcuts: / focus search, ←/→ page, f fullscreen,
       t table of contents, Esc back to library.
     • Reading stats: pages read today + total reading time, stored locally.
   ============================================================ */

(() => {
  'use strict';

  // ---- API base -------------------------------------------------------
  // The Reader API lives on its own dev server (:8081) while this page is
  // served by the main Dot server (:8080). Default to the same hostname on
  // 8081; allow an override via ?api=http://host:port (use ?api= for same
  // origin). Covers + book endpoints are prefixed with this base.
  const API_BASE = (() => {
    const ov = new URLSearchParams(location.search).get('api');
    if (ov !== null) return ov;                // ?api=  → same origin
    return location.protocol + '//' + location.hostname + ':8081';
  })();
  const api = (p) => API_BASE + p;

  // ---- DOM refs -------------------------------------------------------
  const gridEl       = document.getElementById('book-grid');
  const metaEl       = document.getElementById('library-meta');
  const emptyEl      = document.getElementById('empty-state');
  const errorBanner  = document.getElementById('error-banner');
  const searchInput  = document.getElementById('search-input');
  const importBtn    = document.getElementById('import-btn');
  const emptyImport  = document.getElementById('empty-import');
  const fileInput    = document.getElementById('import-file');
  const toastEl      = document.getElementById('import-toast');

  // Sidebar (desktop)
  const sidebarList  = document.getElementById('sidebar-list');
  const sidebarCount = document.getElementById('sidebar-count');
  const sidebarStats = document.getElementById('sidebar-stats');

  // Search results
  const searchResultsEl = document.getElementById('search-results');

  // Reading view refs
  const rvEl     = document.getElementById('reader-view');
  const rvBar    = document.getElementById('rv-bar');
  const rvNav    = document.getElementById('rv-nav');
  const rvClose  = document.getElementById('rv-close');
  const rvTitle  = document.getElementById('rv-title');
  const rvAuthor = document.getElementById('rv-author');
  const rvPos    = document.getElementById('rv-pos');
  const rvPage   = document.getElementById('rv-page');
  const rvPager  = document.getElementById('rv-pager');
  const rvPrev   = document.getElementById('rv-prev');
  const rvNext   = document.getElementById('rv-next');
  const rvFontInc   = document.getElementById('rv-font-inc');
  const rvFontDec   = document.getElementById('rv-font-dec');
  const rvFontFam   = document.getElementById('rv-font-family');
  const rvFsBtn     = document.getElementById('rv-fullscreen');
  const rvTocBtn    = document.getElementById('rv-toc-btn');
  const rvToc       = document.getElementById('rv-toc');
  const rvTocClose  = document.getElementById('rv-toc-close');
  const rvTocList   = document.getElementById('rv-toc-list');

  // ---- State ----------------------------------------------------------
  let library = [];          // all books (newest-first)
  let filterQuery = '';
  let currentBook = null;    // { id, title, author, page, page_count, toc, lastViewedPage }

  // ---- Reading preferences (persisted) -------------------------------
  const FONT_SIZE_KEY = 'dot.reader.font';
  const FONT_FAMILY_KEY = 'dot.reader.fontFamily';
  const FONT_MIN = 14, FONT_MAX = 28, FONT_STEP = 2;
  const FONT_FAMILIES = ['serif', 'sans', 'mono'];

  function loadFontSize() {
    const n = Number(localStorage.getItem(FONT_SIZE_KEY));
    return isFinite(n) && n >= FONT_MIN && n <= FONT_MAX ? n : 20;
  }
  function loadFontFamily() {
    const v = localStorage.getItem(FONT_FAMILY_KEY);
    return FONT_FAMILIES.includes(v) ? v : 'serif';
  }
  let readerFontPx = loadFontSize();
  let readerFontFamily = loadFontFamily();

  function applyFontPrefs() {
    rvPage.style.fontSize = readerFontPx + 'px';
    rvPage.setAttribute('data-font', readerFontFamily);
    rvFontFam.value = readerFontFamily;
  }
  function saveFontPrefs() {
    try {
      localStorage.setItem(FONT_SIZE_KEY, String(readerFontPx));
      localStorage.setItem(FONT_FAMILY_KEY, readerFontFamily);
    } catch (_) {}
  }
  function bumpFont(delta) {
    const next = Math.max(FONT_MIN, Math.min(FONT_MAX, readerFontPx + delta));
    if (next === readerFontPx) return;
    readerFontPx = next;
    applyFontPrefs();
    saveFontPrefs();
  }
  function setFontFamily(fam) {
    if (!FONT_FAMILIES.includes(fam)) return;
    if (fam === readerFontFamily) return;
    readerFontFamily = fam;
    applyFontPrefs();
    saveFontPrefs();
  }

  // ============================================================
  // Reading stats (localStorage) — pages read today + total time.
  // ============================================================
  const STATS_KEY = 'dot.reader.stats';
  function todayStr() {
    const d = new Date();
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0')
      + '-' + String(d.getDate()).padStart(2, '0');
  }
  function loadStats() {
    let s = null;
    try { s = JSON.parse(localStorage.getItem(STATS_KEY) || 'null'); } catch (_) {}
    if (!s || typeof s !== 'object') {
      s = { date: todayStr(), pagesToday: 0, totalSeconds: 0 };
    }
    if (s.date !== todayStr()) {
      // New day: reset daily counter, keep lifetime total.
      s.date = todayStr();
      s.pagesToday = 0;
    }
    return s;
  }
  function saveStats(s) {
    try { localStorage.setItem(STATS_KEY, JSON.stringify(s)); } catch (_) {}
  }
  let stats = loadStats();
  let statsTimer = null;

  function recordPageView(page) {
    // Count a page the first time it's viewed in this session.
    if (!currentBook) return;
    if (currentBook.lastViewedPage === page) return;
    currentBook.lastViewedPage = page;
    stats = loadStats();
    stats.pagesToday = (stats.pagesToday || 0) + 1;
    saveStats(stats);
    renderStats();
  }
  function startReadingTimer() {
    stopReadingTimer();
    stats = loadStats();
    // Tick every second; persist every 5s to avoid hammering localStorage.
    let sinceFlush = 0;
    statsTimer = setInterval(() => {
      stats.totalSeconds = (stats.totalSeconds || 0) + 1;
      sinceFlush++;
      if (sinceFlush >= 5) {
        saveStats(stats);
        sinceFlush = 0;
      }
      renderStats();
    }, 1000);
  }
  function stopReadingTimer() {
    if (statsTimer) {
      clearInterval(statsTimer);
      statsTimer = null;
      stats = loadStats();
      saveStats(stats);
      renderStats();
    }
  }
  function formatDuration(totalSeconds) {
    const s = Math.max(0, Math.floor(totalSeconds || 0));
    const m = Math.floor(s / 60);
    const h = Math.floor(m / 60);
    if (h >= 1) return h + 'h ' + (m % 60) + 'm';
    if (m >= 1) return m + 'm';
    return s + 's';
  }
  function renderStats() {
    stats = loadStats();
    const pages = stats.pagesToday || 0;
    const time = formatDuration(stats.totalSeconds || 0);
    sidebarStats.innerHTML =
      '<strong>' + pages + '</strong> page' + (pages === 1 ? '' : 's') +
      ' read today · <strong>' + time + '</strong> total';
  }

  // ============================================================
  // Utilities
  // ============================================================
  function pct(book) {
    const p = Number(book.last_position || 0);
    if (!isFinite(p)) return 0;
    return Math.max(0, Math.min(100, Math.round(p * 100)));
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function escapeRegex(s) {
    return String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }

  function highlight(text, query) {
    // Wrap each case-insensitive occurrence of `query` in <mark>. The
    // surrounding text is escaped first so user content can't inject HTML.
    const safe = escapeHtml(text);
    if (!query) return safe;
    const re = new RegExp(escapeRegex(query), 'gi');
    return safe.replace(re, (m) => '<mark>' + escapeHtml(m) + '</mark>');
  }

  // Cover image URL for the library grid. The server writes a 200px-wide
  // thumbnail alongside every full cover (covers/<id>.thumb.jpg); the grid
  // tiles are ~150–200px wide, so requesting ?size=thumb avoids shipping a
  // 1000px JPEG for every book on every library load.
  function coverSrc(book) {
    return book.cover_url ? api(book.cover_url) + '?size=thumb' : null;
  }

  function initials(title) {
    const words = String(title || '?').trim().split(/\s+/).filter(Boolean);
    if (!words.length) return '?';
    if (words.length === 1) return words[0].slice(0, 1).toUpperCase();
    return (words[0][0] + words[words.length - 1][0]).toUpperCase();
  }

  function showError(msg) {
    errorBanner.textContent = msg;
    errorBanner.hidden = false;
  }
  function clearError() {
    errorBanner.hidden = true;
    errorBanner.textContent = '';
  }

  // ---- Toast ----------------------------------------------------------
  let toastTimer = null;
  function toast(html, { spin = false, sticky = false } = {}) {
    toastEl.innerHTML = '';
    if (spin) {
      const sp = document.createElement('span');
      sp.className = 'reader-toast__spinner';
      toastEl.appendChild(sp);
    }
    const txt = document.createElement('span');
    txt.innerHTML = html;
    toastEl.appendChild(txt);
    toastEl.hidden = false;
    if (toastTimer) { clearTimeout(toastTimer); toastTimer = null; }
    if (!sticky) {
      toastTimer = setTimeout(() => { toastEl.hidden = true; }, 3200);
    }
  }
  function hideToast() {
    toastEl.hidden = true;
    if (toastTimer) { clearTimeout(toastTimer); toastTimer = null; }
  }

  // ============================================================
  // Library loading + rendering
  // ============================================================
  async function loadLibrary() {
    metaEl.textContent = 'Loading…';
    try {
      const res = await fetch(api('/api/reader/library'), { cache: 'no-store' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      library = Array.isArray(data.books) ? data.books : [];
      render();
    } catch (e) {
      library = [];
      render();
      showError('Could not reach the Reader API at ' + api('/api/reader/library') +
                '. Is reader_server.py running? (' + e.message + ')');
    }
  }

  // Local title/author filter — used for instant sidebar/grid filtering
  // when the query is too short to justify a server round-trip.
  function localFilter(books, q) {
    if (!q) return books;
    const ql = q.toLowerCase();
    return books.filter(b =>
      String(b.title || '').toLowerCase().includes(ql) ||
      String(b.author || '').toLowerCase().includes(ql));
  }

  function isSearching() {
    return filterQuery.trim().length >= 2;
  }

  function render() {
    clearError();
    const total = library.length;
    sidebarCount.textContent = total ? String(total) : '';

    if (total === 0) {
      gridEl.hidden = true;
      emptyEl.hidden = false;
      searchResultsEl.hidden = true;
      metaEl.textContent = '';
      sidebarList.innerHTML = '';
      return;
    }
    emptyEl.hidden = true;

    // Sidebar always reflects the local title/author filter so the book
    // list narrows as you type, even before full-text results arrive.
    renderSidebar(localFilter(library, filterQuery.trim()));

    if (isSearching()) {
      // Full-text search results own the main pane; the grid is hidden.
      // (The search itself is fired debounced from the input handler.)
      gridEl.hidden = true;
      metaEl.textContent = 'Searching…';
      return;
    }

    // Library mode: hide search results, show grid.
    searchResultsEl.hidden = true;
    searchResultsEl.innerHTML = '';
    gridEl.hidden = false;
    const books = localFilter(library, filterQuery.trim());
    metaEl.textContent = total + (total === 1 ? ' book' : ' books') +
      (filterQuery && books.length !== total
        ? ' · ' + books.length + ' matching' : '');

    const frag = document.createDocumentFragment();
    for (const book of books) frag.appendChild(renderCard(book));
    gridEl.innerHTML = '';
    gridEl.appendChild(frag);
  }

  function renderSidebar(books) {
    const frag = document.createDocumentFragment();
    if (!books.length) {
      const empty = document.createElement('div');
      empty.className = 'reader-sidebar__empty';
      empty.style.cssText = 'padding:var(--dot-space-4);color:var(--dot-text-tertiary);font-size:var(--dot-fs-small);';
      empty.textContent = filterQuery ? 'No matches' : 'No books yet';
      sidebarList.innerHTML = '';
      sidebarList.appendChild(empty);
      return;
    }
    for (const book of books) frag.appendChild(renderSidebarRow(book));
    sidebarList.innerHTML = '';
    sidebarList.appendChild(frag);
  }

  function renderSidebarRow(book) {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'reader-sidebar__row';
    row.dataset.id = book.id;
    if (currentBook && currentBook.id === book.id) row.classList.add('is-active');
    row.setAttribute('aria-label',
      'Open ' + (book.title || 'book') + ' by ' + (book.author || 'unknown'));

    const title = document.createElement('span');
    title.className = 'reader-sidebar__row-title';
    title.textContent = book.title || 'Untitled';

    const meta = document.createElement('span');
    meta.className = 'reader-sidebar__row-meta';
    const author = document.createElement('span');
    author.className = 'reader-sidebar__row-author';
    author.textContent = book.author || 'Unknown';
    const pc = Number(book.page_count || 0);
    const pos = document.createElement('span');
    pos.textContent = pc ? (pct(book) + '%') : '—';
    meta.appendChild(author);
    meta.appendChild(pos);

    const bar = document.createElement('div');
    bar.className = 'reader-sidebar__row-bar';
    const fill = document.createElement('div');
    fill.className = 'reader-sidebar__row-bar-fill';
    fill.style.width = pct(book) + '%';
    bar.appendChild(fill);

    row.appendChild(title);
    row.appendChild(meta);
    row.appendChild(bar);
    row.addEventListener('click', () => openReader(book));
    return row;
  }

  function renderCard(book) {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'reader-card';
    card.dataset.id = book.id;
    card.setAttribute('aria-label',
      'Open ' + (book.title || 'book') + ' by ' + (book.author || 'unknown'));

    const cover = document.createElement('div');
    cover.className = 'reader-card__cover';

    if (book.source_format) {
      const fmt = document.createElement('span');
      fmt.className = 'dot-tag dot-tag--neutral reader-card__fmt';
      fmt.textContent = book.source_format.toUpperCase();
      cover.appendChild(fmt);
    }

    const src = coverSrc(book);
    if (src) {
      const img = document.createElement('img');
      img.alt = 'Cover of ' + (book.title || 'book');
      img.loading = 'lazy';
      img.src = src;
      img.addEventListener('error', () => {
        cover.querySelector('img')?.remove();
        cover.appendChild(placeholder(book));
      });
      cover.appendChild(img);
    } else {
      cover.appendChild(placeholder(book));
    }

    const titleEl = document.createElement('div');
    titleEl.className = 'reader-card__title';
    titleEl.textContent = book.title || 'Untitled';

    const authorEl = document.createElement('div');
    authorEl.className = 'reader-card__author';
    authorEl.textContent = book.author || 'Unknown author';

    const progress = document.createElement('div');
    progress.className = 'reader-card__progress';

    const bar = document.createElement('div');
    bar.className = 'reader-card__bar';
    const fill = document.createElement('div');
    fill.className = 'reader-card__bar-fill';
    fill.style.width = pct(book) + '%';
    bar.appendChild(fill);

    const meta = document.createElement('div');
    meta.className = 'reader-card__progress-meta';
    const pc = Number(book.page_count || 0);
    const lp = Number(book.last_page || 0) || (pc ? 1 : 0);
    const left = document.createElement('span');
    left.textContent = pct(book) + '% read';
    const right = document.createElement('span');
    right.textContent = pc ? ('p. ' + lp + ' / ' + pc) : '—';
    meta.appendChild(left);
    meta.appendChild(right);

    progress.appendChild(bar);
    progress.appendChild(meta);

    card.appendChild(cover);
    card.appendChild(titleEl);
    card.appendChild(authorEl);
    card.appendChild(progress);

    card.addEventListener('click', () => openReader(book));
    return card;
  }

  function placeholder(book) {
    const ph = document.createElement('div');
    ph.className = 'reader-card__placeholder';
    const mark = document.createElement('div');
    mark.className = 'reader-card__placeholder-mark';
    mark.textContent = initials(book.title);
    const t = document.createElement('div');
    t.className = 'reader-card__placeholder-title';
    t.textContent = book.title || 'Untitled';
    ph.appendChild(mark);
    ph.appendChild(t);
    return ph;
  }

  // ============================================================
  // Full-text search (server-side, debounced)
  // ============================================================
  let searchTimer = null;
  let searchSeq = 0;

  function scheduleSearch() {
    if (searchTimer) clearTimeout(searchTimer);
    if (!isSearching()) {
      // Query too short: clear any pending search and return to library.
      searchResultsEl.hidden = true;
      searchResultsEl.innerHTML = '';
      // Re-render grid/sidebar for the local filter.
      render();
      return;
    }
    metaEl.textContent = 'Searching…';
    searchTimer = setTimeout(runSearch, 220);
  }

  async function runSearch() {
    if (!isSearching()) return;
    const q = filterQuery.trim();
    const seq = ++searchSeq;
    try {
      const res = await fetch(api('/api/reader/search?q=' + encodeURIComponent(q)),
                              { cache: 'no-store' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      // Drop stale results if the user kept typing.
      if (seq !== searchSeq) return;
      renderSearchResults(q, data);
    } catch (e) {
      if (seq !== searchSeq) return;
      searchResultsEl.hidden = false;
      gridEl.hidden = true;
      searchResultsEl.innerHTML =
        '<p class="reader-search-results__empty">Search failed: ' +
        escapeHtml(e.message) + '</p>';
      metaEl.textContent = '';
    }
  }

  function renderSearchResults(q, data) {
    gridEl.hidden = true;
    searchResultsEl.hidden = false;
    searchResultsEl.innerHTML = '';

    const results = Array.isArray(data.results) ? data.results : [];
    const count = results.length;
    const totalMatches = results.reduce((n, r) => n + (r.match_count || 0), 0);

    const head = document.createElement('p');
    head.className = 'reader-search-results__meta';
    head.textContent = count
      ? (count + ' book' + (count === 1 ? '' : 's') + ' · ' +
         totalMatches + ' match' + (totalMatches === 1 ? '' : 'es') +
         ' for “' + q + '”')
      : 'No books matched “' + q + '”';
    searchResultsEl.appendChild(head);

    if (!count) {
      const empty = document.createElement('div');
      empty.className = 'reader-search-results__empty';
      empty.textContent = 'Try a different keyword, or check that books have been imported.';
      searchResultsEl.appendChild(empty);
      metaEl.textContent = '';
      return;
    }

    for (const r of results) {
      searchResultsEl.appendChild(renderSearchGroup(r, q));
    }
    metaEl.textContent = '';
  }

  function renderSearchGroup(r, q) {
    const group = document.createElement('div');
    group.className = 'reader-search-results__group';

    // Header: cover thumb + title/author + match count. Clicking the
    // header opens the book at page 1 (or its saved position).
    const head = document.createElement('button');
    head.type = 'button';
    head.className = 'reader-search-results__group-head';
    head.style.cursor = 'pointer';

    const src = r.cover_url ? api(r.cover_url) + '?size=thumb' : null;
    if (src) {
      const img = document.createElement('img');
      img.className = 'reader-search-results__group-cover';
      img.alt = '';
      img.loading = 'lazy';
      img.src = src;
      img.addEventListener('error', () => img.replaceWith(phCover(r)));
      head.appendChild(img);
    } else {
      head.appendChild(phCover(r));
    }

    const titles = document.createElement('div');
    titles.className = 'reader-search-results__group-titles';
    const t = document.createElement('span');
    t.className = 'reader-search-results__group-title';
    t.innerHTML = highlight(r.title || 'Untitled', q);
    const a = document.createElement('span');
    a.className = 'reader-search-results__group-author';
    a.innerHTML = highlight(r.author || 'Unknown', q);
    titles.appendChild(t);
    titles.appendChild(a);

    const cnt = document.createElement('span');
    cnt.className = 'reader-search-results__group-count';
    cnt.textContent = r.match_count
      ? (r.match_count + ' match' + (r.match_count === 1 ? '' : 'es'))
      : (r.title_match || r.author_match ? 'metadata' : '');

    head.appendChild(titles);
    head.appendChild(cnt);
    head.addEventListener('click', () => openReader({
      id: r.id, title: r.title, author: r.author,
      page_count: r.page_count, last_page: r.last_page || 1,
      last_position: 0,
    }));
    group.appendChild(head);

    // Match list (page + snippet).
    if (r.matches && r.matches.length) {
      const matches = document.createElement('div');
      matches.className = 'reader-search-results__matches';
      for (const m of r.matches) {
        const row = document.createElement('button');
        row.type = 'button';
        row.className = 'reader-search-result';

        const page = document.createElement('span');
        page.className = 'reader-search-result__page';
        page.textContent = 'Page ' + m.page;

        const snip = document.createElement('span');
        snip.className = 'reader-search-result__snippet';
        snip.innerHTML = highlight(m.snippet || '', q);

        row.appendChild(page);
        row.appendChild(snip);
        row.addEventListener('click', () => openReader({
          id: r.id, title: r.title, author: r.author,
          page_count: r.page_count, last_page: m.page,
          last_position: 0,
        }, m.page));
        matches.appendChild(row);
      }
      group.appendChild(matches);
    }
    return group;
  }

  function phCover(r) {
    const ph = document.createElement('span');
    ph.className = 'reader-search-results__group-cover ph';
    ph.textContent = initials(r.title);
    return ph;
  }

  // ============================================================
  // Import — file picker → raw upload to /api/reader/import
  // ============================================================
  function triggerImport() { fileInput.click(); }

  async function handleFiles(fileList) {
    const files = Array.from(fileList || []).filter(f =>
      /\.(epub|pdf)$/i.test(f.name) ||
      f.type === 'application/epub+zip' || f.type === 'application/pdf');
    if (!files.length) {
      toast('No EPUB or PDF files selected.');
      return;
    }
    toast('Importing ' + files.length +
      (files.length === 1 ? ' book…' : ' books…'), { spin: true, sticky: true });

    let ok = 0;
    let failed = [];
    for (const file of files) {
      try {
        const res = await fetch(api('/api/reader/import'), {
          method: 'POST',
          headers: {
            'Content-Type': file.type || 'application/octet-stream',
            'X-Filename': file.name,
          },
          body: file,
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.error || ('HTTP ' + res.status));
        }
        ok++;
      } catch (e) {
        failed.push(file.name + ': ' + e.message);
      }
    }

    hideToast();
    await loadLibrary();
    if (failed.length) {
      showError('Some imports failed:\n' + failed.join('\n'));
      toast('Imported ' + ok + ', ' + failed.length + ' failed.');
    } else {
      toast('Imported ' + ok + (ok === 1 ? ' book.' : ' books.'));
    }
  }

  // ============================================================
  // Reading view
  // ============================================================
  async function openReader(book, page) {
    currentBook = {
      id: book.id,
      title: book.title || 'Untitled',
      author: book.author || 'Unknown author',
      page: Number(page || book.last_page || 1) || 1,
      page_count: Number(book.page_count || 0),
      toc: null,            // populated on first page load
      lastViewedPage: null,
    };
    rvTitle.textContent = currentBook.title;
    rvAuthor.textContent = currentBook.author;
    rvEl.hidden = false;
    document.body.classList.add('is-reading');
    document.body.style.overflow = 'hidden';
    applyFontPrefs();
    revealChrome();
    closeToc();
    startReadingTimer();
    await loadPage(currentBook.page);
    markActiveSidebar();
  }

  function closeReader() {
    rvEl.hidden = true;
    document.body.classList.remove('is-reading');
    document.body.style.overflow = '';
    currentBook = null;
    rvPage.textContent = '';
    closeToc();
    stopReadingTimer();
    // Cancel any pending idle-hide timer so a stale callback can't
    // re-apply `is-idle` after the reader has already closed (or to a
    // freshly reopened view).
    if (idleTimer) { clearTimeout(idleTimer); idleTimer = null; }
    rvEl.classList.remove('is-idle');
    exitFullscreenIfActive();
    markActiveSidebar();
    // Refresh library so progress bars reflect the new position.
    loadLibrary();
  }

  function markActiveSidebar() {
    const id = currentBook ? currentBook.id : null;
    sidebarList.querySelectorAll('.reader-sidebar__row').forEach(row => {
      row.classList.toggle('is-active', row.dataset.id === id);
    });
  }

  async function loadPage(page) {
    if (!currentBook) return;
    rvPage.textContent = 'Loading…';
    rvPager.textContent = '';
    rvPrev.disabled = true;
    rvNext.disabled = true;
    try {
      const url = api('/api/reader/book/' + encodeURIComponent(currentBook.id) +
                      '?page=' + page);
      const res = await fetch(url, { cache: 'no-store' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      currentBook.page = data.page || page;
      currentBook.page_count = data.page_count || currentBook.page_count;
      if (currentBook.toc === null) {
        currentBook.toc = Array.isArray(data.toc) ? data.toc : [];
        setupToc();
      }
      renderPageContent(data.content || '');
      const pc = currentBook.page_count;
      const pctVal = pc ? Math.round((currentBook.page / pc) * 100) : 0;
      rvPos.textContent = pctVal + '%';
      rvPager.textContent = pc
        ? ('Page ' + currentBook.page + ' of ' + pc)
        : ('Page ' + currentBook.page);
      rvPrev.disabled = currentBook.page <= 1;
      rvNext.disabled = pc ? currentBook.page >= pc : true;
      rvPage.scrollTop = 0;
      recordPageView(currentBook.page);
      markTocCurrent();
      // Persist reading position explicitly on every page turn so progress
      // auto-resumes on reopen even if the book GET side-effect is later
      // removed. Fire-and-forget; failures are non-fatal.
      saveProgress(currentBook.id, currentBook.page);
    } catch (e) {
      rvPage.textContent = 'Failed to load page: ' + e.message;
      // Re-enable nav buttons based on the current known position so the
      // user can retry / move away after a failed page load (otherwise
      // they'd stay disabled from the pre-fetch state above).
      rvPrev.disabled = currentBook.page <= 1;
      rvNext.disabled = currentBook.page_count
        ? currentBook.page >= currentBook.page_count : true;
    }
  }

  // Render extracted plain text as clean paragraphs. Book pages are stored
  // as plain text with paragraph breaks (blank lines); split on those and
  // wrap each non-empty chunk in a <p> for a tidy reading column.
  function renderPageContent(content) {
    rvPage.textContent = '';
    const blocks = String(content).split(/\n{2,}/);
    let added = 0;
    for (const raw of blocks) {
      const text = raw.replace(/\s+/g, ' ').trim();
      if (!text) continue;
      const p = document.createElement('p');
      p.className = 'reader-view__para';
      p.textContent = text;
      rvPage.appendChild(p);
      added++;
    }
    if (!added) {
      rvPage.textContent = content || '(empty page)';
    }
  }

  async function saveProgress(bookId, page) {
    if (!bookId) return;
    try {
      await fetch(api('/api/reader/progress'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ bookId: bookId, page: page }),
      });
    } catch (_) { /* progress save is best-effort */ }
  }

  // ============================================================
  // Table of contents
  // ============================================================
  function setupToc() {
    const toc = (currentBook && currentBook.toc) || [];
    if (toc.length) {
      rvTocBtn.hidden = false;
      rvTocList.innerHTML = '';
      for (const entry of toc) {
        const li = document.createElement('li');
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'reader-view__toc-entry';
        btn.dataset.page = entry.page;
        btn.style.paddingLeft =
          'calc(var(--dot-space-5) + ' + (entry.depth || 0) + ' * 14px)';
        const t = document.createElement('span');
        t.className = 'reader-view__toc-entry-title';
        t.textContent = entry.title;
        const p = document.createElement('span');
        p.className = 'reader-view__toc-entry-page';
        p.textContent = entry.page;
        btn.appendChild(t);
        btn.appendChild(p);
        btn.addEventListener('click', () => {
          loadPage(Number(entry.page) || 1);
          // Keep TOC open on desktop (handy while browsing chapters),
          // close on mobile to reclaim screen.
          if (!window.matchMedia('(min-width: 1201px)').matches) closeToc();
        });
        li.appendChild(btn);
        rvTocList.appendChild(li);
      }
    } else {
      rvTocBtn.hidden = true;
    }
    markTocCurrent();
  }

  function markTocCurrent() {
    if (!currentBook) return;
    const page = currentBook.page;
    rvTocList.querySelectorAll('.reader-view__toc-entry').forEach(btn => {
      btn.classList.toggle('is-current', Number(btn.dataset.page) === page);
    });
  }

  function toggleToc() {
    const toc = (currentBook && currentBook.toc) || [];
    if (!toc.length) {
      toast('This book has no table of contents.');
      return;
    }
    if (rvToc.hidden) openToc(); else closeToc();
  }
  function openToc() {
    if ((currentBook && currentBook.toc && currentBook.toc.length) || false) {
      rvToc.hidden = false;
      rvEl.classList.add('is-toc-open');
      revealChrome();
    }
  }
  function closeToc() {
    rvToc.hidden = true;
    rvEl.classList.remove('is-toc-open');
  }

  // ============================================================
  // Fullscreen
  // ============================================================
  function isFullscreen() {
    return !!(document.fullscreenElement || document.webkitFullscreenElement);
  }
  function toggleFullscreen() {
    if (isFullscreen()) {
      exitFullscreenIfActive();
    } else {
      const el = document.documentElement;
      const req = el.requestFullscreen || el.webkitRequestFullscreen;
      if (req) req.call(el).catch(() => {});
    }
  }
  function exitFullscreenIfActive() {
    if (isFullscreen()) {
      (document.exitFullscreen || document.webkitExitFullscreen || function(){})
        .call(document);
    }
  }

  // ============================================================
  // Event wiring
  // ============================================================
  importBtn.addEventListener('click', triggerImport);
  emptyImport.addEventListener('click', triggerImport);
  fileInput.addEventListener('change', () => {
    handleFiles(fileInput.files);
    fileInput.value = '';   // allow re-importing the same file
  });

  // Search/filter — debounced. Short queries filter titles/authors locally;
  // queries ≥2 chars trigger a full-text server search.
  searchInput.addEventListener('input', () => {
    filterQuery = searchInput.value;
    scheduleSearch();
  });
  searchInput.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      searchInput.value = '';
      filterQuery = '';
      searchInput.blur();
      render();
    }
  });

  // Reading view nav
  rvClose.addEventListener('click', closeReader);
  rvPrev.addEventListener('click', () => {
    if (currentBook && currentBook.page > 1) loadPage(currentBook.page - 1);
  });
  rvNext.addEventListener('click', () => {
    if (currentBook) loadPage(currentBook.page + 1);
  });
  rvFontInc.addEventListener('click', () => bumpFont(+FONT_STEP));
  rvFontDec.addEventListener('click', () => bumpFont(-FONT_STEP));
  rvFontFam.addEventListener('change', () => setFontFamily(rvFontFam.value));
  rvFsBtn.addEventListener('click', toggleFullscreen);
  rvTocBtn.addEventListener('click', toggleToc);
  rvTocClose.addEventListener('click', closeToc);

  // React to the browser's own fullscreen changes so the button label
  // stays accurate even when the user exits fullscreen with Esc.
  document.addEventListener('fullscreenchange', () => { revealChrome(); });
  document.addEventListener('webkitfullscreenchange', () => { revealChrome(); });

  // ---- Zero-chrome reading: hide top bar + nav when idle, reveal on
  //      mouse move / hover / focus. A short idle timer re-hides them.
  let idleTimer = null;
  function revealChrome() {
    rvEl.classList.remove('is-idle');
    if (idleTimer) clearTimeout(idleTimer);
    idleTimer = setTimeout(() => {
      if (!rvEl.hidden) rvEl.classList.add('is-idle');
    }, 2600);
  }
  rvEl.addEventListener('mousemove', revealChrome);
  rvEl.addEventListener('mouseenter', revealChrome);
  rvEl.addEventListener('mouseleave', () => {
    if (!rvEl.hidden) rvEl.classList.add('is-idle');
  });
  [rvBar, rvNav].forEach(el => {
    el.addEventListener('focusin', revealChrome);
    el.addEventListener('mouseenter', revealChrome);
  });

  // ---- Keyboard shortcuts --------------------------------------------
  // /  focus search       (library)
  // ←  previous page      (reader)
  // →  next page          (reader)
  // f  toggle fullscreen  (reader)
  // t  toggle TOC         (reader)
  // Esc back to library   (reader); clear search (search focused)
  document.addEventListener('keydown', (e) => {
    const inField = /^(INPUT|TEXTAREA|SELECT)$/.test(
      (e.target && e.target.tagName) || '');
    const readerOpen = !rvEl.hidden;

    // Global: '/' focuses search (unless typing in a field).
    if (e.key === '/' && !inField && !readerOpen) {
      e.preventDefault();
      searchInput.focus();
      return;
    }

    if (!readerOpen) return;

    if (e.key === 'Escape') {
      // Esc closes the TOC first if it's open, otherwise the reader.
      if (!rvToc.hidden) { e.preventDefault(); closeToc(); return; }
      e.preventDefault();
      closeReader();
      return;
    }
    if (inField) return;  // don't hijack typing inside inputs

    switch (e.key) {
      case 'ArrowLeft':
        e.preventDefault(); rvPrev.click(); break;
      case 'ArrowRight':
        e.preventDefault(); rvNext.click(); break;
      case 'f': case 'F':
        e.preventDefault(); toggleFullscreen(); break;
      case 't': case 'T':
        e.preventDefault(); toggleToc(); break;
    }
  });

  // ---- Drag & drop import anywhere on the page ------------------------
  let dragCounter = 0;
  window.addEventListener('dragenter', (e) => {
    if (!e.dataTransfer || !Array.from(e.dataTransfer.types).includes('Files')) return;
    e.preventDefault();
    dragCounter++;
    document.body.classList.add('is-dragging');
  });
  window.addEventListener('dragover', (e) => {
    if (e.dataTransfer && Array.from(e.dataTransfer.types).includes('Files')) {
      e.preventDefault();
    }
  });
  window.addEventListener('dragleave', () => {
    dragCounter = Math.max(0, dragCounter - 1);
    if (dragCounter === 0) document.body.classList.remove('is-dragging');
  });
  window.addEventListener('drop', (e) => {
    if (!e.dataTransfer || !e.dataTransfer.files.length) return;
    e.preventDefault();
    dragCounter = 0;
    document.body.classList.remove('is-dragging');
    handleFiles(e.dataTransfer.files);
  });

  // ---- Initial render -------------------------------------------------
  applyFontPrefs();
  renderStats();
  loadLibrary();
})();
