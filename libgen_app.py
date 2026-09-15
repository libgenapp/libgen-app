#!/usr/bin/env python3
"""LibGen desktop app: search tabs, checkbox table, download queue, naming scheme.

Run:  python3 libgen_app.py             (PyQt5 required; use the Anaconda python3)
      python3 libgen_app.py --selftest  (no window, no PyQt5 needed; checks the pure functions)
      QT_QPA_PLATFORM=offscreen python3 libgen_app.py --smoke   (headless: real search + one download)
"""
import functools
import hashlib
import html
import base64
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

APP_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
if APP_DIR.endswith("/Contents/MacOS"):  # inside a macOS .app bundle (may live in /Applications): use the user's support dir
    APP_DIR = os.path.expanduser("~/Library/Application Support/LibGen")
    os.makedirs(APP_DIR, exist_ok=True)
sys.path.insert(0, APP_DIR)
import lgsearch  # noqa: E402  (same folder; bundled by PyInstaller because it's imported)

DOWNLOAD_DIR = os.path.join(os.path.expanduser("~"), "Downloads", "libgen")  # default; change in Settings
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")
SESSION_PATH = os.path.join(APP_DIR, "session.json")
try:
    IS_WSL = "microsoft" in open("/proc/version").read().lower()
except OSError:
    IS_WSL = False

DEFAULTS = {
    "download_dir": DOWNLOAD_DIR, "attempts": 5, "request_timeout": 45,
    "rename": True, "scheme": "{NAME} ({YEAR})",
    "ext": "epub", "lang": "english", "min_size": 0.0, "max_size": 0.0, "limit": 25, "topics": "lf", "fields": "ta",
    "mirror": "", "search_timeout": 45, "search_retries": 3, "restore_session": True, "show_covers": True, "skip_downloaded": True,
    "dark": False,
}
ILLEGAL = re.compile(r'[<>:"/\\|?*]')  # NTFS-illegal; the library drive is Windows-mounted
GET_RE = re.compile(r'href="([^"]*get\.php\?[^"]*)"')
COVER_RE = re.compile(r'<img[^>]+src="([^"]*cover[^"]*)"', re.I)
EXTS = ["any", "pdf", "epub", "djvu", "mobi", "azw3", "fb2", "cbz", "cbr"]

C_CHECK, C_TITLE, C_AUTHOR, C_YEAR, C_PUBLISHER, C_LANG, C_PAGES, C_SIZE, C_EXT, C_LINK, C_INFO = range(11)
COL_WIDTH = {C_TITLE: (320, 520), C_AUTHOR: (140, 260), C_PUBLISHER: (120, 220)}  # (min, max) px; others default
HEADERS = ["", "Title", "Author(s)", "Year", "Publisher", "Language", "Pages", "Size", "Ext", "Link", ""]

PLACEHOLDERS = [
    ("{NAME}", "book title (alias {TITLE})"), ("{YEAR}", "year"),
    ("{FIRST_AUTHOR}", "first author"), ("{AUTHORS}", "all authors, comma-separated"),
    ("{PUBLISHER}", "publisher"), ("{LANGUAGE}", "language"), ("{PAGES}", "page count"),
    ("{SIZE}", "file size as listed"), ("{MD5}", "MD5 hash"),
]
SAMPLE = {"md5": "d41d8cd98f00b204e9800998ecf8427e", "title": "Moby Dick", "author": "Herman Melville",
          "publisher": "Harper & Brothers", "year": "1851", "language": "English", "pages": "635",
          "size": "1.2 MB", "extension": "epub", "href": ""}

HELP_HTML = """
<h2>LibGen — quick guide</h2>

<h3>Searching</h3>
<ul>
<li>Type a title and/or author in the search box and press <b>Enter</b> to search in the current tab, or <b>Ctrl+Enter</b> to open the results in a new tab.</li>
<li>The <b>filter bar</b> (extension, language, min/max size, result limit, page) applies to the next search. Its starting values come from <b>Settings → Default search filters</b>. The mirror serves 100 results per page; the app keeps fetching pages until <b>Limit</b> is filled (up to 10 pages), so raise Limit to see a prolific author's whole catalogue.</li>
<li><b>Fields ▾</b> picks which fields the query is matched against (Title, Author(s), Series, Year, Publisher, ISBN — default Title + Author). <b>Topics ▾</b> picks the catalogues searched (non-fiction, fiction, comics, magazines, articles, standards…). Both are multi-choice, like the site's own checkboxes. Objects is fixed to <i>Files</i> because only file rows carry a downloadable MD5.</li>
<li><b>Search list…</b> loads a plain text file (one query per line; blank lines and lines starting with <code>#</code> are skipped) and opens one tab per query, running them one after another.</li>
<li>The per-tab <b>quick filter</b> box narrows the rows already loaded (any column) without searching again.</li>
</ul>

<h3>Choosing books</h3>
<ul>
<li>Tick the checkbox on a row to select it. Selection is shared across <i>all</i> tabs.</li>
<li><b>Space</b> or <b>Enter</b> toggles the current row's checkbox. <b>Ctrl+A</b> checks every currently-visible (filtered) row. Clicking the <b>checkbox header</b> toggles all visible rows.</li>
<li><b>Double-click</b> a row to open its <b>Details</b> (cover + full record).</li>
<li>Click a column header to <b>sort</b> by it (Year, Pages and Size sort numerically). Drag a header edge to resize a column.</li>
<li><b>↗ open</b> opens the book's page in your browser. <b>ⓘ</b> opens the full mirror record, with the cover image when the book has one.</li>
<li><b>Right-click</b> a row for: Download this · Download this to… · Open page · Details · Copy MD5.</li>
</ul>

<h3>Downloading</h3>
<ul>
<li><b>Download (N)</b> (<b>Ctrl+D</b>) queues every checked book to the default folder; <b>Download to…</b> (<b>Ctrl+Shift+D</b>) asks for a folder first. <b>Ctrl+Enter</b> on a row queues just that row.</li>
<li>The <b>Downloads</b> panel (bottom) is a queue: items are added, never replaced, and download one at a time. Each shows status and progress.</li>
<li><b>Retry</b> re-queues failed/cancelled items (mirrors are flaky — a retry often works). <b>Cancel</b> (or <b>Delete</b>) stops selected items, or all pending when none are selected. <b>Clear done</b> tidies finished rows. <b>Open folder</b> opens the download folder.</li>
<li>Every download is verified against its MD5, so a corrupt or truncated file is rejected and retried automatically.</li>
<li>Books already recorded in the destination folder's <code>downloaded.txt</code> are <b>skipped</b> (queue status "skipped") — untick <b>Settings → Duplicates</b> to re-download, or use <b>Retry</b> on the skipped row after unticking.</li>
</ul>

<h3>Settings</h3>
<ul>
<li><b>Default download folder</b>, retry <b>attempts</b> per mirror, and request <b>timeouts</b>.</li>
<li><b>Rename files?</b> on → files are named by the <b>scheme</b> using placeholders like <code>{NAME}</code>, <code>{YEAR}</code>, <code>{FIRST_AUTHOR}</code>, <code>{AUTHORS}</code> (see the live preview). Off → the mirror's own filename is kept.</li>
<li><b>Force mirror</b> pins one mirror (e.g. <code>https://libgen.gl/</code>); empty uses the live list with automatic fallback.</li>
<li><b>Session</b>: load the previous tabs + queue on startup, or with the <b>Load last session now</b> button.</li>
<li><b>Appearance</b>: <b>Dark theme</b> switches the whole window to a dark palette immediately.</li>
</ul>

<h3>Keyboard reference</h3>
<table border="1" cellpadding="5" cellspacing="0">
<tr><th align="left">Key</th><th align="left">Action</th></tr>
<tr><td>Enter <i>(search box)</i></td><td>Search in the current tab</td></tr>
<tr><td>Ctrl+Enter <i>(search box)</i></td><td>Search in a new tab</td></tr>
<tr><td>Enter / Space <i>(results)</i></td><td>Toggle the current row's checkbox</td></tr>
<tr><td>Ctrl+Enter <i>(results)</i></td><td>Download the current row</td></tr>
<tr><td>Double-click <i>(results)</i></td><td>Open the row's details</td></tr>
<tr><td>Ctrl+A <i>(results)</i></td><td>Check all visible rows</td></tr>
<tr><td>Ctrl+D / Ctrl+Shift+D</td><td>Download checked / Download checked to…</td></tr>
<tr><td>Ctrl+F</td><td>Focus the quick-filter box</td></tr>
<tr><td>Esc <i>(filter box)</i></td><td>Clear the quick filter</td></tr>
<tr><td>Ctrl+L or /</td><td>Focus the search box</td></tr>
<tr><td>Ctrl+W</td><td>Close the current tab</td></tr>
<tr><td>Delete <i>(queue)</i></td><td>Cancel selected downloads</td></tr>
</table>

<h3>Disclaimer</h3>
<p>This project is independent and not affiliated with, endorsed by, or connected to Library Genesis or any of its mirrors. It is a client only: it hosts no content and connects to public mirrors whose availability changes. Make sure downloading a given work is legal where you are.</p>
"""


