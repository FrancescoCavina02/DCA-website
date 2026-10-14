#!/usr/bin/env python3
"""Stage selected DBN team fixtures from Tournify in competitions.html.

The live Tournify page exposes public Firestore reads. This script only performs
read requests to Tournify and only replaces the explicitly marked sections in
competitions.html. Hand-entered HTML is parsed for duplicates and conflicts,
then left byte-for-byte untouched.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import stat
import sys
import tempfile
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LIVE_URL = "https://www.tournify.nl/live/dbn-competitie"
USER_AGENT = "League-Wire-DCA-results-sync/1.0"
COLLECTIONS = ("teams", "divisions", "days", "matches")
DIVISION_TO_TIMELINE = {
    "1e divisie | heren": ("man1", "DCA 1"),
    "2e divisie | heren": ("man2", "DCA 2"),
    "1e divisie | mixed": ("mixed1", "DCA 1"),
    "2e divisie | mixed": ("mixed2", "DCA 2"),
}
TEAM_NAME_TO_TIMELINE = {
    "damdelftdames": ("women", "DAM Delft Dames"),
}
TIMELINES = ("man1", "man2", "women", "mixed1", "mixed2")
STANDINGS_TIMELINES = ("mixed1", "mixed2")
SCHEDULE_FILE = Path(__file__).resolve().parents[1] / "data" / "dbn_competition_days.json"

# Short labels already used on the DCA competition page. Unknown opponents keep
# their Tournify names after the division marker has been removed.
OPPONENT_LABELS = {
    "u treffers": "Utrecht",
    "u treffers 2": "Utrecht 2",
    "delftsche dodgers": "Delft",
    "delftsche dodgers 2": "Delft 2",
    "haarlem dodge": "Haarlem",
    "dodgeball eindhoven": "Eindhoven",
    "dodgeball eindhoven 2": "Eindhoven",
    "hgv hengelo": "Hengelo",
}


def request_json(url: str, *, method: str = "GET", body: Any = None) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        detail = getattr(exc, "reason", None) or str(exc)
        raise RuntimeError(f"Tournify request failed: {detail}") from exc


def parse_public_firebase_config(live_url: str) -> tuple[str, str]:
    page = request_json_text(live_url)
    api_key = re.search(r'apiKey\s*:\s*["\']([^"\']+)', page)
    project_id = re.search(r'projectId\s*:\s*["\']([^"\']+)', page)
    if not api_key or not project_id:
        raise RuntimeError("Could not read the public Firebase config from Tournify's live page")
    return project_id.group(1), api_key.group(1)


def request_json_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    try:
        with urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8")
    except (HTTPError, URLError, TimeoutError, UnicodeDecodeError) as exc:
        detail = getattr(exc, "reason", None) or str(exc)
        raise RuntimeError(f"Could not read Tournify's live page: {detail}") from exc


def decode_firestore(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    for key in (
        "stringValue",
        "booleanValue",
        "integerValue",
        "doubleValue",
        "timestampValue",
        "nullValue",
        "referenceValue",
        "bytesValue",
    ):
        if key in value:
            result = value[key]
            if key == "integerValue":
                try:
                    return int(result)
                except (TypeError, ValueError):
                    return result
            if key == "doubleValue":
                try:
                    return float(result)
                except (TypeError, ValueError):
                    return result
            return result
    if "arrayValue" in value:
        return [decode_firestore(item) for item in value["arrayValue"].get("values", [])]
    if "mapValue" in value:
        return {
            key: decode_firestore(item)
            for key, item in value["mapValue"].get("fields", {}).items()
        }
    return value


def decode_document(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": document["name"].rsplit("/", 1)[-1],
        **{
            key: decode_firestore(value)
            for key, value in document.get("fields", {}).items()
        },
    }


def firestore_base(project_id: str) -> str:
    return f"https://firestore.googleapis.com/v1/projects/{project_id}/databases/(default)/documents"


def fetch_tournament(project_id: str, api_key: str) -> dict[str, Any]:
    endpoint = f"{firestore_base(project_id)}:runQuery?{urlencode({'key': api_key})}"
    query = {
        "structuredQuery": {
            "from": [{"collectionId": "tournaments"}],
            "where": {
                "fieldFilter": {
                    "field": {"fieldPath": "liveLink"},
                    "op": "EQUAL",
                    "value": {"stringValue": "dbn-competitie"},
                }
            },
            "limit": 10,
        }
    }
    response = request_json(endpoint, method="POST", body=query)
    documents = [row["document"] for row in response if isinstance(row, dict) and "document" in row]
    if len(documents) != 1:
        raise RuntimeError(
            "Expected one public Tournify tournament with liveLink 'dbn-competitie'; "
            f"found {len(documents)}"
        )
    return decode_document(documents[0])


def fetch_collection(project_id: str, api_key: str, tournament_id: str, name: str) -> list[dict[str, Any]]:
    parent = f"{firestore_base(project_id)}/tournaments/{quote(tournament_id, safe='')}/{name}"
    page_token: str | None = None
    documents: list[dict[str, Any]] = []
    while True:
        params = {"key": api_key, "pageSize": 1000}
        if page_token:
            params["pageToken"] = page_token
        payload = request_json(f"{parent}?{urlencode(params)}")
        documents.extend(decode_document(document) for document in payload.get("documents", []))
        page_token = payload.get("nextPageToken")
        if not page_token:
            return documents


def fetch_tournify(live_url: str) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    project_id, api_key = parse_public_firebase_config(live_url)
    tournament = fetch_tournament(project_id, api_key)
    collections = {
        name: fetch_collection(project_id, api_key, tournament["id"], name)
        for name in COLLECTIONS
    }
    return tournament, collections


def as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalized_division(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def clean_team_name(name: str) -> str:
    cleaned = unicodedata.normalize("NFKC", name).strip()
    cleaned = re.sub(r"\s*[♂♀⚥]\s*\d*\s*$", "", cleaned).strip()
    cleaned = re.sub(r"(?<=\D)1$", "", cleaned).strip()
    cleaned = re.sub(r"(?<=\D)2$", " 2", cleaned).strip()
    return cleaned


def canonical_team_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", clean_team_name(name).casefold())


def canonical_opponent(name: str) -> str:
    normalized = unicodedata.normalize("NFKC", name).casefold()
    normalized = " ".join(normalized.replace("–", "-").split())
    normalized = re.sub(r"[^\w ]+", " ", normalized)
    normalized = " ".join(normalized.split())
    return OPPONENT_LABELS.get(normalized, normalized)


def event_date(value: Any, timezone_name: str) -> datetime | None:
    timestamp = as_int(value)
    if timestamp is None:
        return None
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        zone = timezone.utc
    return datetime.fromtimestamp(timestamp, tz=zone)


def schedule_for_match(tournament: dict[str, Any], match: dict[str, Any]) -> dict[str, Any]:
    round_times = tournament.get("roundTimes") or {}
    round_key = str(match.get("roundN", match.get("round", "")))
    value = round_times.get(round_key, {}) if isinstance(round_times, dict) else {}
    return value if isinstance(value, dict) else {}


def match_date(
    tournament: dict[str, Any],
    match: dict[str, Any],
    days_by_id: dict[str, dict[str, Any]],
) -> tuple[str | None, str | None]:
    schedule = schedule_for_match(tournament, match)
    day_id = match.get("day")
    if day_id is None:
        day_id = schedule.get("day")
    if day_id is None:
        raw_date = match.get("date")
    elif str(day_id) == "0":
        raw_date = match.get("date", tournament.get("date"))
    else:
        raw_date = match.get("date") or (days_by_id.get(str(day_id)) or {}).get("date")
    when = event_date(raw_date, str(tournament.get("timezone") or "UTC"))
    time_value = match.get("st") or match.get("startTime") or schedule.get("time")
    time_text = str(time_value).strip() if time_value else None
    if time_text and not re.fullmatch(r"\d{1,2}:\d{2}", time_text):
        time_text = None
    return (when.date().isoformat() if when else None, time_text)


def opponent_label(name: str) -> str:
    cleaned = clean_team_name(name)
    key = canonical_opponent(cleaned)
    short_labels = {
        "u treffers": "Utrecht",
        "u treffers 2": "Utrecht 2",
        "delftsche dodgers": "Delft",
        "delftsche dodgers 2": "Delft 2",
        "haarlem dodge": "Haarlem",
        "dodgeball eindhoven": "Eindhoven",
        "dodgeball eindhoven 2": "Eindhoven",
        "hgv hengelo": "Hengelo",
    }
    return short_labels.get(key, cleaned)


def extract_fixtures(
    tournament: dict[str, Any], collections: dict[str, list[dict[str, Any]]]
) -> tuple[list[dict[str, Any]], set[str], list[str]]:
    divisions = {doc["id"]: str(doc.get("name", "")) for doc in collections["divisions"]}
    teams = collections["teams"]
    teams_by_poule_and_position: dict[tuple[str, str], dict[str, Any]] = {}
    target_team_ids: set[str] = set()
    mapped_timelines: set[str] = set()
    unmapped_dca: list[str] = []

    for team in teams:
        name = str(team.get("name", ""))
        division_name = divisions.get(str(team.get("division", "")), "")
        team_target = TEAM_NAME_TO_TIMELINE.get(canonical_team_name(name))
        target = team_target or DIVISION_TO_TIMELINE.get(normalized_division(division_name))
        team_info = {
            "id": team["id"],
            "name": name,
            "timeline": target[0] if target else None,
            "site_name": target[1] if target else None,
            "division": division_name,
        }
        is_target_team = name.casefold().startswith("dca") or team_target is not None
        if is_target_team:
            target_team_ids.add(team["id"])
            if target:
                mapped_timelines.add(target[0])
            elif name.casefold().startswith("dca"):
                unmapped_dca.append(f"{name} ({division_name or 'division unknown'})")
        for field, poule_id in team.items():
            phase = re.fullmatch(r"poule(\d+)", field)
            if not phase or not poule_id:
                continue
            position = team.get(f"numInPoule{phase.group(1)}")
            if position is None:
                continue
            teams_by_poule_and_position[(str(poule_id), str(position))] = team_info

    days_by_id = {doc["id"]: doc for doc in collections["days"]}
    fixtures: list[dict[str, Any]] = []
    for match in collections["matches"]:
        poule_id = str(match.get("poule", ""))
        side1 = teams_by_poule_and_position.get((poule_id, str(match.get("team1", ""))))
        side2 = teams_by_poule_and_position.get((poule_id, str(match.get("team2", ""))))
        if not side1 or not side2:
            continue
        target1 = side1["id"] in target_team_ids
        target2 = side2["id"] in target_team_ids
        if not target1 and not target2:
            continue
        if target1 and target2:
            # An internal target-team fixture belongs in both timelines, one row per side.
            target_sides = (
                (side1, side2, match.get("score1"), match.get("score2")),
                (side2, side1, match.get("score2"), match.get("score1")),
            )
        else:
            target_sides = ((side1, side2, match.get("score1"), match.get("score2")),) if target1 else (
                (side2, side1, match.get("score2"), match.get("score1")),
            )
        date_iso, time_text = match_date(tournament, match, days_by_id)
        for target_team, opponent, target_score, opponent_score in target_sides:
            if not target_team["timeline"]:
                unmapped_dca.append(
                    f"match {match['id']}: {target_team['name']} has no website timeline mapping"
                )
                continue
            score_for = as_int(target_score)
            score_against = as_int(opponent_score)
            played = score_for is not None and score_against is not None
            if played:
                result = (
                    "win"
                    if score_for > score_against
                    else "loss"
                    if score_for < score_against
                    else "draw"
                )
                icon = {"win": "✅", "loss": "❌", "draw": "➖"}[result]
            else:
                result = "upcoming"
                icon = "⏳"
            fixtures.append(
                {
                    "id": match["id"],
                    "timeline": target_team["timeline"],
                    "site_name": target_team["site_name"],
                    "opponent": opponent_label(opponent["name"]),
                    "opponent_key": canonical_opponent(opponent_label(opponent["name"])),
                    "date": date_iso,
                    "time": time_text,
                    "score_for": score_for,
                    "score_against": score_against,
                    "played": played,
                    "result": result,
                    "icon": icon,
                }
            )
    fixtures.sort(
        key=lambda item: (
            item["timeline"],
            item["date"] or "9999-99-99",
            item["time"] or "99:99",
            item["id"],
        )
    )
    return fixtures, mapped_timelines, sorted(set(unmapped_dca))


def _ranking_number(rules: dict[str, Any], key: str, default: int) -> int:
    value = rules.get(key)
    if value is None or value is False or value == "":
        return default
    parsed = as_int(value)
    return parsed if parsed is not None else default


def _points_for_match(
    rules: dict[str, Any], score_for: int, score_against: int, winner: str | None = None,
    slot1: str | None = None, slot2: str | None = None,
) -> tuple[int, int]:
    advanced = bool(rules.get("advancedGroupRanking"))
    if rules.get("goalsAsPoints") and advanced:
        return score_for, score_against
    if score_for > score_against:
        margin = _ranking_number(rules, "pointsBigWinMargin", 2)
        winner = _ranking_number(rules, "pointsWin", 3)
        if advanced and score_for - score_against >= margin:
            winner = _ranking_number(rules, "pointsBigWin", 3)
        return winner, _ranking_number(rules, "pointsLoss", 0)
    if score_for < score_against:
        margin = _ranking_number(rules, "pointsBigWinMargin", 2)
        winner = _ranking_number(rules, "pointsWin", 3)
        if advanced and score_against - score_for >= margin:
            winner = _ranking_number(rules, "pointsBigWin", 3)
        return _ranking_number(rules, "pointsLoss", 0), winner
    if advanced and rules.get("noDraw") and winner in {slot1, slot2}:
        win_points = _ranking_number(rules, "pointsWinOnPenalties", 3)
        loss_points = _ranking_number(rules, "pointsLossOnPenalties", 0)
        return (win_points, loss_points) if winner == slot1 else (loss_points, win_points)
    if advanced and score_for == 0:
        draw_points = _ranking_number(rules, "pointsTieNoGoals", 1)
    else:
        draw_points = _ranking_number(rules, "pointsTie", 1)
    return draw_points, draw_points


def extract_standings(
    tournament: dict[str, Any], collections: dict[str, list[dict[str, Any]]]
) -> dict[str, list[dict[str, Any]]]:
    """Recreate the active Mixed poule standings from Tournify's public Firestore data."""
    if not collections.get("teams") or not collections.get("divisions") or not collections.get("matches"):
        raise RuntimeError("Tournify returned an empty teams, divisions, or matches collection; kept existing standings")

    division_names = {doc["id"]: str(doc.get("name", "")) for doc in collections["divisions"]}
    mixed_divisions = {
        normalized_division(name): (timeline, site_name)
        for name, (timeline, site_name) in DIVISION_TO_TIMELINE.items()
        if timeline in STANDINGS_TIMELINES
    }
    poules: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = {}
    target_poules: dict[str, set[tuple[str, str, str]]] = {
        timeline: set() for timeline in STANDINGS_TIMELINES
    }

    for team in collections["teams"]:
        division_id = str(team.get("division", ""))
        division_name = division_names.get(division_id, "")
        target = mixed_divisions.get(normalized_division(division_name))
        if not target:
            continue
        timeline, site_name = target
        is_dca = canonical_team_name(str(team.get("name", ""))).startswith("dca")
        for field, poule_id in team.items():
            phase_match = re.fullmatch(r"poule(\d+)", field)
            if not phase_match or not poule_id:
                continue
            phase = phase_match.group(1)
            slot = as_int(team.get(f"numInPoule{phase}"))
            if slot is None:
                raise RuntimeError(f"Tournify team {team.get('name', team.get('id'))} has a poule without a position")
            key = (timeline, phase, str(poule_id))
            members = poules.setdefault(key, {})
            if str(slot) in members:
                raise RuntimeError(f"Tournify returned duplicate team position {slot} in poule {poule_id}")
            members[str(slot)] = {
                "id": str(team["id"]),
                "name": site_name if is_dca else clean_team_name(str(team.get("name", ""))),
                "is_dca": is_dca,
                "timeline": timeline,
                "division_id": division_id,
                "phase": phase,
                "slot": str(slot),
            }
            if is_dca:
                target_poules[timeline].add(key)

    result: dict[str, list[dict[str, Any]]] = {}
    scoring_types = tournament.get("scoringTypesPerDivisionPerPhase") or {}
    alternate_rules = tournament.get("scoringTypes") or {}
    for timeline in STANDINGS_TIMELINES:
        groups = target_poules[timeline]
        if len(groups) != 1:
            raise RuntimeError(
                f"Expected one active Mixed poule for {timeline}; found {len(groups)}. Kept existing standings"
            )
        key = next(iter(groups))
        _, phase, poule_id = key
        members = poules[key]
        dca_members = [member for member in members.values() if member["is_dca"]]
        if len(members) < 2 or len(dca_members) != 1:
            raise RuntimeError(f"Tournify returned incomplete teams for {timeline}; kept existing standings")

        division_id = dca_members[0]["division_id"]
        type_name = str((scoring_types.get(division_id) or {}).get(phase, "default"))
        rules = tournament if type_name == "default" else alternate_rules.get(type_name)
        if not isinstance(rules, dict):
            raise RuntimeError(f"Tournify has unsupported scoring rules for {timeline}; kept existing standings")
        if rules.get("useSets") in (True, 1, "1"):
            raise RuntimeError(f"Set-based standings are not supported for {timeline}; kept existing standings")
        members_by_slot = members
        stats = {
            slot: {
                **member,
                "played": 0,
                "won": 0,
                "lost": 0,
                "points": 0,
                "score_for": 0,
                "score_against": 0,
                "point_difference": 0,
            }
            for slot, member in members_by_slot.items()
        }
        scored_matches: list[dict[str, Any]] = []
        for match in collections["matches"]:
            if str(match.get("poule", "")) != poule_id:
                continue
            slot1, slot2 = str(match.get("team1", "")), str(match.get("team2", ""))
            if slot1 not in stats or slot2 not in stats:
                raise RuntimeError(f"Tournify returned a match with unknown team positions in poule {poule_id}")
            score1, score2 = as_int(match.get("score1")), as_int(match.get("score2"))
            if score1 is None or score2 is None:
                continue
            winner = str(match.get("winner")) if match.get("winner") is not None else None
            points1, points2 = _points_for_match(rules, score1, score2, winner, slot1, slot2)
            goal1, goal2 = score1, score2
            if (
                score1 == score2
                and rules.get("advancedGroupRanking")
                and rules.get("noDraw")
                and rules.get("enterPenaltyScores")
                and rules.get("includePenaltiesInGoals")
                and not rules.get("useSets")
            ):
                penalty1 = as_int(match.get("penalties1")) or 0
                penalty2 = as_int(match.get("penalties2")) or 0
                goal1 += penalty1
                goal2 += penalty2
            for slot, score_for, score_against, points in (
                (slot1, goal1, goal2, points1),
                (slot2, goal2, goal1, points2),
            ):
                row = stats[slot]
                row["played"] += 1
                row["points"] += points
                row["score_for"] += score_for
                row["score_against"] += score_against
                row["point_difference"] += score_for - score_against
            if score1 > score2:
                stats[slot1]["won"] += 1
                stats[slot2]["lost"] += 1
            elif score1 < score2:
                stats[slot2]["won"] += 1
                stats[slot1]["lost"] += 1
            else:
                if rules.get("advancedGroupRanking") and rules.get("noDraw"):
                    if winner == slot1:
                        stats[slot1]["won"] += 1
                        stats[slot2]["lost"] += 1
                    elif winner == slot2:
                        stats[slot2]["won"] += 1
                        stats[slot1]["lost"] += 1
            scored_matches.append({
                "slot1": slot1,
                "slot2": slot2,
                "score1": goal1,
                "score2": goal2,
                "points1": points1,
                "points2": points2,
            })

        if not scored_matches or stats[dca_members[0]["slot"]]["played"] == 0:
            raise RuntimeError(f"Tournify has no completed Mixed results for {timeline}; kept existing standings")

        points_groups: dict[int, list[str]] = defaultdict(list)
        for slot, row in stats.items():
            points_groups[row["points"]].append(slot)
        ordered_slots: list[str] = []
        for points_total in sorted(points_groups, reverse=True):
            tied_slots = points_groups[points_total]
            head_to_head = {
                slot: {"points": 0, "difference": 0, "for": 0}
                for slot in tied_slots
            }
            tied_set = set(tied_slots)
            direct_matches = [
                match for match in scored_matches
                if match["slot1"] in tied_set and match["slot2"] in tied_set
            ]
            for match in direct_matches:
                first, second = match["slot1"], match["slot2"]
                # Tournify's recursive head-to-head tiebreak uses points, difference, then points scored.
                score1, score2 = match["score1"], match["score2"]
                head_to_head[first]["points"] += match["points1"]
                head_to_head[second]["points"] += match["points2"]
                head_to_head[first]["difference"] += score1 - score2
                head_to_head[second]["difference"] += score2 - score1
                head_to_head[first]["for"] += score1
                head_to_head[second]["for"] += score2
            has_head_to_head = bool(direct_matches)
            tied_slots.sort(
                key=lambda slot: (
                    head_to_head[slot]["points"] if has_head_to_head else 0,
                    head_to_head[slot]["difference"] if has_head_to_head else 0,
                    head_to_head[slot]["for"] if has_head_to_head else 0,
                    stats[slot]["won"],
                    stats[slot]["point_difference"],
                    stats[slot]["score_for"],
                    stats[slot]["name"].casefold(),
                ),
                reverse=True,
            )
            ordered_slots.extend(tied_slots)

        result[timeline] = []
        for position, slot in enumerate(ordered_slots, start=1):
            row = stats[slot]
            result[timeline].append({**row, "position": position})
    return result


