#!/usr/bin/env python3
"""Refresh competition dates and exact GitHub Actions checks from DBN's public agenda."""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

AGENDA_URL = "https://www.dodgeballnederland.nl/agenda.php"
TIMEZONE = "Europe/Amsterdam"
CHECK_OFFSETS_DAYS = [1, 8]
WORKFLOW_MARKER_START = "    # BEGIN DBN AGENDA SCHEDULES"
WORKFLOW_MARKER_END = "    # END DBN AGENDA SCHEDULES"
COMPETITION_TYPES = {
    "mixed": {
        "calendar": re.compile(r"^DBN\s*\|\s*Competitie\s+Mixed$", re.I),
        "summary": re.compile(r"^Competitie\s+MIX\s+dodgeball\s+\(dag\s+(\d+)\)$", re.I),
        "division": "mixed",
        "timelines": ["mixed1", "mixed2"],
        "title": "DBN Mixed · day {day}",
    },
    "men_women": {
        "calendar": re.compile(r"^DBN\s*\|\s*Competitie\s+Dames/Heren$", re.I),
        "summary": re.compile(r"^Competitie\s+M/V\s+dodgeball\s+\(dag\s+(\d+)\)$", re.I),
        "division": "men_women",
        "timelines": ["man1", "man2", "women"],
        "title": "DBN Men & Women · day {day}",
    },
}
VALID_TIMELINES = {"man1", "man2", "women", "mixed1", "mixed2"}


class AgendaPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.embed_urls: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs = dict(attrs)
        candidate = attrs.get("src") if tag in ("iframe", "script") else attrs.get("href")
        if candidate and "calendar.google.com/calendar/embed" in candidate:
            self.embed_urls.append(candidate)


def read_url(url: str, accept: str) -> str:
    request = Request(url, headers={"User-Agent": "League-Wire-DCA-agenda-sync/1.0", "Accept": accept})
    try:
        with urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8")
    except (HTTPError, URLError, TimeoutError, UnicodeDecodeError) as exc:
        detail = getattr(exc, "reason", None) or str(exc)
        raise RuntimeError(f"DBN agenda request failed for {url}: {detail}") from exc


def agenda_calendar_ids(page_html: str) -> list[str]:
    parser = AgendaPageParser()
    parser.feed(page_html)
    found: list[str] = []
    for embed in parser.embed_urls:
        for calendar_id in parse_qs(urlparse(html.unescape(embed)).query).get("src", []):
            if calendar_id.endswith("@group.calendar.google.com") and calendar_id not in found:
                found.append(calendar_id)
    if not found:
        raise RuntimeError("Could not find public Google Calendar feeds on the DBN agenda page")
    return found


def unfold_ics(text: str) -> list[str]:
    lines: list[str] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw_line.startswith((" ", "\t")) and lines:
            lines[-1] += raw_line[1:]
        else:
            lines.append(raw_line)
    return lines


def unescape_ics(value: str) -> str:
    return re.sub(r"\\([\\,;nN])", lambda match: {"n": "\n", "N": "\n"}.get(match.group(1), match.group(1)), value)


def parse_ics_datetime(key: str, value: str, zone: ZoneInfo) -> datetime | date:
    if ";VALUE=DATE" in key or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value[:8], "%Y%m%d").date()
    match = re.fullmatch(r"(\d{8})T(\d{6})(Z)?", value)
    if not match:
        raise ValueError(f"Unsupported DBN agenda date value: {key}:{value}")
    parsed = datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
    if match.group(3):
        return parsed.replace(tzinfo=timezone.utc).astimezone(zone)
    timezone_name = next((item.split("=", 1)[1] for item in key.split(";") if item.startswith("TZID=")), None)
    return parsed.replace(tzinfo=ZoneInfo(timezone_name) if timezone_name else zone)


def parse_ics_events(ics_text: str, zone: ZoneInfo) -> tuple[str, list[dict[str, object]]]:
    lines = unfold_ics(ics_text)
    name_line = next((line for line in lines if line.startswith("X-WR-CALNAME:")), None)
    if not name_line:
        raise RuntimeError("A DBN public calendar feed did not include a calendar name")
    calendar_name = unescape_ics(name_line.split(":", 1)[1]).strip()
    events: list[dict[str, object]] = []
    current: dict[str, tuple[str, str]] | None = None
    for line in lines:
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT" and current is not None:
            if "DTSTART" in current and "SUMMARY" in current:
                start_key, start_value = current["DTSTART"]
                start = parse_ics_datetime(start_key, start_value, zone)
                end = None
                if "DTEND" in current:
                    end_key, end_value = current["DTEND"]
                    end = parse_ics_datetime(end_key, end_value, zone)
                events.append({
                    "start": start,
                    "end": end,
                    "summary": unescape_ics(current["SUMMARY"][1]).strip(),
                    "location": unescape_ics(current.get("LOCATION", ("", ""))[1]).replace("\n", " ").strip(),
                })
            current = None
        elif current is not None and ":" in line:
            key, value = line.split(":", 1)
            field = key.split(";", 1)[0]
            if field in {"DTSTART", "DTEND", "SUMMARY", "LOCATION"}:
                current[field] = (key, value)
    return calendar_name, events


