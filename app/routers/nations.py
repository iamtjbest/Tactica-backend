"""
app/routers/nations.py — World Cup 2026 nations module (v3 — CONFIRMED FIELD NAMES)

CONFIRMED from BSD debug response for Germany (bsd_team_id=467):
  Field names:  id, team_id, name, jersey_number, position, status,
                call_up_date, club, club_country, caps, goals,
                date_of_birth, age, player_id
  position values: "DF", "FW", "GK", "MF"  (no sub-positions)
  club_country:    FULL COUNTRY NAMES e.g. "Spain", "Germany", "England"
                   NOT country codes — this was the root cause of 50/52 ratings.

ROOT CAUSE OF FLAT 50/52 RATINGS (now fixed):
  LEAGUE_WEIGHTS used country codes ("ESP","GER","ENG") but BSD sends
  full names ("Spain","Germany","England"). Every lookup fell through to
  the 0.74 default, compressing every player score to 50-53 regardless
  of how many caps/goals they had.

4-3-3 WINGER FIX:
  BSD classifies wingers (Musiala, Wirtz, Sané) as "MF", not "FW".
  A naive position-match would put them in midfield slots and fill the
  front 3 with pure strikers only. The lineup builder now uses a
  position-flexible slot-filling strategy:
    - FW slots: draw from combined FW+MF pool (highest-rated first)
    - MF slots: draw from remaining MF not already placed in FW slots
  This means Wirtz/Musiala naturally float to the front 3 in a 4-3-3
  while Goretzka/Groß fill the deeper midfield slots — which is correct.
"""

import time
from datetime import date
from fastapi import APIRouter, Query, Path, HTTPException
from app.config import bsd_get, bsd_find_team, cache_read, cache_write, cache_age

router = APIRouter()

# ── LEAGUE WEIGHTS — FULL COUNTRY NAMES matching BSD's club_country field ────
# Verified against actual BSD response: club_country = "Spain", "England", etc.
LEAGUE_WEIGHTS_BY_NAME: dict[str, float] = {
    # Top 5 European leagues
    "England":        1.00,
    "Spain":          0.97,
    "Germany":        0.95,
    "Italy":          0.94,
    "France":         0.91,
    # Second tier European
    "Portugal":       0.88,
    "Netherlands":    0.87,
    "Belgium":        0.85,
    "Turkey":         0.84,
    "Türkiye":        0.84,   # BSD uses this spelling (confirmed from Sané)
    "Russia":         0.82,
    "Ukraine":        0.82,
    "Greece":         0.81,
    "Scotland":       0.80,
    "Czech Republic": 0.80,
    "Czechia":        0.80,
    "Austria":        0.79,
    "Switzerland":    0.78,
    "Denmark":        0.78,
    "Norway":         0.77,
    "Sweden":         0.77,
    "Poland":         0.76,
    "Croatia":        0.76,
    "Serbia":         0.76,
    # Americas
    "Brazil":         0.84,
    "Argentina":      0.82,
    "Mexico":         0.80,
    "Colombia":       0.79,
    "Uruguay":        0.78,
    "Ecuador":        0.77,
    "United States":  0.78,
    "USA":            0.78,
    # Asia / Gulf
    "Saudi Arabia":   0.76,
    "Japan":          0.77,
    "South Korea":    0.77,
    "China":          0.74,
    "Qatar":          0.75,
    # Africa
    "Morocco":        0.76,
    "Egypt":          0.75,
    "South Africa":   0.75,
}
LEAGUE_WEIGHT_DEFAULT = 0.74


def league_weight(club_country: str) -> float:
    """Case-insensitive lookup with default fallback."""
    w = LEAGUE_WEIGHTS_BY_NAME.get(club_country)
    if w is not None:
        return w
    # Try title-case fallback for unexpected capitalisations
    w = LEAGUE_WEIGHTS_BY_NAME.get(club_country.title())
    return w if w is not None else LEAGUE_WEIGHT_DEFAULT


