"""
POST /api/lineup
Body: { team_name, formation }
Returns: { team_name, formation, xi: [ {name, pos, spec_pos, minutes, g_a, fallback} ] }
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.xi_selector import select_xi

router = APIRouter()

class LineupRequest(BaseModel):
    team_name: str
    formation: str

@router.post("/lineup")
def lineup(body: LineupRequest):
    xi = select_xi(body.team_name, body.formation)
    if xi is None:
        raise HTTPException(
            status_code=404,
            detail=f"No player data found for '{body.team_name}'. "
                   f"Use GET /api/squad?team={body.team_name} to fetch and cache their squad first."
        )
    return {
        "team_name": body.team_name,
        "formation": body.formation,
        "xi":        xi,
        "count":     len(xi),
    }