def season_for_date(day: date) -> tuple[int, int]:
    start_year = day.year if day.month >= 7 else day.year - 1
    return start_year, start_year + 1


def format_time(start: datetime | date, end: datetime | date | None) -> str:
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return ""
    return f"{start:%H:%M}–{end:%H:%M}"


def clean_location(value: str) -> str:
    value = " ".join(value.split()).strip(" ,")
    if not value or re.search(r"(?:volgt later|locatie volgt|volgt nog)", value, re.I):
        return "Venue to be announced"
    value = re.sub(r",\s*Nederland$", "", value, flags=re.I)
    return value


def build_schedule(calendar_payloads: list[str], as_of: date) -> dict[str, object] | None:
    start_year, end_year = season_for_date(as_of)
    season_start, season_end = date(start_year, 7, 1), date(end_year, 7, 1)
    zone = ZoneInfo(TIMEZONE)
    categorized: dict[str, list[dict[str, object]]] = {key: [] for key in COMPETITION_TYPES}
    for content in calendar_payloads:
        calendar_name, events = parse_ics_events(content, zone)
        for kind, config in COMPETITION_TYPES.items():
            if not config["calendar"].fullmatch(calendar_name):
                continue
            for event in events:
                summary = str(event["summary"])
                day_match = config["summary"].fullmatch(summary)
                if not day_match:
                    continue
                start = event["start"]
                event_date = start.date() if isinstance(start, datetime) else start
                if not season_start <= event_date < season_end:
                    continue
                day_number = int(day_match.group(1))
                if not 1 <= day_number <= 5:
                    raise RuntimeError(f"DBN agenda has an invalid competition day number: {summary}")
                categorized[kind].append({
                    "date": event_date.isoformat(),
                    "day": day_number,
                    "division": config["division"],
                    "timelines": list(config["timelines"]),
                    "time": format_time(event["start"], event["end"]),
                    "location": clean_location(str(event["location"])),
                    "title": config["title"].format(day=day_number),
                })
    # No entries for either competition means the next season agenda is not published yet.
    if not any(categorized.values()):
        return None
    if any(not categorized[kind] for kind in COMPETITION_TYPES):
        return None
    all_events: list[dict[str, object]] = []
    for kind, entries in categorized.items():
        entries.sort(key=lambda item: (str(item["date"]), int(item["day"])))
        days = [int(event["day"]) for event in entries]
        if len(days) != len(set(days)) or days != sorted(days):
            raise RuntimeError(f"DBN agenda contains duplicate or out-of-order {kind} competition days")
        if days != list(range(1, max(days) + 1)):
            raise RuntimeError(f"DBN agenda has a gap in {kind} competition day numbering")
        all_events.extend(entries)
    all_events.sort(key=lambda item: (str(item["date"]), str(item["division"])))
    return {
        "timezone": TIMEZONE,
        "season": f"{start_year}/{end_year}",
        "check_offsets_days": list(CHECK_OFFSETS_DAYS),
        "events": all_events,
    }


def cron_entries(schedule: dict[str, object]) -> list[str]:
    dates: set[date] = set()
    for event in schedule["events"]:
        event_day = date.fromisoformat(str(event["date"]))
        dates.update(event_day + timedelta(days=int(offset)) for offset in schedule["check_offsets_days"])
    by_month: dict[int, list[int]] = {}
    for check_date in sorted(dates):
        by_month.setdefault(check_date.month, []).append(check_date.day)
    return [f'    - cron: "0 8 {",".join(map(str, days))} {month} *"' for month, days in sorted(by_month.items())]


