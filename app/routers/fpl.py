"""
FPL Scout — Full rebuild using FPL official API as primary data source.

Data sources:
  FPL API  → real players, real £ prices, real ownership %, real points
             https://fantasy.premierleague.com/api/bootstrap-static/
  BSD API  → fixture difficulty ratings only (opponent defence)

Why FPL API over BSD for players:
  - BSD market_value_eur is transfer value, not FPL price
  - BSD /players/?team_id=X returns wrong Liverpool (Uruguay), wrong squads
  - BSD ownership data doesn't exist
  - FPL API is free, public, 841 real registered players, updated weekly

Architecture:
  1. Fetch FPL bootstrap data (cached 1h)
  2. Build player list with real prices, ownership, points
  3. Per endpoint: filter by position/price/ownership
  4. Get fixture difficulty from BSD (what we already do well)
  5. Combine and score

FPL team_id → BSD search name mapping:
  FPL uses numeric team IDs. We map to BSD team names for fixture lookup.
"""
import time
import requests
from datetime import datetime, timezone
from fastapi import APIRouter, Query, HTTPException
from app.config import bsd_get, bsd_find_team, cache_read, cache_write, cache_age
from app.routers.form import _dynamic_ratings

router   = APIRouter()
FPL_URL  = "https://fantasy.premierleague.com/api/bootstrap-static/"
FPL_TTL  = 3600   # 1 hour — FPL data updates daily at most
FDR_TTL  = 3600
CAP_TTL  = 1800
TRANS_TTL= 3600
DIFF_TTL = 1800

# ── FPL team_id → BSD search name ────────────────────────────────────────────
# FPL team IDs are fixed per season. These match the 2025/26 bootstrap response.
# Update if team IDs change for 2026/27 (they usually shift when teams are
# promoted/relegated). Map to the name bsd_find_team() can resolve.

FPL_TEAM_TO_BSD: dict[int, str] = {
    1:  "Arsenal",
    2:  "Aston Villa",
    3:  "Burnley",
    4:  "Bournemouth",
    5:  "Brentford",
    6:  "Brighton",
    7:  "Chelsea",
    8:  "Crystal Palace",
    9:  "Everton",
    10: "Fulham",
    11: "Leeds United",
    12: "Liverpool",
    13: "Manchester City",
    14: "Manchester United",
    15: "Newcastle United",
    16: "Nottingham Forest",
    17: "Sunderland",
    18: "Tottenham Hotspur",
    19: "West Ham United",
    20: "Wolverhampton",
}

# FPL position codes
POS_MAP = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
POS_LABEL = {1: "Goalkeeper", 2: "Defender", 3: "Midfielder", 4: "Forward"}

# ── FPL data fetch ────────────────────────────────────────────────────────────

