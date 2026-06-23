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
from datetime import datetime, timezone
from fastapi import APIRouter, Query, HTTPException
from app.config import (bsd_get, bsd_find_team, cache_read, cache_write,
                        cache_age, LEAGUE_NAMES)

router   = APIRouter()
FORM_TTL = 3600   # 1 hour

# Fixed window start — covers end of 2025-26 season.
# Change this to the new season start date (e.g. 2026-08-01) when clubs
# resume in August 2026.
SEASON_START = "2026-03-01T00:00:00Z"


def _dynamic_ratings(matches: list) -> tuple[int, int]:
    if not matches: return 80, 80
    avg_s = sum(m["scored"]   for m in matches) / len(matches)
    avg_c = sum(m["conceded"] for m in matches) / len(matches)
    att   = min(99, int(60 + avg_s * 9.75))
    dfc   = max(60, min(99, int(99 - avg_c * 9.75)))
    return att, dfc


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
    cache_key = f"form__{team.lower().replace(' ', '_')}"
    cached    = cache_read(cache_key)
    if cached and cache_age(cached) < FORM_TTL:
        cached["cached"] = True
        return cached

    # Resolve team_id
    team_id, bsd_name = bsd_find_team(team)
    if not team_id:
        raise HTTPException(status_code=404, detail=f"Team '{team}' not found in BSD.")

    # Window: March 1 2026 → today (counting backwards)
    # date_from = today, date_to = March 1 (searches backwards)
    # Fetch 20, sort by date DESC in Python, slice to 5 most recent.
    # Never trust BSD's default ordering — confirmed ascending by default.
    date_from = datetime.now(timezone.utc).strftime("%Y-%m-%dT23:59:59Z")

    data = bsd_get(f"/teams/{team_id}/fixtures/", params={
        "status":    "finished",
        "limit":     20,
        "date_from": date_from,
        "date_to":   SEASON_START,
    })
    if not data:
        raise HTTPException(status_code=502, detail="BSD API error fetching fixtures.")

    fixtures = data.get("results", [])

    # Sort descending by date in Python — do NOT trust BSD's default order
    fixtures.sort(key=_fixture_date, reverse=True)
    fixtures = fixtures[:5]   # true 5 most recent

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
        "best_formation": best_form,
        "cached":         False,
    }
    cache_write(cache_key, result_doc)
    return result_doc
