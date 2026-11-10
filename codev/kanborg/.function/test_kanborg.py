#!/usr/bin/env python3
"""Tests for the kanborg core:  python3 .function/test_kanborg.py   (works on a temp copy, never on datum/)."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import kanborg as kb  # noqa: E402

CARD = open(os.path.join(HERE, "..", "CARD.md"), encoding="utf-8").read()


class CoreTest(unittest.TestCase):
    def setUp(self):
        os.environ["KANBORG_TODAY"] = "2026-11-10"
        os.environ.pop("CYCLE", None)
        self.root = tempfile.mkdtemp()
        shutil.copy(os.path.join(HERE, "..", "CARD.md"), self.root)
        subprocess.run(["git", "init", "-q"], cwd=self.root)
        self.b = kb.Board(self.root, "2026")
        for code, title in (("VIM", "editor"), ("VIM", "split"), ("VIM", "audit"), ("HIPG", "perf"), ("HIPG", "bench")):
            self.b.new_card(code, title, description=f"{code} project")

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_zero_based_project_root(self):
        self.assertEqual(sorted(self.b.cards), ["HIPG-0", "HIPG-1", "VIM-0", "VIM-1", "VIM-2"])
        self.assertEqual(self.b.cards["VIM-1"].parent, "VIM-0")
        self.assertEqual(self.b.projects["VIM"]["last_id"], 2)
        self.assertEqual(self.b.cards["VIM-0"].get("start"), "2026-11-10")

    def test_legacy_footer_kept(self):
        c = self.b.cards["VIM-1"]
        self.assertEqual(list(c.meta)[:4], ["project_code", "links", "location", "fpoint"])
        self.assertEqual(c.title, "split")
        self.assertEqual(c.kind, "ISSUE")

    def test_dependencies_and_cycles(self):
        self.b.link("VIM-2", "depends", "VIM-1")
        self.b.link("VIM-1", "depends", "HIPG-1")
        with self.assertRaises(kb.KbError):
            self.b.link("HIPG-1", "depends", "VIM-2")
        self.assertTrue(self.b.blocked(self.b.cards["VIM-2"]))
        _, warn = self.b.move("HIPG-1", "DONE")
        self.assertIn("unblocked: VIM-1", warn)
        self.assertEqual(self.b.related_projects("VIM"), ["HIPG"])
        self.assertEqual(kb.validate(kb.Board(self.root, "2026")), [])

    def test_fields_dates_progress(self):
        self.b.set_fields("VIM-1", [("start", "11-01"), ("due", "+1w"), ("fpoint", "3"), ("progress", "40")])
        self.b.move("VIM-2", "DONE")
        b = kb.Board(self.root, "2026")
        c = b.cards["VIM-1"]
        self.assertEqual((c.get("start"), c.get("due"), c.progress), ("2026-11-01", "2026-11-17", 40))
        self.assertEqual(b.project_stats("VIM")["progress"], round((40 * 3 + 100) / 4))
        with self.assertRaises(kb.KbError):
            self.b.set_fields("VIM-1", [("due", "2026-10-01")])

    def test_title_replaces_whole_heading(self):
        self.b.set_fields("VIM-1", [("title", "FEATURE : timeline view")])
        c = kb.Board(self.root, "2026").cards["VIM-1"]
        self.assertEqual(c.lines[0], "# FEATURE : timeline view")
        self.assertEqual((c.kind, c.title), ("FEATURE", "timeline view"))
        self.b.set_fields("VIM-1", [("title", "Plain title")])
        self.assertEqual(kb.Board(self.root, "2026").cards["VIM-1"].lines[0], "# Plain title")
        with self.assertRaises(kb.KbError):
            self.b.set_fields("VIM-1", [("title", "  ")])

    def test_checkbox_progress_and_journal(self):
        self.b.set_section("VIM-1", "SOLUTION", "- [x] a\n- [ ] b")
        self.b.journal("VIM-1", "note", who="bee")
        c = kb.Board(self.root, "2026").cards["VIM-1"]
        self.assertEqual(c.progress, 50)
        self.assertIn("note", c.section_text("JOURNAL"))
        self.assertIsNotNone(c.footer_at)

    def test_views_render(self):
        self.b.set_fields("VIM-1", [("start", "11-01"), ("due", "11-20")])
        for text in (kb.render_overview(self.b), kb.render_board(self.b, []), kb.render_lanes(self.b, ["VIM"]),
                     kb.render_related(self.b, "VIM"), kb.render_progress(self.b, []), kb.render_deps(self.b, []),
                     kb.render_timeline(self.b, [])):
            self.assertTrue(text.strip())
        self.assertIn("VIM-1", kb.render_timeline(self.b, ["VIM"]))


if __name__ == "__main__":
    unittest.main(verbosity=1)
