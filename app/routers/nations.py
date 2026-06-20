"""
app/routers/nations.py — World Cup 2026 nations module (v2 — CORRECTED)
 
ROOT CAUSE OF THE 70/70 BUG (fixed in this version):
The previous version called generic club endpoints (/players/?team_id=,
/teams/{id}/squad/) to fetch national squads. BSD does NOT serve World Cup
squads from those endpoints — it has a DEDICATED endpoint:
 
    GET /api/v2/worldcup/squads/{bsd_team_id}/
 
This returns players with this shape (confirmed from prior integration work):
    { id, team_id, name, jersey_number, position, status, call_up_date,
      club, club_country, caps, goals, date_of_birth, age, player_id }
 
Note: this endpoint does NOT return per-90 stats or BSD match ratings —
only caps, goals, age, club, club_country. The rating formula below is
redesigned to use ONLY fields this endpoint actually provides (no more
silently falling back to fabricated "quality" numbers).
 
Endpoints:
  GET  /api/nations/squads           — registry of all 48 nations
  GET  /api/nations/squads/{id}      — single nation's live squad + ratings
  POST /api/nations/predict          — formation prediction for two nations
  POST /api/nations/lineup           — probable Starting XI for one nation  [NEW]
"""
 
import time
from datetime import date
from fastapi import APIRouter, Query, Path, HTTPException
from app.config import bsd_get, bsd_find_team, cache_read, cache_write, cache_age, LEAGUE_WEIGHTS
 
router = APIRouter()
 