# ------------------------------------------------------------------ settings
def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            s.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return s


def save_settings(s):
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)


# ------------------------------------------------------------------ naming
def split_authors(s):
    # ponytail: heuristic; LibGen author strings are inconsistent and "Last, First" is kept as-is
    s = re.sub(r"[(\[][^)\]]*[)\]]", "", s or "")
    return [a.strip(" ,") for a in re.split(r"\s*(?:;|&|\band\b)\s*", s) if a.strip(" ,")]


def clean_stem(s):
    s = ILLEGAL.sub(" ", s)
    s = re.sub(r"[(\[{]\s*[)\]}]", "", s)  # brackets left empty by blank fields
    return re.sub(r"\s+", " ", s).strip(" .-_,;")[:150]


def format_name(scheme, entry):
    """Render a naming scheme to a filename stem (no extension)."""
    authors = split_authors(entry.get("author", ""))
    fields = {
        "NAME": entry.get("title", ""), "TITLE": entry.get("title", ""), "YEAR": entry.get("year", ""),
        "FIRST_AUTHOR": authors[0] if authors else "", "AUTHORS": ", ".join(authors),
        "PUBLISHER": entry.get("publisher", ""), "LANGUAGE": entry.get("language", ""),
        "PAGES": entry.get("pages", ""), "SIZE": entry.get("size", ""), "MD5": entry.get("md5", ""),
    }

    def sub(m):
        key = m.group(1)
        if key not in fields:
            return m.group(0)  # unknown placeholder stays literal so the preview exposes the typo
        v = str(fields[key]).strip()
        return "" if v in ("", "0") else v  # LibGen uses 0 for unknown year/pages

    name = clean_stem(re.sub(r"\{(\w+)\}", sub, scheme))
    return name or clean_stem(entry.get("title", "")) or entry.get("md5", "book")


def unique_path(d, stem, ext):
    path = os.path.join(d, stem + ext)
    n = 2
    while os.path.exists(path):
        path = os.path.join(d, f"{stem} ({n}){ext}")
        n += 1
    return path


def sort_key(col, entry):
    if col == C_YEAR:
        m = re.search(r"\d{4}", entry["year"])
        return int(m.group()) if m else -1
    if col == C_PAGES:
        m = re.search(r"\d+", entry["pages"])
        return int(m.group()) if m else -1
    if col == C_SIZE:
        mb = lgsearch._size_to_mb(entry["size"])
        return mb if mb is not None else -1.0
    field = {C_TITLE: "title", C_AUTHOR: "author", C_PUBLISHER: "publisher",
             C_LANG: "language", C_EXT: "extension"}.get(col)
    return entry[field].lower() if field else ""


def edition_url(entry, mirror):
    return urllib.parse.urljoin(mirror, entry.get("href") or "ads.php?md5=" + entry["md5"])


def open_external(target):
    """Open a folder or URL: Explorer on Windows/WSL, the desktop default elsewhere."""
    is_url = target.startswith("http")
    if sys.platform == "win32":
        os.startfile(target)  # noqa: S606
    elif IS_WSL:
        if is_url:
            # explorer.exe opens URLs unreliably (often lands on File Explorer); `start` is the browser
            subprocess.Popen(["cmd.exe", "/c", "start", "", target])
        else:
            win = subprocess.run(["wslpath", "-w", target], capture_output=True, text=True).stdout.strip()
            subprocess.Popen(["explorer.exe", win])
    else:
        from PyQt5.QtCore import QUrl
        from PyQt5.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl(target) if is_url else QUrl.fromLocalFile(target))


# ------------------------------------------------------------------ download engine
class Cancelled(Exception):
    pass


_mirrors = []


def mirrors_for(settings):
    """Live mirror list, fetched once per process (it hits GitHub); an explicit override wins."""
    if settings.get("mirror"):
        return lgsearch.get_mirrors(settings["mirror"], settings["search_timeout"])
    if not _mirrors:
        _mirrors.extend(lgsearch.get_mirrors(None, settings["search_timeout"]))
    return list(_mirrors)


def find_get_link(page, mirror):
    # ponytail: the live ads.php page carries exactly one get.php link (verified). The Node tool's
    # structural selector "#main > tr:first-child > td:nth-child(2) > a" via HTMLParser is the upgrade.
    m = GET_RE.search(page)
    return urllib.parse.urljoin(mirror, html.unescape(m.group(1))) if m else None