def replace_workflow_schedules(workflow_text: str, schedule: dict[str, object]) -> str:
    if workflow_text.count(WORKFLOW_MARKER_START) != 1 or workflow_text.count(WORKFLOW_MARKER_END) != 1:
        raise RuntimeError("Workflow is missing exactly one DBN agenda schedule marker pair")
    start = workflow_text.index(WORKFLOW_MARKER_START)
    end = workflow_text.index(WORKFLOW_MARKER_END, start)
    if end < start:
        raise RuntimeError("DBN agenda workflow schedule markers are out of order")
    generated = "\n".join([WORKFLOW_MARKER_START, *cron_entries(schedule), WORKFLOW_MARKER_END])
    end += len(WORKFLOW_MARKER_END)
    return workflow_text[:start] + generated + workflow_text[end:]


def extract_marker_body(page: str, marker: str) -> tuple[int, int, str]:
    begin = f"<!-- BEGIN TOURNIFY {marker} -->"
    end = f"<!-- END TOURNIFY {marker} -->"
    if page.count(begin) != 1 or page.count(end) != 1:
        raise RuntimeError(f"Competition page must have exactly one managed {marker} marker pair")
    start = page.index(begin) + len(begin)
    finish = page.index(end, start)
    if finish <= start:
        raise RuntimeError(f"Competition page has malformed managed {marker} markers")
    return start, finish, page[start:finish]


def placeholder_markup(event: dict[str, object]) -> str:
    import sync_tournify

    return "\n" + sync_tournify.render_schedule_placeholder(event) + "\n          "


def refresh_agenda_placeholders(page: str, schedule: dict[str, object], as_of: date) -> str:
    import sync_tournify

    updated = page
    placeholder_pattern = re.compile(
        r'(?m)^[ \t]*<div class="timeline-item" data-tournify-sync="true">'
        r'(?:(?!<div class="timeline-item").)*?'
        r'Fixtures are warming up — they will land here when Tournify publishes the lineup\.'
        r'.*?</div>\s*</div>\s*</div>\s*</div>',
        re.S,
    )
    for timeline in sync_tournify.TIMELINES:
        event = min(
            (item for item in schedule["events"] if timeline in item["timelines"] and date.fromisoformat(str(item["date"])) >= as_of),
            key=lambda item: str(item["date"]),
            default=None,
        )
        start, end, body = extract_marker_body(updated, f"SYNC: {timeline}")
        replacement = sync_tournify.render_schedule_placeholder(event) if event else ""
        refreshed, count = placeholder_pattern.subn(replacement, body, count=1)
        if count:
            body = refreshed
        elif event and not body.strip():
            body = "\n" + replacement + "\n          "
        updated = updated[:start] + body + updated[end:]
    return updated


def rotate_page_season(page: str, previous_season: str, schedule: dict[str, object], as_of: date) -> str:
    import sync_tournify

    current_season = str(schedule["season"])
    new_page = page
    for timeline in sync_tournify.TIMELINES:
        sync_start, sync_end, sync_body = extract_marker_body(new_page, f"SYNC: {timeline}")
        standings_start, standings_end, standings_body = extract_marker_body(new_page, f"STANDINGS: {timeline}")
        archived = (sync_body.strip() + "\n" + standings_body.strip()).strip()
        archive = (
            f'\n          <details class="dbn-season-archive" data-season="{html.escape(previous_season, quote=True)}">\n'
            f'            <summary>Season {html.escape(previous_season)} results and standings</summary>\n'
            f'            <div class="dbn-season-archive-content">\n{archived}\n            </div>\n'
            f'          </details>\n          '
        )
        current_event = min(
            (event for event in schedule["events"] if timeline in event["timelines"] and date.fromisoformat(str(event["date"])) >= as_of),
            key=lambda event: str(event["date"]),
            default=None,
        )
        active_sync = placeholder_markup(current_event) if current_event else "\n          "
        # Replace sync content before locating standings, since preceding offsets can shift.
        sync_start, sync_end, _ = extract_marker_body(new_page, f"SYNC: {timeline}")
        new_page = new_page[:sync_start] + active_sync + new_page[sync_end:]
        standings_start, standings_end, _ = extract_marker_body(new_page, f"STANDINGS: {timeline}")
        empty_standings = "\n" + sync_tournify.render_standings_unavailable(timeline) + "\n          "
        new_page = new_page[:standings_start] + empty_standings + new_page[standings_end:]
        marker_end = f"<!-- END TOURNIFY STANDINGS: {timeline} -->"
        insert_at = new_page.index(marker_end) + len(marker_end)
        new_page = new_page[:insert_at] + archive + new_page[insert_at:]
        timeline_start = new_page.index(f'<div class="timeline" id="timeline-{timeline}"')
        tag_end = new_page.index(">", timeline_start)
        opening_tag = new_page[timeline_start:tag_end + 1]
        if "data-season=" in opening_tag:
            opening_tag = re.sub(r'data-season="[^"]*"', f'data-season="{html.escape(current_season, quote=True)}"', opening_tag)
        else:
            opening_tag = opening_tag[:-1] + f' data-season="{html.escape(current_season, quote=True)}">'
        new_page = new_page[:timeline_start] + opening_tag + new_page[tag_end + 1:]
    new_page = re.sub(
        r"<body([^>]*)>",
        lambda match: _body_with_season(match.group(1), current_season),
        new_page,
        count=1,
    )
    return new_page


