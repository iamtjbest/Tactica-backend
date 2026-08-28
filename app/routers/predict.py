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


# ── Known baseline ratings (mirrors form.py / fpl.py) ────────────────────────
# Used when teams.json doesn't have a team entry (UCL sides, newly promoted
# clubs, anyone not in the static file). Prevents symmetric 80/80 fallback
# which produces identical 42.2%/42.2% win probabilities for both teams.
_KNOWN_RATINGS: dict[str, tuple[int, int]] = {
    # Pot 1 / elite
    "Real Madrid":(88,88),"Barcelona":(87,85),"Manchester City":(87,86),
    "Liverpool":(85,84),"Bayern Munich":(86,87),"Paris Saint-Germain":(85,83),
    "Arsenal":(82,82),"Inter Milan":(80,85),"Atletico Madrid":(78,86),
    # Pot 2 / strong
    "Borussia Dortmund":(80,78),"Aston Villa":(78,76),"Manchester United":(76,78),
    "Porto":(74,75),"Roma":(74,73),"AS Roma":(74,73),"Sporting CP":(72,74),
    "Club Brugge":(68,70),"Club Brugge KV":(68,70),
    "Real Betis":(70,72),"PSV Eindhoven":(72,70),"PSV":(72,70),
    # Pot 3
    "Napoli":(76,74),"Feyenoord":(70,68),"Lille":(68,70),"LOSC Lille":(68,70),
    "RB Leipzig":(74,72),"Villarreal":(72,70),"Villarreal CF":(72,70),
    "Galatasaray":(68,66),"Galatasaray SK":(68,66),
    "Fenerbahce":(66,65),"Fenerbahçe SK":(66,65),
    "Shakhtar Donetsk":(65,68),
    # Pot 4 / UCL qualifiers
    "Celtic":(65,63),"Slavia Prague":(60,62),"SK Slavia Prague":(60,62),
    "Sparta Prague":(60,62),"AC Sparta Praha":(60,62),
    "Stuttgart":(70,68),"VfB Stuttgart":(70,68),
    "Como":(55,55),"RC Lens":(65,66),"AEK Athens":(55,58),
    "LASK":(52,55),"Slovan Bratislava":(50,52),
    "Viking":(50,50),"Bodo/Glimt":(55,55),"Sabah":(52,52),
    # PL clubs
    "Chelsea":(78,78),"Tottenham Hotspur":(76,74),"Tottenham":(76,74),
    "Newcastle United":(76,78),"Brighton & Hove Albion":(72,74),
    "Brighton":(72,74),"Fulham":(68,70),"Brentford":(68,68),
    "Crystal Palace":(65,66),"Everton":(62,65),
    "Bournemouth":(65,64),"AFC Bournemouth":(65,64),
    "Coventry City":(58,60),"Hull City":(56,58),
    "Ipswich Town":(58,58),"Leeds United":(60,60),
    "Sunderland":(58,58),"Nottingham Forest":(62,64),
    # La Liga
    "Atletico Madrid":(78,86),"Atlético Madrid":(78,86),
    "Sevilla":(68,68),"Sevilla FC":(68,68),
    "Real Sociedad":(68,67),"Osasuna":(60,62),
    "Celta Vigo":(62,62),"Celta de Vigo":(62,62),
    "Getafe":(58,62),"Getafe CF":(58,62),
    "Alaves":(57,60),"Deportivo Alavés":(57,60),
    "Espanyol":(60,62),"RCD Espanyol":(60,62),
    "Elche":(55,58),"Valencia":(62,62),"Valencia CF":(62,62),
    # Bundesliga
    "FC Bayern München":(86,87),"Bayer Leverkusen":(80,78),
    "Bayer 04 Leverkusen":(80,78),"Eintracht Frankfurt":(70,68),
    "SC Freiburg":(65,66),"Freiburg":(65,66),
    "Werder Bremen":(62,62),"SV Werder Bremen":(62,62),
    "Hoffenheim":(62,62),"TSG Hoffenheim":(62,62),
    "Mainz":(60,62),"1. FSV Mainz 05":(60,62),
    "Union Berlin":(60,62),"1. FC Union Berlin":(60,62),
    "Augsburg":(58,60),"FC Augsburg":(58,60),
    # Serie A
    "AC Milan":(78,76),"Juventus":(76,76),
    "Lazio":(70,70),"Atalanta":(74,72),
    "Fiorentina":(68,67),"Bologna":(66,66),
    "Torino":(62,64),"Udinese":(58,60),
    "Genoa":(58,60),"Cagliari":(55,58),
    "Monza":(60,60),"Venezia":(52,54),
    "Lecce":(55,58),"US Lecce":(55,58),
    "Empoli":(55,56),"Hellas Verona":(55,56),
    "Parma":(56,58),"Inter":(80,85),
    # Ligue 1
    "Monaco":(74,72),"AS Monaco":(74,72),
    "Marseille":(72,70),"Olympique de Marseille":(72,70),
    "Olympique Lyonnais":(70,68),"Lens":(65,66),
    "Nice":(65,64),"OGC Nice":(65,64),
    "Stade Rennais FC":(62,62),"Stade Brestois 29":(60,60),
    "Stade de Reims":(58,60),"Montpellier HSC":(56,58),
    # Eredivisie
    "AFC Ajax":(70,68),"Ajax":(70,68),
    "AZ":(65,65),"AZ Alkmaar":(65,65),
    "FC Utrecht":(60,60),"FC Twente":(62,62),
    # Primeira Liga
    "SL Benfica":(72,72),"Benfica":(72,72),
    "SC Braga":(64,65),"Braga":(64,65),
    # Scottish / Belgian / Others
    "Rangers":(62,62),"RSC Anderlecht":(62,62),
    "Anderlecht":(62,62),"KRC Genk":(60,60),"Genk":(60,60),
    "Olympiacos":(60,62),"PAOK":(58,60),
    "FC Red Bull Salzburg":(62,62),"Red Bull Salzburg":(62,62),
    "SK Sturm Graz":(58,60),"Sturm Graz":(58,60),
}


@router.post("/predict")
def predict(body: PredictRequest):
    teams = load_teams()

    def get_rating(name, field, override):
        if override is not None:
            return override
        # 1. Check teams.json (live/dynamic ratings from real match data)
        static = teams.get(name, {}).get(field)
        if static is not None:
            return static
        # 2. Fall back to KNOWN_RATINGS — prevents symmetric 80/80 default
        #    for UCL clubs and anyone not yet in teams.json
        known = _KNOWN_RATINGS.get(name)
        if known:
            return known[0] if field == "Attack" else known[1]
        # 3. Last resort generic fallback
        return 72  # mid-table average, better than 80 which skews everything high

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
