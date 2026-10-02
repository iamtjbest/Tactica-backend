"""
app/xi_predictor.py — National team Starting XI predictor + XI-specific ratings

Two responsibilities:
  1. PREDICT the most likely starting XI from a 23-26 man squad
  2. RATE that specific XI with ATK/DEF numbers that change when the XI changes

Rating formula (agreed weights):
  60% player club form  (raw stats from current season)
  25% international track record  (caps, goals, goal rate)
  15% squad quality baseline  (market value proxy for depth)

Stats used per position:
  FW: goals/90, assists/90, G+A/90, minutes share
  MF: goals/90, assists/90, G+A/90, minutes share
  DF: clean_sheets ratio, minutes share, goals_conceded rate (GK proxy)
  GK: clean_sheets ratio, goals_conceded rate, minutes share

Since Transfermarkt free pages only provide basic stats (goals, assists,
minutes, appearances, cards, clean sheets), the engine works with those.
Advanced stats (xG, xA, tackles, interceptions) can be layered in later
if FBref or a paid API becomes available.
"""

import math
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ── LEAGUE WEIGHTS (for club form) ─────────────────────────────────────────
# A player's club stats matter more in a top league.
# Weight = multiplier on the club-form component (1.0 = reference).
LEAGUE_WEIGHTS: dict[str, float] = {
    # Top 5
    "England": 1.0, "Spain": 1.0, "Germany": 0.98, "Italy": 0.97,
    "France": 0.95,
    # Strong leagues
    "Portugal": 0.88, "Netherlands": 0.86, "Belgium": 0.84,
    "Turkey": 0.80, "Scotland": 0.78, "Austria": 0.77,
    "Switzerland": 0.76, "Greece": 0.75,
    # Mid-tier
    "Saudi Arabia": 0.72, "United States": 0.72, "Brazil": 0.85,
    "Argentina": 0.82, "Mexico": 0.74, "Japan": 0.73,
    "South Korea": 0.72, "Australia": 0.68,
    # Default
    "_default": 0.65,
}


def _league_weight(club_country: str) -> float:
    """Look up the league multiplier for a player's club country."""
    if not club_country:
        return LEAGUE_WEIGHTS["_default"]
    # Try exact match, then partial
    if club_country in LEAGUE_WEIGHTS:
        return LEAGUE_WEIGHTS[club_country]
    for key, w in LEAGUE_WEIGHTS.items():
        if key.lower() in club_country.lower() or club_country.lower() in key.lower():
            return w
    return LEAGUE_WEIGHTS["_default"]


# ── POSITION BUCKETS ───────────────────────────────────────────────────────

def _pos_bucket(position: str, specific_pos: str) -> str:
    """Classify into GK / DF / MF / FW for rating purposes."""
    pos = (position or "MF").upper()
    spec = (specific_pos or "").upper()
    if pos == "GK" or spec == "GK":
        return "GK"
    if pos == "DF" or spec in ("CB", "RB", "LB", "RWB", "LWB"):
        return "DF"
    if pos == "FW" or spec in ("ST", "CF", "LW", "RW", "SS", "LWF", "RWF"):
        return "FW"
    return "MF"


# ── 1. XI PREDICTOR ────────────────────────────────────────────────────────