# ── 48-nation registry — MUST match frontend WC_2026_NATIONS exactly ─────────
# FIFA-confirmed final field (locked March 2026).
WC_NATIONS: list[dict] = [
    {"id": 1,  "name": "Canada",        "bsd_names": ["Canada"],                                   "conf": "CONCACAF"},
    {"id": 2,  "name": "Mexico",        "bsd_names": ["Mexico", "México"],                          "conf": "CONCACAF"},
    {"id": 3,  "name": "USA",           "bsd_names": ["USA", "United States"],                       "conf": "CONCACAF"},
    {"id": 4,  "name": "Curaçao",       "bsd_names": ["Curacao", "Curaçao"],                         "conf": "CONCACAF"},
    {"id": 5,  "name": "Haiti",         "bsd_names": ["Haiti"],                                      "conf": "CONCACAF"},
    {"id": 6,  "name": "Panama",        "bsd_names": ["Panama", "Panamá"],                           "conf": "CONCACAF"},
    {"id": 7,  "name": "Argentina",     "bsd_names": ["Argentina"],                                  "conf": "CONMEBOL"},
    {"id": 8,  "name": "Brazil",        "bsd_names": ["Brazil", "Brasil"],                            "conf": "CONMEBOL"},
    {"id": 9,  "name": "Colombia",      "bsd_names": ["Colombia"],                                   "conf": "CONMEBOL"},
    {"id": 10, "name": "Ecuador",       "bsd_names": ["Ecuador"],                                    "conf": "CONMEBOL"},
    {"id": 11, "name": "Paraguay",      "bsd_names": ["Paraguay"],                                   "conf": "CONMEBOL"},
    {"id": 12, "name": "Uruguay",       "bsd_names": ["Uruguay"],                                    "conf": "CONMEBOL"},
    {"id": 13, "name": "Austria",       "bsd_names": ["Austria"],                                    "conf": "UEFA"},
    {"id": 14, "name": "Belgium",       "bsd_names": ["Belgium", "Belgique"],                        "conf": "UEFA"},
    {"id": 15, "name": "Bosnia and Herzegovina", "bsd_names": ["Bosnia and Herzegovina", "Bosnia"],  "conf": "UEFA"},
    {"id": 16, "name": "Croatia",       "bsd_names": ["Croatia", "Hrvatska"],                        "conf": "UEFA"},
    {"id": 17, "name": "Czechia",       "bsd_names": ["Czechia", "Czech Republic"],                  "conf": "UEFA"},
    {"id": 18, "name": "England",       "bsd_names": ["England"],                                    "conf": "UEFA"},
    {"id": 19, "name": "France",        "bsd_names": ["France"],                                     "conf": "UEFA"},
    {"id": 20, "name": "Germany",       "bsd_names": ["Germany", "Deutschland"],                     "conf": "UEFA"},
    {"id": 21, "name": "Netherlands",   "bsd_names": ["Netherlands", "Holland"],                     "conf": "UEFA"},
    {"id": 22, "name": "Norway",        "bsd_names": ["Norway", "Norge"],                            "conf": "UEFA"},
    {"id": 23, "name": "Portugal",      "bsd_names": ["Portugal"],                                   "conf": "UEFA"},
    {"id": 24, "name": "Scotland",      "bsd_names": ["Scotland"],                                   "conf": "UEFA"},
    {"id": 25, "name": "Spain",         "bsd_names": ["Spain", "España"],                            "conf": "UEFA"},
    {"id": 26, "name": "Sweden",        "bsd_names": ["Sweden", "Sverige"],                          "conf": "UEFA"},
    {"id": 27, "name": "Switzerland",   "bsd_names": ["Switzerland", "Schweiz"],                     "conf": "UEFA"},
    {"id": 28, "name": "Türkiye",       "bsd_names": ["Turkey", "Türkiye"],                          "conf": "UEFA"},
    {"id": 29, "name": "Algeria",       "bsd_names": ["Algeria"],                                    "conf": "CAF"},
    {"id": 30, "name": "Cabo Verde",    "bsd_names": ["Cabo Verde", "Cape Verde"],                   "conf": "CAF"},
    {"id": 31, "name": "DR Congo",      "bsd_names": ["DR Congo", "Congo DR", "DRC"],                "conf": "CAF"},
    {"id": 32, "name": "Côte d'Ivoire", "bsd_names": ["Cote d'Ivoire", "Côte d'Ivoire", "Ivory Coast"],"conf": "CAF"},
    {"id": 33, "name": "Egypt",         "bsd_names": ["Egypt"],                                      "conf": "CAF"},
    {"id": 34, "name": "Ghana",         "bsd_names": ["Ghana"],                                      "conf": "CAF"},
    {"id": 35, "name": "Morocco",       "bsd_names": ["Morocco", "Maroc"],                           "conf": "CAF"},
    {"id": 36, "name": "Senegal",       "bsd_names": ["Senegal"],                                    "conf": "CAF"},
    {"id": 37, "name": "South Africa",  "bsd_names": ["South Africa", "Bafana Bafana"],              "conf": "CAF"},
    {"id": 38, "name": "Tunisia",       "bsd_names": ["Tunisia", "Tunisie"],                         "conf": "CAF"},
    {"id": 39, "name": "Australia",     "bsd_names": ["Australia", "Socceroos"],                     "conf": "AFC"},
    {"id": 40, "name": "Iraq",          "bsd_names": ["Iraq"],                                       "conf": "AFC"},
    {"id": 41, "name": "Iran",          "bsd_names": ["Iran", "IR Iran"],                             "conf": "AFC"},
    {"id": 42, "name": "Japan",         "bsd_names": ["Japan", "Japon"],                              "conf": "AFC"},
    {"id": 43, "name": "Jordan",        "bsd_names": ["Jordan"],                                     "conf": "AFC"},
    {"id": 44, "name": "South Korea",   "bsd_names": ["South Korea", "Korea Republic", "Korea South"],"conf": "AFC"},
    {"id": 45, "name": "Qatar",         "bsd_names": ["Qatar"],                                      "conf": "AFC"},
    {"id": 46, "name": "Saudi Arabia",  "bsd_names": ["Saudi Arabia"],                                "conf": "AFC"},
    {"id": 47, "name": "Uzbekistan",    "bsd_names": ["Uzbekistan"],                                  "conf": "AFC"},
    {"id": 48, "name": "New Zealand",   "bsd_names": ["New Zealand", "All Whites"],                  "conf": "OFC"},
]
 
_BY_ID   = {n["id"]:   n for n in WC_NATIONS}
_BY_NAME = {n["name"]: n for n in WC_NATIONS}
 
SQUAD_TTL = 21_600  # 6h — squads change rarely, but allow same-day correction
 
