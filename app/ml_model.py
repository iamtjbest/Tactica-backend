"""
app/ml_model.py — ML model loader and prediction helpers
"""
import os, json
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
import joblib

from app.config import FORMATIONS, FORMATION_NAME_TO_CODE

MODEL_PATH   = os.environ.get("MODEL_PATH", "tactical_model.pkl")
TEAMS_PATH   = os.environ.get("TEAMS_PATH", "teams.json")

# ── Load or bootstrap model ───────────────────────────────────────────────────
def load_model() -> RandomForestClassifier:
    try:
        return joblib.load(MODEL_PATH)
    except Exception:
        # Bootstrap: train on synthetic data so the API never 500s on cold start
        rng = np.random.default_rng(42)
        n   = 2000
        df  = pd.DataFrame({
            "Formation":    rng.integers(0, 17, n),
            "Team_Attack":  rng.integers(60, 99, n),
            "Team_Defense": rng.integers(60, 99, n),
            "Opp_Attack":   rng.integers(60, 99, n),
            "Opp_Defense":  rng.integers(60, 99, n),
            "Win":          rng.integers(0, 2, n),
        })
        m = RandomForestClassifier(n_estimators=200, random_state=42)
        m.fit(df[["Formation","Team_Attack","Team_Defense","Opp_Attack","Opp_Defense"]], df["Win"])
        return m

model = load_model()

def load_teams() -> dict:
    try:
        return json.load(open(TEAMS_PATH, encoding="utf-8"))
    except Exception:
        return {}

def score_all_formations(
    team_att: int,
    team_def: int,
    opp_att: int,
    opp_def: int,
    familiarity_bonus: str | None = None,
    opp_habit: str | None = None,
) -> list[dict]:
    """
    Score all 17 formations through the ML model.
    Returns list of {formation, probability} sorted best → worst.
    """
    results = []
    for code, name in FORMATIONS.items():
        test = pd.DataFrame({
            "Formation":    [code],
            "Team_Attack":  [team_att],
            "Team_Defense": [team_def],
            "Opp_Attack":   [opp_att],
            "Opp_Defense":  [opp_def],
        })
        prob = float(model.predict_proba(test)[0][1] * 100)

        # +5% if team already plays this formation regularly
        if familiarity_bonus and name == familiarity_bonus:
            prob += 5.0

        # Penalty if opponent's formation historically exploits ours
        if opp_habit and opp_habit[0].isdigit():
            opp_backs = int(opp_habit.split("-")[0])
            if opp_backs >= 5 and name.startswith("3"):
                prob -= 5.0
            if opp_backs <= 3 and name in ["4-2-3-1","4-3-3","4-4-2"]:
                prob -= 3.0

        results.append({"formation": name, "probability": round(prob, 1)})

    return sorted(results, key=lambda x: x["probability"], reverse=True)