def predict_starting_xi(
    squad: list[dict],
    lineup_history: list[dict] = None,
    formation: str = "4-3-3",
) -> list[dict]:
    """
    Predict the most likely starting XI from a national team squad.

    Selection factors (weighted):
      40% historical starts — how often they started recent internationals
      35% minutes played at club — proxy for match fitness / form
      15% caps — experience matters for national teams
      10% market value — squad depth / quality signal

    Args:
        squad: list of player dicts from tm_scraper.scrape_squad()
        lineup_history: list of match lineup dicts from tm_scraper.scrape_match_lineups()
        formation: target formation string (e.g. "4-3-3")

    Returns:
        list of 11 player dicts, each with a "selection_score" field
    """
    import re

    # Parse formation into positional slots
    parts = [int(x) for x in re.findall(r"\d+", formation)]
    if not parts or sum(parts) != 10:
        parts = [4, 3, 3]  # default

    def_count = parts[0]
    att_count = parts[-1]
    mid_count = sum(parts[1:-1]) if len(parts) > 2 else parts[1]

    # Build start-frequency map from lineup history
    start_counts: dict[str, int] = {}
    total_matches = 0
    if lineup_history:
        matches = lineup_history if isinstance(lineup_history, list) else lineup_history.get("matches", [])
        total_matches = len(matches)
        for match in matches:
            starters = match.get("starters", [])
            for s in starters:
                name = s.get("name", "")
                if name:
                    # Fuzzy-match: store lowered, stripped version
                    key = name.strip().lower()
                    start_counts[key] = start_counts.get(key, 0) + 1

    # Score each player
    scored_players = []
    for p in squad:
        name = (p.get("name") or "").strip()
        if not name:
            continue

        bucket = _pos_bucket(p.get("position", "MF"), p.get("specific_position", ""))

        # Factor 1: Historical start frequency (40%)
        name_key = name.lower()
        starts = start_counts.get(name_key, 0)
        # Also try surname match for fuzzy matching
        if starts == 0 and " " in name:
            surname = name.split()[-1].lower()
            for k, v in start_counts.items():
                if surname in k:
                    starts = max(starts, v)

        start_freq = (starts / max(total_matches, 1)) * 100 if total_matches > 0 else 0

        # Factor 2: Club minutes (35%) — proxy for form/fitness
        club_stats = p.get("club_stats") or {}
        minutes = club_stats.get("minutes", 0) if isinstance(club_stats, dict) else 0
        # Also check top-level minutes if present
        if minutes == 0:
            minutes = p.get("minutes", 0) or p.get("Min", 0) or 0
        # Normalize: 2500+ mins = 100, scale linearly
        minutes_score = min(100, (minutes / 2500) * 100)

        # Factor 3: Caps (15%)
        caps = p.get("caps", 0) or 0
        # Normalize: 80+ caps = 100
        caps_score = min(100, (caps / 80) * 100)

        # Factor 4: Market value (10%) — depth signal
        mv_str = p.get("market_value", "") or ""
        mv_score = _parse_market_value_score(mv_str)

        # Weighted total
        selection_score = (
            0.40 * start_freq +
            0.35 * minutes_score +
            0.15 * caps_score +
            0.10 * mv_score
        )

        scored_players.append({
            **p,
            "bucket": bucket,
            "selection_score": round(selection_score, 1),
            "_start_freq": round(start_freq, 1),
            "_minutes_score": round(minutes_score, 1),
            "_caps_score": round(caps_score, 1),
            "_mv_score": round(mv_score, 1),
        })

    # Sort by selection_score within each positional bucket, then draft
    xi = []
    named = set()

    def draft_bucket(bucket_name: str, count: int):
        pool = sorted(
            [p for p in scored_players if p["bucket"] == bucket_name and p["name"] not in named],
            key=lambda x: x["selection_score"],
            reverse=True,
        )
        for p in pool[:count]:
            xi.append(p)
            named.add(p["name"])

    draft_bucket("GK", 1)
    draft_bucket("DF", def_count)
    draft_bucket("MF", mid_count)
    draft_bucket("FW", att_count)

    # Emergency pad if short
    remaining = sorted(
        [p for p in scored_players if p["name"] not in named and p["bucket"] != "GK"],
        key=lambda x: x["selection_score"],
        reverse=True,
    )
    while len(xi) < 11 and remaining:
        xi.append(remaining.pop(0))

    return xi[:11]