# Standard formations → slot counts. First number = DF, last = FW, middle = MF.
def parse_formation_slots(formation: str) -> dict:
    nums_str = formation.split()[0]  # strip suffix words like "Diamond"
    parts = [int(x) for x in nums_str.split("-") if x.isdigit()]
    if len(parts) < 2:
        return {"GK": 1, "DF": 4, "MF": 3, "FW": 3}  # safe default
    df = parts[0]
    fw = parts[-1]
    mf = sum(parts[1:-1]) if len(parts) > 2 else 0
    return {"GK": 1, "DF": df, "MF": mf, "FW": fw}
 
 
# ── Resolve nation → real BSD team_id ─────────────────────────────────────────
 
def resolve_nation_bsd_id(nation: dict) -> tuple[int | None, str]:
    cache_key = f"nation_bsd_id_v2__{nation['id']}"
    cached = cache_read(cache_key)
    if cached and cache_age(cached) < SQUAD_TTL:
        return cached.get("bsd_id"), cached.get("bsd_name", "")
 
    for name_try in nation["bsd_names"]:
        bsd_id, bsd_name = bsd_find_team(name_try)
        if bsd_id:
            cache_write(cache_key, {"bsd_id": bsd_id, "bsd_name": bsd_name})
            return bsd_id, bsd_name
 
    cache_write(cache_key, {"bsd_id": None, "bsd_name": ""})
    return None, ""
 
 
# ── Fetch + score a national squad using the CORRECT BSD endpoint ────────────
 
def fetch_and_score_squad(bsd_team_id: int, nation_name: str) -> dict:
    """
    Calls GET /worldcup/squads/{bsd_team_id}/ — the actual BSD endpoint for
    World Cup national squads. Scores each player using ONLY fields this
    endpoint provides: caps, goals, age, club_country, position, status.
 
    Rating formula (honest given available data):
      Attacking positions (F/M):
        score = caps_score×0.35 + goals_score×0.35 + age_score×0.30
      Defensive positions (D/G):
        score = caps_score×0.55 + age_score×0.25 + goals_score×0.20
      Final score × league_weight (based on club_country)
    """
    cache_key = f"nation_squad_v2_{bsd_team_id}"
    cached = cache_read(cache_key)
    if cached and cache_age(cached) < SQUAD_TTL:
        return cached
 
    data = bsd_get(f"/worldcup/squads/{bsd_team_id}/")
 
    players_raw = []
    if data:
        if isinstance(data, list):
            players_raw = data
        elif isinstance(data, dict):
            players_raw = data.get("results") or data.get("squad") or data.get("players") or []
 
    scored: list[dict] = []
    for p in players_raw:
        # Skip withdrawn/injured players if status field indicates exclusion
        status = (p.get("status") or "").lower()
        if status in ("withdrawn", "injured", "out", "unavailable"):
            continue
 
        name = p.get("name") or "Unknown"
        pos_raw = (p.get("position") or "M").upper()
        # Normalise to single-letter G/D/M/F regardless of how BSD spells it
        if pos_raw.startswith("G"):   pos = "G"
        elif pos_raw.startswith("D"): pos = "D"
        elif pos_raw.startswith("F") or pos_raw in ("ST", "CF", "FW"): pos = "F"
        else: pos = "M"
 
        caps  = int(p.get("caps", 0) or 0)
        goals = int(p.get("goals", 0) or 0)
 
        age = p.get("age")
        if not age:
            dob = p.get("date_of_birth", "")
            try:
                birth = date.fromisoformat(str(dob)[:10])
                age = (date.today() - birth).days // 365
            except Exception:
                age = 27
        age = int(age or 27)
 
        club_country = (p.get("club_country") or "").strip().upper()
        league_weight = LEAGUE_WEIGHTS.get(club_country, LEAGUE_WEIGHTS.get("__default__", 0.74))
 
        # ── Sub-scores (0-100 scale) ──────────────────────────────────────
        caps_score  = min(caps, 100)
        # Goals: attackers/midfielders scored more generously than defenders
        goals_cap   = 30 if pos in ("F", "M") else 10
        goals_score = min((goals / max(goals_cap, 1)) * 100, 100)
        # Age: peak at 26-29
        if   age < 20: age_score = 55
        elif age < 23: age_score = 65 + (age - 20) * 5
        elif age < 26: age_score = 80 + (age - 23) * 2.5
        elif age <= 29: age_score = 88
        elif age <= 32: age_score = 88 - (age - 29) * 4
        elif age <= 35: age_score = 76 - (age - 32) * 6
        else:           age_score = 58
 
        if pos in ("F", "M"):
            raw_score = caps_score * 0.35 + goals_score * 0.35 + age_score * 0.30
        else:  # D, G
            raw_score = caps_score * 0.55 + age_score * 0.25 + goals_score * 0.20
 
        final_score = round(raw_score * league_weight, 2)
 
        scored.append({
            "name": name, "pos": pos, "club": p.get("club", ""),
            "club_country": club_country, "caps": caps, "goals": goals,
            "age": age, "score": final_score,
        })
 
    scored.sort(key=lambda x: x["score"], reverse=True)
 
    attackers = [p for p in scored if p["pos"] in ("F", "M")][:4]
    defenders = [p for p in scored if p["pos"] in ("D", "G")][:4]
 
    attack  = round(sum(p["score"] for p in attackers)  / len(attackers),  1) if attackers  else None
    defence = round(sum(p["score"] for p in defenders) / len(defenders), 1) if defenders else None
 
    result = {
        "_cached_at":  time.time(),
        "team_id":     bsd_team_id,
        "team_name":   nation_name,
        "attack":      max(50, min(98, attack))  if attack  is not None else None,
        "defence":     max(50, min(98, defence)) if defence is not None else None,
        "squad_count": len(scored),
        "raw_player_count": len(players_raw),
        "all_players": scored,
    }
    cache_write(cache_key, result)
    return result
 
 
