"""
GET  /api/nations/squads?group={A-L}&status=official
GET  /api/nations/squads/{team_id}
POST /api/nations/predict  { team_id, opp_id, team_name, opp_name }

BSD World Cup 2026 endpoints used:
  GET /api/v2/worldcup/squads/                  → all squads
  GET /api/v2/worldcup/squads/{team_id}/         → one nation's squad
  GET /api/v2/players/{id}/career/               → club form
  GET /api/v2/players/{id}/stats/?limit=10       → recent ratings
  GET /api/v2/players/{id}/national-team/        → caps
"""
import time
from fastapi import APIRouter, Query, HTTPException, Path
from pydantic import BaseModel
from app.config import bsd_get, cache_read, cache_write, cache_age
from app.national_ratings import rate_national_team
from app.ml_model import score_all_formations

router = APIRouter(prefix="/nations")

# ── GET /api/nations/squads ───────────────────────────────────────────────────
@router.get("/squads")
def list_squads(
    group:  str | None = Query(None, description="Group letter A–L"),
    status: str        = Query("official", description="official | preliminary | projected"),
):
    cache_key = f"wc_squads__{group or 'all'}__{status}"
    cached = cache_read(cache_key)
    if cached and cache_age(cached) < 43200:  # 12h
        return cached

    params = {"status": status, "limit": 200}
    if group:
        params["group"] = group.upper()

    # GET /api/v2/worldcup/squads/
    # Response: list of {team_id, name, position, caps, goals, player_id, ...}
    data = bsd_get("/worldcup/squads/", params=params)
    if not data:
        raise HTTPException(status_code=502, detail="BSD World Cup API unavailable.")

    result = {"_cached_at": time.time(), "squads": data.get("results",[])}
    cache_write(cache_key, result)
    return result

# ── GET /api/nations/squads/{team_id} ─────────────────────────────────────────
@router.get("/squads/{team_id}")
def team_squad(team_id: int = Path(...)):
    cache_key = f"wc_squad_{team_id}"
    cached = cache_read(cache_key)
    if cached and cache_age(cached) < 43200:
        return cached

    # GET /api/v2/worldcup/squads/{team_id}/
    # Returns: { team_id, team_name, group, count, results: [{...player...}] }
    data = bsd_get(f"/worldcup/squads/{team_id}/")
    if not data:
        raise HTTPException(status_code=404, detail=f"No World Cup squad for team {team_id}.")

    result = {"_cached_at": time.time(), **data}
    cache_write(cache_key, result)
    return result

# ── POST /api/nations/predict ─────────────────────────────────────────────────
class NationsPredict(BaseModel):
    team_id:   int
    opp_id:    int
    team_name: str = ""
    opp_name:  str = ""

@router.post("/predict")
def nations_predict(body: NationsPredict):
    """
    Formation prediction for a national team matchup.
    Fetches both squads, computes player scores → team ratings → ML prediction.
    """
    # Fetch squads
    my_squad_data  = bsd_get(f"/worldcup/squads/{body.team_id}/")
    opp_squad_data = bsd_get(f"/worldcup/squads/{body.opp_id}/")

    if not my_squad_data or not opp_squad_data:
        raise HTTPException(status_code=404,
            detail="Could not fetch one or both national team squads from BSD.")

    my_squad  = my_squad_data.get("results", [])
    opp_squad = opp_squad_data.get("results", [])

    # Compute ratings from actual player quality
    my_ratings  = rate_national_team(body.team_id,  my_squad)
    opp_ratings = rate_national_team(body.opp_id,   opp_squad)

    my_att  = int(my_ratings["attack"])
    my_def  = int(my_ratings["defence"])
    opp_att = int(opp_ratings["attack"])
    opp_def = int(opp_ratings["defence"])

    formations = score_all_formations(my_att, my_def, opp_att, opp_def)

    return {
        "team":            body.team_name or f"Team {body.team_id}",
        "opponent":        body.opp_name  or f"Team {body.opp_id}",
        "my_attack":       my_att,
        "my_defence":      my_def,
        "opp_attack":      opp_att,
        "opp_defence":     opp_def,
        "best_formation":  formations[0]["formation"],
        "probability":     formations[0]["probability"],
        "all_formations":  formations,
        "my_squad_count":  len(my_squad),
        "opp_squad_count": len(opp_squad),
        "players_scored":  my_ratings.get("players_scored", 0),
    }