def _parse_market_value_score(mv_str: str) -> float:
    """Convert a market value string like '€180M' to a 0-100 score."""
    if not mv_str:
        return 30.0  # unknown = mid-low
    mv_str = mv_str.strip().replace("€", "").replace("$", "").replace("£", "")
    multiplier = 1.0
    if "bn" in mv_str.lower() or "b" in mv_str.lower():
        multiplier = 1000.0
    elif "m" in mv_str.lower():
        multiplier = 1.0
    elif "k" in mv_str.lower() or "th" in mv_str.lower():
        multiplier = 0.001

    import re
    nums = re.findall(r"[\d.]+", mv_str)
    if not nums:
        return 30.0
    try:
        val = float(nums[0]) * multiplier  # in millions
    except (ValueError, IndexError):
        return 30.0

    # Scale: €150M+ = 100, €50M = 70, €10M = 45, €1M = 20, <€1M = 10
    if val >= 150:
        return 100.0
    elif val >= 50:
        return 70 + (val - 50) * (30 / 100)
    elif val >= 10:
        return 45 + (val - 10) * (25 / 40)
    elif val >= 1:
        return 20 + (val - 1) * (25 / 9)
    else:
        return max(10, val * 20)


# ── 2. XI-SPECIFIC RATING ENGINE ──────────────────────────────────────────
#
# The key principle: different XIs produce different ATK/DEF ratings.
# France with Mbappe/Olise/Dembele ≠ France with Barcola/Akliouche/Lepaul.

def rate_xi(
    xi: list[dict],
    nation_name: str = "",
) -> dict:
    """
    Rate a specific Starting XI and produce ATK/DEF numbers.

    Formula (per player):
      60% club form  → goals/assists/minutes per 90, weighted by league
      25% international track record  → caps, goals, goal rate
      15% squad quality baseline  → market value as quality floor

    Team ATK = weighted average of FW + attacking MF ratings
    Team DEF = weighted average of DF + GK + defensive MF ratings

    Returns:
        {
            "attack": 72.5,
            "defence": 68.3,
            "xi_quality": 70.4,  # overall
            "player_ratings": [
                {"name": "Mbappe", "rating": 88.2, "pos": "FW", ...},
                ...
            ]
        }
    """
    if not xi:
        return {"attack": 50.0, "defence": 50.0, "xi_quality": 50.0, "player_ratings": []}

    player_ratings = []

    for p in xi:
        bucket = _pos_bucket(
            p.get("position", p.get("pos", "MF")),
            p.get("specific_position", p.get("spec_pos", ""))
        )
        club_stats = p.get("club_stats") or {}
        if not isinstance(club_stats, dict):
            club_stats = {}

        # ── 60% CLUB FORM ────────────────────────────────────────────
        club_form = _score_club_form(p, club_stats, bucket)
        league_w = _league_weight(p.get("club_country", ""))
        club_form *= league_w

        # ── 25% INTERNATIONAL TRACK RECORD ───────────────────────────
        intl_score = _score_international(p, bucket)

        # ── 15% SQUAD QUALITY BASELINE ───────────────────────────────
        mv_str = p.get("market_value", "")
        quality_floor = _parse_market_value_score(mv_str)

        # Combined rating
        rating = (0.60 * club_form + 0.25 * intl_score + 0.15 * quality_floor)
        rating = max(30, min(99, rating))  # clamp

        player_ratings.append({
            "name": p.get("name", "Unknown"),
            "pos": bucket,
            "spec_pos": p.get("specific_position", p.get("spec_pos", "")),
            "rating": round(rating, 1),
            "_club_form": round(club_form, 1),
            "_intl_score": round(intl_score, 1),
            "_quality_floor": round(quality_floor, 1),
        })

    # ── TEAM ATK / DEF ───────────────────────────────────────────────────
    # ATK = FW (weight 1.0) + MF (weight 0.5) contributions
    # DEF = DF (weight 1.0) + GK (weight 1.0) + MF (weight 0.3) contributions
    att_scores = []
    def_scores = []

    for pr in player_ratings:
        r = pr["rating"]
        if pr["pos"] == "FW":
            att_scores.append((r, 1.0))    # full weight to ATK
            def_scores.append((r, 0.05))   # negligible DEF contribution
        elif pr["pos"] == "MF":
            att_scores.append((r, 0.5))    # partial ATK
            def_scores.append((r, 0.3))    # partial DEF
        elif pr["pos"] == "DF":
            att_scores.append((r, 0.05))   # negligible ATK
            def_scores.append((r, 1.0))    # full weight to DEF
        elif pr["pos"] == "GK":
            def_scores.append((r, 1.0))    # GK is pure DEF

    attack = _weighted_avg(att_scores) if att_scores else 50.0
    defence = _weighted_avg(def_scores) if def_scores else 50.0
    xi_quality = (attack + defence) / 2.0

    return {
        "attack": round(attack, 1),
        "defence": round(defence, 1),
        "xi_quality": round(xi_quality, 1),
        "player_ratings": player_ratings,
    }


