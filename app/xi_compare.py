"""
GET /api/xi-compare?team={name}&formation={optional}

Fetches the manager's actual/predicted lineup from BSD for the team's
next fixture, then runs the AI's suggested XI for comparison.

Returns:
  {
    team, next_fixture, competition, event_date, opponent,
    manager_xi: [ {name, pos, spec_pos, jersey_number} ],
    lineup_status: "confirmed" | "predicted" | "unavailable",
    ai_xi: [ {name, pos, spec_pos, minutes, g_a} ],
    ai_formation: str,
    differences: [ {slot, manager_player, ai_player} ]
  }

BSD calls used:
  1 x GET /api/v2/teams/?name={name}                          → team_id
  1 x GET /api/v2/teams/{id}/fixtures/?status=notstarted&limit=1 → next match
  1 x GET /api/v2/events/{id}/lineups/                        → manager lineup
  + lineup endpoint for Starting XI selection (local, no API call)
"""
import time
from datetime import datetime, timedelta
from fastapi import APIRouter, Query, HTTPException
from app.config import bsd_get, bsd_find_team, cache_read, cache_write, cache_age, LEAGUE_NAMES
from app.xi_selector import select_xi, load_players
from app.ml_model import score_all_formations, load_teams

router = APIRouter()
COMPARE_TTL = 1800  # 30 min — lineup data changes close to kickoff


@router.get("/xi-compare")
def xi_compare(
    team:      str = Query(..., description="Team name e.g. Arsenal"),
    formation: str = Query(None, description="Override AI formation (optional)"),
):
    cache_key = f"xi_compare__{team.lower().replace(' ','_')}"
    cached    = cache_read(cache_key)
    if cached and cache_age(cached) < COMPARE_TTL:
        cached["cached"] = True
        return cached

    # ── 1. Resolve BSD team_id ────────────────────────────────────────────────
    team_id, bsd_name = bsd_find_team(team)
    if not team_id:
        raise HTTPException(status_code=404, detail=f"Team '{team}' not found in BSD.")

    # ── 2. Fetch next fixture ─────────────────────────────────────────────────
    # GET /api/v2/teams/{id}/fixtures/?status=notstarted&limit=1
    # Falls back to last finished match if no upcoming game found
    date_from = datetime.utcnow().strftime("%Y-%m-%dT00:00:00Z")
    date_to   = (datetime.utcnow() + timedelta(days=14)).strftime("%Y-%m-%dT00:00:00Z")

    fix_data = bsd_get(f"/teams/{team_id}/fixtures/", params={
        "status": "notstarted", "limit": 1,
        "date_from": date_from, "date_to": date_to,
    })

    fixtures = (fix_data or {}).get("results", [])

    # Fallback: use most recent finished match if no upcoming game
    if not fixtures:
        fix_data = bsd_get(f"/teams/{team_id}/fixtures/", params={
            "status": "finished", "limit": 1,
        })
        fixtures = (fix_data or {}).get("results", [])

    if not fixtures:
        raise HTTPException(status_code=404,
            detail=f"No fixtures found for '{team}'. BSD may not have upcoming schedule data yet.")

    fix        = fixtures[0]
    event_id   = fix.get("id", 0)
    home_id    = fix.get("home_team_id", 0)
    is_home    = (home_id == team_id)
    opponent   = fix.get("away_team","?") if is_home else fix.get("home_team","?")
    league_id  = fix.get("league_id", 0)
    competition = LEAGUE_NAMES.get(league_id, f"League {league_id}")
    event_date  = fix.get("event_date","")
    status      = fix.get("status","notstarted")

    # ── 3. Fetch manager's lineup from BSD ────────────────────────────────────
    # GET /api/v2/events/{id}/lineups/
    # Response: { lineup_status: confirmed|predicted|unavailable,
    #             lineups: { home: { formation, players[] }, away: { ... } } }
    # lineups is null when status == "unavailable"
    manager_xi      = []
    lineup_status   = "unavailable"
    manager_formation = None

    ld = bsd_get(f"/events/{event_id}/lineups/")
    if ld:
        lineup_status = ld.get("lineup_status", "unavailable")
        lineups       = ld.get("lineups")

        if lineup_status != "unavailable" and lineups:
            side     = "home" if is_home else "away"
            side_data = lineups.get(side) or {}
            manager_formation = side_data.get("formation")

            for p in side_data.get("players", []):
                pos = p.get("position","M")  # G / D / M / F
                manager_xi.append({
                    "name":          p.get("name") or p.get("short_name","Unknown"),
                    "pos":           pos,
                    "jersey_number": p.get("jersey_number", 0),
                })

    # ── 4. AI's suggested XI ──────────────────────────────────────────────────
    teams_db    = load_teams()
    players_db  = load_players()
    team_entry  = teams_db.get(team, {"Attack":80,"Defense":80})

    # Determine best formation: use manager's if known, else predict
    if formation:
        ai_formation = formation
    elif manager_formation:
        # Score manager's own formation against a neutral opponent
        scores = score_all_formations(
            team_entry.get("Attack",80), team_entry.get("Defense",80), 80, 80
        )
        ai_formation = scores[0]["formation"]
    else:
        scores = score_all_formations(
            team_entry.get("Attack",80), team_entry.get("Defense",80), 80, 80
        )
        ai_formation = scores[0]["formation"]

    ai_xi_raw = select_xi(team, ai_formation, players_db)
    ai_xi     = ai_xi_raw or []

    # ── 5. Compute differences ────────────────────────────────────────────────
    # Match by slot position (GK, DF1-4, MF1-3, FW1-3)
    differences = []
    manager_names = {p["name"].lower() for p in manager_xi}
    ai_names      = {p["name"].lower() for p in ai_xi}

    # Players in AI XI not in manager's XI
    ai_only      = [p for p in ai_xi      if p["name"].lower() not in manager_names]
    # Players in manager's XI not in AI's XI
    manager_only = [p for p in manager_xi if p["name"].lower() not in ai_names]
    # Players in both
    shared_count = len(manager_names & ai_names)

    result = {
        "_cached_at":       time.time(),
        "team":             team,
        "bsd_name":         bsd_name,
        "next_fixture":     f"{fix.get('home_team','')} vs {fix.get('away_team','')}",
        "opponent":         opponent,
        "competition":      competition,
        "event_date":       event_date,
        "fixture_status":   status,
        "lineup_status":    lineup_status,  # confirmed | predicted | unavailable
        "manager_formation":manager_formation,
        "ai_formation":     ai_formation,
        "manager_xi":       manager_xi,
        "ai_xi":            ai_xi,
        "shared_players":   shared_count,
        "ai_only":          ai_only,
        "manager_only":     manager_only,
        "cached":           False,
    }
    cache_write(cache_key, result)
    return result
