"""
GET /api/form?team={name}
Fetches last 5 finished matches for a team.
Returns: { team, matches[], attack, defence, best_formation }
24-hour cache per team.

BSD calls:
  1 x GET /api/v2/teams/?name={name}          → team_id
  1 x GET /api/v2/teams/{id}/fixtures/?status=finished&limit=5
  5 x GET /api/v2/events/{id}/lineups/        → formation used
"""
import time
from datetime import datetime, timedelta
from fastapi import APIRouter, Query, HTTPException
from app.config import (bsd_get, bsd_find_team, cache_read, cache_write,
                        cache_age, LEAGUE_NAMES)

router  = APIRouter()
FORM_TTL = 86400  # 24 hours

def _dynamic_ratings(matches: list) -> tuple[int, int]:
    if not matches: return 80, 80
    avg_s = sum(m["scored"]   for m in matches) / len(matches)
    avg_c = sum(m["conceded"] for m in matches) / len(matches)
    att   = min(99, int(60 + avg_s * 9.75))
    dfc   = max(60, min(99, int(99 - avg_c * 9.75)))
    return att, dfc

def _most_used_formation(matches: list) -> str | None:
    counts = {}
    for m in matches:
        f = m.get("formation","Unknown")
        if f and f != "Unknown":
            counts[f] = counts.get(f, 0) + 1
    return max(counts, key=counts.get) if counts else None

@router.get("/form")
def form(team: str = Query(..., description="Team name")):
    cache_key = f"form__{team.lower().replace(' ','_')}"
    cached    = cache_read(cache_key)
    if cached and cache_age(cached) < FORM_TTL:
        cached["cached"] = True
        return cached

    # Resolve team_id
    team_id, bsd_name = bsd_find_team(team)
    if not team_id:
        raise HTTPException(status_code=404, detail=f"Team '{team}' not found in BSD.")

    # Fetch last 5 finished fixtures
    # date_from = 6 months back (fixtures endpoint defaults to now-3h→+7d without it)
    date_from = (datetime.utcnow() - timedelta(days=180)).strftime("%Y-%m-%dT00:00:00Z")
    data = bsd_get(f"/teams/{team_id}/fixtures/", params={
        "status": "finished", "limit": 5, "date_from": date_from,
    })
    if not data:
        raise HTTPException(status_code=502, detail="BSD API error fetching fixtures.")

    fixtures = data.get("results", [])
    matches  = []

    for fix in fixtures:
        fid        = fix.get("id", 0)
        home_id    = fix.get("home_team_id", 0)
        home_score = fix.get("home_score") or 0
        away_score = fix.get("away_score") or 0
        is_home    = (home_id == team_id)
        scored     = home_score if is_home else away_score
        conceded   = away_score if is_home else home_score
        opp        = fix.get("away_team","?") if is_home else fix.get("home_team","?")
        league_id  = fix.get("league_id", 0)
        competition = LEAGUE_NAMES.get(league_id, f"League {league_id}")
        result     = "W" if scored > conceded else ("D" if scored == conceded else "L")

        # Lineup for formation
        # GET /api/v2/events/{id}/lineups/
        # lineups.home.formation / lineups.away.formation
        # lineups is null when lineup_status == "unavailable"
        formation = "Unknown"
        ld = bsd_get(f"/events/{fid}/lineups/")
        if ld:
            status  = ld.get("lineup_status","unavailable")
            lineups = ld.get("lineups")
            if status != "unavailable" and lineups:
                side      = "home" if is_home else "away"
                formation = (lineups.get(side) or {}).get("formation","Unknown")

        matches.append({
            "fixture_id":  fid,
            "opponent":    opp,
            "competition": competition,
            "scored":      scored,
            "conceded":    conceded,
            "result":      result,
            "formation":   formation,
        })

    att, dfc  = _dynamic_ratings(matches)
    best_form = _most_used_formation(matches)

    result_doc = {
        "_cached_at":    time.time(),
        "team":          team,
        "bsd_name":      bsd_name,
        "matches":       matches,
        "attack":        att,
        "defence":       dfc,
        "best_formation":best_form,
        "cached":        False,
    }
    cache_write(cache_key, result_doc)
    return result_doc
