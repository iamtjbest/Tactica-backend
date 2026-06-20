"""
app/routers/nations.py — World Cup 2026 nations module

FIX: The 70/70 default rating was caused by using placeholder team IDs (1-18)
that don't exist in BSD's database. The fix: resolve teams by NAME first using
bsd_find_team(), then fetch their BSD squad by the resolved ID.

Endpoints:
  GET  /api/nations/squads           — all WC squads (optional ?group=A)
  GET  /api/nations/squads/{id}      — single team squad (by our internal id)
  POST /api/nations/predict          — formation prediction for two nations
"""

import time
import math
from fastapi import APIRouter, Query, Path, HTTPException
from app.config import (
    bsd_get, bsd_find_team, cache_read, cache_write, cache_age,
    LEAGUE_WEIGHTS,
)
from app.ml_model import score_all_formations

router = APIRouter()

# ── Static WC2026 nation registry (matches frontend WC_2026_NATIONS) ──────────
# id: our internal reference id (matches frontend)
# bsd_names: list of name variants to try when resolving against BSD
WC_NATIONS: list[dict] = [
    # CONMEBOL (6)
    {"id":  1, "name": "Argentina",              "bsd_names": ["Argentina"],                    "conf": "CONMEBOL", "code": "AR"},
    {"id":  2, "name": "Brazil",                 "bsd_names": ["Brazil", "Brasil"],             "conf": "CONMEBOL", "code": "BR"},
    {"id":  3, "name": "Colombia",               "bsd_names": ["Colombia"],                     "conf": "CONMEBOL", "code": "CO"},
    {"id":  4, "name": "Ecuador",                "bsd_names": ["Ecuador"],                      "conf": "CONMEBOL", "code": "EC"},
    {"id":  5, "name": "Paraguay",               "bsd_names": ["Paraguay"],                     "conf": "CONMEBOL", "code": "PY"},
    {"id":  6, "name": "Uruguay",                "bsd_names": ["Uruguay"],                      "conf": "CONMEBOL", "code": "UY"},
    
    # UEFA (16)
    {"id":  7, "name": "Austria",                "bsd_names": ["Austria", "Österreich"],        "conf": "UEFA",     "code": "AT"},
    {"id":  8, "name": "Belgium",                "bsd_names": ["Belgium", "Belgique"],          "conf": "UEFA",     "code": "BE"},
    {"id":  9, "name": "Bosnia and Herzegovina", "bsd_names": ["Bosnia and Herzegovina", "Bosnia"], "conf": "UEFA", "code": "BA"},
    {"id": 10, "name": "Croatia",                "bsd_names": ["Croatia", "Hrvatska"],          "conf": "UEFA",     "code": "HR"},
    {"id": 11, "name": "Czechia",                "bsd_names": ["Czechia", "Czech Republic"],    "conf": "UEFA",     "code": "CZ"},
    {"id": 12, "name": "England",                "bsd_names": ["England"],                      "conf": "UEFA",     "code": "ENG"},
    {"id": 13, "name": "France",                 "bsd_names": ["France"],                       "conf": "UEFA",     "code": "FR"},
    {"id": 14, "name": "Germany",                "bsd_names": ["Germany", "Deutschland"],       "conf": "UEFA",     "code": "DE"},
    {"id": 15, "name": "Netherlands",            "bsd_names": ["Netherlands", "Holland"],       "conf": "UEFA",     "code": "NL"},
    {"id": 16, "name": "Norway",                 "bsd_names": ["Norway", "Norge"],              "conf": "UEFA",     "code": "NO"},
    {"id": 17, "name": "Portugal",               "bsd_names": ["Portugal"],                     "conf": "UEFA",     "code": "PT"},
    {"id": 18, "name": "Scotland",               "bsd_names": ["Scotland"],                     "conf": "UEFA",     "code": "SCO"},
    {"id": 19, "name": "Spain",                  "bsd_names": ["Spain", "España"],              "conf": "UEFA",     "code": "ES"},
    {"id": 20, "name": "Sweden",                 "bsd_names": ["Sweden", "Sverige"],            "conf": "UEFA",     "code": "SE"},
    {"id": 21, "name": "Switzerland",            "bsd_names": ["Switzerland", "Schweiz"],       "conf": "UEFA",     "code": "CH"},
    {"id": 22, "name": "Türkiye",                "bsd_names": ["Türkiye", "Turkey"],            "conf": "UEFA",     "code": "TR"},

    # CAF (10)
    {"id": 23, "name": "Algeria",                "bsd_names": ["Algeria", "Algérie"],           "conf": "CAF",      "code": "DZ"},
    {"id": 24, "name": "Cabo Verde",             "bsd_names": ["Cabo Verde", "Cape Verde"],     "conf": "CAF",      "code": "CV"},
    {"id": 25, "name": "Côte d'Ivoire",          "bsd_names": ["Côte d'Ivoire", "Ivory Coast"], "conf": "CAF",      "code": "CI"},
    {"id": 26, "name": "DR Congo",               "bsd_names": ["DR Congo", "Congo DR", "DRC"],  "conf": "CAF",      "code": "CD"},
    {"id": 27, "name": "Egypt",                  "bsd_names": ["Egypt"],                        "conf": "CAF",      "code": "EG"},
    {"id": 28, "name": "Ghana",                  "bsd_names": ["Ghana"],                        "conf": "CAF",      "code": "GH"},
    {"id": 29, "name": "Morocco",                "bsd_names": ["Morocco", "Maroc"],             "conf": "CAF",      "code": "MA"},
    {"id": 30, "name": "Senegal",                "bsd_names": ["Senegal"],                      "conf": "CAF",      "code": "SN"},
    {"id": 31, "name": "South Africa",           "bsd_names": ["South Africa", "Bafana Bafana"],"conf": "CAF",      "code": "ZA"},
    {"id": 32, "name": "Tunisia",                "bsd_names": ["Tunisia", "Tunisie"],           "conf": "CAF",      "code": "TN"},

    # AFC (9)
    {"id": 33, "name": "Australia",              "bsd_names": ["Australia", "Socceroos"],       "conf": "AFC",      "code": "AU"},
    {"id": 34, "name": "Iran",                   "bsd_names": ["Iran", "IR Iran"],              "conf": "AFC",      "code": "IR"},
    {"id": 35, "name": "Iraq",                   "bsd_names": ["Iraq"],                         "conf": "AFC",      "code": "IQ"},
    {"id": 36, "name": "Japan",                  "bsd_names": ["Japan", "Japon"],               "conf": "AFC",      "code": "JP"},
    {"id": 37, "name": "Jordan",                 "bsd_names": ["Jordan"],                       "conf": "AFC",      "code": "JO"},
    {"id": 38, "name": "Qatar",                  "bsd_names": ["Qatar"],                        "conf": "AFC",      "code": "QA"},
    {"id": 39, "name": "Saudi Arabia",           "bsd_names": ["Saudi Arabia"],                 "conf": "AFC",      "code": "SA"},
    {"id": 40, "name": "South Korea",            "bsd_names": ["South Korea", "Korea Republic"],"conf": "AFC",      "code": "KR"},
    {"id": 41, "name": "Uzbekistan",             "bsd_names": ["Uzbekistan"],                   "conf": "AFC",      "code": "UZ"},

    # CONCACAF (6)
    {"id": 42, "name": "Canada",                 "bsd_names": ["Canada"],                       "conf": "CONCACAF", "code": "CA"},
    {"id": 43, "name": "Curaçao",                "bsd_names": ["Curaçao", "Curacao"],           "conf": "CONCACAF", "code": "CW"},
    {"id": 44, "name": "Haiti",                  "bsd_names": ["Haiti"],                        "conf": "CONCACAF", "code": "HT"},
    {"id": 45, "name": "Mexico",                 "bsd_names": ["Mexico", "México"],             "conf": "CONCACAF", "code": "MX"},
    {"id": 46, "name": "Panama",                 "bsd_names": ["Panama", "Panamá"],             "conf": "CONCACAF", "code": "PA"},
    {"id": 47, "name": "United States",          "bsd_names": ["United States", "USA"],         "conf": "CONCACAF", "code": "US"},

    # OFC (1)
    {"id": 48, "name": "New Zealand",            "bsd_names": ["New Zealand", "All Whites"],    "conf": "OFC",      "code": "NZ"}
]

