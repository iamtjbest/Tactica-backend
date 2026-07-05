"""
GET /api/fpl/debug
Temporary endpoint to verify BSD player + fixture endpoints for FPL Scout.
Hit this once, paste the response, then this file gets deleted.
"""
from fastapi import APIRouter
from app.config import bsd_get, bsd_find_team

router = APIRouter()

@router.get("/fpl/debug")
def fpl_debug():
    results = {}

    # 1. Find Salah — confirms /players/ search works and shows field names
    players = bsd_get("/players/", params={"name": "salah", "limit": 3})
    results["player_search_salah"] = players

    salah_id = None
    if players and isinstance(players, list) and len(players) > 0:
        salah_id = players[0].get("id")
        results["salah_id"] = salah_id

    # Also try dict with results key
    if not salah_id and players and isinstance(players, dict):
        items = players.get("results", [])
        if items:
            salah_id = items[0].get("id")
            results["salah_id"] = salah_id
            results["player_search_salah"] = items

    # 2. Per-match stats for Salah — confirms what stat fields exist
    if salah_id:
        stats = bsd_get(f"/players/{salah_id}/stats/", params={"limit": 5})
        results["salah_stats_5matches"] = stats

        # 3. Career summary — season-by-season totals
        career = bsd_get(f"/players/{salah_id}/career/")
        results["salah_career"] = career

        # 4. Transfers — just to see field shape
        transfers = bsd_get(f"/players/{salah_id}/transfers/")
        results["salah_transfers"] = transfers
    else:
        results["note"] = "Salah not found — checking raw player search response above"

    # 5. Premier League upcoming fixtures — confirms event fields for fixture ticker
    # PL league_id is typically 1 in BSD — trying common values
    for league_id in [1, 2, 39]:
        fixtures = bsd_get("/events/", params={
            "league_id": league_id,
            "status": "notstarted",
            "limit": 3,
        })
        if fixtures and (
            (isinstance(fixtures, list) and len(fixtures) > 0) or
            (isinstance(fixtures, dict) and fixtures.get("results"))
        ):
            results[f"upcoming_fixtures_league_{league_id}"] = fixtures
            results["working_pl_league_id"] = league_id
            break
        else:
            results[f"fixtures_league_{league_id}"] = "empty or not found"

    # 6. Confirm team form endpoint still works (sanity check)
    tid, bname = bsd_find_team("Arsenal")
    results["arsenal_bsd_id"] = tid
    results["arsenal_bsd_name"] = bname

    return results
