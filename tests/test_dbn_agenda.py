import importlib.util
import sys
import unittest
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

SPEC = importlib.util.spec_from_file_location("sync_dbn_agenda", SCRIPTS / "sync_dbn_agenda.py")
AGENDA = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(AGENDA)


def calendar(name, event_summary, event_date, location):
    return f"""BEGIN:VCALENDAR
X-WR-CALNAME:{name}
BEGIN:VEVENT
DTSTART;VALUE=DATE:{event_date.replace('-', '')}
DTEND;VALUE=DATE:{event_date.replace('-', '')}
SUMMARY:{event_summary}
LOCATION:{location.replace(',', r'\\,')}
END:VEVENT
END:VCALENDAR
"""


class DbNAgendaTests(unittest.TestCase):
    def setUp(self):
        self.feeds = [
            calendar("DBN | Competitie Mixed", "Competitie MIX dodgeball (dag 1)", "2026-10-11", "St. Nicolaaslyceum, Amsterdam"),
            calendar("DBN | Competitie Dames/Heren", "Competitie M/V dodgeball (dag 1)", "2026-11-14", "X TU Delft, Delft"),
        ]

    def test_builds_current_season_from_both_agenda_feeds(self):
        schedule = AGENDA.build_schedule(self.feeds, date(2026, 10, 15))
        self.assertEqual(schedule["season"], "2026/2027")
        self.assertEqual([event["date"] for event in schedule["events"]], ["2026-10-11", "2026-11-14"])
        self.assertEqual(schedule["events"][0]["timelines"], ["mixed1", "mixed2"])
        self.assertEqual(schedule["events"][1]["timelines"], ["man1", "man2", "women"])

    def test_partial_agenda_keeps_existing_season_unchanged(self):
        self.assertIsNone(AGENDA.build_schedule(self.feeds[:1], date(2027, 8, 15)))

    def test_schedule_crons_cover_day_after_and_one_week_later(self):
        schedule = AGENDA.build_schedule(self.feeds, date(2026, 10, 15))
        self.assertEqual(
            AGENDA.cron_entries(schedule),
            ['    - cron: "0 8 12,19 10 *"', '    - cron: "0 8 15,22 11 *"'],
        )

    def test_replaces_only_generated_cron_block(self):
        workflow = "on:\\n  schedule:\\n    # BEGIN DBN AGENDA SCHEDULES\\n    - cron: old\\n    # END DBN AGENDA SCHEDULES\\n"
        updated = AGENDA.replace_workflow_schedules(workflow, AGENDA.build_schedule(self.feeds, date(2026, 10, 15)))
        self.assertIn('    - cron: "0 8 12,19 10 *"', updated)
        self.assertNotIn("cron: old", updated)

    def test_rollover_archives_results_and_standings_and_sets_new_season(self):
        page = (REPO / "competitions.html").read_text(encoding="utf-8")
        next_season = {
            "season": "2027/2028",
            "events": [
                {"date": "2027-11-07", "timelines": ["mixed1", "mixed2"], "title": "DBN Mixed · day 1", "time": "11:00–17:00", "location": "Venue to be announced"},
                {"date": "2027-11-14", "timelines": ["man1", "man2", "women"], "title": "DBN Men & Women · day 1", "time": "10:00–17:00", "location": "Venue to be announced"},
            ],
        }
        updated = AGENDA.rotate_page_season(page, "2026/2027", next_season, date(2027, 8, 15))
        self.assertIn('<body data-current-season="2027/2028">', updated)
        self.assertIn('data-season="2027/2028"', updated)
        self.assertIn('data-season="2026/2027"', updated)
        self.assertIn("40 - 12", updated)
        self.assertIn("DCA 1</td><td>3</td><td>3</td>", updated)
        self.assertIn("07 November 2027", updated)
        self.assertIn("Fixtures are warming up", updated)
        self.assertEqual(updated.count('data-season="2026/2027"'), 5)

    def test_refresh_replaces_placeholder_without_doubling_indentation_or_erasing_results(self):
        page = (REPO / "competitions.html").read_text(encoding="utf-8")
        schedule = AGENDA.build_schedule(self.feeds, date(2026, 10, 15))
        updated = AGENDA.refresh_agenda_placeholders(page, schedule, date(2026, 10, 15))
        self.assertIn("14 November 2026", updated)
        self.assertIn("40 - 12", updated)
        self.assertNotRegex(updated, r"(?m)^ {20}<div class=\"timeline-item\" data-tournify-sync=\"true\">")


if __name__ == "__main__":
    unittest.main()