# Quick lookup maps
_BY_ID   = {n["id"]:   n for n in WC_NATIONS}
_BY_NAME = {n["name"]: n for n in WC_NATIONS}

# Cache TTL: 24 h for WC squads (they change infrequently)
SQUAD_TTL = 86_400


# ── Helper: resolve nation → BSD team_id via name ─────────────────────────────

def resolve_nation_bsd_id(nation: dict) -> tuple[int | None, str]:
    """
    Try each name variant in bsd_names order.
    Returns (bsd_team_id, bsd_team_name) or (None, "").
    """
    cache_key = f"nation_bsd_id__{nation['code']}"
    cached = cache_read(cache_key)
    if cached and cache_age(cached) < SQUAD_TTL:
        return cached["bsd_id"], cached["bsd_name"]

    for name_try in nation["bsd_names"]:
        bsd_id, bsd_name = bsd_find_team(name_try)
        if bsd_id:
            cache_write(cache_key, {"bsd_id": bsd_id, "bsd_name": bsd_name})
            return bsd_id, bsd_name

    return None, ""


# ── Helper: score a national team's squad ────────────────────────────────────

def score_nation_squad(bsd_team_id: int, nation_name: str) -> dict:
    """
    Fetches players for a national team via BSD /players/?team_id={id}
    and computes attack/defence ratings using the national team formula:

        Player Score = (Form×0.35 + Quality×0.30 + Experience×0.20 + Age×0.15)
                       × league_weight

        Form:       goals_per90 + assists_per90 (from BSD stats)
        Quality:    average_rating × 10  (BSD match ratings, 0–10 scale × 10)
        Experience: min(caps, 100) / 100 × 100  (capped at 100)
        Age:        peak at 26–29 yrs, bell-curve scoring

    Attack  = avg score of top-4 FW/MF players
    Defence = avg score of top-4 DF/GK players
    """
    cache_key = f"nation_squad_{bsd_team_id}"
    cached = cache_read(cache_key)
    if cached and cache_age(cached) < SQUAD_TTL:
        return cached

    players_raw = []

    # BSD endpoint: /players/?team_id={id}&limit=50
    data = bsd_get("/players/", params={"team_id": bsd_team_id, "limit": 50})
    if data and data.get("results"):
        players_raw = data["results"]
    elif data and isinstance(data, list):
        players_raw = data

    if not players_raw:
        # Fallback: try /teams/{id}/squad/
        squad_data = bsd_get(f"/teams/{bsd_team_id}/squad/")
        if squad_data and squad_data.get("players"):
            players_raw = squad_data["players"]

    scored: list[dict] = []
    for p in players_raw:
        name    = p.get("name") or p.get("display_name") or "Unknown"
        pos_raw = (p.get("position") or p.get("pos") or "M").upper()
        pos     = pos_raw[0] if pos_raw else "M"  # G / D / M / F

        # ── Form (goals+assists per 90 from BSD stats) ────────────────────
        stats = p.get("stats") or p.get("season_stats") or {}
        goals  = float(stats.get("goals", 0) or 0)
        assists= float(stats.get("assists", 0) or 0)
        mins   = float(stats.get("minutes_played", stats.get("minutes", 90)) or 90)
        mins   = max(mins, 1)
        ga_p90 = (goals + assists) / (mins / 90)
        form_score = min(ga_p90 * 25, 100)  # 4 G+A/90 = 100

        # ── Quality (BSD match rating) ────────────────────────────────────
        rating_raw = float(
            stats.get("rating", stats.get("average_rating", 7.0)) or 7.0
        )
        quality_score = rating_raw * 10  # 0–10 → 0–100

        # ── Experience (international caps) ──────────────────────────────
        caps = int(p.get("caps", p.get("national_caps", 30)) or 30)
        exp_score = min(caps, 100)

        # ── Age factor ───────────────────────────────────────────────────
        dob = p.get("date_of_birth", p.get("dob", ""))
        age = p.get("age", 27)
        if not age and dob:
            try:
                from datetime import date
                birth = date.fromisoformat(str(dob)[:10])
                age   = (date.today() - birth).days // 365
            except Exception:
                age = 27
        age = int(age or 27)
        # Peak at 26–29; below 22 and above 34 penalised
        if   age < 18: age_score = 40
        elif age < 22: age_score = 60 + (age - 18) * 5
        elif age < 26: age_score = 80 + (age - 22) * 2
        elif age <= 29: age_score = 88
        elif age <= 32: age_score = 88 - (age - 29) * 4
        elif age <= 35: age_score = 76 - (age - 32) * 6
        else:           age_score = 58

        # ── League quality weight ─────────────────────────────────────────
        club_country = (p.get("club_country") or p.get("nationality") or "").upper()
        league_weight = LEAGUE_WEIGHTS.get(club_country, 0.74)

        # ── Composite score ───────────────────────────────────────────────
        raw_score = (
            form_score    * 0.35 +
            quality_score * 0.30 +
            exp_score     * 0.20 +
            age_score     * 0.15
        )
        final_score = round(raw_score * league_weight, 2)

        scored.append({
            "name":   name,
            "pos":    pos,
            "score":  final_score,
        })

    # Sort by score descending
    scored.sort(key=lambda x: x["score"], reverse=True)

    # Attack = avg of top-4 FW/MF
    attackers = [p for p in scored if p["pos"] in ("F", "M")][:4]
    # Defence = avg of top-4 DF/GK
    defenders = [p for p in scored if p["pos"] in ("D", "G")][:4]

    attack  = round(sum(p["score"] for p in attackers)  / max(len(attackers), 1), 1) if attackers  else 70.0
    defence = round(sum(p["score"] for p in defenders) / max(len(defenders), 1), 1) if defenders else 70.0

    # Clamp to 50–98
    attack  = max(50, min(98, attack))
    defence = max(50, min(98, defence))

    result = {
        "_cached_at":   time.time(),
        "team_id":      bsd_team_id,
        "team_name":    nation_name,
        "attack":       attack,
        "defence":      defence,
        "squad_count":  len(scored),
        "players_rated": len(scored),
        "top_players":  scored[:10],
    }
    cache_write(cache_key, result)
    return result


