"""
POST /api/predict
Body: { my_team, opp_team, my_att?, my_def?, opp_att?, opp_def?,
        familiarity_formation?, opp_habit_formation? }
Returns: { best_formation, probability, all_formations[] }
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.ml_model import score_all_formations, load_teams
from app.config import bsd_find_team, bsd_get

def _h2h_strength(my_team_name: str, opp_team_name: str) -> float:
    """Calculate H2H strength (0-100) based on last 5 direct meetings.
    Uses BSD API to fetch recent fixtures between the two teams.
    """
    my_id, _ = bsd_find_team(my_team_name)
    opp_id, _ = bsd_find_team(opp_team_name)
    if not my_id or not opp_id:
        return 0.0
    data = bsd_get(f"/teams/{my_id}/fixtures/", params={"status": "finished", "limit": 50})
    fixtures = data.get("results", []) if data else []
    direct = []
    for f in fixtures:
        home = f.get("home_team_id")
        away = f.get("away_team_id")
        if (home == my_id and away == opp_id) or (home == opp_id and away == my_id):
            direct.append(f)
    # sort newest first
    direct.sort(key=lambda x: x.get("event_date") or "", reverse=True)
    recent = direct[:5]
    if not recent:
        return 0.0
    total = 0.0
    for fix in recent:
        is_home = fix.get("home_team_id") == my_id
        scored = fix.get("home_score") if is_home else fix.get("away_score")
        conceded = fix.get("away_score") if is_home else fix.get("home_score")
        if scored > conceded:
            S = 1.0
        elif scored == conceded:
            S = 0.5
        else:
            S = 0.0
        gd = abs(scored - conceded)
        if gd <= 1:
            G = 1.0
        elif gd == 2:
            G = 1.5
        else:
            G = (11 + gd) / 8.0
        total += S * G
    strength = (total / len(recent)) * 100.0
    return strength
router = APIRouter()

class PredictRequest(BaseModel):
    my_team:  str
    opp_team: str
    # Optional overrides (dynamic ratings from last-5 fetch)
    my_att:   int | None = None
    my_def:   int | None = None
    opp_att:  int | None = None
    opp_def:  int | None = None
    # Formation hints
    familiarity_formation: str | None = None  # formation team already plays
    opp_habit_formation:   str | None = None  # opponent's most-used formation

@router.post("/predict")
def predict(body: PredictRequest):
    teams = load_teams()

    def get_rating(name, field, override):
        if override is not None:
            return override
        return teams.get(name, {}).get(field, 80)

    my_att  = get_rating(body.my_team,  "Attack",  body.my_att)
    my_def  = get_rating(body.my_team,  "Defense", body.my_def)
    opp_att = get_rating(body.opp_team, "Attack",  body.opp_att)
    opp_def = get_rating(body.opp_team, "Defense", body.opp_def)

    formations = score_all_formations(
        team_att=my_att, team_def=my_def,
        opp_att=opp_att, opp_def=opp_def,
        familiarity_bonus=body.familiarity_formation,
        opp_habit=body.opp_habit_formation,
    )
    # Blend with head‑to‑head strength (70% model, 30% H2H)
    base_prob = formations[0]["probability"]
    h2h_strength = _h2h_strength(body.my_team, body.opp_team)
    blended_prob = round(0.7 * base_prob + 0.3 * h2h_strength, 1)
    formations[0]["probability"] = blended_prob

    return {
        "best_formation":  formations[0]["formation"],
        "probability":     formations[0]["probability"],
        "my_attack":       my_att,
        "my_defence":      my_def,
        "opp_attack":      opp_att,
        "opp_defence":     opp_def,
        "all_formations":  formations,
    }
