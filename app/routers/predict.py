"""
POST /api/predict
Body: { my_team, opp_team, my_att?, my_def?, opp_att?, opp_def?,
        familiarity_formation?, opp_habit_formation? }
Returns: { best_formation, probability, all_formations[] }

Rating methodology (v2):
  - 90% ML model (formation profiles × attack/defence ratings)
  - 10% H2H blending — only when ≥3 recent meetings exist.
    If fewer than 3 H2H matches are found, the model runs at 100%
    weight. This matches Opta/ELO convention: H2H is a minor
    tiebreaker, not a primary signal.
  - H2H adjustment applied uniformly across ALL formations, not
    just the top-ranked one — keeps the ranking internally consistent.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.ml_model import score_all_formations, load_teams
from app.config import bsd_find_team, bsd_get

# Minimum H2H matches required before blending kicks in.
# Fewer than this → model runs at 100% weight.
H2H_MIN_MATCHES = 3
# How much H2H adjusts the model output (10% is Opta/ELO convention).
H2H_WEIGHT = 0.10
MODEL_WEIGHT = 1.0 - H2H_WEIGHT


def _h2h_strength(my_team_name: str, opp_team_name: str) -> tuple[float, int]:
    """Calculate H2H strength (0–100) based on last 5 direct meetings.

    Returns (strength, n_matches):
      strength  — weighted win rate scaled to 0–100
      n_matches — number of H2H meetings found (caller uses this to
                  decide whether to blend at all)

    Scoring per match:
      Win  → S=1.0, Draw → S=0.5, Loss → S=0.0
      Goal-difference multiplier G:
        |GD|≤1 → 1.0, |GD|=2 → 1.5, |GD|≥3 → (11+GD)/8
    """
    my_id, _  = bsd_find_team(my_team_name)
    opp_id, _ = bsd_find_team(opp_team_name)
    if not my_id or not opp_id:
        return 0.0, 0

    data     = bsd_get(f"/teams/{my_id}/fixtures/",
                       params={"status": "finished", "limit": 50})
    fixtures = data.get("results", []) if data else []

    direct = [
        f for f in fixtures
        if {f.get("home_team_id"), f.get("away_team_id")} == {my_id, opp_id}
    ]
    direct.sort(key=lambda x: x.get("event_date") or "", reverse=True)
    recent = direct[:5]

    if not recent:
        return 0.0, 0

    total = 0.0
    for fix in recent:
        is_home  = fix.get("home_team_id") == my_id
        scored   = (fix.get("home_score") if is_home else fix.get("away_score")) or 0
        conceded = (fix.get("away_score") if is_home else fix.get("home_score")) or 0

        S  = 1.0 if scored > conceded else (0.5 if scored == conceded else 0.0)
        gd = abs(scored - conceded)
        G  = 1.0 if gd <= 1 else (1.5 if gd == 2 else (11 + gd) / 8.0)
        total += S * G

    strength = (total / len(recent)) * 100.0
    return round(strength, 1), len(recent)


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
    familiarity_formation: str | None = None
    opp_habit_formation:   str | None = None


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

    # ── ML formation scores (all 17 formations) ───────────────────────────────
    formations = score_all_formations(
        team_att=my_att, team_def=my_def,
        opp_att=opp_att, opp_def=opp_def,
        familiarity_bonus=body.familiarity_formation,
        opp_habit=body.opp_habit_formation,
    )

    # ── H2H blending (Opta/ELO convention) ───────────────────────────────────
    # Only blend when we have enough H2H history (≥3 meetings).
    # Apply the same adjustment uniformly to ALL formations so the
    # relative ranking stays consistent (not just the top pick).
    h2h_strength, n_h2h = _h2h_strength(body.my_team, body.opp_team)

    if n_h2h >= H2H_MIN_MATCHES:
        # H2H delta: how much H2H differs from each formation's model output.
        # We apply 10% of that delta as a uniform nudge across all formations.
        for f in formations:
            model_prob = f["probability"]
            blended    = MODEL_WEIGHT * model_prob + H2H_WEIGHT * h2h_strength
            f["probability"] = round(blended, 1)
        # Re-sort after blending (uniform shift won't change order, but keeps
        # output consistent if we ever use non-uniform weights in future).
        formations.sort(key=lambda x: x["probability"], reverse=True)

    return {
        "best_formation": formations[0]["formation"],
        "probability":    formations[0]["probability"],
        "my_attack":      my_att,
        "my_defence":     my_def,
        "opp_attack":     opp_att,
        "opp_defence":    opp_def,
        "h2h_matches":    n_h2h,         # visible in API response for transparency
        "h2h_strength":   h2h_strength,  # visible for debugging
        "h2h_blended":    n_h2h >= H2H_MIN_MATCHES,
        "all_formations": formations,
    }