# ── GET /api/nations/squads ───────────────────────────────────────────────────
 
@router.get("/nations/squads")
def nations_squads(group: str = Query(None, description="Filter by confederation e.g. UEFA")):
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
        return {
            "nation_id": nation_id, "nation_name": nation["name"], "conf": nation["conf"],
            "bsd_found": False,
            "message": f"BSD could not resolve a team for '{nation['name']}' using any name variant.",
            "squad": [],
        }
 
    squad_data = fetch_and_score_squad(bsd_id, nation["name"])
    if squad_data["squad_count"] == 0:
        return {
            "nation_id": nation_id, "nation_name": nation["name"], "bsd_name": bsd_name,
            "conf": nation["conf"], "bsd_found": True, "squad_found": False,
            "raw_player_count": squad_data["raw_player_count"],
            "message": f"BSD resolved team ID {bsd_id} but /worldcup/squads/{bsd_id}/ returned no players. "
                       f"BSD may not have published this nation's WC2026 squad yet.",
            "squad": [],
        }
 
    return {
        "nation_id": nation_id, "nation_name": nation["name"], "bsd_name": bsd_name,
        "conf": nation["conf"], "bsd_found": True, "squad_found": True,
        "squad_count": squad_data["squad_count"],
        "attack": squad_data["attack"], "defence": squad_data["defence"],
        "top_players": squad_data["all_players"][:10],
    }
 
 
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
        raise HTTPException(status_code=404, detail=f"Nation '{team_name}' (id={team_id}) not in registry.")
    if not opp_nation:
        raise HTTPException(status_code=404, detail=f"Nation '{opp_name}' (id={opp_id}) not in registry.")
 
    warnings = []
 
    my_bsd_id,  my_bsd_name  = resolve_nation_bsd_id(my_nation)
    opp_bsd_id, opp_bsd_name = resolve_nation_bsd_id(opp_nation)
 
    if my_bsd_id:
        my_data = fetch_and_score_squad(my_bsd_id, my_nation["name"])
        if my_data["attack"] is None:
            warnings.append(f"BSD team resolved for '{my_nation['name']}' but squad endpoint returned no usable players. Using 70/70 default.")
            my_attack, my_defence, my_count = 70.0, 70.0, 0
        else:
            my_attack, my_defence, my_count = my_data["attack"], my_data["defence"], my_data["squad_count"]
    else:
        warnings.append(f"BSD has no team match for '{my_nation['name']}'. Using 70/70 default.")
        my_attack, my_defence, my_count = 70.0, 70.0, 0
 
    if opp_bsd_id:
        opp_data = fetch_and_score_squad(opp_bsd_id, opp_nation["name"])
        if opp_data["attack"] is None:
            warnings.append(f"BSD team resolved for '{opp_nation['name']}' but squad endpoint returned no usable players. Using 70/70 default.")
            opp_attack, opp_defence, opp_count = 70.0, 70.0, 0
        else:
            opp_attack, opp_defence, opp_count = opp_data["attack"], opp_data["defence"], opp_data["squad_count"]
    else:
        warnings.append(f"BSD has no team match for '{opp_nation['name']}'. Using 70/70 default.")
        opp_attack, opp_defence, opp_count = 70.0, 70.0, 0
 
    all_formations = score_all_formations(my_attack, my_defence, opp_attack, opp_defence)
    best = all_formations[0]
 
    response = {
        "team": my_nation["name"], "opponent": opp_nation["name"],
        "my_attack": my_attack, "my_defence": my_defence,
        "opp_attack": opp_attack, "opp_defence": opp_defence,
        "best_formation": best["formation"], "probability": best["probability"],
        "all_formations": all_formations,
        "my_squad_count": my_count, "opp_squad_count": opp_count,
        "players_scored": my_count,
        "bsd_resolved": {"team": my_bsd_name or None, "opp": opp_bsd_name or None},
    }
    if warnings:
        response["warnings"] = warnings
    return response
 
 