def fetch_cover(entry, mirror, timeout):
    """Cover image bytes from the book's edition page on ONE mirror (ads.php as fallback), b"" if
    none. libgen gates covers behind a `covers` cookie, so we send it on the image request."""
    pages = ([entry["href"]] if entry.get("href") else []) + ["ads.php?md5=" + entry["md5"]]
    for page in pages:
        page_url = urllib.parse.urljoin(mirror, page)
        try:
            m = COVER_RE.search(lgsearch.fetch(page_url, timeout))
            if not m:
                continue
            req = urllib.request.Request(urllib.parse.urljoin(mirror, html.unescape(m.group(1))),
                                         headers={"User-Agent": lgsearch.UA, "Referer": page_url,
                                                  "Cookie": "covers=on"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            if r.headers.get_content_type().startswith("image/") and len(data) > 200:
                return data
        except Exception:  # noqa: BLE001 - a missing cover is not worth surfacing
            continue
    return b""


def download_one(entry, dest, settings, progress_cb, cancel_event):
    """Fetch one book into dest/<md5>.part and verify its MD5. Returns (part_path, server_filename)."""
    md5, t = entry["md5"], settings["request_timeout"]
    os.makedirs(dest, exist_ok=True)
    part = os.path.join(dest, md5 + ".part")

    def grab(mirror):
        page = lgsearch.fetch(mirror + "ads.php?md5=" + md5, t)  # browser UA + gzip handled there
        if lgsearch.is_overloaded(page):
            raise Exception("mirror overloaded")
        url = find_get_link(page, mirror)  # resolved right before the GET: the key expires per request
        if not url:
            raise Exception("no download link")
        req = urllib.request.Request(url, headers={"User-Agent": lgsearch.UA})  # no gzip on binaries
        h, done = hashlib.md5(), 0
        with urllib.request.urlopen(req, timeout=t) as resp:  # follows the 307 to the CDN host
            if resp.headers.get_content_type() == "text/html":
                raise Exception("got an HTML page instead of a file")
            total = int(resp.headers.get("Content-Length") or 0)
            name = resp.headers.get_filename() or ""
            with open(part, "wb") as f:
                while True:
                    if cancel_event.is_set():
                        raise Cancelled()
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    progress_cb(done, total)
        if done < 20_000:
            raise Exception(f"file too small ({done} bytes): an error page, not a book")
        if h.hexdigest() != md5:
            raise Exception("md5 mismatch: the mirror's copy differs from the catalog")
        return part, name

    last = "no mirrors available"
    try:
        for mirror in mirrors_for(settings):
            for _ in range(max(1, int(settings["attempts"]))):
                # ponytail: retries the whole ads+get round-trip; the ads page is tiny and the key expires anyway
                try:
                    return grab(mirror)
                except Cancelled:
                    raise
                except Exception as e:  # any network/parse failure just means "try again"
                    last = f"{mirror}: {e}"
                    if cancel_event.wait(2):
                        raise Cancelled()
        raise Exception("all mirrors failed; last: " + last)
    except BaseException:
        if os.path.exists(part):
            os.remove(part)
        raise


def already_downloaded(md5, dest):
    """True if md5 is in dest/downloaded.txt (the per-folder ledger finish_download appends to)."""
    try:
        with open(os.path.join(dest, "downloaded.txt"), encoding="utf-8") as f:
            return md5 in f.read().split()
    except OSError:
        return False


def finish_download(part, server_name, entry, dest, settings):
    """Move the verified .part into place under its final name; record the md5 in dest/downloaded.txt."""
    stem, ext = os.path.splitext(server_name or "")
    ext = ext.lower() or "." + entry["extension"]
    stem = format_name(settings["scheme"], entry) if settings["rename"] else clean_stem(stem)
    target = unique_path(dest, stem or entry["md5"], ext)
    os.replace(part, target)
    log = os.path.join(dest, "downloaded.txt")  # same per-folder ledger batch_download.sh keeps
    try:
        seen = open(log, encoding="utf-8").read().split()
    except OSError:
        seen = []
    if entry["md5"] not in seen:
        with open(log, "a", encoding="utf-8") as f:
            f.write(entry["md5"] + "\n")
    return target


# ------------------------------------------------------------------ selftest
def selftest():
    import tempfile
    assert format_name("{NAME} ({YEAR})", SAMPLE) == "Moby Dick (1851)"
    assert format_name("{NAME} ({YEAR})", {**SAMPLE, "year": "0"}) == "Moby Dick"
    assert format_name("{YEAR}-{NAME}", {**SAMPLE, "year": ""}) == "Moby Dick"
    assert format_name("{NAME}", {**SAMPLE, "title": "Foo: Bar?/Baz"}) == "Foo Bar Baz"
    assert format_name("{FIRST_AUTHOR} - {NAME}", {**SAMPLE, "author": ""}) == "Moby Dick"
    assert format_name("{FIRST_AUTHOR} - {NAME}", SAMPLE) == "Herman Melville - Moby Dick"
    assert format_name("{X} {NAME}", SAMPLE) == "{X} Moby Dick"
    assert format_name("{YEAR}", {**SAMPLE, "year": "", "title": ""}) == SAMPLE["md5"]
    assert split_authors("A; B & C and D (editor)") == ["A", "B", "C", "D"]
    assert split_authors("Nagatsuki, Tappei ; Otsuka, Shinichirou") == ["Nagatsuki, Tappei", "Otsuka, Shinichirou"]
    assert split_authors("Alexander Anderson") == ["Alexander Anderson"]
    snippet = '<td align="center"><a href="get.php?md5=X&amp;key=Y"><h2>GET</h2></a></td>'
    assert find_get_link(snippet, "https://m/") == "https://m/get.php?md5=X&key=Y"
    assert find_get_link("<p>nothing</p>", "https://m/") is None
    assert COVER_RE.search('<img src="/img/logo.png"><img src="/fictioncovers/28/ab.jpg">').group(1) == "/fictioncovers/28/ab.jpg"
    fake = [{"extension": "epub", "language": "English", "size": "3 MB"},
            {"extension": "pdf", "language": "English", "size": "30 MB"},
            {"extension": "epub", "language": "German", "size": "500 kB"}]
    assert len(lgsearch.apply_filters(fake, ext="epub")) == 2
    assert len(lgsearch.apply_filters(fake, lang="english")) == 2
    assert len(lgsearch.apply_filters(fake, min_size=1, max_size=10)) == 1
    assert len(lgsearch.apply_filters(fake)) == 3
    assert sort_key(C_SIZE, fake[2]) < sort_key(C_SIZE, fake[0]) < sort_key(C_SIZE, fake[1])
    assert edition_url({**SAMPLE, "href": "edition.php?id=5"}, "https://m/") == "https://m/edition.php?id=5"
    assert edition_url(SAMPLE, "https://m/") == "https://m/ads.php?md5=" + SAMPLE["md5"]
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "a.epub"), "w").close()
        assert unique_path(d, "a", ".epub").endswith("a (2).epub")
        part = os.path.join(d, "x.part")
        with open(part, "wb") as f:
            f.write(b"x")
        out = finish_download(part, "Whatever - server name.EPUB", SAMPLE, d, {**DEFAULTS, "rename": True})
        assert os.path.basename(out) == "Moby Dick (1851).epub", out
        assert SAMPLE["md5"] in open(os.path.join(d, "downloaded.txt")).read()
        with open(part, "wb") as f:
            f.write(b"x")
        out = finish_download(part, "[Set 1] Some: Name.pdf", SAMPLE, d, {**DEFAULTS, "rename": False})
        assert os.path.basename(out) == "[Set 1] Some Name.pdf", out
        assert open(os.path.join(d, "downloaded.txt")).read().count(SAMPLE["md5"]) == 1
        assert already_downloaded(SAMPLE["md5"], d) and not already_downloaded("0" * 32, d)
        assert not already_downloaded(SAMPLE["md5"], os.path.join(d, "nowhere"))
    print("selftest OK")
    return 0


if __name__ == "__main__" and "--selftest" in sys.argv:
    sys.exit(selftest())  # stop here: everything below needs PyQt5