# ── GET /api/nations/squads ───────────────────────────────────────────────────

@router.get("/nations/squads")
def nations_squads(group: str = Query(None, description="Filter by confederation e.g. UEFA")):
    nations = WC_NATIONS
    if group:
        nations = [n for n in nations if n["conf"].upper() == group.upper()]
    return {
        "count":  len(nations),
        "nations": nations,
    }


# ── GET /api/nations/squads/{id} ─────────────────────────────────────────────

@router.get("/nations/squads/{nation_id}")
def nation_squad(nation_id: int = Path(..., description="Nation internal ID (1-48)")):
    nation = _BY_ID.get(nation_id)
    if not nation:
        raise HTTPException(status_code=404, detail=f"Nation ID {nation_id} not found.")

    bsd_id, bsd_name = resolve_nation_bsd_id(nation)
    if not bsd_id:
        return {
            "nation_id":   nation_id,
            "nation_name": nation["name"],
            "conf":        nation["conf"],
            "bsd_found":   False,
            "message":     f"BSD does not have '{nation['name']}' in its national team database yet.",
            "squad":       [],
        }

    squad_data = score_nation_squad(bsd_id, nation["name"])
    return {
        "nation_id":    nation_id,
        "nation_name":  nation["name"],
        "bsd_name":     bsd_name,
        "conf":         nation["conf"],
        "bsd_found":    True,
        "squad_count":  squad_data["squad_count"],
        "attack":       squad_data["attack"],
        "defence":      squad_data["defence"],
        "top_players":  squad_data["top_players"],
    }