def _body_with_season(attrs: str, season: str) -> str:
    attrs = re.sub(r'\sdata-current-season="[^"]*"', "", attrs)
    return f'<body{attrs} data-current-season="{html.escape(season, quote=True)}">'


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode if path.exists() else 0o644
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            handle.write(content)
            handle.flush()
            os.fchmod(handle.fileno(), mode & 0o777)
            temporary = Path(handle.name)
        temporary.replace(path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def write_github_output(path: str | None, values: dict[str, str]) -> None:
    if not path:
        return
    with open(path, "a", encoding="utf-8") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agenda-url", default=AGENDA_URL)
    parser.add_argument("--schedule-file", type=Path, default=Path("data/dbn_competition_days.json"))
    parser.add_argument("--workflow-file", type=Path, default=Path(".github/workflows/tournify-sync.yml"))
    parser.add_argument("--site-file", type=Path, default=Path("competitions.html"))
    parser.add_argument("--as-of", help="Use a local date (YYYY-MM-DD) for a deterministic run")
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    args = parser.parse_args()

    try:
        previous = json.loads(args.schedule_file.read_text(encoding="utf-8"))
        zone = ZoneInfo(TIMEZONE)
        today = date.fromisoformat(args.as_of) if args.as_of else datetime.now(zone).date()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not load the current DBN schedule or date: {exc}") from exc

    page_html = read_url(args.agenda_url, "text/html")
    calendar_ids = agenda_calendar_ids(page_html)
    feeds = [read_url(f"https://calendar.google.com/calendar/ical/{quote(calendar_id, safe='@')}/public/basic.ics", "text/calendar") for calendar_id in calendar_ids]
    updated = build_schedule(feeds, today)
    if updated is None:
        message = f"No complete {season_for_date(today)[0]}/{season_for_date(today)[1]} DBN competition agenda is published yet; left the site and schedule unchanged."
        print(message)
        write_github_output(args.github_output, {"changed": "false", "season_changed": "false"})
        return 0

    prior_year = int(str(previous["season"]).split("/", 1)[0])
    next_year = int(str(updated["season"]).split("/", 1)[0])
    if next_year < prior_year:
        raise RuntimeError(f"DBN agenda season {updated['season']} is older than checked-in season {previous['season']}; no files were changed")
    season_changed = next_year > prior_year
    new_workflow = replace_workflow_schedules(args.workflow_file.read_text(encoding="utf-8"), updated)
    new_schedule_text = json.dumps(updated, indent=2, ensure_ascii=False)
    new_schedule_text = re.sub(
        r'("(?:check_offsets_days|timelines)": )\[\n\s*([^\]]*?)\n\s*\]',
        lambda match: match.group(1) + json.dumps(
            json.loads("[" + match.group(2) + "]"), ensure_ascii=False
        ),
        new_schedule_text,
    ) + "\n"
    old_schedule_text = args.schedule_file.read_text(encoding="utf-8")
    new_page = args.site_file.read_text(encoding="utf-8")
    if season_changed:
        new_page = rotate_page_season(new_page, str(previous["season"]), updated, today)
    else:
        new_page = refresh_agenda_placeholders(new_page, updated, today)
    changed = new_schedule_text != old_schedule_text or new_workflow != args.workflow_file.read_text(encoding="utf-8") or new_page != args.site_file.read_text(encoding="utf-8")
    if changed:
        # Validation and rendering finish before any write. Each file is atomically replaced.
        write_atomic(args.schedule_file, new_schedule_text)
        write_atomic(args.workflow_file, new_workflow)
        if new_page != args.site_file.read_text(encoding="utf-8"):
            write_atomic(args.site_file, new_page)
    message = f"DBN agenda {updated['season']}: {len(updated['events'])} competition days; {'updated' if changed else 'already current'}."
    print(message)
    write_github_output(args.github_output, {"changed": str(changed).lower(), "season_changed": str(season_changed).lower()})
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
