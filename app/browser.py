"""Vexon browser: tabs, address bar, bookmarks and browsing history.

pywebview gives every window exactly one web view, so the browser is split in two:

* the *shell* (ui/browser.html) is that one web view. It draws the tab strip, toolbar and
  bookmarks bar and talks to Python through the normal pywebview js_api;
* every *tab* is an extra Microsoft Edge WebView2 (Chromium) control that this module adds
  to the same WinForms window through pythonnet, right below the shell.

Internal pages (New Tab, History, Bookmarks and the original Vexon dashboard) are served
from ui/ under a private virtual host, so they behave like real https pages. Only those pages
get the small bridge that lets them call the same Python API as the shell.

Everything that needs Windows/.NET is imported lazily, so the URL helpers and BrowserStore
can be imported and tested anywhere.
"""

from __future__ import annotations

import functools
import ipaddress
import itertools
import json
import math
import re
import secrets
import sqlite3
import threading
import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus, urlparse

INTERNAL_HOST = "vexon.example"           # .example is reserved, so it can never clash with a real site
INTERNAL_BASE = f"https://{INTERNAL_HOST}/"
INTERNAL_PAGES = {                            # vexon://<name> -> file in ui/
    "newtab": "newtab.html",
    "history": "history.html",
    "bookmarks": "bookmarks.html",
    "app": "index.html",                      # the original Vexon dashboard
}
NEW_TAB = "vexon://newtab"

# Cookies Google sets only for a signed-in account; their presence means "signed in".
GOOGLE_SESSION_COOKIES = {"SID", "__Secure-1PSID", "__Secure-3PSID"}
GOOGLE_SIGN_IN = "https://accounts.google.com/ServiceLogin?service=mail&continue=https://mail.google.com/mail/"

SEARCH_ENGINES = {
    "google": ("Google", "https://www.google.com/search?q=%s"),
    "duckduckgo": ("DuckDuckGo", "https://duckduckgo.com/?q=%s"),
    "bing": ("Bing", "https://www.bing.com/search?q=%s"),
    "brave": ("Brave Search", "https://search.brave.com/search?q=%s"),
}

# Last labels that make "word.word" look like a website rather than a file name ("notes.txt").
COMMON_TLDS = {
    "com", "org", "net", "edu", "gov", "mil", "int", "io", "ai", "app", "dev", "co", "info", "biz",
    "xyz", "online", "site", "tech", "shop", "store", "blog", "cloud", "page", "news", "wiki",
    "tv", "me", "us", "uk", "in", "de", "fr", "jp", "cn", "ru", "br", "au", "ca", "nl", "es",
}


# --------------------------------------------------------------------------- URL helpers

def internal_url(name: str) -> str:
    """vexon://newtab -> the real https URL the page is served from."""
    return INTERNAL_BASE + INTERNAL_PAGES[name]


def internal_name(url: str) -> str | None:
    """Reverse of internal_url(): the page name for a URL served from ui/, else None."""
    if not url.startswith(INTERNAL_BASE):
        return None
    page = urlparse(url).path.lstrip("/")
    for name, filename in INTERNAL_PAGES.items():
        if page == filename:
            return name
    return None


def is_internal(url: str) -> bool:
    return url.startswith(INTERNAL_BASE)


def search_url(query: str, engine: str = "google") -> str:
    template = SEARCH_ENGINES.get(engine, SEARCH_ENGINES["google"])[1]
    return template.replace("%s", quote_plus(query))


def _looks_like_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        pass
    if "." not in host:
        return False
    labels = host.split(".")
    if not all(re.fullmatch(r"[a-z0-9_]([a-z0-9_-]*[a-z0-9_])?", p, re.IGNORECASE) for p in labels):
        return False
    tld = labels[-1].lower()
    return tld.startswith("xn--") or tld in COMMON_TLDS or (len(tld) == 2 and tld.isalpha())


def normalize_input(text: str, engine: str = "google") -> str | None:
    """Turn whatever was typed in the address bar into a URL to load (None if it is empty)."""
    text = (text or "").strip()
    if not text:
        return None
    lower = text.lower()
    if lower.startswith("vexon://"):
        name = lower[len("vexon://"):].strip("/ ")
        return internal_url(name) if name in INTERNAL_PAGES else search_url(text, engine)
    if lower == "about:blank":
        return "about:blank"
    if re.match(r"^(https?|file)://", lower):
        return text
    # "host", "host:port", "host/path", "host?query" with no spaces: a site if the host looks like one.
    m = re.fullmatch(r"(\[[0-9a-f:]+\]|[^\s:/?#@\[\]]+)(:\d{1,5})?([/?#]\S*)?", text, re.IGNORECASE)
    if m and _looks_like_host(m.group(1)):
        host = m.group(1)
        local = host.lower() == "localhost" or re.fullmatch(r"[\d.]+|\[[0-9a-f:]+\]", host, re.IGNORECASE)
        return ("http://" if local else "https://") + text
    # Everything else, including javascript:, data: and mailto:, is a search, never navigated to.
    return search_url(text, engine)


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def display_url(url: str) -> str:
    """What the address bar shows when it is not being edited."""
    name = internal_name(url)
    if name is not None:
        return "" if name == "newtab" else f"vexon://{name}"
    if url == "about:blank":
        return ""
    shown = re.sub(r"^https?://", "", url)
    shown = shown.removeprefix("www.") if url.startswith(("http://www.", "https://www.")) else shown
    if shown.endswith("/") and shown.count("/") == 1:
        shown = shown[:-1]
    return shown