def _get_fpl_data() -> dict:
    """
    Fetch and cache FPL bootstrap data.
    Returns dict with 'players', 'teams', 'team_map' (id→name).
    """
    cache_key = "fpl_bootstrap_v1"
    cached    = cache_read(cache_key)
    if cached and cache_age(cached) < FPL_TTL:
        return cached

    try:
        resp = requests.get(FPL_URL, timeout=10,
                           headers={"User-Agent": "Tactica/1.0"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        raise HTTPException(502, f"FPL API unavailable: {e}")

    teams      = {t["id"]: t["name"] for t in data.get("teams", [])}
    players    = data.get("elements", [])

    result = {
        "players":  players,
        "teams":    teams,
        "_cached_at": time.time(),
    }
    cache_write(cache_key, result)
    return result

# ── BSD fixture helpers ───────────────────────────────────────────────────────

def _fdr(defence: int, is_away: bool) -> int:
    base = defence + (5 if is_away else 0)
    return max(1, min(5, 1 + int(base // 21)))

def _fdr_label(fdr: int) -> str:
    return "Easy" if fdr <= 2 else ("Medium" if fdr == 3 else "Hard")

def _fdr_colour(fdr: int) -> str:
    return "green" if fdr <= 2 else ("amber" if fdr == 3 else "red")

FDR_MULTIPLIER = {1: 1.30, 2: 1.15, 3: 1.00, 4: 0.85, 5: 0.70}
EASE_BONUS     = {1: 1.40, 2: 1.20, 3: 1.00}

def _get_opponent_defence(opp_id: int) -> int:
    """Calculates opponent defensive rating using strictly historical PL matches."""
    try:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT23:59:59Z")
        # Query with league_id to filter out cups at the API resource level
        data = bsd_get(f"/teams/{opp_id}/fixtures/", params={
            "status": "finished", "limit": 40,
            "date_from": "2025-08-01T00:00:00Z", "date_to": now,
            "league_id": 39 
        })
        if not data:
            return 75
        raw = data if isinstance(data, list) else data.get("results", [])
        matches = []
        for fix in raw:
            # Code validation fallback to drop unauthorized competition formats
            l_id = fix.get("league_id") or fix.get("competition_id")
            league_str = str(fix.get("league", "")) + str(fix.get("competition", ""))
            
            if l_id != 39 and "Premier League" not in league_str and l_id is not None:
                continue
                
            is_home  = fix.get("home_team_id") == opp_id
            scored   = fix.get("home_score" if is_home else "away_score") or 0
            conceded = fix.get("away_score" if is_home else "home_score") or 0
            matches.append({"scored": scored, "conceded": conceded,
                            "result": "W" if scored > conceded else
                            ("D" if scored == conceded else "L"), "formation": ""})
        if matches:
            _, defence = _dynamic_ratings(matches)
            return defence
    except Exception:
        pass
    return 75

def _next_fixture(bsd_team_id: int) -> dict:
    """Get next upcoming Premier League fixture using date_from=today."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fixes = []
    for params in [
        {"status": "notstarted", "limit": 15, "date_from": today, "league_id": 39},
        {"limit": 15, "date_from": today, "league_id": 39},
    ]:
        d = bsd_get(f"/teams/{bsd_team_id}/fixtures/", params=params)
        if d:
            f = d if isinstance(d, list) else d.get("results", [])
            if f:
                # Isolate target league records explicitly
                fixes = [
                    fix for fix in f 
                    if fix.get("league_id") == 39 
                    or str(fix.get("competition_id")) == "39"
                    or "Premier League" in str(fix.get("league", ""))
                    or "Premier League" in str(fix.get("competition", ""))
                    or fix.get("round_number")
                ]
                if fixes:
                    break
    if not fixes:
        return {}
    fixes.sort(key=lambda f: f.get("event_date") or "")
    nf      = fixes[0]
    is_home = nf.get("home_team_id") == bsd_team_id
    opp_id  = nf.get("away_team_id" if is_home else "home_team_id") or 0
    opp_name= nf.get("away_team" if is_home else "home_team", "Unknown")
    opp_def = _get_opponent_defence(opp_id)
    fdr     = _fdr(opp_def, is_away=not is_home)
    try:
        dt      = datetime.fromisoformat(
            (nf.get("event_date") or "").replace("Z", "+00:00"))
        date_str= dt.strftime("%-d %b")
    except Exception:
        date_str= (nf.get("event_date") or "")[:10]
    return {
        "opponent": opp_name, "venue": "H" if is_home else "A",
        "date": date_str, "fdr": fdr,
        "fdr_label": _fdr_label(fdr), "fdr_colour": _fdr_colour(fdr),
        "multiplier": FDR_MULTIPLIER.get(fdr, 1.0),
    }

# ── FPL scoring helpers ───────────────────────────────────────────────────────

def _fpl_score(p: dict) -> float:
    """
    Score a player using FPL API confirmed fields.
    Primary signal: points_per_game (most stable preseason metric)
    Secondary: xG_per_90, xA_per_90, form (when season is live)
    """
    ppg   = float(p.get("points_per_game") or 0)
    xg90  = float(p.get("expected_goals_per_90") or 0)
    xa90  = float(p.get("expected_assists_per_90") or 0)
    form  = float(p.get("form") or 0)
    ep    = float(p.get("ep_next") or 0)
    mins  = int(p.get("minutes") or 0)

    # Preseason: ppg is the most reliable signal (full season data)
    # During season: form and ep_next become more relevant
    if mins > 900:   # played enough to trust ppg
        score = ppg * 2.0 + xg90 * 3.0 + xa90 * 2.0 + form * 0.5 + ep * 0.3
    else:            # limited data — rely more on ep_next
        score = ep * 1.0 + ppg * 1.0
    return round(score, 3)

def _build_player(p: dict, teams: dict) -> dict:
    """Build a clean player dict from FPL API element."""
    return {
        "id":          p.get("id"),
        "name":        p.get("known_name") or p.get("web_name") or
                       f"{p.get('first_name','')} {p.get('second_name','')}".strip(),
        "team_id":     p.get("team"),
        "team":        teams.get(p.get("team",""), "Unknown"),
        "position":    POS_MAP.get(p.get("element_type"), "UNK"),
        "pos_id":      p.get("element_type"),
        "price":       round((p.get("now_cost") or 0) / 10, 1),
        "ownership":   float(p.get("selected_by_percent") or 0),
        "form":        float(p.get("form") or 0),
        "ppg":         float(p.get("points_per_game") or 0),
        "total_pts":   int(p.get("total_points") or 0),
        "ep_next":     float(p.get("ep_next") or 0),
        "minutes":     int(p.get("minutes") or 0),
        "goals":       int(p.get("goals_scored") or 0),
        "assists":     int(p.get("assists") or 0),
        "xg90":        float(p.get("expected_goals_per_90") or 0),
        "xa90":        float(p.get("expected_assists_per_90") or 0),
        "status":      p.get("status","a"),
        "news":        p.get("news",""),
        "fpl_score":   _fpl_score(p),
    }

def _reason_fpl(player: dict, fdr: int, fdr_label: str, opp: str, venue: str) -> str:
    parts = []
    if player["ppg"] >= 5.0:
        parts.append(f"{player['ppg']} pts/game last season")
    if player["xg90"] >= 0.3:
        parts.append(f"{player['xg90']:.2f} xG/90")
    if player["xa90"] >= 0.2:
        parts.append(f"{player['xa90']:.2f} xA/90")
    if player["form"] > 0:
        parts.append(f"form {player['form']}")
    form_str = ", ".join(parts) if parts else "consistent performer"
    fix_str  = f"{fdr_label.lower()} fixture ({venue} vs {opp}, FDR {fdr})"
    return f"{form_str.capitalize()} · {fix_str}."


# ── Step 1: Fixture Ticker (Strict Premier League Isolation) ──────────────────

@router.get("/fpl/fixtures")
def fixture_ticker(
    team: str = Query(..., description="Club name e.g. Arsenal, Liverpool"),
    gws:  int = Query(38, description="Gameweeks to show", ge=1, le=50),
):
    cache_key = f"fpl_fixtures_v5__{team.lower().replace(' ','_')}"
    cached    = cache_read(cache_key)
    if cached and cache_age(cached) < FDR_TTL:
        cached["cached"] = True
        return cached

    team_id, bsd_name = bsd_find_team(team)
    if not team_id:
        raise HTTPException(404, f"Team '{team}' not found.")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw   = []
    
    # Restrict inbound structural parameters down to League ID 39
    for params in [
        {"status": "notstarted", "limit": min(gws+15,200), "date_from": today, "league_id": 39},
        {"limit": min(gws+15,200), "date_from": today, "league_id": 39},
        {"team_id": team_id, "date_from": today, "status":"notstarted","limit":min(gws+15,200), "league_id": 39},
    ]:
        path = f"/teams/{team_id}/fixtures/" if "team_id" not in params else "/events/"
        d    = bsd_get(path, params=params)
        if d:
            r = d if isinstance(d, list) else d.get("results", [])
            if r:
                raw = r
                break

    if not raw:
        raise HTTPException(404,
            f"No upcoming fixtures for '{team}'. "
            "BSD may not have fixtures indexed yet — check back soon.")

    # Python-side extraction filtering to drop domestic/continental cups
    raw = [
        fix for fix in raw 
        if fix.get("league_id") == 39 
        or str(fix.get("competition_id")) == "39"
        or "Premier League" in str(fix.get("league", ""))
        or "Premier League" in str(fix.get("competition", ""))
        or (isinstance(fix.get("round_number"), int) and fix.get("round_number") <= 38)
    ]

    raw.sort(key=lambda f: f.get("event_date") or "")
    upcoming = raw[:gws]
    fixtures_out = []
    for fix in upcoming:
        is_home    = fix.get("home_team_id") == team_id
        opp_id     = fix.get("away_team_id" if is_home else "home_team_id") or 0
        opp_name   = fix.get("away_team" if is_home else "home_team", "Unknown")
        try:
            dt = datetime.fromisoformat((fix.get("event_date","")).replace("Z","+00:00"))
            date_display = dt.strftime("%-d %b")
        except Exception:
            date_display = (fix.get("event_date",""))[:10]
        opp_def = _get_opponent_defence(opp_id)
        fdr     = _fdr(opp_def, is_away=not is_home)
        fixtures_out.append({
            "gameweek":   fix.get("round_number"),
            "date":       date_display, "date_iso": fix.get("event_date",""),
            "opponent":   opp_name, "venue": "H" if is_home else "A",
            "opp_defence": opp_def, "fdr": fdr,
            "fdr_label":  _fdr_label(fdr), "fdr_colour": _fdr_colour(fdr),
        })

    easy = sum(1 for f in fixtures_out if f["fdr_colour"]=="green")
    hard = sum(1 for f in fixtures_out if f["fdr_colour"]=="red")
    n3   = fixtures_out[:3]
    n3s  = " · ".join(f"{f['opponent']} ({f['fdr_label'][0]})" for f in n3)
    insight = ("🟢 Great run ahead" if easy >= len(fixtures_out)*0.6
               else "🔴 Tough run ahead" if hard >= len(fixtures_out)*0.6
               else "Mixed fixtures")
    share_text = (
        f"📅 {team} Fixture Ticker via @TacticaEngine\n"
        f"Next 3: {n3s}\n{insight} — {easy} easy, {hard} hard\n"
        f"Full list: app.tactica.com.ng/fpl #FPL #FPL2627"
    )
    result = {"team":team,"bsd_name":bsd_name,"fixtures":fixtures_out,
              "share_text":share_text,"cached":False,"_cached_at":time.time()}
    cache_write(cache_key, result)
    return result


# ── Step 2: Captain Pick (FPL API data + BSD fixture) ────────────────────────

@router.get("/fpl/captain")
def captain_pick(
    team: str = Query(..., description="FPL club name e.g. Arsenal"),
    top:  int = Query(5, ge=1, le=10),
):
    """
    Best captain candidates from a club.
    Uses FPL API for real player data + BSD for next fixture FDR.
    """
    cache_key = f"fpl_captain_v4__{team.lower().replace(' ','_')}"
    cached    = cache_read(cache_key)
    if cached and cache_age(cached) < CAP_TTL:
        cached["cached"] = True
        return cached

    fpl   = _get_fpl_data()
    teams = fpl["teams"]

    # Find FPL team_id by name (case-insensitive)
    fpl_team_id = None
    for tid, tname in teams.items():
        if tname.lower() == team.lower() or tname.lower().startswith(team.lower()[:4]):
            fpl_team_id = tid
            break
    if not fpl_team_id:
        # Try partial match
        for tid, tname in teams.items():
            if team.lower() in tname.lower() or tname.lower() in team.lower():
                fpl_team_id = tid
                break
    if not fpl_team_id:
        raise HTTPException(404, f"'{team}' not found in FPL data.")

    team_name = teams[fpl_team_id]

    # Get BSD team ID for fixture data
    bsd_team_id, _ = bsd_find_team(
        FPL_TEAM_TO_BSD.get(fpl_team_id, team_name)
    )
    nf  = _next_fixture(bsd_team_id) if bsd_team_id else {}
    fdr = nf.get("fdr", 3)

    # Filter to attackers and midfielders (pos 3=MID, 4=FWD)
    squad = [
        _build_player(p, teams)
        for p in fpl["players"]
        if p.get("team") == fpl_team_id
        and p.get("element_type") in {3, 4}
        and p.get("status") != "u"   # exclude unavailable
        and int(p.get("minutes") or 0) > 0
    ]

    if not squad:
        raise HTTPException(404, f"No attacking players found for {team_name}.")

    # Score and rank
    fdr_mult = FDR_MULTIPLIER.get(fdr, 1.0)
    for p in squad:
        p["weighted_score"] = round(p["fpl_score"] * fdr_mult, 3)
        p["next_fixture"]   = nf
        p["reason"]         = _reason_fpl(p, fdr, _fdr_label(fdr),
                                           nf.get("opponent","?"), nf.get("venue","H"))

    squad.sort(key=lambda p: p["weighted_score"], reverse=True)
    picks = squad[:top]

    rec = picks[0]
    share_text = (
        f"🎯 FPL Captain Pick: {rec['name']} ({team_name}) £{rec['price']}m\n"
        f"{rec['ppg']} pts/game last season · {rec['ownership']}% owned\n"
        f"Next: {nf.get('venue','H')} vs {nf.get('opponent','?')} (FDR {fdr})\n"
        f"via @TacticaEngine · app.tactica.com.ng/fpl #FPL"
    )
    result = {
        "team": team_name, "fpl_team_id": fpl_team_id,
        "next_fixture": nf,
        "recommendation": f"Captain {rec['name']} — {rec['reason']}",
        "picks": picks, "share_text": share_text,
        "cached": False, "_cached_at": time.time(),
    }
    cache_write(cache_key, result)
    return result


# ── Step 3: Transfer Recommender (FPL API + BSD fixture) ─────────────────────

@router.get("/fpl/transfers")
def transfer_recommender(
    position: str = Query("FWD", description="GKP, DEF, MID, or FWD"),
    min_price: float = Query(0.0,  description="Min price in £m", ge=0, le=20),
    max_price: float = Query(15.0, description="Max price in £m", ge=3, le=30),
    limit:     int   = Query(10,   description="Results to return", ge=3, le=25),
):
    """
    Best transfer targets by position and price range.
    All 20 PL clubs searched. Ranked by value_score = fpl_score × fixture / price.
    Uses real FPL prices in £m, not BSD market values.
    """
    pos_upper = position.strip().upper()
    pos_id_map = {"GKP":1,"DEF":2,"MID":3,"FWD":4}
    if pos_upper not in pos_id_map:
        raise HTTPException(400, "position must be GKP, DEF, MID, or FWD")
    pos_id = pos_id_map[pos_upper]

    cache_key = f"fpl_trans_v2__{pos_upper}__{min_price}__{max_price}"
    cached    = cache_read(cache_key)
    if cached and cache_age(cached) < TRANS_TTL:
        cached["cached"] = True
        return cached

    fpl   = _get_fpl_data()
    teams = fpl["teams"]

    # Filter players by position + price + availability
    candidates_raw = [
        p for p in fpl["players"]
        if p.get("element_type") == pos_id
        and p.get("status") != "u"
        and min_price <= (p.get("now_cost") or 0) / 10 <= max_price
        and int(p.get("minutes") or 0) > 0
    ]

    # Build players + get fixture per team (cache BSD calls per team)
    team_fixtures: dict[int, dict] = {}
    results = []

    for p in candidates_raw:
        tid  = p.get("team")
        if tid not in team_fixtures:
            bsd_id, _ = bsd_find_team(FPL_TEAM_TO_BSD.get(tid, teams.get(tid,"")))
            team_fixtures[tid] = _next_fixture(bsd_id) if bsd_id else {}
        nf      = team_fixtures[tid]
        fdr     = nf.get("fdr", 3)
        player  = _build_player(p, teams)
        player["next_fixture"]   = nf
        player["weighted_score"] = round(player["fpl_score"] * FDR_MULTIPLIER.get(fdr,1.0), 3)
        player["reason"]         = _reason_fpl(player, fdr, _fdr_label(fdr),
                                                nf.get("opponent","?"), nf.get("venue","H"))
        # Value score — output relative to price
        price = player["price"] or 4.0
        player["value_score"] = round(player["weighted_score"] / price, 3)
        results.append(player)

    results.sort(key=lambda p: p["value_score"], reverse=True)
    top_picks = results[:limit]

    if not top_picks:
        raise HTTPException(404,
            f"No {pos_upper} players found between £{min_price}m and £{max_price}m.")

    pos_label = POS_LABEL.get(pos_id, pos_upper)
    t3 = top_picks[:3]
    t3s = " · ".join(f"{p['name']} ({p['team']}, £{p['price']}m)" for p in t3)
    share_text = (
        f"🔄 Top FPL {pos_label} transfers via @TacticaEngine\n"
        f"Budget £{min_price}m–£{max_price}m · ranked by form + fixture value\n"
        f"{t3s}\n"
        f"Full list: app.tactica.com.ng/fpl #FPL #FPLTransfers"
    )
    result = {
        "position": pos_upper, "min_price": min_price, "max_price": max_price,
        "total_found": len(results), "picks": top_picks,
        "share_text": share_text,
        "cached": False, "_cached_at": time.time(),
    }
    cache_write(cache_key, result)
    return result


# ── Step 4: Differential Finder (FPL API + BSD fixture) ──────────────────────

@router.get("/fpl/differentials")
def differential_finder(
    position:      str   = Query("FWD", description="GKP, DEF, MID, or FWD"),
    max_ownership: float = Query(15.0, description="Max ownership %", ge=0.5, le=50),
    max_price:     float = Query(8.0,  description="Max price in £m", ge=3, le=20),
    limit:         int   = Query(8,    description="Results to return", ge=3, le=20),
):
    """
    Low-ownership players with good form and easy fixtures.
    Uses real FPL ownership % — not a proxy. Proper differentials.
    """
    pos_upper = position.strip().upper()
    pos_id_map = {"GKP":1,"DEF":2,"MID":3,"FWD":4}
    if pos_upper not in pos_id_map:
        raise HTTPException(400, "position must be GKP, DEF, MID, or FWD")
    pos_id = pos_id_map[pos_upper]

    cache_key = f"fpl_diff_v2__{pos_upper}__{max_ownership}__{max_price}"
    cached    = cache_read(cache_key)
    if cached and cache_age(cached) < DIFF_TTL:
        cached["cached"] = True
        return cached

    fpl   = _get_fpl_data()
    teams = fpl["teams"]

    candidates_raw = [
        p for p in fpl["players"]
        if p.get("element_type") == pos_id
        and p.get("status") != "u"
        and float(p.get("selected_by_percent") or 0) <= max_ownership
        and (p.get("now_cost") or 0) / 10 <= max_price
        and int(p.get("minutes") or 0) > 450   # must have played meaningfully
        and float(p.get("points_per_game") or 0) >= 3.0  # minimum output
    ]

    team_fixtures: dict[int, dict] = {}
    results = []

    for p in candidates_raw:
        tid = p.get("team")
        if tid not in team_fixtures:
            bsd_id, _ = bsd_find_team(FPL_TEAM_TO_BSD.get(tid, teams.get(tid,"")))
            team_fixtures[tid] = _next_fixture(bsd_id) if bsd_id else {}
        nf  = team_fixtures[tid]
        fdr = nf.get("fdr", 3)
        if fdr > 3:
            continue   # only easy/medium fixtures for differentials

        player  = _build_player(p, teams)
        ease    = EASE_BONUS.get(fdr, 1.0)
        # Differential score rewards: output × easy fixture × low ownership
        own_bonus = max(1.0, (max_ownership - player["ownership"]) / 5)
        player["diff_score"]     = round(player["fpl_score"] * ease * own_bonus, 3)
        player["weighted_score"] = round(player["fpl_score"] * FDR_MULTIPLIER.get(fdr,1.0), 3)
        player["next_fixture"]   = nf
        player["reason"]         = (
            f"{_reason_fpl(player, fdr, _fdr_label(fdr), nf.get('opponent','?'), nf.get('venue','H'))} "
            f"Only {player['ownership']}% owned — genuine differential."
        )
        results.append(player)

    results.sort(key=lambda p: p["diff_score"], reverse=True)
    top_picks = results[:limit]

    if not top_picks:
        raise HTTPException(404,
            f"No differential {pos_upper}s found under {max_ownership}% ownership "
            f"and £{max_price}m with easy fixtures. Try relaxing the filters.")

    pos_label = POS_LABEL.get(pos_id, pos_upper)
    t3 = top_picks[:3]
    t3s = " · ".join(
        f"{p['name']} ({p['team']}, {p['ownership']}% owned)" for p in t3
    )
    share_text = (
        f"💡 FPL Differential {pos_label}s via @TacticaEngine\n"
        f"Under {max_ownership}% ownership · easy fixtures · in form\n\n"
        f"🔥 {t3s}\n\n"
        f"Full list: app.tactica.com.ng/fpl #FPL #FPLDifferentials"
    )
    result = {
        "position": pos_upper, "max_ownership": max_ownership,
        "max_price": max_price, "total_found": len(results),
        "picks": top_picks, "share_text": share_text,
        "cached": False, "_cached_at": time.time(),
    }
    cache_write(cache_key, result)
    return result