def _weighted_avg(pairs: list[tuple[float, float]]) -> float:
    """Weighted average from (value, weight) pairs."""
    total_w = sum(w for _, w in pairs)
    if total_w == 0:
        return 50.0
    return sum(v * w for v, w in pairs) / total_w


def _score_club_form(player: dict, stats: dict, bucket: str) -> float:
    """
    Score club form (0-100) based on raw season stats.

    FW/MF: goals/90, assists/90, G+A/90, minutes share
    DF:    clean_sheets ratio, minutes share, low goals_conceded
    GK:    clean_sheets ratio, goals_conceded per 90, minutes share
    """
    minutes = stats.get("minutes", 0) or 0
    apps = stats.get("appearances", 0) or 0
    goals = stats.get("goals", 0) or 0
    assists = stats.get("assists", 0) or 0
    clean_sheets = stats.get("clean_sheets", 0) or 0
    goals_conceded = stats.get("goals_conceded", 0) or 0

    # If no stats at all, check player-level fields
    if minutes == 0:
        minutes = player.get("minutes", 0) or player.get("Min", 0) or 0
    if goals == 0:
        goals = player.get("goals", 0) or player.get("G", 0) or 0
    if assists == 0:
        assists = player.get("assists", 0) or player.get("A", 0) or 0

    # Minutes share: how much they've played (fitness/form proxy)
    # A player with 2000+ mins is fully match-fit
    minutes_share = min(1.0, minutes / 2000) if minutes > 0 else 0.0

    if bucket in ("FW", "MF"):
        # Goal output
        g_per_90 = (goals / minutes * 90) if minutes > 0 else 0
        a_per_90 = (assists / minutes * 90) if minutes > 0 else 0
        ga_per_90 = g_per_90 + a_per_90

        if bucket == "FW":
            # FW: goals matter most
            # Elite FW: 0.7+ G/90, 0.3+ A/90 = 100
            goal_score = min(100, (g_per_90 / 0.7) * 100)
            assist_score = min(100, (a_per_90 / 0.4) * 100)
            ga_score = min(100, (ga_per_90 / 0.9) * 100)
            fitness = minutes_share * 100

            return 0.35 * goal_score + 0.20 * assist_score + 0.25 * ga_score + 0.20 * fitness
        else:
            # MF: balanced contribution
            # Good MF: 0.2+ G/90, 0.3+ A/90 = 100
            goal_score = min(100, (g_per_90 / 0.25) * 100)
            assist_score = min(100, (a_per_90 / 0.35) * 100)
            ga_score = min(100, (ga_per_90 / 0.5) * 100)
            fitness = minutes_share * 100

            return 0.20 * goal_score + 0.30 * assist_score + 0.25 * ga_score + 0.25 * fitness

    elif bucket == "DF":
        # Defenders: clean sheets, low concession, fitness
        cs_ratio = (clean_sheets / apps) if apps > 0 else 0
        cs_score = min(100, (cs_ratio / 0.4) * 100)  # 40%+ CS rate = elite

        # Goals conceded rate (lower is better) — mainly for GK but DF too
        gc_per_90 = (goals_conceded / minutes * 90) if minutes > 0 else 1.5
        gc_score = max(0, 100 - (gc_per_90 / 2.0) * 100)  # 0 GC/90 = 100, 2+ = 0

        fitness = minutes_share * 100

        # DF also get a small attacking bonus
        g_per_90 = (goals / minutes * 90) if minutes > 0 else 0
        att_bonus = min(15, (g_per_90 / 0.1) * 15)  # max 15 pts for scoring defenders

        return 0.30 * cs_score + 0.25 * gc_score + 0.30 * fitness + 0.15 * att_bonus

    elif bucket == "GK":
        # Goalkeepers: clean sheets and goals conceded
        cs_ratio = (clean_sheets / apps) if apps > 0 else 0
        cs_score = min(100, (cs_ratio / 0.4) * 100)

        gc_per_90 = (goals_conceded / minutes * 90) if minutes > 0 else 1.5
        gc_score = max(0, 100 - (gc_per_90 / 1.5) * 100)

        fitness = minutes_share * 100

        return 0.40 * cs_score + 0.35 * gc_score + 0.25 * fitness

    return 50.0  # fallback


