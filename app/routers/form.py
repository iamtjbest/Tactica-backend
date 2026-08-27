"""
GET /api/form?team={name}
Fetches last 5 finished matches for a team.
Returns: { team, matches[], attack, defence, best_formation }
1-hour cache per team.

Date window: March 1 2026 → today
This covers the full end of the 2025-26 European season (Mar-May/Jun 2026)
while excluding older data from December 2025 and earlier.

BSD calls:
  1 x GET /api/v2/teams/?name={name}            → team_id
  1 x GET /api/v2/teams/{id}/fixtures/?...      → up to 20 finished fixtures
  ≤5 x GET /api/v2/events/{id}/lineups/         → formation used (top 5 only)
"""
import time
import os
from datetime import datetime, timezone, timedelta
USE_DUMMY_DATA = os.getenv('USE_DUMMY_DATA', 'False') == 'True'
from fastapi import APIRouter, Query, HTTPException
from app.config import (bsd_get, bsd_find_team, cache_read, cache_write, cache_age, LEAGUE_NAMES)

def _get_team_primary_league(team_id: int) -> int | None:
    """Return the primary league_id for a team (e.g., Premier League).
    If the API fails or the team has fewer than 5 fixtures in that league, return None so the caller can fall back.
    """
    try:
        data = bsd_get(f"/teams/{team_id}/")
        league_id = data.get("league_id")
        if not league_id:
            leagues = data.get("leagues") or []
            if leagues:
                league_id = leagues[0].get("id")
        return int(league_id) if league_id is not None else None
    except Exception:
        return None

router   = APIRouter()
FORM_TTL = 3600   # 1 hour

# Window covers the full 2025-26 season so ratings (attack/defence) reflect
# the whole season's form. limit=50 ensures BSD returns enough fixtures that
# after sorting DESC we reliably hit the 5 most recent regardless of how many
# games a team has played since August.
# NOTE: BSD returns fixtures ASCENDING by default — we sort DESC in Python.
# With limit=20 and a wide window, BSD returned the oldest 20 (Aug-Nov) and
# the most recent May fixtures were never fetched. Raising to 50 fixes this.
SEASON_START = "2026-06-01T00:00:00Z"
FORM_LIMIT   = 80


def _dynamic_ratings(matches: list) -> tuple[int, int]:
    """Calculate attack and defence ratings using Dixon‑Coles style.
    • Uses up to the 10 most recent matches.
    • Binary decay weighting: weight 1.0 for the 5 newest, 0.5 for the next 5.
    • Expected league‑average goals per game = 1.4.
    • Clamps final ratings to the 10‑90 range.
    """
    if not matches:
        return 80, 80
    weighted_scored = 0.0
    weighted_conceded = 0.0
    total_weight = 0.0
    expected_goals = 1.4
    for idx, m in enumerate(matches[:10]):
        weight = 1.0 if idx < 5 else 0.5
        weighted_scored += m["scored"] * weight
        weighted_conceded += m["conceded"] * weight
        total_weight += weight
    # Prevent division by zero – enforce a small minimum on conceded goals
    weighted_conceded = max(weighted_conceded, 0.2)
    avg_scored = weighted_scored / total_weight
    avg_conceded = weighted_conceded / total_weight
    attack = int(10 + (avg_scored / expected_goals) * 80)
    defence = int(10 + (expected_goals / avg_conceded) * 80)
    # Clamp to keep values within sane bounds
    attack = max(10, min(90, attack))
    defence = max(10, min(90, defence))
    return attack, defence


def _most_used_formation(matches: list) -> str | None:
    counts: dict[str, int] = {}
    for m in matches:
        f = m.get("formation", "Unknown")
        if f and f != "Unknown":
            counts[f] = counts.get(f, 0) + 1
    return max(counts, key=counts.get) if counts else None


def _fixture_date(fix: dict) -> str:
    """Try known BSD date field names. Empty string sorts last under reverse=True."""
    return (
        fix.get("event_date")
        or fix.get("date")
        or fix.get("kickoff_time")
        or fix.get("starting_at")
        or ""
    )