# ── POST /api/nations/predict ─────────────────────────────────────────────────

@router.post("/nations/predict")
def nations_predict(body: dict):
    """
    POST body:
      team_id:   int     — our internal nation id (1-48)
      opp_id:    int     — our internal nation id
      team_name: str     — display name (used as fallback if BSD ID fails)
      opp_name:  str     — display name

    Returns formation prediction based on national team ratings.
    Falls back to default 70/70 ratings only when BSD truly has no data —
    includes a clear warning in the response when this happens.
    """
    team_id   = int(body.get("team_id",   0))
    opp_id    = int(body.get("opp_id",    0))
    team_name = str(body.get("team_name", ""))
    opp_name  = str(body.get("opp_name",  ""))

    # Resolve nation objects
    my_nation  = _BY_ID.get(team_id)  or _BY_NAME.get(team_name)
    opp_nation = _BY_ID.get(opp_id)   or _BY_NAME.get(opp_name)

    if not my_nation:
        raise HTTPException(status_code=404, detail=f"Nation '{team_name}' (id={team_id}) not in registry.")
    if not opp_nation:
        raise HTTPException(status_code=404, detail=f"Nation '{opp_name}' (id={opp_id}) not in registry.")

    warnings = []

    # ── Resolve team from BSD ──────────────────────────────────────────────
    my_bsd_id,  my_bsd_name  = resolve_nation_bsd_id(my_nation)
    opp_bsd_id, opp_bsd_name = resolve_nation_bsd_id(opp_nation)

    # ── Score squads (or fall back to 70 with a warning) ──────────────────
    if my_bsd_id:
        my_data  = score_nation_squad(my_bsd_id,  my_nation["name"])
        my_attack   = my_data["attack"]
        my_defence  = my_data["defence"]
        my_count    = my_data["squad_count"]
        my_rated    = my_data["players_rated"]
    else:
        warnings.append(f"BSD has no squad data for '{my_nation['name']}'. Using default ratings (70/70).")
        my_attack  = 70.0
        my_defence = 70.0
        my_count   = 0
        my_rated   = 0

    if opp_bsd_id:
        opp_data  = score_nation_squad(opp_bsd_id, opp_nation["name"])
        opp_attack  = opp_data["attack"]
        opp_defence = opp_data["defence"]
        opp_count   = opp_data["squad_count"]
    else:
        warnings.append(f"BSD has no squad data for '{opp_nation['name']}'. Using default ratings (70/70).")
        opp_attack  = 70.0
        opp_defence = 70.0
        opp_count   = 0

    # ── ML formation prediction ───────────────────────────────────────────
    all_formations = score_all_formations(
        my_attack, my_defence, opp_attack, opp_defence
    )
    best = all_formations[0]

    response = {
        "team":          my_nation["name"],
        "opponent":      opp_nation["name"],
        "my_attack":     my_attack,
        "my_defence":    my_defence,
        "opp_attack":    opp_attack,
        "opp_defence":   opp_defence,
        "best_formation": best["formation"],
        "probability":   best["probability"],
        "all_formations": all_formations,
        "my_squad_count":  my_count,
        "opp_squad_count": opp_count,
        "players_scored":  my_rated,
        "bsd_resolved": {
            "team": my_bsd_name  or None,
            "opp":  opp_bsd_name or None,
        },
    }

    if warnings:
        response["warnings"] = warnings

    return response
