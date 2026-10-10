"""Tests for paint.py — run with: python3 -m unittest discover tests"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paint  # noqa: E402


def run(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class RepoTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        run(self.path, "init", "-q", "-b", "main")
        run(self.path, "config", "user.name", "Test")
        run(self.path, "config", "user.email", "test@example.com")
        run(self.path, "config", "commit.gpgsign", "false")
        self.commit("a.txt", "first")
        self.repo = paint.Repo(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, name, msg, date=None):
        (self.path / name).write_text(msg)
        run(self.path, "add", name)
        args = ["commit", "-q", "-m", msg] + (["--date", date] if date else [])
        run(self.path, *args)

    def subjects(self):
        return run(self.path, "log", "--format=%s").splitlines()


class LevelTests(unittest.TestCase):
    def test_level_counts(self):
        self.assertEqual(paint.level_counts(0, 10), [0, 3, 5, 8, 10])
        self.assertEqual(paint.level_counts(0, 1), [0, 1, 1, 1, 1])
        self.assertEqual(paint.level_counts(2, 100), [2, 27, 51, 76, 100])
        with self.assertRaises(paint.PainterError):
            paint.level_counts(5, 1)

    def test_design_to_counts(self):
        design = {"contrast": {"min": 0, "max": 10}, "levels": {"2026-01-01": 4, "2026-01-02": 1, "2026-01-03": 0}}
        self.assertEqual(paint.design_to_counts(design), {"2026-01-01": 10, "2026-01-02": 3, "2026-01-03": 0})

    def test_design_with_background(self):
        design = {"contrast": {"min": 1, "max": 9}, "range": {"start": "2026-01-01", "end": "2026-01-03"},
                  "levels": {"2026-01-02": 4}}
        self.assertEqual(paint.design_to_counts(design), {"2026-01-01": 1, "2026-01-02": 9, "2026-01-03": 1})

    def test_shipped_design(self):
        design = json.loads((Path(paint.HERE) / "designs" / "statur.co.json").read_text())
        counts = paint.design_to_counts(design)
        self.assertTrue(all(v in (3, 5, 8, 10) for v in counts.values()))


class PaintTests(RepoTestCase):
    def test_paint_status_and_clear(self):
        result = self.repo.paint({"2026-01-01": 3, "2026-01-05": 1, "2026-01-06": 0})
        self.assertEqual(result, {"removed": 0, "created": 4, "days": 2})
        st = self.repo.status()
        self.assertEqual(st["painted"], {"2026-01-01": 3, "2026-01-05": 1})
        self.assertEqual(st["painted_total"], 4)
        self.assertEqual(st["other_total"], 1)
        # Commits are empty and dated at noon UTC.
        self.assertEqual(run(self.path, "diff", "--stat", "HEAD~4", "HEAD"), "")
        self.assertEqual(run(self.path, "log", "-1", "--format=%ad", "--date=iso"), "2026-01-05 12:00:00 +0000")
        self.assertEqual(self.repo.clear(), 4)
        self.assertEqual(self.subjects(), ["first"])

    def test_repaint_replaces(self):
        self.repo.paint({"2026-01-01": 5})
        self.repo.paint({"2026-02-01": 2})
        self.assertEqual(self.repo.status()["painted"], {"2026-02-01": 2})

    def test_clear_keeps_commits_made_after_painting(self):
        self.repo.paint({"2026-01-01": 2})
        self.commit("b.txt", "second")
        tree = run(self.path, "rev-parse", "HEAD^{tree}")
        self.assertEqual(self.repo.clear(), 2)
        self.assertEqual(self.subjects(), ["second", "first"])
        self.assertEqual(run(self.path, "rev-parse", "HEAD^{tree}"), tree)
        self.assertEqual(run(self.path, "status", "--porcelain"), "")

    def test_non_empty_commit_with_trailer_is_kept(self):
        (self.path / "c.txt").write_text("x")
        run(self.path, "add", "c.txt")
        run(self.path, "commit", "-q", "-m", "real work\n\nActivity-Painter: true")
        self.assertEqual(self.repo.clear(), 0)
        self.assertEqual(self.subjects(), ["real work", "first"])

    def test_author(self):
        self.repo.paint({"2026-01-01": 1}, name="Jane", email="jane@example.com")
        self.assertEqual(run(self.path, "log", "-1", "--format=%an <%ae> | %cn"), "Jane <jane@example.com> | Jane")

    def test_validation(self):
        for bad in ({"2026-13-01": 1}, {"2026-01-01": -1}, {"2026-01-01": "3"}, {"x": 1}):
            with self.assertRaises(paint.PainterError):
                self.repo.paint(bad)
        with self.assertRaises(paint.PainterError):
            self.repo.paint({"2026-01-01": 1}, name="Evil", email="a>b")

    def test_detached_head(self):
        run(self.path, "checkout", "-q", "--detach")
        with self.assertRaises(paint.PainterError):
            self.repo.paint({"2026-01-01": 1})


class EmptyRepoTests(unittest.TestCase):
    def test_paint_in_empty_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            run(tmp, "init", "-q", "-b", "main")
            repo = paint.Repo(Path(tmp))
            repo.paint({"2026-01-01": 2}, name="A", email="a@example.com")
            self.assertEqual(repo.status()["painted_total"], 2)
            self.assertEqual(repo.clear(), 2)
            self.assertIsNone(repo.head())


class GitHubParserTests(unittest.TestCase):
    def test_parse(self):
        page = '''
        <td tabindex="0" data-ix="0" data-date="2026-01-04" id="contribution-day-component-0-0" data-level="0" class="ContributionCalendar-day"></td>
        <td tabindex="0" data-ix="0" data-date="2026-01-05" id="contribution-day-component-1-0" data-level="2" class="ContributionCalendar-day"></td>
        <td tabindex="0" data-ix="0" data-date="2026-01-06" id="contribution-day-component-2-0" data-level="4" class="ContributionCalendar-day"></td>
        <tool-tip id="t1" for="contribution-day-component-0-0" popover="manual">No contributions on January 4th.</tool-tip>
        <tool-tip id="t2" for="contribution-day-component-1-0" popover="manual">1 contribution on January 5th.</tool-tip>
        <tool-tip id="t3" for="contribution-day-component-2-0" popover="manual">1,204 contributions on January 6th.</tool-tip>
        '''
        self.assertEqual(paint.parse_github_contributions(page),
                         {"2026-01-04": 0, "2026-01-05": 1, "2026-01-06": 1204})


class CommunityTests(RepoTestCase):
    """Painting on top of a shared wall, and the CI check of pull requests."""

    LOGIN, USER_ID = "tester", 123
    EMAIL = "123+tester@users.noreply.github.com"

    def setUp(self):
        super().setUp()
        # Someone else's painting already merged on the wall.
        self.repo.paint({"2026-03-01": 4}, name="Other", email="9+other@users.noreply.github.com")
        run(self.path, "branch", "wall")
        self.repo.base = "wall"
        self.today = paint.dt.date(2026, 10, 10)

    def validate(self, login=LOGIN, user_id=USER_ID):
        return paint.validate_painting(self.repo, "wall", "HEAD", login, user_id, today=self.today)

    def test_base_commits_are_protected(self):
        self.repo.paint({"2026-04-01": 2}, name="T", email=self.EMAIL)
        st = self.repo.status()
        self.assertEqual(st["painted_total"], 6)
        self.assertEqual(st["mine"], {"2026-04-01": 2})
        self.repo.paint({"2026-05-01": 1}, name="T", email=self.EMAIL)  # repaint replaces only ours
        self.assertEqual(self.repo.status()["painted"], {"2026-03-01": 4, "2026-05-01": 1})
        self.assertEqual(self.repo.clear(), 1)
        self.assertEqual(run(self.path, "rev-parse", "HEAD"), run(self.path, "rev-parse", "wall"))

    def test_valid_painting(self):
        self.repo.paint({"2026-04-01": 2, "2026-04-02": 1}, name="T", email=self.EMAIL)
        result = self.validate()
        self.assertEqual(result["verdict"], "valid", result["problems"])
        self.assertEqual((result["commits"], result["days"]), (3, 2))

    def test_someone_elses_email_is_rejected(self):
        self.repo.paint({"2026-04-01": 2}, name="T", email="victim@example.com")
        result = self.validate()
        self.assertEqual(result["verdict"], "invalid")
        self.assertIn("no-reply", result["problems"][0])

    def test_file_changes_are_rejected(self):
        self.repo.paint({"2026-04-01": 1}, name="T", email=self.EMAIL)
        (self.path / "paint.py").write_text("import os; os.system('evil')")
        run(self.path, "add", "paint.py")
        run(self.path, "-c", f"user.email={self.EMAIL}", "commit", "-q", "--date", "2026-04-02T12:00:00+00:00",
            "-m", "Paint 2026-04-02 (1/1)\n\nActivity-Painter: true")
        result = self.validate()
        self.assertEqual(result["verdict"], "invalid")
        self.assertTrue(any("changes files" in p and "paint.py" in p for p in result["problems"]))

    def test_old_dates_are_rejected(self):
        self.repo.paint({"2024-01-01": 1}, name="T", email=self.EMAIL)
        problems = " ".join(self.validate()["problems"])
        self.assertIn("outside the last year", problems)

    def test_code_pull_request_is_not_a_painting(self):
        self.commit("b.txt", "Improve the docs")
        self.assertEqual(self.validate()["verdict"], "not-a-painting")

    def test_wall_svg(self):
        self.repo.paint({"2026-04-01": 2}, name="T", email=self.EMAIL)
        svg = paint.wall_svg(self.repo, today=self.today)
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn("2 painters", svg)
        self.assertIn("6 painted commits", svg)
        self.assertIn("4 painted commits on 2026-03-01", svg)

    def test_github_slug(self):
        self.assertEqual(paint.github_slug("git@github.com:me/repo.git"), ("me", "repo"))
        self.assertEqual(paint.github_slug("https://github.com/me/repo"), ("me", "repo"))
        self.assertIsNone(paint.github_slug("/tmp/repo.git"))


if __name__ == "__main__":
    unittest.main()
