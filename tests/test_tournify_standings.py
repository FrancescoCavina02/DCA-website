import importlib.util
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync_tournify.py"
SPEC = importlib.util.spec_from_file_location("sync_tournify", SCRIPT)
SYNC = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(SYNC)
TEST_TMP = SCRIPT.resolve().parents[2] / "tmp"
TEST_TMP.mkdir(parents=True, exist_ok=True)


class TournifyStandingsTests(unittest.TestCase):
    def setUp(self):
        self.tournament = {
            "advancedGroupRanking": True,
            "pointsBigWin": "2",
            "pointsWin": "2",
            "standingsTableWithPoints": True,
            "scoringTypesPerDivisionPerPhase": {
                "mixed1": {"0": "default"},
                "mixed2": {"0": "default"},
            },
        }
        self.collections = {
            "divisions": [
                {"id": "mixed1", "name": "1e divisie | MIXED"},
                {"id": "mixed2", "name": "2e divisie | MIXED"},
            ],
            "teams": [
                {"id": "dca1", "name": "DCA¹ ⚥¹", "division": "mixed1", "poule0": "p1", "numInPoule0": 0},
                {"id": "haarlem", "name": "Haarlem Dodge ⚥¹", "division": "mixed1", "poule0": "p1", "numInPoule0": 1},
                {"id": "spellen", "name": "SV Spellen ⚥¹", "division": "mixed1", "poule0": "p1", "numInPoule0": 2},
                {"id": "dca2", "name": "DCA² ⚥²", "division": "mixed2", "poule0": "p2", "numInPoule0": 0},
                {"id": "eindhoven", "name": "Dodgeball Eindhoven ⚥²", "division": "mixed2", "poule0": "p2", "numInPoule0": 1},
            ],
            "matches": [
                {"poule": "p1", "team1": 0, "team2": 1, "score1": 12, "score2": 10},
                {"poule": "p1", "team1": 1, "team2": 2, "score1": 14, "score2": 10},
                {"poule": "p1", "team1": 2, "team2": 0, "score1": 20, "score2": 10},
                {"poule": "p2", "team1": 0, "team2": 1, "score1": 30, "score2": 10},
            ],
        }

    def test_matches_tournify_head_to_head_order_and_highlights_dca(self):
        standings = SYNC.extract_standings(self.tournament, self.collections)
        mixed1 = standings["mixed1"]
        self.assertEqual([row["name"] for row in mixed1], ["SV Spellen", "Haarlem Dodge", "DCA 1"])
        dca1 = mixed1[-1]
        self.assertEqual((dca1["position"], dca1["played"], dca1["won"], dca1["lost"], dca1["points"], dca1["point_difference"]), (3, 2, 1, 1, 2, -8))
        dca2 = next(row for row in standings["mixed2"] if row["is_dca"])
        self.assertEqual((dca2["position"], dca2["played"], dca2["won"], dca2["lost"], dca2["points"], dca2["point_difference"]), (1, 1, 1, 0, 2, 20))
        rendered = SYNC.render_standings_table("mixed2", standings["mixed2"])
        self.assertIn('<tr class="dca-team-row">', rendered)
        self.assertIn("DCA 2", rendered)

    def test_atomic_write_preserves_file_mode(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            path = Path(directory) / "competitions.html"
            path.write_text("old", encoding="utf-8")
            path.chmod(0o644)

            SYNC.atomic_write_text(path, "new")

            self.assertEqual(path.read_text(encoding="utf-8"), "new")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)

    def _assert_failed_sync_keeps_existing_page(self, fetch_patch, message):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            site_file = Path(directory) / "competitions.html"
            existing = "<main>existing results and standings</main>\n"
            site_file.write_text(existing, encoding="utf-8")
            argv = [
                str(SCRIPT),
                "--site-file", str(site_file),
                "--schedule-file", str(SYNC.SCHEDULE_FILE),
                "--as-of", "2026-10-14",
            ]
            with patch.object(sys, "argv", argv), patch.object(
                SYNC, "extract_fixtures", return_value=([], {}, [])
            ), fetch_patch:
                with self.assertRaisesRegex(RuntimeError, message):
                    SYNC.main()
            self.assertEqual(site_file.read_text(encoding="utf-8"), existing)

    def test_empty_match_fetch_keeps_existing_standings(self):
        empty_matches = {**self.collections, "matches": []}
        self._assert_failed_sync_keeps_existing_page(
            patch.object(SYNC, "fetch_tournify", return_value=(self.tournament, empty_matches)),
            "empty teams, divisions, or matches",
        )

    def test_failed_tournify_fetch_keeps_existing_standings(self):
        self._assert_failed_sync_keeps_existing_page(
            patch.object(SYNC, "fetch_tournify", side_effect=RuntimeError("offline")),
            "offline",
        )


if __name__ == "__main__":
    unittest.main()