def render_standings_table(timeline: str, rows: list[dict[str, Any]]) -> str:
    division = "1st Division" if timeline == "mixed1" else "2nd Division"
    body = []
    for row in rows:
        css_class = ' class="dca-team-row"' if row["is_dca"] else ""
        point_difference = int(row["point_difference"])
        difference_label = f"+{point_difference}" if point_difference > 0 else str(point_difference)
        body.append(
            f"              <tr{css_class}>"
            f"<th scope=\"row\">{row['position']}</th>"
            f"<td class=\"standings-team\">{html.escape(row['name'])}</td>"
            f"<td>{row['played']}</td><td>{row['won']}</td><td>{row['lost']}</td>"
            f"<td>{row['points']}</td><td>{difference_label}</td></tr>"
        )
    table_rows = "\n".join(body)
    return (
        f'          <section class="dbn-standings" aria-labelledby="standings-{timeline}-title">\n'
        f'            <h3 class="dbn-standings-title" id="standings-{timeline}-title">DBN Mixed Standings</h3>\n'
        f'            <p class="dbn-standings-division">{division}</p>\n'
        f'            <div class="dbn-standings-scroll" role="region" aria-label="{division} standings table" tabindex="0">\n'
        f'              <table class="dbn-standings-table">\n'
        f'                <caption class="visually-hidden">DBN Mixed {division} standings from Tournify</caption>\n'
        f'                <thead><tr><th scope="col">Position</th><th scope="col" class="standings-team">Team</th>'
        f'<th scope="col">Played</th><th scope="col">Won</th><th scope="col">Lost</th>'
        f'<th scope="col">Points</th><th scope="col">PD</th></tr></thead>\n'
        f'                <tbody>\n{table_rows}\n                </tbody>\n'
        f'              </table>\n'
        f'            </div>\n'
        f'          </section>'
    )