# ==================================================================== Qt from here on
from PyQt5.QtCore import QEvent, QSortFilterProxyModel, Qt, pyqtSignal  # noqa: E402
from PyQt5.QtGui import QColor, QKeySequence, QPalette, QPixmap, QStandardItem, QStandardItemModel  # noqa: E402
from PyQt5.QtWidgets import (  # noqa: E402
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDockWidget, QDoubleSpinBox,
    QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QMainWindow,
    QMenu, QMessageBox, QPlainTextEdit, QPushButton, QShortcut, QSpinBox, QStackedWidget, QTableView,
    QTableWidget, QTableWidgetItem, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

ENTRY_ROLE = Qt.UserRole + 1  # the result dict lives on the checkbox item
LANGS = ["any", "english", "german", "french", "spanish", "italian", "russian", "hebrew", "japanese"]


ICON_B64 = (
    "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"
)


def app_icon():
    """The app icon (book with the LibGen logo + download ribbon), embedded as base64."""
    from PyQt5.QtGui import QIcon, QPixmap
    pm = QPixmap()
    pm.loadFromData(base64.b64decode(ICON_B64))
    return QIcon(pm)


# ------------------------------------------------------------------ shared filter bar
def fill_tree(parent, obj):
    """Recursively add a dict/list to a QTreeWidget as Field / Value rows."""
    from PyQt5.QtWidgets import QTreeWidgetItem
    for k, v in (obj.items() if isinstance(obj, dict) else enumerate(obj)):
        if isinstance(v, (dict, list)):
            fill_tree(QTreeWidgetItem(parent, [str(k), ""]), v)
        else:
            QTreeWidgetItem(parent, [str(k), "" if v is None else str(v)])


def checkable_menu(label, options, selected):
    """A drop-down of checkboxes (like the mirror's own multi-choice rows); codes in `selected` start ticked."""
    btn = QToolButton()
    btn.setText(label + " ▾")
    btn.setPopupMode(QToolButton.InstantPopup)
    menu = QMenu(btn)
    for code, name in options.items():
        a = menu.addAction(name[0].upper() + name[1:])  # the mirror's code letter stays in a.data()
        a.setCheckable(True)
        a.setChecked(code in selected)
        a.setData(code)
    btn.setMenu(menu)
    return btn


def menu_codes(btn, default):
    return "".join(a.data() for a in btn.menu().actions() if a.isChecked()) or default


def make_filter_widgets(s, with_page=False):
    w = {}
    w["ext"] = QComboBox()
    w["ext"].setEditable(True)
    w["ext"].addItems(EXTS)
    w["ext"].setCurrentText(s["ext"] or "any")
    w["lang"] = QComboBox()
    w["lang"].setEditable(True)
    w["lang"].addItems(LANGS)
    w["lang"].setCurrentText(s["lang"] or "any")
    for key in ("min_size", "max_size"):
        w[key] = QDoubleSpinBox()
        w[key].setRange(0, 5000)
        w[key].setDecimals(1)
        w[key].setSuffix(" MB")
        w[key].setSpecialValueText("any")
        w[key].setValue(float(s[key] or 0))
    w["limit"] = QSpinBox()
    w["limit"].setRange(1, 1000)  # the mirror serves 100/page; search_all pages until filled
    w["limit"].setValue(int(s["limit"]))
    w["fields"] = checkable_menu("Fields", lgsearch.FIELDS, s["fields"])
    w["topics"] = checkable_menu("Topics", lgsearch.TOPICS, s["topics"])
    if with_page:
        w["page"] = QSpinBox()
        w["page"].setRange(1, 50)
    return w


def read_filters(w):
    ext, lang = w["ext"].currentText().strip().lower(), w["lang"].currentText().strip().lower()
    return {
        "ext": "" if ext in ("", "any") else ext.lstrip("."),
        "lang": "" if lang in ("", "any") else lang,
        "min_size": w["min_size"].value(), "max_size": w["max_size"].value(), "limit": w["limit"].value(),
        "topics": menu_codes(w["topics"], lgsearch.DEFAULT_TOPICS),
        "fields": menu_codes(w["fields"], lgsearch.DEFAULT_FIELDS),
        "page": w["page"].value() if "page" in w else 1,
    }


def filter_row(w):
    h = QHBoxLayout()
    for label, key in (("Ext", "ext"), ("Language", "lang"), ("Min", "min_size"), ("Max", "max_size"),
                       ("Limit", "limit"), ("Page", "page")):
        if key in w:
            h.addWidget(QLabel(label))
            h.addWidget(w[key])
    h.addWidget(w["fields"])
    h.addWidget(w["topics"])
    h.addStretch()
    return h


def connect_filters(w, slot):
    for key, widget in w.items():
        if key in ("topics", "fields"):
            for a in widget.menu().actions():
                a.triggered.connect(slot)
        elif isinstance(widget, QComboBox):
            widget.currentTextChanged.connect(slot)
        else:
            widget.valueChanged.connect(slot)


# ------------------------------------------------------------------ one results tab
class ResultsTab(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.query, self.mirror, self.entries = "", "", []
        self.sort_col, self.sort_order = -1, Qt.AscendingOrder
        self._bulk = False

        self.model = QStandardItemModel(0, len(HEADERS))
        self.model.setHorizontalHeaderLabels(HEADERS)
        self.model.itemChanged.connect(self.on_item_changed)
        self.proxy = QSortFilterProxyModel()
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterKeyColumn(-1)
        self.proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.proxy.setSortRole(Qt.UserRole)
        self.proxy.setDynamicSortFilter(True)

        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter these results…   (Ctrl+F · Esc clears)")
        self.filter.textChanged.connect(self.proxy.setFilterFixedString)
        QShortcut(QKeySequence(Qt.Key_Escape), self.filter, self.filter.clear, context=Qt.WidgetShortcut)

        self.view = QTableView()
        self.view.setModel(self.proxy)
        self.view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.view.setEditTriggers(QAbstractItemView.NoEditTriggers)  # checkbox clicks still work
        self.view.verticalHeader().setVisible(False)
        self.view.setSortingEnabled(False)  # manual: a click on the checkbox header must not sort
        hdr = self.view.horizontalHeader()
        hdr.setSectionsClickable(True)
        hdr.setSortIndicatorShown(True)
        hdr.setSortIndicator(-1, Qt.AscendingOrder)
        hdr.setSectionResizeMode(QHeaderView.Interactive)  # every column user-resizable
        hdr.sectionClicked.connect(self.on_header)
        self.view.clicked.connect(self.on_click)
        self.view.doubleClicked.connect(self.on_double)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self.on_menu)
        self.view.installEventFilter(self)

        self.status = QLabel("")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.filter)
        lay.addWidget(self.view, 1)
        lay.addWidget(self.status)

    # ---- data
    def set_entries(self, entries, mirror, checked=()):
        self.entries, self.mirror = entries, mirror
        self.model.setRowCount(0)  # not clear(): that drops the headers
        for e in entries:
            texts = ["", e["title"], e["author"], e["year"], e["publisher"], e["language"],
                     e["pages"], e["size"], e["extension"], "↗ open", "ⓘ"]
            row = []
            for col, text in enumerate(texts):
                it = QStandardItem(text)
                it.setEditable(False)
                it.setData(sort_key(col, e), Qt.UserRole)  # every cell: the proxy sorts on this role
                row.append(it)
            row[C_CHECK].setCheckable(True)
            row[C_CHECK].setCheckState(Qt.Checked if e["md5"] in checked else Qt.Unchecked)
            row[C_CHECK].setData(e, ENTRY_ROLE)
            row[C_LINK].setToolTip(edition_url(e, mirror))
            row[C_INFO].setToolTip("Details (full mirror record)")
            for col in (C_LINK, C_INFO):
                row[col].setTextAlignment(Qt.AlignCenter)
            self.model.appendRow(row)
        self.view.resizeColumnsToContents()
        for col in range(self.model.columnCount()):  # clamp so one long cell can't stretch a column
            lo, hi = COL_WIDTH.get(col, (40, 120))
            self.view.setColumnWidth(col, min(hi, max(lo, self.view.columnWidth(col))))
        self.update_status()
        self.win.update_selection_count()

    def item_at(self, proxy_index):
        return self.model.item(self.proxy.mapToSource(proxy_index).row(), C_CHECK)

    def current_entry(self):
        idx = self.view.currentIndex()
        return self.item_at(idx).data(ENTRY_ROLE) if idx.isValid() else None

    def checked_entries(self):
        return [self.model.item(r, C_CHECK).data(ENTRY_ROLE) for r in range(self.model.rowCount())
                if self.model.item(r, C_CHECK).checkState() == Qt.Checked]

    def visible_items(self):
        return [self.item_at(self.proxy.index(r, C_CHECK)) for r in range(self.proxy.rowCount())]

    def toggle(self, proxy_index):
        it = self.item_at(proxy_index)
        it.setCheckState(Qt.Unchecked if it.checkState() == Qt.Checked else Qt.Checked)

    def set_all_visible(self, checked):
        self._bulk = True
        for it in self.visible_items():
            it.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        self._bulk = False
        self.on_item_changed(None)

    def update_status(self):
        n, m = len(self.entries), len(self.checked_entries())
        self.status.setText(f"{n} results from {self.mirror} · {m} checked" if self.mirror else "")

    # ---- events
    def on_item_changed(self, item):
        if self._bulk or (item is not None and item.column() != C_CHECK):
            return
        self.update_status()
        self.win.update_selection_count()

    def on_header(self, col):
        hdr = self.view.horizontalHeader()
        if col == C_CHECK:
            items = self.visible_items()
            self.set_all_visible(not all(i.checkState() == Qt.Checked for i in items) if items else False)
        elif col not in (C_LINK, C_INFO):
            if col == self.sort_col:
                self.sort_order = Qt.DescendingOrder if self.sort_order == Qt.AscendingOrder else Qt.AscendingOrder
            else:
                self.sort_col, self.sort_order = col, Qt.AscendingOrder
            self.proxy.sort(col, self.sort_order)
        hdr.setSortIndicator(self.sort_col, self.sort_order)  # undo the header's own flip on non-sort columns

    def on_click(self, idx):
        e = self.item_at(idx).data(ENTRY_ROLE)
        if idx.column() == C_LINK:
            open_external(edition_url(e, self.mirror))
        elif idx.column() == C_INFO:
            self.win.show_details(e)

    def on_double(self, idx):
        if idx.column() not in (C_LINK, C_INFO):  # those cells have their own single-click action
            e = self.item_at(idx).data(ENTRY_ROLE)
            if e:
                self.win.show_details(e)

    def on_menu(self, pos):
        idx = self.view.indexAt(pos)
        if not idx.isValid():
            return
        e = self.item_at(idx).data(ENTRY_ROLE)
        m = QMenu(self)
        m.addAction("Download this", lambda: self.win.enqueue([e]))
        m.addAction("Download this to…", lambda: self.win.enqueue([e], ask=True))
        m.addSeparator()
        m.addAction("Open page in browser", lambda: open_external(edition_url(e, self.mirror)))
        m.addAction("Details", lambda: self.win.show_details(e))
        m.addAction("Copy MD5", lambda: QApplication.clipboard().setText(e["md5"]))
        m.exec_(self.view.viewport().mapToGlobal(pos))

    def eventFilter(self, obj, ev):
        if obj is self.view and ev.type() in (QEvent.KeyPress, QEvent.ShortcutOverride):
            key, ctrl = ev.key(), bool(ev.modifiers() & Qt.ControlModifier)
            mine = (key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space)) or (ctrl and key == Qt.Key_A)
            if not mine:
                return super().eventFilter(obj, ev)
            if ev.type() == QEvent.ShortcutOverride:  # claim the key before window shortcuts see it
                ev.accept()
                return True
            idx = self.view.currentIndex()
            if key in (Qt.Key_Return, Qt.Key_Enter) and ctrl:
                e = self.current_entry()
                if e:
                    self.win.enqueue([e])
            elif key == Qt.Key_A:
                self.set_all_visible(True)
            elif idx.isValid():
                self.toggle(idx)
            return True
        return super().eventFilter(obj, ev)