# ── 48-nation registry ────────────────────────────────────────────────────────
WC_NATIONS: list[dict] = [
    {"id": 1,  "name": "Canada",        "bsd_names": ["Canada"],                                    "conf": "CONCACAF"},
    {"id": 2,  "name": "Mexico",        "bsd_names": ["Mexico", "México"],                           "conf": "CONCACAF"},
    {"id": 3,  "name": "USA",           "bsd_names": ["USA", "United States"],                        "conf": "CONCACAF"},
    {"id": 4,  "name": "Curaçao",       "bsd_names": ["Curacao", "Curaçao"],                          "conf": "CONCACAF"},
    {"id": 5,  "name": "Haiti",         "bsd_names": ["Haiti"],                                       "conf": "CONCACAF"},
    {"id": 6,  "name": "Panama",        "bsd_names": ["Panama", "Panamá"],                            "conf": "CONCACAF"},
    {"id": 7,  "name": "Argentina",     "bsd_names": ["Argentina"],                                   "conf": "CONMEBOL"},
    {"id": 8,  "name": "Brazil",        "bsd_names": ["Brazil", "Brasil"],                             "conf": "CONMEBOL"},
    {"id": 9,  "name": "Colombia",      "bsd_names": ["Colombia"],                                    "conf": "CONMEBOL"},
    {"id": 10, "name": "Ecuador",       "bsd_names": ["Ecuador"],                                     "conf": "CONMEBOL"},
    {"id": 11, "name": "Paraguay",      "bsd_names": ["Paraguay"],                                    "conf": "CONMEBOL"},
    {"id": 12, "name": "Uruguay",       "bsd_names": ["Uruguay"],                                     "conf": "CONMEBOL"},
    {"id": 13, "name": "Austria",       "bsd_names": ["Austria"],                                     "conf": "UEFA"},
    {"id": 14, "name": "Belgium",       "bsd_names": ["Belgium", "Belgique"],                         "conf": "UEFA"},
    {"id": 15, "name": "Bosnia and Herzegovina", "bsd_names": ["Bosnia and Herzegovina", "Bosnia"],   "conf": "UEFA"},
    {"id": 16, "name": "Croatia",       "bsd_names": ["Croatia", "Hrvatska"],                         "conf": "UEFA"},
    {"id": 17, "name": "Czechia",       "bsd_names": ["Czechia", "Czech Republic"],                   "conf": "UEFA"},
    {"id": 18, "name": "England",       "bsd_names": ["England"],                                     "conf": "UEFA"},
    {"id": 19, "name": "France",        "bsd_names": ["France"],                                      "conf": "UEFA"},
    {"id": 20, "name": "Germany",       "bsd_names": ["Germany", "Deutschland"],                      "conf": "UEFA"},
    {"id": 21, "name": "Netherlands",   "bsd_names": ["Netherlands", "Holland"],                      "conf": "UEFA"},
    {"id": 22, "name": "Norway",        "bsd_names": ["Norway", "Norge"],                             "conf": "UEFA"},
    {"id": 23, "name": "Portugal",      "bsd_names": ["Portugal"],                                    "conf": "UEFA"},
    {"id": 24, "name": "Scotland",      "bsd_names": ["Scotland"],                                    "conf": "UEFA"},
    {"id": 25, "name": "Spain",         "bsd_names": ["Spain", "España"],                             "conf": "UEFA"},
    {"id": 26, "name": "Sweden",        "bsd_names": ["Sweden", "Sverige"],                           "conf": "UEFA"},
    {"id": 27, "name": "Switzerland",   "bsd_names": ["Switzerland", "Schweiz"],                      "conf": "UEFA"},
    {"id": 28, "name": "Türkiye",       "bsd_names": ["Turkey", "Türkiye"],                           "conf": "UEFA"},
    {"id": 29, "name": "Algeria",       "bsd_names": ["Algeria"],                                     "conf": "CAF"},
    {"id": 30, "name": "Cabo Verde",    "bsd_names": ["Cabo Verde", "Cape Verde"],                    "conf": "CAF"},
    {"id": 31, "name": "DR Congo",      "bsd_names": ["DR Congo", "Congo DR", "DRC"],                 "conf": "CAF"},
    {"id": 32, "name": "Côte d'Ivoire", "bsd_names": ["Cote d'Ivoire", "Côte d'Ivoire", "Ivory Coast"], "conf": "CAF"},
    {"id": 33, "name": "Egypt",         "bsd_names": ["Egypt"],                                       "conf": "CAF"},
    {"id": 34, "name": "Ghana",         "bsd_names": ["Ghana"],                                       "conf": "CAF"},
    {"id": 35, "name": "Morocco",       "bsd_names": ["Morocco", "Maroc"],                            "conf": "CAF"},
    {"id": 36, "name": "Senegal",       "bsd_names": ["Senegal"],                                     "conf": "CAF"},
    {"id": 37, "name": "South Africa",  "bsd_names": ["South Africa", "Bafana Bafana"],               "conf": "CAF"},
    {"id": 38, "name": "Tunisia",       "bsd_names": ["Tunisia", "Tunisie"],                          "conf": "CAF"},
    {"id": 39, "name": "Australia",     "bsd_names": ["Australia", "Socceroos"],                      "conf": "AFC"},
    {"id": 40, "name": "Iraq",          "bsd_names": ["Iraq"],                                        "conf": "AFC"},
    {"id": 41, "name": "Iran",          "bsd_names": ["Iran", "IR Iran"],                              "conf": "AFC"},
    {"id": 42, "name": "Japan",         "bsd_names": ["Japan", "Japon"],                               "conf": "AFC"},
    {"id": 43, "name": "Jordan",        "bsd_names": ["Jordan"],                                      "conf": "AFC"},
    {"id": 44, "name": "South Korea",   "bsd_names": ["South Korea", "Korea Republic", "Korea South"], "conf": "AFC"},
    {"id": 45, "name": "Qatar",         "bsd_names": ["Qatar"],                                       "conf": "AFC"},
    {"id": 46, "name": "Saudi Arabia",  "bsd_names": ["Saudi Arabia"],                                 "conf": "AFC"},
    {"id": 47, "name": "Uzbekistan",    "bsd_names": ["Uzbekistan"],                                   "conf": "AFC"},
    {"id": 48, "name": "New Zealand",   "bsd_names": ["New Zealand", "All Whites"],                   "conf": "OFC"},
]

