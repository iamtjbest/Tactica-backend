"""
POST /api/lineup
Body: { team_name, formation }
Returns: { team_name, formation, xi: [ {name, pos, spec_pos, minutes, g_a, fallback} ] }
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.xi_selector import select_xi, load_players
from app.routers.squad import squad as fetch_squad

router = APIRouter()

class LineupRequest(BaseModel):
    team_name: str
    formation: str

@router.post("/lineup")
def lineup(body: LineupRequest):
    xi = select_xi(body.team_name, body.formation)

    if xi is None:
        # players.json has no entry for this team yet — this happens on
        # pages like Auto-Tactics that never call /api/squad themselves.
        # Fetch and cache it now instead of requiring another page to
        # have "warmed" it first.
        try:
            fetch_squad(team=body.team_name, refresh=False)
        except HTTPException:
            pass  # team genuinely not found in BSD — fall through to the 404 below
        xi = select_xi(body.team_name, body.formation, players_db=load_players())

    if xi is None:
        raise HTTPException(
            status_code=404,
            detail=f"No player data found for '{body.team_name}' even after "
                   f"attempting a live BSD fetch. Try GET /api/squad?team={body.team_name} "
                   f"directly to see the underlying error."
        )
    return {
        "team_name": body.team_name,
        "formation": body.formation,
        "xi":        xi,
        "count":     len(xi),
    }
