"""
app/ml_model.py — ML model loader and prediction helpers

BUGS FIXED (v2):
  1. Bootstrap trained on random Wins (rng.integers(0, 2, n)) — coin flip data.
     The RF could learn no signal, so every formation returned ~60% regardless
     of ratings. Fixed: Win is now computed from tactical logic, not random.

  2. Training range was 60–99 for attack/defense. WC nations score 50–75.
     Values below 60 were out-of-distribution, so the RF returned near-random
     predictions (manifesting as a flat 60.5% for all nations). Fixed: range
     now 50–99, covering all teams including weaker WC nations.

  3. 5-ATB bias: because all predictions were near-random noise, certain
     formation codes happened to land on slightly higher leaves due to seed
     randomness (random_state=42). Fixed: formations are now scored by actual
     tactical suitability, so defensive formations only rank high when the
     opponent genuinely outclasses our attack.
"""
import os, json
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
import joblib

from app.config import FORMATIONS, FORMATION_NAME_TO_CODE

MODEL_PATH = os.environ.get("MODEL_PATH", "tactical_model.pkl")
TEAMS_PATH = os.environ.get("TEAMS_PATH", "teams.json")

# ── Formation tactical profiles ───────────────────────────────────────────────
# (att_weight, def_weight): how much a formation amplifies attack vs defence.
# Derived from standard tactical conventions:
#   3-4-3, 3-3-1-3 = very attacking; 5-4-1, 5-3-2 = very defensive.
# Must match the order of FORMATIONS in config.py (codes 0–16).
FORMATION_PROFILES: dict[int, tuple[float, float]] = {
    0:  (0.90, 0.50),  # 3-4-3      — 3 at back, heavy attack
    1:  (0.75, 0.65),  # 3-5-2      — 3 at back, 5 mid
    2:  (0.80, 0.60),  # 3-4-1-2    — 3 at back, attacking
    3:  (0.85, 0.55),  # 3-2-4-1    — high-press attacking
    4:  (0.80, 0.60),  # 3-4-2-1    — 3 at back, attack-minded
    5:  (0.88, 0.52),  # 3-3-1-3    — very attacking, 3 forwards
    6:  (0.75, 0.70),  # 4-2-3-1    — 4-4-2 variant, balanced lean attack
    7:  (0.82, 0.65),  # 4-3-3      — standard balanced-attack
    8:  (0.70, 0.72),  # 4-4-2      — classic balanced
    9:  (0.72, 0.72),  # 4-4-2 Diamond — balanced, creative
    10: (0.65, 0.76),  # 4-1-4-1    — defensive mid pivot
    11: (0.70, 0.70),  # 4-3-2-1    — narrow, balanced
    12: (0.75, 0.65),  # 4-2-2-2    — wide, attacking
    13: (0.58, 0.84),  # 5-3-2      — defensive, 5 at back
    14: (0.48, 0.90),  # 5-4-1      — very defensive, park-the-bus
    15: (0.62, 0.82),  # 5-2-2-1    — defensive
    16: (0.68, 0.80),  # 5-2-3      — 5 at back but 3 forwards
}


def _compute_win_prob(
    code: int, team_att: float, team_def: float,
    opp_att: float, opp_def: float,
) -> float:
    """
    Compute a realistic win probability for a given formation and match stats.
    Used to generate meaningful synthetic training labels.

    Logic:
      - att_edge = normalised difference between our attack and their defence.
                   Positive = we outgun them, negative = they shut us down.
      - def_edge = normalised difference between our defence and their attack.
                   Positive = we absorb them, negative = they overrun us.
      - Formation weights amplify the relevant edge:
          attacking formations (4-3-3, 3-4-3) amplify att_edge
          defensive formations (5-4-1, 5-3-2) amplify def_edge
      - net = weighted sum of both edges
      - win_prob = sigmoid(net × 2.5) → realistic spread

    Verified examples (all ratings 0-99):
      Arsenal(88,85) vs AZ(65,68)   4-3-3 → ~68%  ✓ strong favourite
      PSG(72,71)     vs AZ(65,68)   4-3-3 → ~55%  ✓ slight edge
      Equal(75,75)   vs (75,75)     4-4-2 → ~50%  ✓ coin flip
      Weak(55,58)    vs Strong(88,90) 5-4-1→ ~25%  ✓ underdog parking the bus
    """
    att_w, def_w = FORMATION_PROFILES.get(code, (0.70, 0.70))

    # Edges: difference divided by max range (99) → -1.0 to +1.0
    att_edge = (team_att - opp_def) / 99.0
    def_edge = (team_def - opp_att) / 99.0

    # Formation weights scale the relevant edge
    net = att_edge * att_w + def_edge * def_w

    win_prob = 1.0 / (1.0 + np.exp(-net * 2.5))
    return float(np.clip(win_prob, 0.04, 0.96))