_BY_ID   = {n["id"]:   n for n in WC_NATIONS}
_BY_NAME = {n["name"]: n for n in WC_NATIONS}

SQUAD_TTL = 21_600  # 6h


def parse_formation_slots(formation: str) -> dict:
    """Parse '4-3-3' → {GK:1, DF:4, MF:3, FW:3}"""
    nums_str = formation.split()[0]
    parts = [int(x) for x in nums_str.split("-") if x.isdigit()]
    if len(parts) < 2:
        return {"GK": 1, "DF": 4, "MF": 3, "FW": 3}
    return {
        "GK": 1,
        "DF": parts[0],
        "MF": sum(parts[1:-1]) if len(parts) > 2 else 0,
        "FW": parts[-1],
    }


# ── Resolve nation → real BSD team_id ────────────────────────────────────────

def resolve_nation_bsd_id(nation: dict) -> tuple[int | None, str]:
    cache_key = f"nation_bsd_id_v3__{nation['id']}"
    cached = cache_read(cache_key)
    if cached and cache_age(cached) < SQUAD_TTL:
        return cached.get("bsd_id"), cached.get("bsd_name", "")

    for name_try in nation["bsd_names"]:
        bsd_id, bsd_name = bsd_find_team(name_try)
        if bsd_id:
            cache_write(cache_key, {"bsd_id": bsd_id, "bsd_name": bsd_name,
                                    "_cached_at": time.time()})
            return bsd_id, bsd_name

    cache_write(cache_key, {"bsd_id": None, "bsd_name": "", "_cached_at": time.time()})
    return None, ""


# ── Score a player ────────────────────────────────────────────────────────────