@router.get("/form")
def form(team: str = Query(..., description="Team name")):
    cache_key = f"form_v2__{team.lower().replace(' ', '_')}"
    cached    = cache_read(cache_key)
    if cached and cache_age(cached) < FORM_TTL:
        cached["cached"] = True
        return cached

    # Resolve team_id
    team_id, bsd_name = bsd_find_team(team)
    if not team_id and USE_DUMMY_DATA:
        # Fallback for test environment – provide dummy IDs for known teams
        _fallback = {
            "Arsenal": (1, "Arsenal"),
            "Manchester United": (2, "Manchester United"),
        }
        team_id, bsd_name = _fallback.get(team, (None, None))
    if not team_id:
        raise HTTPException(status_code=404, detail=f"Team '{team}' not found in BSD.")

    # Window: Aug 2025 → now. Fetch 50 (limit raised from 20 — a 10-month window
    # contains ~40-50 fixtures; limit=20 only returned the oldest 20 ascending
    # from BSD, meaning May matches were never fetched). Sort DESC in Python,
    # slice to 5 most recent. Never trust BSD default order (confirmed ascending).
    date_to = datetime.now(timezone.utc).strftime("%Y-%m-%dT23:59:59Z")
    if USE_DUMMY_DATA and team_id in (1, 2):
        # Dummy fixtures for test environment
        dummy_fixtures = []
        for i in range(5):
            dummy_fixtures.append({
                "id": i + 1,
                "home_team_id": team_id,
                "away_team_id": 999,
                "home_score": i % 3,
                "away_score": (i + 1) % 3,
                "league_id": 17,
                "event_date": (datetime.now(timezone.utc) - timedelta(days=i * 7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "home_team": {"name": team},
                "away_team": {"name": "Dummy Opponent"},
            })
        data = {"results": dummy_fixtures}
    else:
        data = bsd_get(f"/teams/{team_id}/fixtures/", params={
            "status":    "finished",
            "limit":     FORM_LIMIT,
            "date_from": SEASON_START,
            "date_to":   date_to,
        })
    if not data:
        raise HTTPException(status_code=502, detail="BSD API error fetching fixtures.")

    fixtures = data.get("results", [])

    # Filter to primary league if we can determine it
    primary_league = _get_team_primary_league(team_id)
    if primary_league is not None:
        league_fixtures = [f for f in fixtures if f.get("league_id") == primary_league]
        # If we have at least 5 league fixtures, use them; otherwise fall back to all
        fixtures = league_fixtures if len(league_fixtures) >= 5 else fixtures

    # Sort descending by date in Python — BSD may return ASC
    # Sort descending by date in Python — BSD may return ASC
    fixtures.sort(key=_fixture_date, reverse=True)
    fixtures = fixtures[:10]   # true up to 10 most recent for decay weighting

    matches = []
    for fix in fixtures:
        fid        = fix.get("id", 0)
        home_id    = fix.get("home_team_id", 0)
        home_score = fix.get("home_score") or 0
        away_score = fix.get("away_score") or 0
        is_home    = (home_id == team_id)
        scored     = home_score if is_home else away_score
        conceded   = away_score if is_home else home_score
        opp        = fix.get("away_team", "?") if is_home else fix.get("home_team", "?")
        league_id  = fix.get("league_id", 0)
        competition = LEAGUE_NAMES.get(league_id, f"League {league_id}")
        result     = "W" if scored > conceded else ("D" if scored == conceded else "L")

        formation = "Unknown"
        ld = bsd_get(f"/events/{fid}/lineups/")
        if ld:
            status  = ld.get("lineup_status", "unavailable")
            lineups = ld.get("lineups")
            if status != "unavailable" and lineups:
                side      = "home" if is_home else "away"
                formation = (lineups.get(side) or {}).get("formation", "Unknown")

        matches.append({
            "fixture_id":   fid,
            "opponent":     opp,
            "competition":  competition,
            "scored":       scored,
            "conceded":     conceded,
            "result":       result,
            "formation":    formation,
            "event_date":   _fixture_date(fix),
        })

    att, dfc  = _dynamic_ratings(matches)
    best_form = _most_used_formation(matches)

    result_doc = {
        "_cached_at":     time.time(),
        "team":           team,
        "bsd_name":       bsd_name,
        "matches":        matches,
        "attack":         att,
        "defence":        dfc,
        "win_percentage": max(0, min(100, int(((att - 10) / 80) * 100))),
        "best_formation": best_form,
        "cached":         False,
    }
    cache_write(cache_key, result_doc)
    return result_doc
