"""Tests for the browser's address-bar logic and bookmark/history storage (no GUI needed).

Run from the project folder:  .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import browser  # noqa: E402
from browser import (BrowserStore, display_url, edit_url, internal_name, internal_url,  # noqa: E402
                     normalize_input)

G = "https://www.google.com/search?q="


class NormalizeInput(unittest.TestCase):
    def test_websites(self):
        self.assertEqual(normalize_input("github.com"), "https://github.com")
        self.assertEqual(normalize_input("  www.python.org/downloads  "), "https://www.python.org/downloads")
        self.assertEqual(normalize_input("example.com/a?b=1#c"), "https://example.com/a?b=1#c")
        self.assertEqual(normalize_input("bbc.co.uk"), "https://bbc.co.uk")
        self.assertEqual(normalize_input("https://example.com/a b"), "https://example.com/a b")
        self.assertEqual(normalize_input("HTTP://Example.com"), "HTTP://Example.com")

    def test_local_addresses_use_http(self):
        self.assertEqual(normalize_input("localhost:3000"), "http://localhost:3000")
        self.assertEqual(normalize_input("localhost"), "http://localhost")
        self.assertEqual(normalize_input("127.0.0.1:8080/x"), "http://127.0.0.1:8080/x")
        self.assertEqual(normalize_input("192.168.1.1"), "http://192.168.1.1")
        self.assertEqual(normalize_input("[::1]:8080"), "http://[::1]:8080")

    def test_searches(self):
        self.assertEqual(normalize_input("cats"), G + "cats")
        self.assertEqual(normalize_input("hello world"), G + "hello+world")
        self.assertEqual(normalize_input("what is 1+1?"), G + "what+is+1%2B1%3F")
        self.assertEqual(normalize_input("notes.txt"), G + "notes.txt")        # a file name, not a site
        self.assertEqual(normalize_input("1.2.3"), G + "1.2.3")
        self.assertEqual(normalize_input("user@example.com"), G + "user%40example.com")
        self.assertEqual(normalize_input("python tutorial site.com"), G + "python+tutorial+site.com")

    def test_search_engine_choice(self):
        self.assertEqual(normalize_input("cats", "duckduckgo"), "https://duckduckgo.com/?q=cats")
        self.assertEqual(normalize_input("cats", "bing"), "https://www.bing.com/search?q=cats")
        self.assertEqual(normalize_input("cats", "no-such-engine"), G + "cats")

    def test_dangerous_schemes_become_searches(self):
        for text in ("javascript:alert(1)", "data:text/html,<h1>x</h1>", "mailto:a@b.com", "vbscript:x"):
            self.assertTrue(normalize_input(text).startswith(G), text)

    def test_empty(self):
        self.assertIsNone(normalize_input(""))
        self.assertIsNone(normalize_input("   "))
        self.assertIsNone(normalize_input(None))

    def test_internal_pages(self):
        self.assertEqual(normalize_input("vexon://history"), internal_url("history"))
        self.assertEqual(normalize_input("vexon://app/"), internal_url("app"))
        self.assertTrue(normalize_input("vexon://nope").startswith(G))
        self.assertEqual(normalize_input("about:blank"), "about:blank")
        self.assertEqual(normalize_input("file:///C:/x.html"), "file:///C:/x.html")


class DisplayUrls(unittest.TestCase):
    def test_display(self):
        self.assertEqual(display_url("https://www.example.com/"), "example.com")
        self.assertEqual(display_url("https://example.com/a/b?x=1"), "example.com/a/b?x=1")
        self.assertEqual(display_url("http://localhost:3000/"), "localhost:3000")
        self.assertEqual(display_url(internal_url("newtab")), "")
        self.assertEqual(display_url(internal_url("history")), "vexon://history")
        self.assertEqual(display_url("about:blank"), "")

    def test_edit_keeps_full_url(self):
        self.assertEqual(edit_url("https://www.example.com/"), "https://www.example.com/")
        self.assertEqual(edit_url(internal_url("bookmarks")), "vexon://bookmarks")
        self.assertEqual(edit_url(internal_url("newtab")), "")

    def test_internal_round_trip(self):
        for name in browser.INTERNAL_PAGES:
            self.assertEqual(internal_name(internal_url(name)), name)
        self.assertIsNone(internal_name("https://example.com/newtab.html"))
        self.assertIsNone(internal_name(browser.INTERNAL_BASE + "other.html"))


class Store(unittest.TestCase):
    def setUp(self):
        self.s = BrowserStore(":memory:")

    def test_bookmarks(self):
        a = self.s.add_bookmark("https://a.com/", "A site", "https://a.com/i.ico")
        self.s.add_bookmark("https://b.com/", "")
        self.assertEqual([b["url"] for b in self.s.bookmarks()], ["https://a.com/", "https://b.com/"])
        self.assertEqual(self.s.bookmarks()[1]["title"], "b.com")                 # falls back to the address
        self.s.add_bookmark("https://a.com/", "Renamed")                          # same URL: update, no duplicate
        self.assertEqual(len(self.s.bookmarks()), 2)
        self.assertEqual(self.s.bookmark_for("https://a.com/")["title"], "Renamed")
        self.assertEqual([b["url"] for b in self.s.bookmarks("renam")], ["https://a.com/"])
        self.s.remove_bookmark(a["id"])
        self.assertIsNone(self.s.bookmark_for("https://a.com/"))
        self.s.remove_bookmark_url("https://b.com/")
        self.assertEqual(self.s.bookmarks(), [])

    def test_history_dedupes_quick_reloads(self):
        first = self.s.add_visit("https://a.com/", "A")
        again = self.s.add_visit("https://a.com/", "A (updated)", "icon")
        self.assertEqual(first, again)
        self.assertEqual(len(self.s.history()), 1)
        self.assertEqual(self.s.history()[0]["title"], "A (updated)")
        self.s.add_visit("https://b.com/", "B")
        self.assertEqual([v["url"] for v in self.s.history()], ["https://b.com/", "https://a.com/"])

    def test_history_search_delete_clear(self):
        self.s.add_visit("https://news.example.com/x", "Daily news")
        vid = self.s.add_visit("https://shop.example.org/", "Shop")
        self.assertEqual([v["title"] for v in self.s.history("news")], ["Daily news"])
        self.assertEqual([v["title"] for v in self.s.history("EXAMPLE.org")], ["Shop"])
        self.s.delete_visit(vid)
        self.assertEqual(len(self.s.history()), 1)
        self.s.clear_history()
        self.assertEqual(self.s.history(), [])

    def test_update_visit_title(self):
        vid = self.s.add_visit("https://a.com/", "")
        self.s.update_visit(vid, title="Late title")
        self.assertEqual(self.s.history()[0]["title"], "Late title")

    def test_top_sites_group_by_host(self):
        for url in ("https://a.com/1", "https://a.com/2", "https://a.com/3", "https://b.com/"):
            self.s.db.execute("INSERT INTO visits (url, host, title, icon, visited) VALUES (?, ?, 't', '', '2000-01-01T00:00:00')",
                              (url, browser.host_of(url)))
        top = self.s.top_sites(5)
        self.assertEqual([t["title"] for t in top], ["a.com", "b.com"])
        self.assertEqual(top[0]["url"], "https://a.com/")
        self.assertEqual(top[0]["visits"], 3)

    def test_suggest_bookmarks_first_then_history(self):
        self.s.add_visit("https://docs.python.org/3/", "Python docs")
        self.s.add_visit("https://python.org/", "Python")
        self.s.add_bookmark("https://python.org/", "Python home")
        out = self.s.suggest("python")
        self.assertEqual(out[0]["kind"], "bookmark")
        self.assertEqual([o["url"] for o in out].count("https://python.org/"), 1)   # no duplicates
        self.assertIn("https://docs.python.org/3/", [o["url"] for o in out])
        self.assertEqual(self.s.suggest("   "), [])
        self.assertEqual(self.s.suggest("zzz-nothing"), [])


if __name__ == "__main__":
    unittest.main()