def score_player(p: dict, for_attack: bool) -> float:
    """
    Scores one player using BSD's confirmed available fields.
    for_attack=True  → use attacker formula (caps 35%, goals 35%, age 30%)
    for_attack=False → use defender formula (caps 55%, age 25%, goals 20%)
    """
    caps  = int(p.get("caps",  0) or 0)
    goals = int(p.get("goals", 0) or 0)
    age   = int(p.get("age",   27) or 27)
    if age == 0:
        try:
            dob = p.get("date_of_birth", "")
            birth = date.fromisoformat(str(dob)[:10])
            age = (date.today() - birth).days // 365
        except Exception:
            age = 27

    # Sub-scores 0-100
    caps_score = min(caps, 100)

    goals_cap  = 30 if for_attack else 10
    goals_score = min((goals / goals_cap) * 100, 100)

    if   age < 20: age_score = 50
    elif age < 23: age_score = 65 + (age - 20) * 5
    elif age < 26: age_score = 80 + (age - 23) * 2.5
    elif age <= 29: age_score = 88
    elif age <= 32: age_score = 88 - (age - 29) * 4
    elif age <= 35: age_score = 76 - (age - 32) * 6
    else:           age_score = 55

    if for_attack:
        raw = caps_score * 0.35 + goals_score * 0.35 + age_score * 0.30
    else:
        raw = caps_score * 0.55 + age_score   * 0.25 + goals_score * 0.20

    # CONFIRMED FIX: use full country name e.g. "England", not code "ENG"
    lw = league_weight(p.get("club_country", ""))
    return round(raw * lw, 2)


# ── Fetch and score an entire squad ──────────────────────────────────────────

def fetch_and_score_squad(bsd_team_id: int, nation_name: str) -> dict:
    cache_key = f"nation_squad_v3_{bsd_team_id}"
    cached = cache_read(cache_key)
    if cached and cache_age(cached) < SQUAD_TTL:
        return cached

    data = bsd_get(f"/worldcup/squads/{bsd_team_id}/")

    players_raw = []
    if data:
        if isinstance(data, list):
            players_raw = data
        elif isinstance(data, dict):
            players_raw = (
                data.get("results") or
                data.get("squad")   or
                data.get("players") or
                []
            )

    scored: list[dict] = []
    for p in players_raw:
        status = (p.get("status") or "").lower()
        if status in ("withdrawn", "injured", "out", "unavailable"):
            continue

        pos_raw  = (p.get("position") or "MF").upper()
        # Normalise BSD's exact values: GK/DF/MF/FW
        if pos_raw == "GK":                       pos = "GK"
        elif pos_raw == "DF":                     pos = "DF"
        elif pos_raw == "FW":                     pos = "FW"
        else:                                     pos = "MF"   # MF catch-all

        is_attack = pos in ("FW", "MF")
        s = score_player(p, for_attack=is_attack)

        scored.append({
            "name":          p.get("name", "Unknown"),
            "pos":           pos,
            "club":          p.get("club", ""),
            "club_country":  p.get("club_country", ""),
            "caps":          int(p.get("caps",  0) or 0),
            "goals":         int(p.get("goals", 0) or 0),
            "age":           int(p.get("age",   0) or 0),
            "jersey_number": int(p.get("jersey_number", 0) or 0),
            "score":         s,
        })

    scored.sort(key=lambda x: x["score"], reverse=True)

    # Team ratings: top 4 attackers (FW+MF) and top 4 defenders (DF+GK)
    attackers = [p for p in scored if p["pos"] in ("FW", "MF")][:4]
    defenders = [p for p in scored if p["pos"] in ("DF", "GK")][:4]

    attack  = round(sum(p["score"] for p in attackers) / len(attackers),  1) if attackers  else None
    defence = round(sum(p["score"] for p in defenders) / len(defenders), 1) if defenders else None

    result = {
        "_cached_at":      time.time(),
        "team_id":         bsd_team_id,
        "team_name":       nation_name,
        "attack":          max(50, min(98, attack))  if attack  is not None else None,
        "defence":         max(50, min(98, defence)) if defence is not None else None,
        "squad_count":     len(scored),
        "raw_player_count": len(players_raw),
        "all_players":     scored,
    }
    cache_write(cache_key, result)
    return result


# ── GET /api/nations/squads ───────────────────────────────────────────────────

@router.get("/nations/squads")
def nations_squads(group: str = Query(None)):
    nations = WC_NATIONS
    if group:
        nations = [n for n in nations if n["conf"].upper() == group.upper()]
    return {"count": len(nations), "nations": nations}


# ── GET /api/nations/squads/{id} ─────────────────────────────────────────────