def replace_standings_sections(
    site_html: str, standings: dict[str, list[dict[str, Any]]]
) -> str:
    for timeline in STANDINGS_TIMELINES:
        start = f"<!-- BEGIN TOURNIFY STANDINGS: {timeline} -->"
        end = f"<!-- END TOURNIFY STANDINGS: {timeline} -->"
        if site_html.count(start) != 1 or site_html.count(end) != 1:
            raise RuntimeError(f"Expected exactly one managed standings marker pair for {timeline}")
        start_index = site_html.index(start) + len(start)
        end_index = site_html.index(end, start_index)
        if end_index < start_index:
            raise RuntimeError(f"Managed standings markers are out of order for {timeline}")
        rendered = "\n" + render_standings_table(timeline, standings[timeline]) + "\n          "
        site_html = site_html[:start_index] + rendered + site_html[end_index:]
    return site_html


class Element:
    def __init__(self, tag: str, attrs: list[tuple[str, str | None]], parent: "Element | None" = None):
        self.tag = tag
        self.attrs = dict(attrs)
        self.parent = parent
        self.children: list[Element | str] = []

    def text(self) -> str:
        return "".join(child.text() if isinstance(child, Element) else child for child in self.children)

    def descendants(self):
        for child in self.children:
            if isinstance(child, Element):
                yield child
                yield from child.descendants()

    def has_class(self, name: str) -> bool:
        return name in (self.attrs.get("class") or "").split()