def _generate_training_data(n_matches: int = 8000, seed: int = 42) -> pd.DataFrame:
    """
    Generate synthetic match data where Win is computed from tactical logic,
    not a coin flip. Each match scenario is evaluated for all 17 formations.
    Total rows = n_matches × 17.

    Attack/Defence range: 50–99 — covers both strong European clubs (80-95)
    and weaker WC nations (50-70) so the model is calibrated across the board.
    """
    rng = np.random.default_rng(seed)
    records = []

    for _ in range(n_matches):
        team_att = float(rng.integers(50, 99))
        team_def = float(rng.integers(50, 99))
        opp_att  = float(rng.integers(50, 99))
        opp_def  = float(rng.integers(50, 99))

        for code in FORMATION_PROFILES:
            p   = _compute_win_prob(code, team_att, team_def, opp_att, opp_def)
            win = int(rng.random() < p)   # Bernoulli draw — not uniform random

            records.append({
                "Formation":    code,
                "Team_Attack":  team_att,
                "Team_Defense": team_def,
                "Opp_Attack":   opp_att,
                "Opp_Defense":  opp_def,
                "Win":          win,
            })

    return pd.DataFrame(records)


# ── Load or bootstrap model ───────────────────────────────────────────────────
def load_model() -> RandomForestClassifier:
    try:
        clf = joblib.load(MODEL_PATH)
        # Sanity-check: a clearly superior team (att=88, def=85) vs weak side
        # (att=65, def=68) must score >55% in 4-3-3 AND beat 5-4-1.
        # Old formula produced ~7% here — force retrain if detected.
        test_att = pd.DataFrame({
            "Formation":    [7, 14],   # 4-3-3 vs 5-4-1
            "Team_Attack":  [88, 88],
            "Team_Defense": [85, 85],
            "Opp_Attack":   [65, 65],
            "Opp_Defense":  [68, 68],
        })
        probs = clf.predict_proba(test_att)[:, 1]
        if probs[0] < 0.55 or probs[0] <= probs[1]:
            raise ValueError("Miscalibrated model — retraining with fixed formula.")
        return clf
    except Exception:
        # Retrain with corrected win-probability formula
        df  = _generate_training_data(n_matches=8000, seed=42)
        clf = RandomForestClassifier(
            n_estimators=300,
            max_depth=12,
            min_samples_leaf=5,
            random_state=42,
            n_jobs=-1,
        )
        clf.fit(
            df[["Formation", "Team_Attack", "Team_Defense", "Opp_Attack", "Opp_Defense"]],
            df["Win"],
        )
        try:
            joblib.dump(clf, MODEL_PATH)
        except Exception:
            pass   # read-only filesystem on Render free tier — in-memory is fine
        return clf


model = load_model()


def load_teams() -> dict:
    try:
        return json.load(open(TEAMS_PATH, encoding="utf-8"))
    except Exception:
        return {}


def score_all_formations(
    team_att: int | float,
    team_def: int | float,
    opp_att:  int | float,
    opp_def:  int | float,
    familiarity_bonus: str | None = None,
    opp_habit:         str | None = None,
) -> list[dict]:
    """
    Score all 17 formations through the ML model.
    Returns list of {formation, probability} sorted best → worst.

    Adjustments applied after ML prediction:
      +5%  if team habitually plays this formation (familiarity bonus)
      -5%  if opponent's 5-ATB typically exploits 3-at-back shapes
      -3%  if opponent's 3-at-back typically exploits standard 4-back shapes
    """
    results = []
    for code, name in FORMATIONS.items():
        test = pd.DataFrame({
            "Formation":    [code],
            "Team_Attack":  [float(team_att)],
            "Team_Defense": [float(team_def)],
            "Opp_Attack":   [float(opp_att)],
            "Opp_Defense":  [float(opp_def)],
        })
        prob = float(model.predict_proba(test)[0][1] * 100)

        # +5% if team already plays this formation regularly
        if familiarity_bonus and name == familiarity_bonus:
            prob += 5.0

        # Penalty if opponent's shape historically exploits ours
        if opp_habit and opp_habit[0].isdigit():
            opp_backs = int(opp_habit.split("-")[0])
            if opp_backs >= 5 and name.startswith("3"):
                prob -= 5.0
            if opp_backs <= 3 and name in ["4-2-3-1", "4-3-3", "4-4-2"]:
                prob -= 3.0

        results.append({"formation": name, "probability": round(prob, 1)})

    return sorted(results, key=lambda x: x["probability"], reverse=True)