@router.get("/nations/squads/{nation_id}")
def nation_squad(nation_id: int = Path(...)):
    nation = _BY_ID.get(nation_id)
    if not nation:
        raise HTTPException(status_code=404, detail=f"Nation ID {nation_id} not found.")

    bsd_id, bsd_name = resolve_nation_bsd_id(nation)
    if not bsd_id:
        return {"nation_id": nation_id, "nation_name": nation["name"],
                "bsd_found": False, "squad": []}

    data = fetch_and_score_squad(bsd_id, nation["name"])
    return {
        "nation_id":    nation_id,
        "nation_name":  nation["name"],
        "bsd_name":     bsd_name,
        "bsd_found":    True,
        "squad_count":  data["squad_count"],
        "attack":       data["attack"],
        "defence":      data["defence"],
        "top_players":  data["all_players"][:12],
    }


# ── GET /api/nations/debug/{id} — keep until ratings confirmed ────────────────

@router.get("/nations/debug/{nation_id}")
def nations_debug(nation_id: int):
    nation = _BY_ID.get(nation_id)
    if not nation:
        raise HTTPException(status_code=404, detail=f"Nation ID {nation_id} not found.")
    bsd_id, bsd_name = resolve_nation_bsd_id(nation)
    if not bsd_id:
        return {"bsd_resolved": False}
    raw = bsd_get(f"/worldcup/squads/{bsd_id}/")
    players_raw = []
    if raw:
        if isinstance(raw, list): players_raw = raw
        elif isinstance(raw, dict):
            players_raw = raw.get("results") or raw.get("squad") or raw.get("players") or []
    first = players_raw[0] if players_raw else None
    scored = [{"name": p.get("name"), "pos": p.get("position"), "score": score_player(p, p.get("position") in ("FW","MF")),
               "club_country": p.get("club_country"), "league_weight": league_weight(p.get("club_country", ""))}
              for p in players_raw[:5]]
    return {"nation_id": nation_id, "bsd_team_id": bsd_id, "bsd_team_name": bsd_name,
            "first_player_raw": first, "first_5_scored": scored}


# ── POST /api/nations/predict ─────────────────────────────────────────────────

@router.post("/nations/predict")
def nations_predict(body: dict):
    from app.ml_model import score_all_formations

    team_id   = int(body.get("team_id",   0))
    opp_id    = int(body.get("opp_id",    0))
    team_name = str(body.get("team_name", ""))
    opp_name  = str(body.get("opp_name",  ""))

    my_nation  = _BY_ID.get(team_id)  or _BY_NAME.get(team_name)
    opp_nation = _BY_ID.get(opp_id)   or _BY_NAME.get(opp_name)
    if not my_nation:
        raise HTTPException(status_code=404, detail=f"Nation '{team_name}' not in registry.")
    if not opp_nation:
        raise HTTPException(status_code=404, detail=f"Nation '{opp_name}' not in registry.")

    warnings = []

    def get_ratings(nation):
        bsd_id, bsd_name = resolve_nation_bsd_id(nation)
        if not bsd_id:
            warnings.append(f"BSD has no match for '{nation['name']}'. Using 70/70 default.")
            return 70.0, 70.0, 0, None
        data = fetch_and_score_squad(bsd_id, nation["name"])
        if data["attack"] is None:
            warnings.append(f"'{nation['name']}' resolved in BSD but squad is empty. Using 70/70.")
            return 70.0, 70.0, 0, bsd_name
        return data["attack"], data["defence"], data["squad_count"], bsd_name

    my_att,  my_def,  my_count,  my_bsd  = get_ratings(my_nation)
    opp_att, opp_def, opp_count, opp_bsd = get_ratings(opp_nation)

    all_formations = score_all_formations(my_att, my_def, opp_att, opp_def)
    best = all_formations[0]

    resp = {
        "team":           my_nation["name"],
        "opponent":       opp_nation["name"],
        "my_attack":      my_att,
        "my_defence":     my_def,
        "opp_attack":     opp_att,
        "opp_defence":    opp_def,
        "best_formation": best["formation"],
        "probability":    best["probability"],
        "all_formations": all_formations,
        "my_squad_count":  my_count,
        "opp_squad_count": opp_count,
        "players_scored":  my_count,
        "bsd_resolved":    {"team": my_bsd, "opp": opp_bsd},
    }
    if warnings:
        resp["warnings"] = warnings
    return resp


