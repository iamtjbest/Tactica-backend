"""
app/national_ratings.py — National team + player rating engine

Formula:
  Player Score (0-100) =
    Form       × 0.35   (G+A per 90 from last club season, scaled)
    Quality    × 0.30   (BSD average match rating last 10 games)
    Experience × 0.20   (international caps, capped at 100)
    Age        × 0.15   (peak 26-29, scales down)

  Team Attack  = weighted avg of top 4 FW/MF player scores × league_weight
  Team Defence = weighted avg of top 4 DF player scores    × league_weight
"""

import time
from datetime import datetime
from app.config import bsd_get, LEAGUE_WEIGHTS, SPECIFIC_POS_MAP, GENERIC_POS_MAP, cache_read, cache_write, cache_age

# ── Player scoring ─────────────────────────────────────────────────────────────

def _age_factor(dob_str: str) -> float:
    """
    Prime age = 26-29 → 1.0
    Younger or older → scales down linearly.
    """
    try:
        dob  = datetime.strptime(dob_str[:10], "%Y-%m-%d")
        age  = (datetime.utcnow() - dob).days / 365.25
    except Exception:
        return 0.75  # default if unknown

    if   26 <= age <= 29: return 1.00
    elif 24 <= age <  26: return 0.90
    elif 22 <= age <  24: return 0.80
    elif 30 <= age <= 32: return 0.88
    elif 33 <= age <= 35: return 0.74
    elif 36 <= age:       return 0.55
    else:                 return 0.70   # < 22

def _form_score(goals_per90: float) -> float:
    """Scale goals+assists per 90 → 0-100."""
    return min(100.0, goals_per90 * 35.0)

def _rating_score(avg_rating: float | None) -> float:
    """BSD match rating is 0-10. Scale to 0-100."""
    if not avg_rating:
        return 55.0  # neutral baseline
    return min(100.0, (avg_rating / 10.0) * 100.0)

def _caps_score(caps: int) -> float:
    """Caps → experience 0-100. 100+ caps = 100."""
    return min(100.0, caps)

def score_player(player_id: int, dob: str, caps: int) -> dict:
    """
    Compute a Player Score for a national team member.
    Fetches: career stats (form) + recent match ratings (quality).
    Caches result for 24 hours.
    """
    cache_key = f"player_score_{player_id}"
    cached = cache_read(cache_key)
    if cached and cache_age(cached) < 86400:
        return cached

    # ── Form: last season G+A per 90 ────────────────────────────────────────
    career = bsd_get(f"/players/{player_id}/career/")
    g_a_per90 = 0.0
    league_name = "unknown"
    if career:
        seasons = career.get("seasons", [])
        if seasons:
            latest = seasons[0]  # newest first
            mins   = latest.get("minutes", 0) or 1
            goals  = latest.get("goals",   0) or 0
            assists= latest.get("assists", 0) or 0
            g_a_per90 = ((goals + assists) / mins) * 90
            # Resolve league name for weight lookup
            league_id = latest.get("league_id", 0)
            from app.config import LEAGUE_NAMES
            league_name = LEAGUE_NAMES.get(league_id, "other_europe")

    # ── Quality: average BSD match rating last 10 appearances ───────────────
    stats_data = bsd_get(f"/players/{player_id}/stats/", params={"limit": 10})
    avg_rating = None
    if stats_data:
        ratings = [
            s["rating"] for s in stats_data.get("results", [])
            if s.get("rating") is not None
        ]
        if ratings:
            avg_rating = sum(ratings) / len(ratings)

    # ── Compose score ────────────────────────────────────────────────────────
    league_wt  = LEAGUE_WEIGHTS.get(league_name, 0.75)
    form_s     = _form_score(g_a_per90)
    quality_s  = _rating_score(avg_rating)
    exp_s      = _caps_score(caps)
    age_s      = _age_factor(dob) * 100

    raw_score  = (form_s * 0.35 + quality_s * 0.30 + exp_s * 0.20 + age_s * 0.15)
    final      = round(min(99, raw_score * league_wt), 1)

    result = {
        "_cached_at": time.time(),
        "player_id":  player_id,
        "score":      final,
        "form":       round(form_s, 1),
        "quality":    round(quality_s, 1),
        "experience": round(exp_s, 1),
        "age_factor": round(age_s, 1),
        "league":     league_name,
        "league_weight": league_wt,
        "g_a_per90":  round(g_a_per90, 3),
        "avg_rating": round(avg_rating, 2) if avg_rating else None,
    }
    cache_write(cache_key, result)
    return result


def rate_national_team(team_id: int, squad: list[dict]) -> dict:
    """
    Compute Attack and Defence ratings for a national team.
    squad: list of player dicts from BSD worldcup/squads/{team_id}/

    Attack  = weighted avg of top 4 FW/MF scores × their league weight
    Defence = weighted avg of top 4 DF scores    × their league weight
    """
    cache_key = f"nat_team_{team_id}"
    cached = cache_read(cache_key)
    if cached and cache_age(cached) < 43200:  # 12-hour cache
        return cached

    att_scores, def_scores = [], []

    for p in squad:
        pid  = p.get("player_id")
        if not pid:
            continue
        dob  = p.get("date_of_birth", "1995-01-01")
        caps = p.get("caps", 0) or 0
        pos  = p.get("position","MF").upper()

        scored = score_player(pid, dob, caps)
        s      = scored.get("score", 60.0)

        if pos in ("FW","MF"):
            att_scores.append(s)
        elif pos == "DF":
            def_scores.append(s)

    def top_avg(scores, n=4):
        top = sorted(scores, reverse=True)[:n]
        return round(sum(top) / len(top), 1) if top else 70.0

    attack  = top_avg(att_scores)
    defence = top_avg(def_scores)

    result = {
        "_cached_at": time.time(),
        "team_id":    team_id,
        "attack":     attack,
        "defence":    defence,
        "players_scored": len(att_scores) + len(def_scores),
    }
    cache_write(cache_key, result)
    return result