# ------------------------------------------------------------------ main window
class MainWindow(QMainWindow):
    job_done = pyqtSignal(object, object)          # (callback, result-or-exception) from worker threads
    dl_changed = pyqtSignal(int, str, str, int)    # (item id, status, note, percent)

    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.setWindowTitle("LibGen")
        self.resize(1400, 900)
        self.items, self.next_id = [], 0
        self.dl_queue = queue.Queue()
        self.search_queue, self.list_total = [], 0
        self.job_done.connect(self._on_job)
        self.dl_changed.connect(self.on_dl_changed)
        self._build_ui()
        self._shortcuts()
        self.dl_thread = threading.Thread(target=self._dl_loop, daemon=True)
        self.dl_thread.start()
        if self.settings["restore_session"] and os.path.exists(SESSION_PATH) and "--smoke" not in sys.argv:
            self.load_session()

    # ---- ui
    def _build_ui(self):
        self.sidebar = QListWidget()
        self.sidebar.addItems(["Search", "Settings", "Help"])
        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_search_page())
        self.stack.addWidget(self._build_settings_page())
        self.stack.addWidget(self._build_help_page())
        self.sidebar.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.sidebar.setCurrentRow(0)
        # left nav: always-visible logo above the Search/Settings/Help list
        from PyQt5.QtGui import QPixmap
        logo_pm = QPixmap()
        logo_pm.loadFromData(base64.b64decode(ICON_B64))
        logo = QLabel()
        logo.setPixmap(logo_pm.scaled(88, 88, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        logo.setAlignment(Qt.AlignHCenter)
        title = QLabel("LibGen")
        title.setAlignment(Qt.AlignHCenter)
        title.setStyleSheet("font-weight:bold; font-size:15px; margin-bottom:6px")
        left = QWidget()
        left.setFixedWidth(120)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(6, 10, 6, 6)
        lv.addWidget(logo)
        lv.addWidget(title)
        lv.addWidget(self.sidebar, 1)
        central = QWidget()
        h = QHBoxLayout(central)
        h.addWidget(left)
        h.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self._build_queue_dock()
        self.dl_label = QLabel("")
        self.statusBar().addPermanentWidget(self.dl_label)

    def _build_search_page(self):
        page = QWidget()
        v = QVBoxLayout(page)
        self.query = QLineEdit()
        self.query.setPlaceholderText("Title and/or author…   (Enter: search this tab · Ctrl+Enter: new tab)")
        self.query.returnPressed.connect(self.search_here)
        row = QHBoxLayout()
        row.addWidget(self.query, 1)
        for text, slot in (("Search", self.search_here), ("+ New tab", lambda: self.new_tab()),
                           ("Search list…", self.open_search_list)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        v.addLayout(row)
        self.fw = make_filter_widgets(self.settings, with_page=True)
        v.addLayout(filter_row(self.fw))
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        v.addWidget(self.tabs, 1)
        bottom = QHBoxLayout()
        bottom.addStretch()
        self.btn_dl = QPushButton("Download (0)")
        self.btn_dl.clicked.connect(lambda: self.enqueue(self.checked_all()))
        self.btn_dl_to = QPushButton("Download to…")
        self.btn_dl_to.clicked.connect(lambda: self.enqueue(self.checked_all(), ask=True))
        for b in (self.btn_dl, self.btn_dl_to):
            b.setEnabled(False)
            bottom.addWidget(b)
        v.addLayout(bottom)
        return page

    def _build_settings_page(self):
        s = self.settings
        page = QWidget()
        form = QFormLayout(page)
        self.sw = sw = {}
        sw["download_dir"] = QLineEdit(s["download_dir"])
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.browse_dir)
        h = QHBoxLayout()
        h.addWidget(sw["download_dir"], 1)
        h.addWidget(browse)
        form.addRow("Default download folder", h)
        for key, lo, hi, suffix, label in (("attempts", 1, 10, "", "Attempts per mirror"),
                                           ("request_timeout", 5, 600, " s", "Download request timeout"),
                                           ("search_timeout", 5, 600, " s", "Search request timeout"),
                                           ("search_retries", 1, 10, "", "Search retries when a mirror is overloaded")):
            sw[key] = QSpinBox()
            sw[key].setRange(lo, hi)
            sw[key].setSuffix(suffix)
            sw[key].setValue(int(s[key]))
            form.addRow(label, sw[key])
        sw["rename"] = QCheckBox("Rename downloaded files with the scheme below (off = keep the mirror's filename)")
        sw["rename"].setChecked(bool(s["rename"]))
        form.addRow("Rename files?", sw["rename"])
        sw["scheme"] = QLineEdit(s["scheme"])
        form.addRow("File naming scheme", sw["scheme"])
        self.preview = QLabel()
        form.addRow("Preview", self.preview)
        help_ = QLabel("<br>".join(f"<b>{p}</b> — {d}" for p, d in PLACEHOLDERS)
                       + "<br><i>Illegal filename characters are removed; blank fields leave no empty brackets.</i>")
        help_.setWordWrap(True)
        form.addRow("Placeholders", help_)
        self.sfw = make_filter_widgets(s)
        form.addRow("Default search filters", filter_row(self.sfw))
        sw["mirror"] = QLineEdit(s["mirror"])
        sw["mirror"].setPlaceholderText("optional, e.g. https://libgen.li/  (empty = live mirror list with fallback)")
        form.addRow("Force mirror", sw["mirror"])
        sw["skip_downloaded"] = QCheckBox("Skip books already listed in the destination folder's downloaded.txt "
                                          "(untick to re-download; queue shows them as 'skipped')")
        sw["skip_downloaded"].setChecked(bool(s["skip_downloaded"]))
        form.addRow("Duplicates", sw["skip_downloaded"])
        sw["show_covers"] = QCheckBox("Load the cover image in the details popup")
        sw["show_covers"].setChecked(bool(s["show_covers"]))
        form.addRow("Covers", sw["show_covers"])
        sw["restore_session"] = QCheckBox("Load the previous session (tabs + download queue) on startup")
        sw["restore_session"].setChecked(bool(s["restore_session"]))
        load = QPushButton("Load last session now")
        load.clicked.connect(self.load_session)
        h2 = QHBoxLayout()
        h2.addWidget(sw["restore_session"], 1)
        h2.addWidget(load)
        form.addRow("Session", h2)
        sw["dark"] = QCheckBox("Dark theme")
        sw["dark"].setChecked(bool(s["dark"]))
        form.addRow("Appearance", sw["dark"])
        for w in sw.values():
            sig = (w.textChanged if isinstance(w, QLineEdit) else
                   w.toggled if isinstance(w, QCheckBox) else w.valueChanged)
            sig.connect(lambda *_: self.on_setting_changed())
        connect_filters(self.sfw, lambda *_: self.on_setting_changed())
        self.on_setting_changed()
        return page

    def _build_help_page(self):
        from PyQt5.QtWidgets import QTextBrowser
        b = QTextBrowser()
        b.setOpenExternalLinks(False)
        banner = (f'<div align="center"><img src="data:image/png;base64,{ICON_B64}" width="120" '
                  f'height="120"><h1 style="margin:4px">LibGen</h1></div>')
        b.setHtml(banner + HELP_HTML)
        return b

    def _build_queue_dock(self):
        dock = QDockWidget("Downloads", self)
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        w = QWidget()
        v = QVBoxLayout(w)
        self.qtable = QTableWidget(0, 4)
        self.qtable.setHorizontalHeaderLabels(["Title", "Status", "Progress", "Destination / message"])
        self.qtable.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.qtable.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.qtable.verticalHeader().setVisible(False)
        self.qtable.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.qtable.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        QShortcut(QKeySequence(Qt.Key_Delete), self.qtable, self.cancel, context=Qt.WidgetShortcut)
        v.addWidget(self.qtable, 1)
        row = QHBoxLayout()
        for text, slot in (("Retry", self.retry), ("Cancel", self.cancel), ("Clear done", self.clear_done),
                           ("Open folder", self.open_folder)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch()
        v.addLayout(row)
        dock.setWidget(w)
        self.addDockWidget(Qt.BottomDockWidgetArea, dock)

    def _shortcuts(self):
        for seq, slot in (("Ctrl+Return", self.search_new_tab), ("Ctrl+Enter", self.search_new_tab),
                          ("Ctrl+L", self.focus_query), ("/", self.focus_query), ("Ctrl+F", self.focus_filter)):
            QShortcut(QKeySequence(seq), self, slot)
        QShortcut(QKeySequence.Close, self, lambda: self.close_tab(self.tabs.currentIndex()))
        self.btn_dl.setShortcut("Ctrl+D")
        self.btn_dl_to.setShortcut("Ctrl+Shift+D")

    def focus_query(self):
        self.sidebar.setCurrentRow(0)
        self.query.setFocus()
        self.query.selectAll()

    def focus_filter(self):
        tab = self.tabs.currentWidget()
        if tab:
            tab.filter.setFocus()

    # ---- settings
    def on_setting_changed(self):
        sw, f = self.sw, read_filters(self.sfw)
        self.settings.update(
            download_dir=sw["download_dir"].text().strip() or DOWNLOAD_DIR,
            attempts=sw["attempts"].value(), request_timeout=sw["request_timeout"].value(),
            search_timeout=sw["search_timeout"].value(), search_retries=sw["search_retries"].value(),
            rename=sw["rename"].isChecked(), scheme=sw["scheme"].text().strip() or DEFAULTS["scheme"],
            mirror=sw["mirror"].text().strip(), restore_session=sw["restore_session"].isChecked(),
            show_covers=sw["show_covers"].isChecked(), skip_downloaded=sw["skip_downloaded"].isChecked(),
            dark=sw["dark"].isChecked(),
            ext=f["ext"], lang=f["lang"], min_size=f["min_size"], max_size=f["max_size"],
            limit=f["limit"], topics=f["topics"], fields=f["fields"],
        )
        save_settings(self.settings)
        apply_theme(QApplication.instance(), self.settings["dark"])
        self.preview.setText(format_name(self.settings["scheme"], SAMPLE) + ".epub")

    def browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Default download folder", self.settings["download_dir"])
        if d:
            self.sw["download_dir"].setText(d)

    # ---- tabs & search
    def new_tab(self, title="New tab"):
        tab = ResultsTab(self)
        self.tabs.setCurrentIndex(self.tabs.addTab(tab, title))
        return tab

    def close_tab(self, i):
        if i >= 0:
            self.tabs.removeTab(i)  # not deleted: a late search callback fills a detached widget harmlessly
            self.update_selection_count()
            self.save_session()

    def search_here(self):
        q = self.query.text().strip()
        if q:
            self.run_search(self.tabs.currentWidget() or self.new_tab(), q)

    def search_new_tab(self):
        q = self.query.text().strip()
        if q:
            self.run_search(self.new_tab(), q)

    def run_search(self, tab, q, then=None):
        f, s = read_filters(self.fw), dict(self.settings)
        tab.query = q
        self.tabs.setTabText(self.tabs.indexOf(tab), q[:32])
        tab.model.setRowCount(0)
        tab.status.setText(f"Searching “{q}”…")

        def job():
            entries, mirror, errors, _ = lgsearch.search_all(
                q, f["topics"], mirrors_for(s), s["search_timeout"], max(1, s["search_retries"]), False,
                fields=f["fields"], want=f["limit"], page=f["page"], ext=f["ext"], lang=f["lang"],
                min_size=f["min_size"], max_size=f["max_size"])
            if entries is None:
                raise Exception("every mirror failed: " + "; ".join(errors)[:400])
            return entries, mirror

        self.spawn(job, functools.partial(self.on_search_done, tab, q, then))

    def on_search_done(self, tab, q, then, r):
        if tab.query == q:  # stale guard: the tab was re-searched meanwhile
            if isinstance(r, Exception):
                tab.status.setText(f"Search failed — {r}")
            else:
                tab.set_entries(*r)
            self.save_session()
        if then:
            then()

    def open_search_list(self):
        path, _ = QFileDialog.getOpenFileName(self, "Search list (one query per line)", APP_DIR,
                                              "Text files (*.txt);;All files (*)")
        if not path:
            return
        with open(path, encoding="utf-8") as fh:
            self.search_queue = [l.strip() for l in fh if l.strip() and not l.startswith("#")]
        self.list_total = len(self.search_queue)
        self._next_in_list()

    def _next_in_list(self):
        if not self.search_queue:
            self.statusBar().showMessage(f"Search list done ({self.list_total} queries)", 5000)
            return
        q = self.search_queue.pop(0)
        self.statusBar().showMessage(f"Search list {self.list_total - len(self.search_queue)}/{self.list_total}: {q}")
        self.run_search(self.new_tab(q[:32]), q, then=self._next_in_list)

    def show_details(self, e):
        s = dict(self.settings)

        def job():
            payload, mirror, errors = lgsearch.get_details(e["md5"], mirrors_for(s), s["search_timeout"], False)
            if payload is None:
                raise Exception("; ".join(errors)[:400])
            return payload, mirror

        self.spawn(job, functools.partial(self._details_dialog, e))

    def _details_dialog(self, e, r):
        """Opens as soon as the record arrives; the cover is fetched separately and dropped in when it lands."""
        from PyQt5.QtWidgets import QTabWidget, QTreeWidget
        d = QDialog(self)
        d.setAttribute(Qt.WA_DeleteOnClose)
        d.setWindowTitle(e["title"][:80])
        d.resize(780, 640)
        h = QHBoxLayout(d)
        if isinstance(r, Exception):
            t = QPlainTextEdit()
            t.setReadOnly(True)
            t.setPlainText(f"Could not fetch details: {r}")
            h.addWidget(t, 1)
            d.show()
            return
        payload, mirror = r
        rec = payload
        if isinstance(payload, dict) and len(payload) == 1 and isinstance(next(iter(payload.values())), dict):
            rec = next(iter(payload.values()))  # json.php wraps the record as {file_id: {...}}
        if self.settings["show_covers"]:
            img = QLabel("loading\ncover…")
            img.setObjectName("cover")
            img.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
            img.setFixedWidth(260)
            h.addWidget(img)
            t_out = self.settings["search_timeout"]
            self.spawn(lambda: fetch_cover(e, mirror or mirrors_for(self.settings)[0], t_out),
                       functools.partial(self._set_cover, img))
        tabs = QTabWidget()
        tree = QTreeWidget()
        tree.setColumnCount(2)
        tree.setHeaderLabels(["Field", "Value"])
        fill_tree(tree.invisibleRootItem(), rec)
        tree.expandAll()
        tree.resizeColumnToContents(0)
        tabs.addTab(tree, "Details")
        raw = QPlainTextEdit()
        raw.setReadOnly(True)
        raw.setPlainText(json.dumps(payload, indent=2, ensure_ascii=False))
        tabs.addTab(raw, "Raw JSON")
        h.addWidget(tabs, 1)
        d.show()

    def _set_cover(self, img, r):
        try:
            pix = QPixmap()
            if not isinstance(r, Exception) and r and pix.loadFromData(r):
                img.setPixmap(pix.scaledToWidth(min(pix.width(), 250), Qt.SmoothTransformation))
            else:
                img.setText("no cover")
        except RuntimeError:
            pass  # the dialog was closed before the cover arrived

    # ---- threads
    def spawn(self, fn, cb):
        def run():
            try:
                r = fn()
            except Exception as e:  # noqa: BLE001 - delivered to the callback as the result
                r = e
            self.job_done.emit(cb, r)
        threading.Thread(target=run, daemon=True).start()

    def _on_job(self, cb, r):
        cb(r)

    def _dl_loop(self):
        while True:
            it = self.dl_queue.get()
            if it is None:
                return
            if it["cancel"].is_set():
                self.dl_changed.emit(it["id"], "cancelled", "", 0)
                continue
            if self.settings["skip_downloaded"] and already_downloaded(it["entry"]["md5"], it["dest"]):
                self.dl_changed.emit(it["id"], "skipped", "already in this folder's downloaded.txt", 0)
                continue
            self.dl_changed.emit(it["id"], "downloading", "", 0)
            last = [-2]

            def progress(done, total, it=it, last=last):
                pct = done * 100 // total if total else -1
                tick = pct if total else done // (1024 * 1024)
                if tick != last[0]:  # throttle: one signal per percent (or per MiB when the size is unknown)
                    last[0] = tick
                    self.dl_changed.emit(it["id"], "downloading", f"{done / 1048576:.1f} MB", pct)

            try:
                part, name = download_one(it["entry"], it["dest"], self.settings, progress, it["cancel"])
                path = finish_download(part, name, it["entry"], it["dest"], self.settings)
                self.dl_changed.emit(it["id"], "done", path, 100)
            except Cancelled:
                self.dl_changed.emit(it["id"], "cancelled", "", 0)
            except Exception as e:  # noqa: BLE001
                self.dl_changed.emit(it["id"], "failed", str(e), 0)

    # ---- download queue
    def checked_all(self):
        seen, out = set(), []
        for i in range(self.tabs.count()):
            for e in self.tabs.widget(i).checked_entries():
                if e["md5"] not in seen:
                    seen.add(e["md5"])
                    out.append(e)
        return out

    def update_selection_count(self):
        n = len(self.checked_all())
        self.btn_dl.setText(f"Download ({n})")
        self.btn_dl.setEnabled(n > 0)
        self.btn_dl_to.setEnabled(n > 0)

    def enqueue(self, entries, ask=False, dest=None):
        """Append to the queue (never replaces); MD5s already queued/downloading are skipped."""
        if not entries:
            return
        if ask:
            dest = QFileDialog.getExistingDirectory(self, "Download to", dest or self.settings["download_dir"])
            if not dest:
                return
        dest = dest or self.settings["download_dir"]
        active = {it["entry"]["md5"] for it in self.items if it["status"] in ("queued", "downloading")}
        for e in entries:
            if e["md5"] in active:
                continue
            active.add(e["md5"])
            self.next_id += 1
            it = {"id": self.next_id, "entry": e, "dest": dest, "status": "queued", "note": "", "pct": 0,
                  "cancel": threading.Event()}
            self.items.append(it)
            self.dl_queue.put(it)
        self.refresh_queue()
        self.save_session()

    def on_dl_changed(self, id_, status, note, pct):
        it = next((i for i in self.items if i["id"] == id_), None)
        if it is None:
            return
        if status == "downloading" and it["status"] == "downloading":  # progress tick only
            it["pct"], it["bytes"] = pct, note
            self.qtable.item(self.items.index(it), 2).setText(self.prog_text(it))
            self._dl_status()
            return
        it.update(status=status, note=note, pct=pct)
        self.refresh_queue()
        self.save_session()

    @staticmethod
    def prog_text(it):
        if it["status"] == "done":
            return "100%"
        if it["status"] == "downloading":
            return f"{it['pct']}%" if it["pct"] >= 0 else it.get("bytes", "…")
        return ""

    def refresh_queue(self):
        # ponytail: full rebuild on every status change, O(n) row lookup; fine for a queue of dozens
        self.qtable.setRowCount(len(self.items))
        for r, it in enumerate(self.items):
            cells = (it["entry"]["title"], it["status"], self.prog_text(it), it["note"] or it["dest"])
            for c, text in enumerate(cells):
                self.qtable.setItem(r, c, QTableWidgetItem(text))
        self._dl_status()

    def _dl_status(self):
        live = [it for it in self.items if it["status"] != "cancelled"]
        cur = next((it for it in self.items if it["status"] == "downloading"), None)
        finished = sum(it["status"] in ("done", "failed", "skipped") for it in live)
        if cur:
            self.dl_label.setText(f"Downloading {finished + 1}/{len(live)} · {self.prog_text(cur)}")
        elif live:
            self.dl_label.setText(f"Downloads: {finished}/{len(live)} finished")
        else:
            self.dl_label.setText("")

    def selected_items(self):
        rows = {i.row() for i in self.qtable.selectionModel().selectedRows()}
        return [self.items[r] for r in sorted(rows) if r < len(self.items)]

    def retry(self):
        sel = self.selected_items() or self.items
        for it in sel:
            if it["status"] in ("failed", "cancelled", "skipped"):
                it.update(status="queued", note="", pct=0, cancel=threading.Event())
                self.dl_queue.put(it)
        self.refresh_queue()
        self.save_session()

    def cancel(self):
        sel = self.selected_items() or [it for it in self.items if it["status"] == "queued"]
        for it in sel:
            if it["status"] in ("queued", "downloading"):
                it["cancel"].set()
                if it["status"] == "queued":
                    it["status"] = "cancelled"
        self.refresh_queue()
        self.save_session()

    def clear_done(self):
        self.items = [it for it in self.items if it["status"] not in ("done", "cancelled", "skipped")]
        self.refresh_queue()
        self.save_session()

    def open_folder(self):
        sel = self.selected_items()
        open_external(sel[0]["dest"] if sel else self.settings["download_dir"])

    # ---- session
    def save_session(self):
        if "--smoke" in sys.argv:
            return
        tabs = [{"query": t.query, "mirror": t.mirror, "entries": t.entries,
                 "checked": [e["md5"] for e in t.checked_entries()]}
                for t in (self.tabs.widget(i) for i in range(self.tabs.count())) if t.entries]
        items = [{"entry": it["entry"], "dest": it["dest"], "status": it["status"], "note": it["note"]}
                 for it in self.items]
        try:
            with open(SESSION_PATH, "w", encoding="utf-8") as f:
                json.dump({"tabs": tabs, "queue": items}, f)
        except OSError:
            pass

    def load_session(self):
        try:
            with open(SESSION_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            self.statusBar().showMessage("No previous session found", 4000)
            return
        for t in data.get("tabs", []):
            tab = self.new_tab(t["query"][:32])
            tab.query = t["query"]
            tab.set_entries(t["entries"], t["mirror"], set(t.get("checked", [])))
        for it in data.get("queue", []):
            if it["status"] in ("queued", "downloading"):  # interrupted last time: resume
                self.enqueue([it["entry"]], dest=it["dest"])
            else:
                self.next_id += 1
                self.items.append({"id": self.next_id, "entry": it["entry"], "dest": it["dest"],
                                   "status": it["status"], "note": it["note"],
                                   "pct": 100 if it["status"] == "done" else 0, "cancel": threading.Event()})
        self.refresh_queue()
        self.update_selection_count()
        self.statusBar().showMessage(f"Session loaded: {len(data.get('tabs', []))} tabs, "
                                     f"{len(data.get('queue', []))} queue items", 4000)

    def closeEvent(self, ev):
        if any(it["status"] == "downloading" for it in self.items) and QMessageBox.question(
                self, "Download in progress", "A download is still running. Quit anyway?\n"
                "(It will be re-queued next time the session is loaded.)") != QMessageBox.Yes:
            ev.ignore()
            return
        self.save_session()
        for it in self.items:
            it["cancel"].set()
        self.dl_queue.put(None)
        self.dl_thread.join(3)
        ev.accept()


# ------------------------------------------------------------------ headless smoke test
def smoke(app, win):
    import tempfile

    def wait(cond, secs):
        t0 = time.time()
        while not cond() and time.time() - t0 < secs:
            app.processEvents()
            time.sleep(0.05)
        return cond()

    win.settings.update(rename=True, scheme=DEFAULTS["scheme"])
    win.query.setText("the art of war")
    win.fw["limit"].setValue(5)
    win.search_here()
    tab = win.tabs.currentWidget()
    assert wait(lambda: tab.model.rowCount() > 0 or "failed" in tab.status.text(), 400), "search timed out"
    assert tab.model.rowCount() > 0, tab.status.text()
    print("search OK:", tab.status.text())
    tab.on_header(C_CHECK)
    assert len(win.checked_all()) == tab.model.rowCount() and win.btn_dl.isEnabled()
    tab.filter.setText("zzzzqqqq")
    assert tab.proxy.rowCount() == 0
    tab.filter.clear()
    tab.on_header(C_SIZE)
    keys = [tab.proxy.index(r, C_SIZE).data(Qt.UserRole) for r in range(tab.proxy.rowCount())]
    assert keys == sorted(keys), keys
    tab.on_header(C_CHECK)
    assert not win.checked_all() and win.btn_dl.text() == "Download (0)"
    print("table OK: check-all, filter, size sort")
    entry = {"md5": "85f25926bfb30163ad5ad45af0f235e3", "title": "The Adventures of Sherlock Holmes",
             "author": "Conan Doyle, Arthur", "publisher": "Otbebookpublishing", "year": "2015",
             "language": "English", "pages": "0", "size": "294 kB", "extension": "epub", "href": ""}
    d = tempfile.mkdtemp()
    win.enqueue([entry], dest=d)
    it = win.items[-1]
    assert wait(lambda: it["status"] in ("done", "failed"), 300), "download timed out"
    assert it["status"] == "done", it["note"]
    assert os.path.basename(it["note"]) == "The Adventures of Sherlock Holmes (2015).epub", it["note"]
    with open(it["note"], "rb") as f:
        assert hashlib.md5(f.read()).hexdigest() == entry["md5"]
    assert not os.path.exists(os.path.join(d, entry["md5"] + ".part"))
    print("download OK:", it["note"], os.path.getsize(it["note"]), "bytes")
    win.settings["show_covers"] = True
    win.show_details(entry)
    assert wait(lambda: win.findChild(QDialog) is not None, 120), "details timed out"
    dlg = win.findChild(QDialog)
    cover = dlg.findChild(QLabel, "cover")
    print("details OK:", "cover shown" if cover and not cover.pixmap().isNull() else "no cover",
          "-", dlg.findChild(QPlainTextEdit).toPlainText()[:60].replace("\n", " "))
    win.close()
    print("smoke OK")
    return 0


def apply_theme(app, dark):
    """Fusion + a dark QPalette; every widget follows it, no stylesheet needed."""
    if not dark:
        app.setPalette(app.light_palette)
        return
    p = QPalette()
    for role, c in ((QPalette.Window, "#2b2b2b"), (QPalette.WindowText, "#e6e6e6"), (QPalette.Base, "#383838"),
                    (QPalette.AlternateBase, "#333333"), (QPalette.ToolTipBase, "#2b2b2b"), (QPalette.ToolTipText, "#e6e6e6"),
                    (QPalette.Text, "#e6e6e6"), (QPalette.Button, "#353535"), (QPalette.ButtonText, "#e6e6e6"),
                    (QPalette.Link, "#5aa0f0"), (QPalette.Highlight, "#2f6fd0"), (QPalette.HighlightedText, "#ffffff")):
        p.setColor(role, QColor(c))
    p.setColor(QPalette.Disabled, QPalette.Text, QColor("#808080"))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#808080"))
    app.setPalette(p)


def main():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.light_palette = app.palette()
    apply_theme(app, load_settings()["dark"])
    app.setWindowIcon(app_icon())
    win = MainWindow()
    win.show()
    if "--smoke" in sys.argv:
        return smoke(app, win)
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