# ── POST /api/nations/lineup ─────────────────────────────────────────────────

@router.post("/nations/lineup")
def nations_lineup(body: dict):
    """
    Builds a probable Starting XI from the WC squad.

    POSITION FLEXIBILITY:
    BSD gives only GK/DF/MF/FW. In a 4-3-3, the front 3 includes wingers
    who BSD tags as MF (e.g. Musiala, Wirtz, Sané). If we only fill FW
    slots from the FW pool, those players end up in midfield and the front
    3 is populated by pure strikers only — wrong.

    Strategy:
      1. Fill GK slots from GK pool.
      2. Fill DF slots from DF pool.
      3. Fill FW slots from the combined FW+MF pool (highest-scored first).
         This lets Wirtz/Musiala float into the front 3 in a 4-3-3.
      4. Fill MF slots from the MF players NOT already used in FW slots.
      5. Fill any remaining gaps with highest-scored unused players (fallback).
    """
    nation_id   = int(body.get("nation_id", 0))
    nation_name = str(body.get("nation_name", ""))
    formation   = str(body.get("formation", "4-3-3"))

    nation = _BY_ID.get(nation_id) or _BY_NAME.get(nation_name)
    if not nation:
        raise HTTPException(status_code=404, detail=f"Nation '{nation_name}' not in registry.")

    bsd_id, bsd_name = resolve_nation_bsd_id(nation)
    if not bsd_id:
        raise HTTPException(status_code=404, detail=f"BSD has no match for '{nation['name']}'.")

    data    = fetch_and_score_squad(bsd_id, nation["name"])
    players = data["all_players"]
    if not players:
        raise HTTPException(status_code=404, detail=f"No squad players found for '{nation['name']}'.")

    slots   = parse_formation_slots(formation)
    used:   set[str] = set()
    xi:     list[dict] = []

    def take(pool: list, count: int, slot_label: str, fallback: bool = False):
        taken = 0
        for p in pool:
            if taken >= count:
                break
            if p["name"] in used:
                continue
            xi.append({**p, "slot": slot_label, "fallback": fallback})
            used.add(p["name"])
            taken += 1
        return count - taken   # shortfall

    by_pos = {pos: [p for p in players if p["pos"] == pos] for pos in ("GK","DF","MF","FW")}

    # Step 1 — GK
    take(by_pos["GK"], slots["GK"], "GK")

    # Step 2 — DF
    take(by_pos["DF"], slots["DF"], "DF")

    # Step 3 — FW slots from COMBINED FW+MF pool (highest score first)
    # This is the winger fix: Musiala/Wirtz can end up here in a 4-3-3
    fw_mf_combined = sorted(by_pos["FW"] + by_pos["MF"], key=lambda p: p["score"], reverse=True)
    fw_shortfall = take(fw_mf_combined, slots["FW"], "FW")

    # Step 4 — MF slots from MF NOT already placed in FW slots
    remaining_mf = [p for p in by_pos["MF"] if p["name"] not in used]
    mf_shortfall = take(remaining_mf, slots["MF"], "MF")

    # Step 5 — fill any shortfall with best remaining players (fallback)
    total_shortfall = fw_shortfall + mf_shortfall
    if total_shortfall > 0:
        all_remaining = sorted(
            [p for p in players if p["name"] not in used],
            key=lambda p: p["score"], reverse=True
        )
        take(all_remaining, total_shortfall, "SUB", fallback=True)

    formatted = [
        {
            "name":     p["name"],
            "pos":      p["slot"],
            "club":     p.get("club", ""),
            "caps":     p.get("caps", 0),
            "goals":    p.get("goals", 0),
            "age":      p.get("age", 0),
            "score":    p.get("score", 0),
            "fallback": p.get("fallback", False),
        }
        for p in xi[:11]
    ]

    return {
        "nation":      nation["name"],
        "formation":   formation,
        "xi":          formatted,
        "count":       len(formatted),
        "squad_size":  data["squad_count"],
        "bsd_resolved": bsd_name or None,
    }
