"""Tests that settings, history and bookmarks survive the VoiceType -> Vexon rename.

Run from the project folder:  .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import engine  # noqa: E402


class MigrateOldFolder(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_moves_old_folder_with_its_files(self):
        old, new = self.root / "VoiceType", self.root / "Vexon"
        old.mkdir()
        (old / "history.db").write_bytes(b"dictations")
        (old / "browser.db").write_bytes(b"bookmarks")
        engine.migrate_old_folder(old, new)
        self.assertFalse(old.exists())
        self.assertEqual((new / "history.db").read_bytes(), b"dictations")
        self.assertEqual((new / "browser.db").read_bytes(), b"bookmarks")

    def test_never_overwrites_an_existing_new_folder(self):
        old, new = self.root / "VoiceType", self.root / "Vexon"
        old.mkdir(); new.mkdir()
        (old / "a").write_text("old"); (new / "a").write_text("new")
        engine.migrate_old_folder(old, new)
        self.assertEqual((new / "a").read_text(), "new")
        self.assertTrue(old.exists())

    def test_nothing_to_do(self):
        engine.migrate_old_folder(self.root / "VoiceType", self.root / "Vexon")
        self.assertFalse((self.root / "Vexon").exists())

    def test_load_config_keeps_settings_and_updates_old_default_hint(self):
        old, new = self.root / "VoiceType", self.root / "Vexon"
        old.mkdir()
        (old / "config.json").write_text(json.dumps(
            {"search_engine": "duckduckgo", "model": "tiny", "vocabulary": ["VoiceType"]}))
        with mock.patch.object(engine, "DATA_DIR", new), \
                mock.patch.object(engine, "CONFIG_PATH", new / "config.json"):
            cfg = engine.load_config()
        self.assertEqual(cfg["search_engine"], "duckduckgo")
        self.assertEqual(cfg["model"], "tiny")
        self.assertEqual(cfg["vocabulary"], ["Vexon"])
        self.assertFalse(old.exists())
        self.assertEqual(json.loads((new / "config.json").read_text())["model"], "tiny")

    def test_custom_vocabulary_is_left_alone(self):
        new = self.root / "Vexon"
        new.mkdir()
        (new / "config.json").write_text(json.dumps({"vocabulary": ["VoiceType", "Kubernetes"]}))
        with mock.patch.object(engine, "DATA_DIR", new), \
                mock.patch.object(engine, "CONFIG_PATH", new / "config.json"):
            cfg = engine.load_config()
        self.assertEqual(cfg["vocabulary"], ["VoiceType", "Kubernetes"])


if __name__ == "__main__":
    unittest.main()