# ── POST /api/nations/lineup ─────────────────────────────────────────────────
# NEW: probable Starting XI for a national team, mirroring club /api/lineup
 
@router.post("/nations/lineup")
def nations_lineup(body: dict):
    nation_id   = int(body.get("nation_id", 0))
    nation_name = str(body.get("nation_name", ""))
    formation   = str(body.get("formation", "4-3-3"))
 
    nation = _BY_ID.get(nation_id) or _BY_NAME.get(nation_name)
    if not nation:
        raise HTTPException(status_code=404, detail=f"Nation '{nation_name}' not in registry.")
 
    bsd_id, bsd_name = resolve_nation_bsd_id(nation)
    if not bsd_id:
        raise HTTPException(status_code=404, detail=f"BSD has no team match for '{nation['name']}'.")
 
    squad_data = fetch_and_score_squad(bsd_id, nation["name"])
    players = squad_data["all_players"]
    if not players:
        raise HTTPException(
            status_code=404,
            detail=f"BSD resolved '{nation['name']}' but returned no squad players for the lineup."
        )
 
    slots = parse_formation_slots(formation)
    by_pos: dict[str, list[dict]] = {"G": [], "D": [], "M": [], "F": []}
    for p in players:
        by_pos[p["pos"]].append(p)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda x: x["score"], reverse=True)
 
    xi: list[dict] = []
    used_names: set[str] = set()
 
    def take(pos_key: str, count: int, label: str):
        pool = [p for p in by_pos.get(pos_key, []) if p["name"] not in used_names]
        for p in pool[:count]:
            xi.append({**p, "slot": label, "fallback": False})
            used_names.add(p["name"])
        return max(0, count - len(pool[:count]))
 
    remaining_gk = take("G", slots["GK"], "GK")
    remaining_df = take("D", slots["DF"], "DF")
    remaining_mf = take("M", slots["MF"], "MF")
    remaining_fw = take("F", slots["FW"], "FW")
 
    # Fill any shortfall from the best remaining outfield players (fallback flag)
    shortfall = remaining_gk + remaining_df + remaining_mf + remaining_fw
    if shortfall > 0:
        all_remaining = sorted(
            [p for pos_list in by_pos.values() for p in pos_list if p["name"] not in used_names],
            key=lambda x: x["score"], reverse=True,
        )
        for p in all_remaining[:shortfall]:
            xi.append({**p, "slot": p["pos"], "fallback": True})
            used_names.add(p["name"])
 
    formatted_xi = [
        {
            "name": p["name"], "pos": p["slot"], "club": p.get("club", ""),
            "caps": p.get("caps", 0), "goals": p.get("goals", 0),
            "age": p.get("age", 0), "score": p.get("score", 0),
            "fallback": p.get("fallback", False),
        }
        for p in xi
    ]
 
    return {
        "nation": nation["name"], "formation": formation,
        "xi": formatted_xi, "count": len(formatted_xi),
        "squad_size": squad_data["squad_count"], "bsd_resolved": bsd_name or None,
    }