def _score_international(player: dict, bucket: str) -> float:
    """
    Score international track record (0-100).

    All positions: caps matter, but FW/MF also score on international goals.
    """
    caps = player.get("caps", 0) or 0
    goals = player.get("goals", 0) or 0

    # Caps score: 100+ caps = 100
    caps_score = min(100, (caps / 100) * 100)

    if bucket in ("FW", "MF"):
        # Goal rate matters for attackers
        goal_rate = (goals / max(caps, 1))
        # Elite intl attackers: 0.5+ goals/cap
        gr_score = min(100, (goal_rate / 0.5) * 100)

        # International goals raw count also matters
        goals_score = min(100, (goals / 40) * 100)  # 40+ intl goals = 100

        return 0.35 * caps_score + 0.35 * gr_score + 0.30 * goals_score
    else:
        # Defenders/GK: caps are the main international signal
        # Small bonus for goals (scoring defenders)
        goals_bonus = min(20, (goals / 10) * 20)

        return 0.80 * caps_score + 0.20 * goals_bonus


# ── PUBLIC API: predict + rate in one call ─────────────────────────────────

def predict_and_rate(
    squad_data: dict,
    lineup_data: dict = None,
    formation: str = "4-3-3",
    nation_name: str = "",
) -> dict:
    """
    Full pipeline: predict starting XI → rate that XI → return ATK/DEF.

    This is what the nations router calls.

    Args:
        squad_data: output of tm_scraper.scrape_squad()
        lineup_data: output of tm_scraper.scrape_match_lineups()
        formation: target formation
        nation_name: for logging

    Returns:
        {
            "formation": "4-3-3",
            "attack": 72.5,
            "defence": 68.3,
            "xi_quality": 70.4,
            "predicted_xi": [...],
            "player_ratings": [...],
        }
    """
    players = squad_data.get("players", []) if squad_data else []
    if not players:
        return {
            "formation": formation,
            "attack": 50.0,
            "defence": 50.0,
            "xi_quality": 50.0,
            "predicted_xi": [],
            "player_ratings": [],
        }

    # 1. Predict the starting XI
    xi = predict_starting_xi(
        squad=players,
        lineup_history=lineup_data,
        formation=formation,
    )

    # 2. Rate that specific XI
    ratings = rate_xi(xi, nation_name=nation_name)

    # 3. Build the response
    predicted_xi = []
    for p in xi:
        predicted_xi.append({
            "name": p.get("name", "Unknown"),
            "position": p.get("position", "MF"),
            "specific_position": p.get("specific_position", ""),
            "club": p.get("club", ""),
            "caps": p.get("caps", 0),
            "selection_score": p.get("selection_score", 0),
        })

    return {
        "formation": formation,
        "attack": ratings["attack"],
        "defence": ratings["defence"],
        "xi_quality": ratings["xi_quality"],
        "predicted_xi": predicted_xi,
        "player_ratings": ratings["player_ratings"],
    }