def edit_url(url: str) -> str:
    """What the address bar contains once you click into it."""
    name = internal_name(url)
    if name is not None:
        return "" if name == "newtab" else f"vexon://{name}"
    return "" if url == "about:blank" else url


# --------------------------------------------------------------------------- storage

class BrowserStore:
    """Bookmarks and browsing history, in browser.db next to Vexon's dictation history."""

    def __init__(self, path: Path | str):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """CREATE TABLE IF NOT EXISTS bookmarks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL UNIQUE,
                title TEXT,
                icon TEXT,
                created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS visits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                host TEXT,
                title TEXT,
                icon TEXT,
                visited TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS visits_url ON visits(url);
            CREATE INDEX IF NOT EXISTS visits_host ON visits(host);"""
        )
        self.db.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    # -- bookmarks
    def bookmarks(self, search: str = "") -> list[dict]:
        with self.lock:
            if search:
                like = f"%{search}%"
                rows = self.db.execute(
                    "SELECT * FROM bookmarks WHERE title LIKE ? OR url LIKE ? ORDER BY id", (like, like))
            else:
                rows = self.db.execute("SELECT * FROM bookmarks ORDER BY id")
            return [dict(r) for r in rows.fetchall()]

    def bookmark_for(self, url: str) -> dict | None:
        with self.lock:
            r = self.db.execute("SELECT * FROM bookmarks WHERE url = ?", (url,)).fetchone()
        return dict(r) if r else None

    def add_bookmark(self, url: str, title: str = "", icon: str = "") -> dict:
        title = (title or "").strip() or display_url(url) or url
        with self.lock:
            self.db.execute(
                "INSERT INTO bookmarks (url, title, icon, created) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(url) DO UPDATE SET title = excluded.title, icon = excluded.icon",
                (url, title, icon or "", self._now()))
            self.db.commit()
            r = self.db.execute("SELECT * FROM bookmarks WHERE url = ?", (url,)).fetchone()
        return dict(r)

    def remove_bookmark(self, bookmark_id: int):
        with self.lock:
            self.db.execute("DELETE FROM bookmarks WHERE id = ?", (int(bookmark_id),))
            self.db.commit()

    def remove_bookmark_url(self, url: str):
        with self.lock:
            self.db.execute("DELETE FROM bookmarks WHERE url = ?", (url,))
            self.db.commit()

    def rename_bookmark(self, bookmark_id: int, title: str):
        with self.lock:
            self.db.execute("UPDATE bookmarks SET title = ? WHERE id = ?", (title.strip(), int(bookmark_id)))
            self.db.commit()

    # -- history
    def add_visit(self, url: str, title: str = "", icon: str = "") -> int:
        """Record a page load. A reload of the same page within a few seconds counts once."""
        with self.lock:
            last = self.db.execute("SELECT id, visited FROM visits WHERE url = ? ORDER BY id DESC LIMIT 1",
                                   (url,)).fetchone()
            if last and (datetime.now() - datetime.fromisoformat(last["visited"])).total_seconds() < 3:
                self.db.execute("UPDATE visits SET title = COALESCE(NULLIF(?, ''), title), "
                                "icon = COALESCE(NULLIF(?, ''), icon) WHERE id = ?",
                                (title, icon, last["id"]))
                self.db.commit()
                return last["id"]
            cur = self.db.execute(
                "INSERT INTO visits (url, host, title, icon, visited) VALUES (?, ?, ?, ?, ?)",
                (url, host_of(url), title or "", icon or "", self._now()))
            self.db.commit()
            return cur.lastrowid

    def update_visit(self, visit_id: int, title: str | None = None, icon: str | None = None):
        with self.lock:
            if title is not None:
                self.db.execute("UPDATE visits SET title = ? WHERE id = ?", (title, visit_id))
            if icon:
                self.db.execute("UPDATE visits SET icon = ? WHERE id = ?", (icon, visit_id))
            self.db.commit()

    def history(self, search: str = "", limit: int = 300) -> list[dict]:
        with self.lock:
            if search:
                like = f"%{search}%"
                rows = self.db.execute(
                    "SELECT * FROM visits WHERE title LIKE ? OR url LIKE ? ORDER BY id DESC LIMIT ?",
                    (like, like, limit))
            else:
                rows = self.db.execute("SELECT * FROM visits ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(r) for r in rows.fetchall()]

    def delete_visit(self, visit_id: int):
        with self.lock:
            self.db.execute("DELETE FROM visits WHERE id = ?", (int(visit_id),))
            self.db.commit()

    def clear_history(self):
        with self.lock:
            self.db.execute("DELETE FROM visits")
            self.db.commit()

    def top_sites(self, limit: int = 10) -> list[dict]:
        """Most visited sites (one per host), newest title/icon of each."""
        with self.lock:
            rows = self.db.execute(
                "SELECT v.url, v.host, v.title, v.icon, t.n FROM visits v "
                "JOIN (SELECT MAX(id) AS mid, COUNT(*) AS n FROM visits "
                "      WHERE host != '' GROUP BY host) t ON v.id = t.mid "
                "ORDER BY t.n DESC, v.id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            scheme = urlparse(r["url"]).scheme or "https"
            name = r["host"].removeprefix("www.")
            out.append({"url": f"{scheme}://{r['host']}/", "title": name, "icon": r["icon"], "visits": r["n"]})
        return out

    def suggest(self, text: str, limit: int = 6) -> list[dict]:
        """Address-bar suggestions: bookmarks first, then the most relevant history."""
        text = text.strip()
        if not text:
            return []
        like = f"%{text}%"
        with self.lock:
            marks = self.db.execute(
                "SELECT url, title, icon FROM bookmarks WHERE title LIKE ? OR url LIKE ? LIMIT ?",
                (like, like, limit)).fetchall()
            pages = self.db.execute(
                "SELECT v.url, v.title, v.icon, t.n FROM visits v "
                "JOIN (SELECT MAX(id) AS mid, COUNT(*) AS n FROM visits "
                "      WHERE title LIKE ? OR url LIKE ? GROUP BY url) t ON v.id = t.mid "
                "ORDER BY t.n DESC, v.id DESC LIMIT ?", (like, like, limit)).fetchall()
        out, seen = [], set()
        for kind, rows in (("bookmark", marks), ("history", pages)):
            for r in rows:
                if r["url"] not in seen and len(out) < limit:
                    seen.add(r["url"])
                    out.append({"kind": kind, "url": r["url"], "title": r["title"] or display_url(r["url"]),
                                "icon": r["icon"]})
        return out


# --------------------------------------------------------------------------- tabs

def log_error(where: str):
    print(f"[browser] error in {where}:\n{traceback.format_exc()}")


def guarded(fn):
    """Event handlers run on the WinForms UI thread: an exception there would take the app down."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            log_error(fn.__name__)
    return wrapper


# Injected into every page of every tab, before the page's own scripts run.
#  - keyboard shortcuts (Ctrl+T, Ctrl+W, Ctrl+L ...) are forwarded to Python from all pages.
#    The message carries a per-session secret that only this script knows (and it keeps its own
#    reference to postMessage), so a website cannot forge "open a tab" commands;
#  - pages served from ui/ additionally get window.pywebview.api, so the original Vexon
#    dashboard (which was written against pywebview) works unchanged inside a tab.
#    The API is never exposed to a real website: the page must be the top frame *and* have the
#    private origin, and Python checks the sender again before running anything.
TAB_SCRIPT = r"""(function () {
__KEYS__
  var wv = window.chrome && window.chrome.webview;
  if (!wv) return;
  var send = wv.postMessage.bind(wv), listen = wv.addEventListener.bind(wv);
  var post = function (m) { try { send(m); } catch (e) {} };
  addEventListener('keydown', function (e) {
    var cmd = e.isTrusted && vtKeyCommand(e);
    if (cmd) { e.preventDefault(); e.stopPropagation(); post({t: 'key', cmd: cmd, k: '__TOKEN__'}); }
  }, true);
  if (window !== window.top || location.origin !== '__ORIGIN__') return;
  var pending = {}, seq = 0;
  listen('message', function (e) {
    var m = e.data;
    if (!m) return;
    if (m.t === 'ret') {
      var p = pending[m.id]; delete pending[m.id];
      if (p) { if (m.err) p.rej(new Error(m.err)); else p.res(m.val); }
    } else if (m.t === 'event' && typeof window.onEngineEvent === 'function') {
      window.onEngineEvent(m.event, m.data);
    }
  });
  window.pywebview = { api: new Proxy({}, { get: function (_, name) {
    if (typeof name !== 'string' || name === 'then') return undefined;
    return function () {
      var args = Array.prototype.slice.call(arguments);
      return new Promise(function (res, rej) {
        var id = ++seq; pending[id] = {res: res, rej: rej};
        post({t: 'call', id: id, name: name, args: args});
      });
    };
  } }) };
  addEventListener('DOMContentLoaded', function () { window.dispatchEvent(new Event('pywebviewready')); });
})();"""


class Tab:
    __slots__ = ("id", "url", "title", "favicon", "loading", "failed", "back", "forward", "view", "core",
                 "pending", "visit_id", "visit_url")

    def __init__(self, tab_id: int, url: str):
        self.id = tab_id
        self.url = url              # what is loaded (or about to be)
        self.title = ""
        self.favicon = ""
        self.loading = True
        self.failed = False         # the last navigation ended in an error page
        self.back = False
        self.forward = False
        self.view = None            # the WebView2 control
        self.core = None            # its CoreWebView2, once initialised
        self.pending = url          # navigate here as soon as the engine is ready
        self.visit_id = None
        self.visit_url = ""


class Browser:
    """Owns the tabs. All WinForms/WebView2 work happens on the UI thread; the public methods
    can be called from any thread (js_api calls arrive on worker threads)."""

    STRIP_DEFAULT = 112             # CSS px; the shell reports the real height

    def __init__(self, engine, ui_dir: Path | str, data_dir: Path | str):
        self.engine = engine
        self.ui_dir = Path(ui_dir)
        self.store = BrowserStore(Path(data_dir) / "browser.db")
        self.api = None             # the js_api object internal pages may call (set by main.py)
        self.tabs: list[Tab] = []
        self.active: Tab | None = None
        self.closed_urls: list[str] = []
        self._ids = itertools.count(1)
        self.window = None
        self.form = None
        self.shell = None           # WebView2 control that shows ui/browser.html
        self.panel = None           # container for the tab controls
        self.env = None
        self.chrome_h = self.STRIP_DEFAULT
        self.popups: list[list[int]] = []
        self.fullscreen = False
        self._saved_frame = None
        self._dirty_pending = False
        keys = (self.ui_dir / "keys.js").read_text(encoding="utf-8")
        self._token = secrets.token_hex(16)
        self._script = (TAB_SCRIPT.replace("__KEYS__", keys).replace("__TOKEN__", self._token)
                        .replace("__ORIGIN__", INTERNAL_BASE.rstrip("/")))

    # ------------------------------------------------------------------ plumbing
    def attach(self, window):
        """Call once, right after webview.create_window()."""
        self.window = window
        window.events.loaded += self._on_shell_loaded

    def _on_shell_loaded(self):
        form = self.window.native
        if self.form is not None or form is None:
            return
        self.form = form
        self._ui(self._init_ui, wait=True)

    def _ui(self, fn, *args, wait: bool = False):
        form = self.form
        if form is None:                  # before the window exists (engine events at startup)
            return None
        from System import Action

        def run():
            try:
                return fn(*args)
            except Exception:
                log_error(getattr(fn, "__name__", "ui call"))

        if not form.InvokeRequired:
            return run()
        if wait:
            box = []
            form.Invoke(Action(lambda: box.append(run())))
            return box[0] if box else None
        form.BeginInvoke(Action(run))
        return None

    def _later(self, fn, *args):
        """Queue fn on the UI thread even when already on it, i.e. after the current WebView2
        event handler has returned (a handler must not dispose the control that raised it)."""
        from System import Action

        def run():
            try:
                fn(*args)
            except Exception:
                log_error(getattr(fn, "__name__", "queued call"))
        self.form.BeginInvoke(Action(run))

    @property
    def _scale(self) -> float:
        try:
            return self.form._scale
        except Exception:
            return 1.0

    def _search_engine(self) -> str:
        return self.engine.cfg.get("search_engine") or "google"

    def shutdown(self):
        """Close every web view and wait (up to 3 s) for WebView2's browser process to exit, so
        cookies and logins are flushed and the profile folder is released before os._exit()."""
        from System.Diagnostics import Process

        pid = self._ui(self._dispose_views, wait=True)
        if pid:
            try:
                Process.GetProcessById(pid).WaitForExit(3000)
            except Exception:
                pass

    def _dispose_views(self):
        pid = None
        try:
            pid = int(self.shell.CoreWebView2.BrowserProcessId)
        except Exception:
            pass
        for tab in self.tabs:
            if tab.view is not None:
                tab.view.Dispose()
        self.tabs.clear()
        self.active = None
        if self.shell is not None:
            self.shell.Dispose()
        return pid

    # ------------------------------------------------------------------ window / layout
    def _init_ui(self):
        from System.Windows.Forms import DockStyle, Panel

        form = self.form
        self.shell = form.webview
        self.env = self.shell.CoreWebView2.Environment
        no_dock = getattr(DockStyle, "None")

        # The shell is the one web view pywebview made; it only ever shows our own page.
        origin = re.match(r"^https?://[^/]+", str(self.shell.CoreWebView2.Source))
        self._shell_origin = origin.group(0) if origin else ""
        self.shell.CoreWebView2.NavigationStarting += self._on_shell_navigation
        self.shell.AllowExternalDrop = False
        s = self.shell.CoreWebView2.Settings
        s.AreBrowserAcceleratorKeysEnabled = False      # F5/Ctrl+R must not reload the toolbar
        s.AreDefaultContextMenusEnabled = True          # Cut/Copy/Paste in the address bar (browser.html
        s.IsZoomControlEnabled = False                  # suppresses it everywhere else)
        s.IsSwipeNavigationEnabled = False

        self.panel = Panel()
        self.panel.BackColor = self._background()
        form.Controls.Add(self.panel)
        self.shell.Dock = no_dock
        self.shell.BringToFront()                       # popups drawn by the shell float over the tabs
        form.SizeChanged += self._on_form_resize
        try:
            form.DpiChanged += self._on_form_resize
        except Exception:
            pass
        self._layout()
        if form.Visible:
            self._new_tab(None)
        self._dirty()

    @guarded
    def _on_shell_navigation(self, sender, args):
        if self._shell_origin and not str(args.Uri).startswith(self._shell_origin):
            args.Cancel = True

    @guarded
    def _on_form_resize(self, sender, args):
        self._layout()

    def _background(self):
        from System.Drawing import Color

        try:
            dark = self.form.is_dark_theme()
        except Exception:
            dark = False
        return Color.FromArgb(255, 10, 10, 12) if dark else Color.FromArgb(255, 255, 255, 255)

    def _layout(self):
        if self.form is None or self.panel is None:
            return
        size = self.form.ClientSize
        w, h = size.Width, size.Height
        if w <= 0 or h <= 0:
            return                                       # minimised
        top = 0 if self.fullscreen else int(round(self.chrome_h * self._scale))
        self.shell.Visible = not self.fullscreen
        self.shell.SetBounds(0, 0, w, h)                 # covers the window; the region clips it
        self.panel.SetBounds(0, top, w, max(0, h - top))
        self._apply_region(w, top)

    def _apply_region(self, w: int, top: int):
        from System.Drawing import Rectangle, Region

        s = self._scale
        region = Region(Rectangle(0, 0, w, top))
        for x, y, pw, ph in self.popups:
            region.Union(Rectangle(int(x * s), int(y * s), int(math.ceil(pw * s)), int(math.ceil(ph * s))))
        self.shell.Region = region

    def set_layout(self, chrome_h: int, popups: list):
        """The shell reports the height of its toolbar area and the rectangles of open popups."""
        self.chrome_h = max(40, int(chrome_h))
        self.popups = [[int(v) for v in r] for r in (popups or []) if len(r) == 4]
        self._ui(self._layout)

    # ------------------------------------------------------------------ tabs
    def new_tab(self, url: str | None = None) -> int | None:
        return self._ui(self._new_tab, url, wait=True)

    def ensure_tab(self):
        """The first tab is created when the window is first shown, so Vexon sitting in the
        tray at login does not pay for a web page nobody is looking at."""
        self._ui(lambda: None if self.tabs or self.panel is None else self._new_tab(None))

    def _new_tab(self, url: str | None) -> int:
        from Microsoft.Web.WebView2.WinForms import WebView2
        from System.Windows.Forms import DockStyle

        tab = Tab(next(self._ids), url or internal_url("newtab"))
        view = WebView2()
        view.Dock = DockStyle.Fill
        view.DefaultBackgroundColor = self._background()
        tab.view = view
        view.CoreWebView2InitializationCompleted += lambda s, e: self._on_core_ready(tab, e)
        self.panel.Controls.Add(view)
        self.tabs.append(tab)
        self._activate(tab)
        view.EnsureCoreWebView2Async(self.env)
        return tab.id

    @guarded
    def _on_core_ready(self, tab: Tab, args):
        from Microsoft.Web.WebView2.Core import CoreWebView2HostResourceAccessKind

        if not args.IsSuccess:
            print(f"[browser] WebView2 failed to start: {args.InitializationException}")
            return
        core = tab.view.CoreWebView2
        tab.core = core
        core.SetVirtualHostNameToFolderMapping(INTERNAL_HOST, str(self.ui_dir),
                                               CoreWebView2HostResourceAccessKind.Allow)
        core.AddScriptToExecuteOnDocumentCreatedAsync(self._script)
        core.SourceChanged += lambda s, e: self._on_source(tab)
        core.DocumentTitleChanged += lambda s, e: self._on_title(tab)
        core.FaviconChanged += lambda s, e: self._on_favicon(tab)
        core.NavigationStarting += lambda s, e: self._on_nav_start(tab, e)
        core.NavigationCompleted += lambda s, e: self._on_nav_done(tab, e)
        core.HistoryChanged += lambda s, e: self._on_history_changed(tab)
        core.NewWindowRequested += lambda s, e: self._on_new_window(e)
        core.WindowCloseRequested += lambda s, e: self._later(self._close, tab)
        core.WebMessageReceived += lambda s, e: self._on_message(tab, e)
        core.ContainsFullScreenElementChanged += lambda s, e: self._on_fullscreen(tab)
        target, tab.pending = tab.pending, None
        if target:
            self._load(tab, target)

    def _load(self, tab: Tab, url: str):
        if tab.core is None:
            tab.pending = url
            return
        try:
            tab.core.Navigate(url)
        except Exception:
            # WebView2 refused the string as a URL: treat it as a search.
            tab.core.Navigate(search_url(url, self._search_engine()))

    def _activate(self, tab: Tab):
        previous = self.active
        self.active = tab
        if previous is not None and previous is not tab and previous.view is not None:
            previous.view.Visible = False
        tab.view.Visible = True
        tab.view.BringToFront()
        self.shell.BringToFront()
        if internal_name(tab.url) == "newtab":
            self._focus_address()
        else:
            tab.view.Focus()
        self._update_window_title()
        self._dirty()

    def activate_tab(self, tab_id: int):
        self._ui(lambda: self._activate(self._find(tab_id)) if self._find(tab_id) else None)

    def _find(self, tab_id) -> Tab | None:
        return next((t for t in self.tabs if t.id == int(tab_id)), None)

    def close_tab(self, tab_id: int):
        self._ui(lambda: self._close(self._find(tab_id)))

    def _close(self, tab: Tab | None):
        if tab is None or tab not in self.tabs:
            return
        index = self.tabs.index(tab)
        self.tabs.remove(tab)
        if internal_name(tab.url) != "newtab" and tab.url not in ("", "about:blank"):
            self.closed_urls.append(tab.url)
            del self.closed_urls[:-20]
        was_active = self.active is tab
        if self.fullscreen and was_active:
            self._set_fullscreen(False)
        self.panel.Controls.Remove(tab.view)
        tab.view.Dispose()
        tab.view = tab.core = None
        if not self.tabs:
            self.active = None
            self._new_tab(None)                        # closing the last tab leaves a fresh New Tab
        elif was_active:
            self._activate(self.tabs[min(index, len(self.tabs) - 1)])
        self._dirty()

    def move_tab(self, tab_id: int, index: int):
        def move():
            tab = self._find(tab_id)
            if tab is not None:
                self.tabs.remove(tab)
                self.tabs.insert(max(0, min(int(index), len(self.tabs))), tab)
                self._dirty()
        self._ui(move)

    def _reopen(self):
        if self.closed_urls:
            self._new_tab(self.closed_urls.pop())

    def _open_internal(self, name: str):
        """History / Bookmarks / dashboard: reuse an open tab, or the blank New Tab, or open one."""
        url = internal_url(name)
        existing = next((t for t in self.tabs if internal_name(t.url) == name), None)
        if existing is not None:
            self._activate(existing)
        elif self.active is not None and internal_name(self.active.url) == "newtab":
            self._load(self.active, url)
        else:
            self._new_tab(url)

    def open_internal(self, name: str):
        if name in INTERNAL_PAGES:
            self._ui(self._open_internal, name)

    # ------------------------------------------------------------------ navigation
    def navigate(self, text: str):
        url = normalize_input(text, self._search_engine())
        if url:
            def go():
                if self.active is None:
                    self._new_tab(url)
                    return
                self._load(self.active, url)
                self.active.view.Focus()
            self._ui(go)

    def _with_core(self, fn):
        def run():
            if self.active is not None and self.active.core is not None:
                fn(self.active)
        self._ui(run)

    def go_back(self):
        self._with_core(lambda t: t.core.GoBack() if t.core.CanGoBack else None)

    def go_forward(self):
        self._with_core(lambda t: t.core.GoForward() if t.core.CanGoForward else None)

    def reload(self, hard: bool = False):
        if hard:
            self._with_core(lambda t: t.core.CallDevToolsProtocolMethodAsync(
                "Page.reload", '{"ignoreCache":true}'))
        else:
            self._with_core(lambda t: t.core.Reload())

    def stop(self):
        self._with_core(lambda t: t.core.Stop())

    def zoom(self, delta: float):
        def apply(tab):
            tab.view.ZoomFactor = 1.0 if delta == 0 else max(0.25, min(5.0, tab.view.ZoomFactor + delta))
        self._with_core(apply)

    def print_page(self):
        def printing(tab):
            from Microsoft.Web.WebView2.Core import CoreWebView2PrintDialogKind
            tab.core.ShowPrintUI(CoreWebView2PrintDialogKind.Browser)
        self._with_core(printing)

    def dev_tools(self):
        self._with_core(lambda t: t.core.OpenDevToolsWindow())

    def focus_content(self):
        self._with_core(lambda t: t.view.Focus())

    # ------------------------------------------------------------------ tab events (UI thread)
    @guarded
    def _on_source(self, tab: Tab):
        tab.url = str(tab.core.Source)
        self._dirty()
        if tab is self.active:
            self._update_window_title()

    @guarded
    def _on_title(self, tab: Tab):
        tab.title = tab.core.DocumentTitle or ""
        if tab.visit_id and tab.visit_url == tab.url:
            self.store.update_visit(tab.visit_id, title=tab.title)
        self._dirty()
        if tab is self.active:
            self._update_window_title()

    @guarded
    def _on_favicon(self, tab: Tab):
        tab.favicon = tab.core.FaviconUri or ""
        if tab.visit_id and tab.visit_url == tab.url and tab.favicon:
            self.store.update_visit(tab.visit_id, icon=tab.favicon)
        self._dirty()

    @guarded
    def _on_nav_start(self, tab: Tab, args):
        tab.loading = True
        tab.failed = False
        if host_of(str(args.Uri)) != host_of(tab.url):
            tab.favicon = ""                             # do not show the old site's icon on the new one
        self._dirty()

    @guarded
    def _on_nav_done(self, tab: Tab, args):
        tab.loading = False
        tab.failed = not args.IsSuccess
        if tab.failed:
            tab.favicon = ""
        tab.back, tab.forward = bool(tab.core.CanGoBack), bool(tab.core.CanGoForward)
        url = str(tab.core.Source)
        if args.IsSuccess and url.startswith(("http://", "https://")) and not is_internal(url):
            tab.visit_url = url
            tab.visit_id = self.store.add_visit(url, tab.core.DocumentTitle or "", tab.core.FaviconUri or "")
            self._broadcast_event("history", None)
        self._dirty()

    @guarded
    def _on_history_changed(self, tab: Tab):
        tab.back, tab.forward = bool(tab.core.CanGoBack), bool(tab.core.CanGoForward)
        self._dirty()

    @guarded
    def _on_new_window(self, args):
        args.Handled = True                              # target=_blank / window.open -> a new tab
        self._later(self._new_tab, str(args.Uri))

    @guarded
    def _on_fullscreen(self, tab: Tab):
        if tab is self.active:
            self._set_fullscreen(bool(tab.core.ContainsFullScreenElement))

    def _set_fullscreen(self, on: bool):
        from System.Windows.Forms import FormBorderStyle, FormWindowState

        if on == self.fullscreen:
            return
        form = self.form
        self.fullscreen = on
        if on:
            self._saved_frame = (form.FormBorderStyle, form.WindowState)
            form.FormBorderStyle = getattr(FormBorderStyle, "None")
            form.WindowState = FormWindowState.Maximized
        elif self._saved_frame:
            form.FormBorderStyle, form.WindowState = self._saved_frame
        self._layout()

    @guarded
    def _on_message(self, tab: Tab, args):
        msg = json.loads(args.WebMessageAsJson)
        kind = msg.get("t")
        if kind == "key":
            if secrets.compare_digest(str(msg.get("k", "")), self._token):
                self._command(str(msg.get("cmd", "")))
        elif kind == "call":
            # Only our own pages may call the API, and only while they are what the tab shows.
            if str(args.Source).startswith(INTERNAL_BASE) and tab.url.startswith(INTERNAL_BASE):
                threading.Thread(target=self._run_call, args=(tab, msg), daemon=True).start()

    # ------------------------------------------------------------------ bridge for internal pages
    def _run_call(self, tab: Tab, msg: dict):
        reply = {"t": "ret", "id": msg.get("id")}
        name = str(msg.get("name", ""))
        try:
            fn = None if name.startswith("_") else getattr(self.api, name, None)
            if not callable(fn):
                raise AttributeError(f"unknown API function {name}")
            reply["val"] = fn(*(msg.get("args") or []))
        except Exception as e:
            reply["err"] = f"{type(e).__name__}: {e}"
            log_error(f"api call {name}")
        self._post(tab, reply)

    def _post(self, tab: Tab, message: dict):
        text = json.dumps(message)
        self._ui(lambda: tab.core.PostWebMessageAsJson(text) if tab.core is not None else None)

    def broadcast(self, event: str, data=None):
        """Forward an engine event (state, config, transcript ...) to the Vexon pages."""
        self._broadcast_event(event, data)

    def _broadcast_event(self, event: str, data):
        message = json.dumps({"t": "event", "event": event, "data": data})

        def send():
            for tab in self.tabs:
                if tab.core is not None and tab.url.startswith(INTERNAL_BASE):
                    tab.core.PostWebMessageAsJson(message)
        self._ui(send)

    # ------------------------------------------------------------------ commands (keyboard / menu)
    def command(self, cmd: str):
        self._ui(self._command, cmd)

    def _command(self, cmd: str):
        tab = self.active
        if cmd == "new_tab":
            self._new_tab(None)
        elif cmd == "close_tab":
            self._close(tab)
        elif cmd == "reopen_tab":
            self._reopen()
        elif cmd == "focus_address":
            self._focus_address()
        elif cmd == "bookmark":
            self.toggle_bookmark()
        elif cmd in ("history", "bookmarks", "app"):
            self._open_internal(cmd)
        elif cmd == "toggle_bookmarks_bar":
            self.engine.update_config({"show_bookmarks_bar": not self.engine.cfg.get("show_bookmarks_bar", True)})
        elif cmd in ("next_tab", "prev_tab") and tab is not None and len(self.tabs) > 1:
            step = 1 if cmd == "next_tab" else -1
            self._activate(self.tabs[(self.tabs.index(tab) + step) % len(self.tabs)])
        elif cmd.startswith("tab_") and self.tabs:
            n = int(cmd[4:])
            self._activate(self.tabs[-1] if n == 9 else self.tabs[min(n, len(self.tabs)) - 1])

    def _focus_address(self):
        self.shell.Focus()
        self._push("focus_address", None)

    # ------------------------------------------------------------------ bookmarks
    def toggle_bookmark(self) -> bool:
        def toggle():
            tab = self.active
            if tab is None or not tab.url.startswith(("http://", "https://")):
                return False
            if self.store.bookmark_for(tab.url):
                self.store.remove_bookmark_url(tab.url)
            else:
                self.store.add_bookmark(tab.url, tab.title, tab.favicon)
            self.bookmarks_changed()
            return True
        return bool(self._ui(toggle, wait=True))

    # ------------------------------------------------------------------ Google account
    def google_signed_in(self) -> bool:
        """True when this browser profile holds a Google session cookie, i.e. you signed in to
        Google (Gmail, YouTube ...) in a tab. Call from a worker thread, not the UI thread."""
        done, found = threading.Event(), []

        def ask():
            from System import Action
            from System.Collections.Generic import List
            from System.Threading.Tasks import Task, TaskScheduler
            from Microsoft.Web.WebView2.Core import CoreWebView2Cookie

            def got(task):                                   # runs on the UI thread
                try:
                    found.extend(c.Name for c in task.Result)
                except Exception:
                    log_error("google cookies")
                done.set()

            self.shell.CoreWebView2.CookieManager.GetCookiesAsync("https://accounts.google.com/").ContinueWith(
                Action[Task[List[CoreWebView2Cookie]]](got), TaskScheduler.FromCurrentSynchronizationContext())

        self._ui(ask)
        done.wait(5)
        return bool(GOOGLE_SESSION_COOKIES & set(found))

    def bookmarks_changed(self):
        self._push("bookmarks", self.store.bookmarks())
        self._broadcast_event("bookmarks", None)
        self._dirty()

    # ------------------------------------------------------------------ state pushed to the shell
    def _update_window_title(self):
        tab = self.active
        title = (tab.title or display_url(tab.url)) if tab else ""
        self.form.Text = f"{title} - Vexon" if title else "Vexon"

    def _dirty(self):
        """Coalesce bursts of events (title, favicon, source ...) into one push."""
        if self._dirty_pending or self.shell is None:
            return
        self._dirty_pending = True
        self._ui(self._flush)

    def _flush(self):
        self._dirty_pending = False
        self._push("tabs", self.state())

    def snapshot(self) -> dict:
        """state() read on the UI thread, for callers on other threads."""
        return self._ui(self.state, wait=True) or {"tabs": [], "active": None, "bookmarked": False}

    def state(self) -> dict:
        def describe(t: Tab) -> dict:
            return {
                "id": t.id, "title": t.title or display_url(t.url) or "New Tab", "url": t.url,
                "address": display_url(t.url), "edit": edit_url(t.url), "internal": internal_name(t.url),
                "favicon": t.favicon, "loading": t.loading, "back": t.back, "forward": t.forward,
                "secure": t.url.startswith("https://") and not is_internal(t.url) and not t.failed,
            }
        active = self.active
        return {
            "tabs": [describe(t) for t in self.tabs],
            "active": active.id if active else None,
            "bookmarked": bool(active and self.store.bookmark_for(active.url)),
        }

    def _push(self, event: str, data):
        js = f"window.onBrowserEvent && window.onBrowserEvent({json.dumps(event)}, {json.dumps(data)})"

        def send():
            if self.shell is not None and self.shell.CoreWebView2 is not None:
                self.shell.CoreWebView2.ExecuteScriptAsync(js)
        self._ui(send)


# --------------------------------------------------------------------------- js_api

class BrowserApi:
    """Mixed into main.Api, so these are callable as window.pywebview.api.<name>() from the
    toolbar and, through the bridge in TAB_SCRIPT, from the internal pages."""

    @property
    def _browser(self) -> Browser:
        return self._app.browser

    # tabs and navigation
    def browser_state(self):
        return self._browser.snapshot()

    def new_tab(self, url=""):
        return self._browser.new_tab(normalize_input(url, self._browser._search_engine()) if url else None)

    def close_tab(self, tab_id):
        self._browser.close_tab(tab_id)

    def activate_tab(self, tab_id):
        self._browser.activate_tab(tab_id)

    def move_tab(self, tab_id, index):
        self._browser.move_tab(tab_id, index)

    def navigate(self, text):
        self._browser.navigate(text)

    def go_back(self):
        self._browser.go_back()

    def go_forward(self):
        self._browser.go_forward()

    def reload_page(self, hard=False):
        self._browser.reload(bool(hard))

    def stop_loading(self):
        self._browser.stop()

    def browser_command(self, cmd):
        self._browser.command(str(cmd))

    def open_internal(self, name):
        self._browser.open_internal(str(name))

    def set_layout(self, chrome_h, popups=None):
        self._browser.set_layout(chrome_h, popups or [])

    def zoom_page(self, delta):
        self._browser.zoom(float(delta))

    def print_page(self):
        self._browser.print_page()

    def dev_tools(self):
        self._browser.dev_tools()

    def focus_content(self):
        self._browser.focus_content()

    # Google account (Gmail sign-in happens on Google's own page, in a tab)
    def google_account(self):
        return {"signed_in": self._browser.google_signed_in(), "sign_in_url": GOOGLE_SIGN_IN}

    # bookmarks
    def get_bookmarks(self, search=""):
        return self._browser.store.bookmarks(search or "")

    def toggle_bookmark(self):
        return self._browser.toggle_bookmark()

    def remove_bookmark(self, bookmark_id):
        self._browser.store.remove_bookmark(bookmark_id)
        self._browser.bookmarks_changed()

    def rename_bookmark(self, bookmark_id, title):
        self._browser.store.rename_bookmark(bookmark_id, str(title))
        self._browser.bookmarks_changed()

    # browsing history (the dictation history is get_history / delete_history / clear_history)
    def browsing_history(self, search="", limit=300):
        return self._browser.store.history(search or "", int(limit))

    def delete_visit(self, visit_id):
        self._browser.store.delete_visit(visit_id)
        self._browser.broadcast("history")

    def clear_browsing_history(self):
        self._browser.store.clear_history()
        self._browser.broadcast("history")

    def top_sites(self, limit=10):
        return self._browser.store.top_sites(int(limit))

    def suggest(self, text):
        """Rows for the address-bar dropdown: what was typed (search or site), bookmarks, history."""
        text = str(text or "").strip()
        if not text:
            return []
        engine = self._browser._search_engine()
        target = normalize_input(text, engine)
        searching = target.startswith(SEARCH_ENGINES.get(engine, SEARCH_ENGINES["google"])[1].split("%s")[0])
        first = {"kind": "search" if searching else "url", "title": text, "url": target,
                 "sub": f"{SEARCH_ENGINES.get(engine, SEARCH_ENGINES['google'])[0]} Search" if searching
                 else display_url(target) or target}
        return [first] + [r for r in self._browser.store.suggest(text, 6) if r["url"] != target]