class SiteTreeParser(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Element("root", [])
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Element(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.stack[-1].children.append(Element(tag, attrs, self.stack[-1]))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def find_first(node: Element, predicate) -> Element | None:
    for descendant in node.descendants():
        if predicate(descendant):
            return descendant
    return None


def site_date_iso(value: str) -> str | None:
    try:
        return datetime.strptime(" ".join(value.split()), "%d %B %Y").date().isoformat()
    except ValueError:
        return None


def dca_site_slot(team_text: str, timeline: str) -> str | None:
    normalized = unicodedata.normalize("NFKC", team_text).casefold()
    match = re.search(r"\bdca\s*([12])\b", normalized)
    if match:
        return match.group(1)
    if timeline == "women" and canonical_team_name(team_text) == "damdelftdames":
        return "women"
    return None


def extract_manual_entries(site_html: str) -> dict[tuple[str, str, str], list[tuple[int | None, int | None]]]:
    parser = SiteTreeParser()
    parser.feed(site_html)
    manual: dict[tuple[str, str, str], list[tuple[int | None, int | None]]] = defaultdict(list)
    timelines = {
        node.attrs.get("id", "").removeprefix("timeline-"): node
        for node in parser.root.descendants()
        if node.attrs.get("id", "").startswith("timeline-")
    }
    for timeline in TIMELINES:
        timeline_node = timelines.get(timeline)
        if not timeline_node:
            continue
        for card in timeline_node.descendants():
            if not isinstance(card, Element) or not card.has_class("timeline-item"):
                continue
            date_node = find_first(card, lambda node: node.has_class("competition-date"))
            date_iso = site_date_iso(date_node.text()) if date_node else None
            if not date_iso:
                continue
            for row in card.descendants():
                if row.tag != "li" or not row.has_class("match-item") or row.attrs.get("data-tournify-match-id"):
                    continue
                teams_node = find_first(row, lambda node: node.has_class("teams"))
                score_node = find_first(row, lambda node: node.has_class("score"))
                if not teams_node:
                    continue
                sides = re.split(r"\s+vs\s+", " ".join(teams_node.text().split()), maxsplit=1, flags=re.IGNORECASE)
                if len(sides) != 2:
                    continue
                slot1 = dca_site_slot(sides[0], timeline)
                slot2 = dca_site_slot(sides[1], timeline)
                if bool(slot1) == bool(slot2):
                    continue
                slot = slot1 or slot2
                opponent = sides[1] if slot1 else sides[0]
                score_match = re.fullmatch(r"\s*(\d+)\s*[-–]\s*(\d+)\s*", score_node.text() if score_node else "")
                score = (None, None)
                if score_match:
                    first_score, second_score = int(score_match.group(1)), int(score_match.group(2))
                    score = (first_score, second_score) if slot1 else (second_score, first_score)
                key = (timeline, date_iso, canonical_opponent(opponent))
                manual[key].append(score)
    return manual


def english_date(date_iso: str) -> str:
    return datetime.strptime(date_iso, "%Y-%m-%d").strftime("%d %B %Y")


def load_competition_schedule(path: Path) -> dict[str, Any]:
    try:
        schedule = json.loads(path.read_text(encoding="utf-8"))
        ZoneInfo(schedule["timezone"])
        for event in schedule["events"]:
            date.fromisoformat(event["date"])
            if not set(event["timelines"]).issubset(TIMELINES):
                raise ValueError(f"Unknown timeline in schedule event: {event}")
        return schedule
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read competition schedule {path}: {exc}") from exc


def next_schedule_event(
    schedule: dict[str, Any], timeline: str, on_or_after: date
) -> dict[str, Any] | None:
    events = [
        event
        for event in schedule["events"]
        if timeline in event["timelines"] and date.fromisoformat(event["date"]) >= on_or_after
    ]
    return min(events, key=lambda event: event["date"]) if events else None


def schedule_check_dates(schedule: dict[str, Any]) -> set[date]:
    offsets = [int(value) for value in schedule.get("check_offsets_days", [1, 8])]
    return {
        date.fromisoformat(event["date"]) + timedelta(days=offset)
        for event in schedule["events"]
        for offset in offsets
    }


def format_event_location(event: dict[str, Any]) -> str:
    pieces = [event.get("time", "").strip(), event.get("location", "").strip()]
    return " · ".join(piece for piece in pieces if piece)


def render_fixture(fixture: dict[str, Any], today: date) -> str:
    teams = f"{fixture['site_name']} vs {fixture['opponent']}"
    if fixture["time"]:
        teams += f" ({fixture['time']})"
    match_id = html.escape(fixture["id"], quote=True)
    if fixture["played"]:
        score = f"{fixture['score_for']} - {fixture['score_against']}"
        item_class = f"match-item {fixture['result']}"
    else:
        is_past = fixture["date"] and date.fromisoformat(fixture["date"]) < today
        score = "Results pending" if is_past else "Upcoming"
        item_class = "match-item upcoming"
    return (
        f'                    <li class="{item_class}" data-tournify-match-id="{match_id}">\n'
        f'                      <span class="teams">{html.escape(teams)}</span>\n'
        f'                      <span class="score">{html.escape(score)}</span>\n'
        f'                      <span class="result-icon">{fixture["icon"]}</span>\n'
        f"                    </li>"
    )


def render_group(
    date_iso: str | None,
    fixtures: list[dict[str, Any]],
    event: dict[str, Any] | None,
    place: str,
    today: date,
) -> str:
    if date_iso:
        date_label = english_date(date_iso)
        heading = event["title"] if event else "DBN Competition"
        venue_text = format_event_location(event) if event else place
        venue = f'                  <p class="venue">{html.escape(venue_text)}</p>\n' if venue_text else ""
    else:
        date_label = "Date pending"
        heading = "DBN Competition · results"
        venue = '                  <p class="venue">Tournify has not assigned a date</p>\n'
    entries = "\n".join(render_fixture(fixture, today) for fixture in fixtures)
    return (
        '          <div class="timeline-item" data-tournify-sync="true">\n'
        '            <div class="timeline-content">\n'
        '              <div class="date-medal-container">\n'
        f'                <div class="competition-date">{html.escape(date_label)}</div>\n'
        '              </div>\n'
        '              <div class="competition-header">\n'
        '                <div class="trophy-icon">🏆</div>\n'
        '                <div class="competition-info">\n'
        f'                  <h3>{html.escape(heading)}</h3>\n'
        f"{venue}"
        '                </div>\n'
        '              </div>\n'
        '              <div class="competition-details">\n'
        '                <div class="results-list">\n'
        '                  <h4 class="results-title">Match Results</h4>\n'
        '                  <ul class="match-results">\n'
        f"{entries}\n"
        '                  </ul>\n'
        '                </div>\n'
        '              </div>\n'
        '            </div>\n'
        '          </div>'
    )


def render_schedule_placeholder(event: dict[str, Any]) -> str:
    date_label = english_date(event["date"])
    details = format_event_location(event)
    return (
        '          <div class="timeline-item" data-tournify-sync="true">\n'
        '            <div class="timeline-content">\n'
        '              <div class="date-medal-container">\n'
        f'                <div class="competition-date">{html.escape(date_label)}</div>\n'
        '              </div>\n'
        '              <div class="competition-header">\n'
        '                <div class="trophy-icon">🏆</div>\n'
        '                <div class="competition-info">\n'
        f'                  <h3>{html.escape(event["title"])}</h3>\n'
        f'                  <p class="venue">{html.escape(details)}</p>\n'
        '                  <p class="venue">Fixtures are warming up — they will land here when Tournify publishes the lineup.</p>\n'
        '                </div>\n'
        '              </div>\n'
        '            </div>\n'
        '          </div>'
    )


def replace_managed_sections(
    site_html: str,
    fixtures: list[dict[str, Any]],
    mapped_timelines: set[str],
    place: str,
    schedule: dict[str, Any],
    today: date,
) -> tuple[str, dict[tuple[str, str | None], list[dict[str, Any]]]]:
    manual = extract_manual_entries(site_html)
    by_timeline: dict[str, list[dict[str, Any]]] = defaultdict(list)
    conflicts: list[str] = []
    duplicates = 0
    for fixture in fixtures:
        key = (fixture["timeline"], fixture["date"], fixture["opponent_key"])
        existing = manual.get(key, []) if fixture["date"] else []
        if existing:
            expected = (fixture["score_for"], fixture["score_against"])
            if fixture["played"] and expected in existing:
                duplicates += 1
                continue
            if not fixture["played"] or expected not in existing:
                conflicts.append(
                    f"{fixture['timeline']} {fixture['date']}: {fixture['site_name']} vs "
                    f"{fixture['opponent']} (Tournify {expected}, site {existing}); kept the site entry"
                )
                continue
        by_timeline[fixture["timeline"]].append(fixture)

    groups_for_report: dict[tuple[str, str | None], list[dict[str, Any]]] = {}
    placeholders: list[str] = []
    omitted_undated = 0
    omitted_unscheduled = 0
    omitted_later = 0
    for timeline in TIMELINES:
        start = f"<!-- BEGIN TOURNIFY SYNC: {timeline} -->"
        end = f"<!-- END TOURNIFY SYNC: {timeline} -->"
        if site_html.count(start) != 1 or site_html.count(end) != 1:
            raise RuntimeError(f"Expected exactly one managed marker pair for {timeline}")
        start_index = site_html.index(start) + len(start)
        end_index = site_html.index(end, start_index)
        if end_index < start_index:
            raise RuntimeError(f"Managed markers are out of order for {timeline}")
        fixtures_for_team = by_timeline.get(timeline, [])
        event = next_schedule_event(schedule, timeline, today)
        next_date = event["date"] if event else None
        if next_date is None:
            future_dates = sorted(
                {
                    fixture["date"]
                    for fixture in fixtures_for_team
                    if fixture["date"] and date.fromisoformat(fixture["date"]) >= today
                }
            )
            next_date = future_dates[0] if future_dates else None
        pending_past_dates = sorted(
            {
                fixture["date"]
                for fixture in fixtures_for_team
                if not fixture["played"]
                and fixture["date"]
                and date.fromisoformat(fixture["date"]) < today
            }
        )
        latest_pending_past = pending_past_dates[-1] if pending_past_dates else None
        visible_fixtures = []
        for fixture in fixtures_for_team:
            if fixture["played"]:
                visible_fixtures.append(fixture)
            elif fixture["date"] is None:
                omitted_undated += 1
            elif fixture["date"] == next_date and fixture["time"]:
                visible_fixtures.append(fixture)
            elif fixture["date"] == next_date:
                omitted_unscheduled += 1
            elif fixture["date"] == latest_pending_past:
                if fixture["time"]:
                    visible_fixtures.append(fixture)
                else:
                    omitted_unscheduled += 1
            else:
                omitted_later += 1

        grouped: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
        for fixture in visible_fixtures:
            grouped[fixture["date"]].append(fixture)
        group_order = sorted(grouped, key=lambda value: (value is not None, value or ""), reverse=True)
        event_by_date = {
            item["date"]: item
            for item in schedule["events"]
            if timeline in item["timelines"]
        }
        cards = [
            render_group(
                date_iso,
                grouped[date_iso],
                event_by_date.get(date_iso) if date_iso else None,
                place,
                today,
            )
            for date_iso in group_order
        ]
        has_next_day_fixtures = any(
            fixture["date"] == next_date and (fixture["played"] or fixture["time"])
            for fixture in fixtures_for_team
        )
        if event and not has_next_day_fixtures:
            cards.insert(0, render_schedule_placeholder(event))
            placeholders.append(f"{timeline}: {event['date']}")
        generated = "\n" + "\n\n".join(cards) + "\n          "
        site_html = site_html[:start_index] + generated + site_html[end_index:]
        for date_iso, values in grouped.items():
            groups_for_report[(timeline, date_iso)] = values

    # Keep one concise report for console output and workflow summary.
    site_html_report = {
        "duplicates": duplicates,
        "conflicts": conflicts,
        "unmapped_timelines": sorted(set(TIMELINES) - mapped_timelines),
        "placeholders": placeholders,
        "omitted_undated_upcoming": omitted_undated,
        "omitted_unscheduled_next_day": omitted_unscheduled,
        "omitted_later_upcoming": omitted_later,
    }
    return site_html, {**groups_for_report, ("__report__", None): [site_html_report]}


def summarize(fixtures: list[dict[str, Any]], grouped: dict[tuple[str, str | None], list[dict[str, Any]]]) -> list[str]:
    report = grouped.get(("__report__", None), [{}])[0]
    dated = [fixture for fixture in fixtures if fixture["date"]]
    undated = [fixture for fixture in fixtures if not fixture["date"]]
    scored = [fixture for fixture in fixtures if fixture["played"]]
    lines = [
        f"Target team fixtures found: {len(fixtures)} ({len(scored)} scored, {len(fixtures) - len(scored)} upcoming)",
        f"Upcoming fixtures with a Tournify date: {len([item for item in dated if not item['played']])}",
        f"Upcoming fixtures without a Tournify date: {len([item for item in undated if not item['played']])}",
        f"Existing matching site rows preserved: {report.get('duplicates', 0)}",
        f"Manual-result conflicts held for review: {len(report.get('conflicts', []))}",
        f"Undated upcoming fixtures kept off the page: {report.get('omitted_undated_upcoming', 0)}",
        f"Fixtures without a published start time kept off the page: {report.get('omitted_unscheduled_next_day', 0)}",
        f"Later-day upcoming fixtures kept off the page: {report.get('omitted_later_upcoming', 0)}",
    ]
    if report.get("placeholders"):
        lines.append("Short next-day placeholders: " + ", ".join(report["placeholders"]))
    if report.get("unmapped_timelines"):
        lines.append("Website timelines without active target registrations: " + ", ".join(report["unmapped_timelines"]))
    for conflict in report.get("conflicts", []):
        lines.append("CONFLICT: " + conflict)
    return lines


def atomic_write_text(path: Path, contents: str) -> None:
    """Replace a text file atomically while retaining its existing mode."""
    mode = stat.S_IMODE(path.stat().st_mode)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as temporary:
            temporary.write(contents)
            temporary.flush()
            os.fchmod(temporary.fileno(), mode)
            temporary_path = Path(temporary.name)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def write_step_summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as summary:
            summary.write("## Tournify results sync\n\n")
            summary.write("\n".join(f"- {line}" for line in lines))
            summary.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-file", type=Path, default=Path("competitions.html"))
    parser.add_argument("--schedule-file", type=Path, default=SCHEDULE_FILE)
    parser.add_argument("--live-url", default=LIVE_URL)
    parser.add_argument("--as-of", help="Use this local date (YYYY-MM-DD) for previewing the timeline")
    parser.add_argument(
        "--scheduled-only",
        action="store_true",
        help="Skip Tournify requests unless today is a configured match-day check date",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report changes without writing the HTML file")
    args = parser.parse_args()

    if not args.site_file.is_file():
        parser.error(f"site file does not exist: {args.site_file}")
    schedule = load_competition_schedule(args.schedule_file)
    try:
        today = date.fromisoformat(args.as_of) if args.as_of else datetime.now(ZoneInfo(schedule["timezone"])).date()
    except ValueError as exc:
        parser.error(f"--as-of must be YYYY-MM-DD: {exc}")
    if args.scheduled_only and today not in schedule_check_dates(schedule):
        message = f"No DBN results check is scheduled for {today.isoformat()}; skipped Tournify fetch."
        print(message)
        write_step_summary([message])
        return 0
    original = args.site_file.read_text(encoding="utf-8")
    tournament, collections = fetch_tournify(args.live_url)
    fixtures, mapped_timelines, unmapped_dca = extract_fixtures(tournament, collections)
    standings = extract_standings(tournament, collections)
    updated, grouped = replace_managed_sections(
        original,
        fixtures,
        mapped_timelines,
        str(tournament.get("place", "")).strip(),
        schedule,
        today,
    )
    updated = replace_standings_sections(updated, standings)
    lines = [f"Tournify tournament: {tournament.get('name', 'unnamed')}", *summarize(fixtures, grouped)]
    for timeline in STANDINGS_TIMELINES:
        dca_row = next(row for row in standings[timeline] if row["is_dca"])
        lines.append(
            f"{dca_row['name']} standings: position {dca_row['position']}/{len(standings[timeline])}, "
            f"{dca_row['played']} played, {dca_row['won']} won, {dca_row['lost']} lost, "
            f"{dca_row['points']} points, point difference {dca_row['point_difference']:+d}"
        )
    lines.extend("UNMAPPED DCA: " + item for item in unmapped_dca)

    changed = updated != original
    lines.append(f"Site HTML {'would change' if changed and args.dry_run else 'changed' if changed else 'unchanged'}: {args.site_file}")
    if changed and not args.dry_run:
        atomic_write_text(args.site_file, updated)
    for line in lines:
        print(line)
    write_step_summary(lines)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
