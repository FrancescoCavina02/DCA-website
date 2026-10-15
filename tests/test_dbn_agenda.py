import importlib.util
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

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


def calendar_with_events(name, events):
    payload = ["BEGIN:VCALENDAR", f"X-WR-CALNAME:{name}"]
    for summary, event_date, location, status in events:
        escaped_location = location.replace(",", "\\,")
        payload.extend([
            "BEGIN:VEVENT",
            f"DTSTART;VALUE=DATE:{event_date.replace('-', '')}",
            f"DTEND;VALUE=DATE:{event_date.replace('-', '')}",
            f"SUMMARY:{summary}",
            f"LOCATION:{escaped_location}",
        ])
        if status:
            payload.append(f"STATUS:{status}")
        payload.append("END:VEVENT")
    payload.append("END:VCALENDAR")
    return "\n".join(payload)


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

    def test_partial_live_agenda_does_not_write_page_or_schedule(self):
        with tempfile.TemporaryDirectory(dir=REPO.parent) as directory:
            root = Path(directory)
            schedule_file = root / "dbn_competition_days.json"
            workflow_file = root / "tournify-sync.yml"
            site_file = root / "competitions.html"
            output_file = root / "github-output.txt"
            shutil.copyfile(REPO / "data/dbn_competition_days.json", schedule_file)
            shutil.copyfile(REPO / ".github/workflows/tournify-sync.yml", workflow_file)
            shutil.copyfile(REPO / "competitions.html", site_file)
            before = [path.read_bytes() for path in (schedule_file, workflow_file, site_file)]
            agenda_page = '<iframe src="https://calendar.google.com/calendar/embed?src=mixed%40group.calendar.google.com"></iframe>'
            argv = [
                "sync_dbn_agenda.py",
                "--schedule-file", str(schedule_file),
                "--workflow-file", str(workflow_file),
                "--site-file", str(site_file),
                "--github-output", str(output_file),
                "--as-of", "2027-08-15",
            ]

            with patch.object(sys, "argv", argv), patch.object(
                AGENDA, "read_url", side_effect=[agenda_page, self.feeds[0]]
            ):
                AGENDA.main()

            self.assertEqual(before, [path.read_bytes() for path in (schedule_file, workflow_file, site_file)])
            self.assertIn("changed=false", output_file.read_text(encoding="utf-8"))

    def test_schedule_crons_cover_day_after_and_one_week_later(self):
        schedule = AGENDA.build_schedule(self.feeds, date(2026, 10, 15))
        self.assertEqual(
            AGENDA.cron_entries(schedule),
            [
                '    - cron: "0 8 1,15 8 *"',
                '    - cron: "0 8 1,15 9 *"',
                '    - cron: "0 8 1,12,15,19 10 *"',
                '    - cron: "0 8 15,22 11 *"',
            ],
        )

    def test_agenda_crons_cover_future_seasons_with_more_than_five_days(self):
        mixed = calendar_with_events(
            "DBN | Competitie Mixed",
            [(f"Competitie MIX dodgeball (dag {day})", f"2027-{day + 6:02d}-01", "Venue", "") for day in range(1, 7)],
        )
        men_women = calendar_with_events(
            "DBN | Competitie Dames/Heren",
            [(f"Competitie M/V dodgeball (dag {day})", f"2027-{day + 6:02d}-02", "Venue", "") for day in range(1, 7)],
        )

        schedule = AGENDA.build_schedule([mixed, men_women], date(2027, 8, 15))

        self.assertEqual(len(schedule["events"]), 12)
        self.assertEqual(max(event["day"] for event in schedule["events"]), 6)

    def test_cancelled_agenda_events_are_ignored(self):
        mixed = calendar_with_events(
            "DBN | Competitie Mixed",
            [
                ("Competitie MIX dodgeball (dag 1)", "2026-10-11", "Venue", "CONFIRMED"),
                ("Competitie MIX dodgeball (dag 2)", "2027-01-09", "Venue", "CANCELLED"),
            ],
        )

        schedule = AGENDA.build_schedule([mixed, self.feeds[1]], date(2026, 10, 15))

        self.assertEqual([event["day"] for event in schedule["events"]], [1, 1])

    def test_cron_offsets_cross_calendar_year_boundaries(self):
        schedule = {
            "events": [{"date": "2026-12-28"}],
            "check_offsets_days": [1, 8],
        }

        self.assertEqual(
            AGENDA.cron_entries(schedule),
            [
                '    - cron: "0 8 5 1 *"',
                '    - cron: "0 8 1,15 8 *"',
                '    - cron: "0 8 1,15 9 *"',
                '    - cron: "0 8 1,15 10 *"',
                '    - cron: "0 8 29 12 *"',
            ],
        )

    def test_replaces_only_generated_cron_block(self):
        workflow = "on:\\n  schedule:\\n    # BEGIN DBN AGENDA SCHEDULES\\n    - cron: old\\n    # END DBN AGENDA SCHEDULES\\n"
        updated = AGENDA.replace_workflow_schedules(workflow, AGENDA.build_schedule(self.feeds, date(2026, 10, 15)))
        self.assertIn('    - cron: "0 8 1,12,15,19 10 *"', updated)
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
