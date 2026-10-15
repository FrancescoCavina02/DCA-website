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
                "men1": {"0": "default"},
                "men2": {"0": "default"},
                "women": {"0": "default"},
            },
        }
        self.collections = {
            "divisions": [
                {"id": "mixed1", "name": "1e divisie | MIXED"},
                {"id": "mixed2", "name": "2e divisie | MIXED"},
                {"id": "men1", "name": "1e divisie | Heren"},
                {"id": "men2", "name": "2e divisie | Heren"},
                {"id": "women", "name": "1e divisie | Dames"},
            ],
            "days": [],
            "teams": [
                {"id": "dca1", "name": "DCA¹ ⚥¹", "division": "mixed1", "poule0": "p1", "numInPoule0": 0},
                {"id": "haarlem", "name": "Haarlem Dodge ⚥¹", "division": "mixed1", "poule0": "p1", "numInPoule0": 1},
                {"id": "spellen", "name": "SV Spellen ⚥¹", "division": "mixed1", "poule0": "p1", "numInPoule0": 2},
                {"id": "dca2", "name": "DCA² ⚥²", "division": "mixed2", "poule0": "p2", "numInPoule0": 0},
                {"id": "eindhoven", "name": "Dodgeball Eindhoven ⚥²", "division": "mixed2", "poule0": "p2", "numInPoule0": 1},
                {"id": "man1", "name": "DCA 1", "division": "men1", "poule0": "p3", "numInPoule0": 0},
                {"id": "man1-opponent", "name": "Men Opponent", "division": "men1", "poule0": "p3", "numInPoule0": 1},
                {"id": "man2", "name": "DCA 2", "division": "men2", "poule0": "p4", "numInPoule0": 0},
                {"id": "man2-opponent", "name": "Men 2 Opponent", "division": "men2", "poule0": "p4", "numInPoule0": 1},
                {"id": "women", "name": "DAM Delft Dames", "division": "women", "poule0": "p5", "numInPoule0": 0},
                {"id": "women-opponent", "name": "Women Opponent", "division": "women", "poule0": "p5", "numInPoule0": 1},
            ],
            "matches": [
                {"poule": "p1", "team1": 0, "team2": 1, "score1": 12, "score2": 10},
                {"poule": "p1", "team1": 1, "team2": 2, "score1": 14, "score2": 10},
                {"poule": "p1", "team1": 2, "team2": 0, "score1": 20, "score2": 10},
                {"poule": "p2", "team1": 0, "team2": 1, "score1": 30, "score2": 10},
                {"poule": "p3", "team1": 0, "team2": 1, "score1": 20, "score2": 10},
                {"poule": "p4", "team1": 0, "team2": 1, "score1": 20, "score2": 10},
                {"poule": "p5", "team1": 0, "team2": 1, "score1": 20, "score2": 10},
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


    def test_standings_cover_all_five_dca_teams_including_women_target(self):
        divisions = [
            {"id": "men1", "name": "1e divisie | Heren"},
            {"id": "men2", "name": "2e divisie | Heren"},
            {"id": "women", "name": "1e divisie | Dames"},
            {"id": "mixed1", "name": "1e divisie | Mixed"},
            {"id": "mixed2", "name": "2e divisie | Mixed"},
        ]
        targets = [
            ("man1", "men1", "DCA 1"),
            ("man2", "men2", "DCA 2"),
            ("women", "women", "DAM Delft Dames"),
            ("mixed1", "mixed1", "DCA 1"),
            ("mixed2", "mixed2", "DCA 2"),
        ]
        teams = []
        matches = []
        scoring = {}
        for timeline, division_id, dca_name in targets:
            poule = f"poule-{timeline}"
            opponent_id = f"opponent-{timeline}"
            teams.extend([
                {"id": f"dca-{timeline}", "name": dca_name, "division": division_id, "poule0": poule, "numInPoule0": 0},
                {"id": opponent_id, "name": f"Opponent {timeline}", "division": division_id, "poule0": poule, "numInPoule0": 1},
            ])
            matches.append({"poule": poule, "team1": 0, "team2": 1, "score1": 20, "score2": 10})
            scoring[division_id] = {"0": "default"}
        tournament = {**self.tournament, "scoringTypesPerDivisionPerPhase": scoring}
        collections = {"divisions": divisions, "days": [], "teams": teams, "matches": matches}

        standings = SYNC.extract_standings(tournament, collections)

        self.assertEqual(set(standings), {"man1", "man2", "women", "mixed1", "mixed2"})
        for timeline, _, expected_name in targets:
            dca_row = next(row for row in standings[timeline] if row["is_dca"])
            self.assertEqual(dca_row["name"], expected_name)
            self.assertEqual((dca_row["played"], dca_row["won"], dca_row["lost"]), (1, 1, 0))
        women_row = next(row for row in standings["women"] if row["is_dca"])
        self.assertEqual(women_row["name"], "DAM Delft Dames")

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
