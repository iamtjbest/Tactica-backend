"""
POST /api/predict
Body: { my_team, opp_team, my_att?, my_def?, opp_att?, opp_def?,
        familiarity_formation?, opp_habit_formation? }
Returns: { best_formation, probability, all_formations[] }
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.ml_model import score_all_formations, load_teams

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

    return {
        "best_formation":  formations[0]["formation"],
        "probability":     formations[0]["probability"],
        "my_attack":       my_att,
        "my_defence":      my_def,
        "opp_attack":      opp_att,
        "opp_defence":     opp_def,
        "all_formations":  formations,
    }
