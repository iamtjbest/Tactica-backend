"""
FPL Scout — Steps 1, 2, 3
Fixture Ticker · Captain Pick · Transfer Recommender

Philosophy: outputs are designed to be SHAREABLE.
Every endpoint returns a share_text field — a plain-English sentence
the user can copy directly to Twitter.

BSD confirmed fields (from debug session):
  /players/{id}/stats/ → goals, goal_assist, shots_on_target,
                          rating, minutes_played
  /events/?league_id=1 → Premier League fixtures
  /teams/{id}/fixtures/ → team-specific fixtures
  /players/?team_id=X  → squad with market_value_eur, position
"""
import time
from datetime import datetime, timezone
from math import floor
from fastapi import APIRouter, Query, HTTPException
from app.config import bsd_get, bsd_find_team, cache_read, cache_write, cache_age
from app.routers.form import _dynamic_ratings

router    = APIRouter()
FDR_TTL   = 3600   # 1 hour
CAP_TTL   = 1800   # 30 min
TRANS_TTL = 3600   # 1 hour

# ── Shared helpers ────────────────────────────────────────────────────────────

def _fdr(defence: int, is_away: bool) -> int:
    base = defence + (5 if is_away else 0)
    return max(1, min(5, 1 + int(base // 21)))

def _fdr_label(fdr: int) -> str:
    return "Easy" if fdr <= 2 else ("Medium" if fdr == 3 else "Hard")

def _fdr_colour(fdr: int) -> str:
    return "green" if fdr <= 2 else ("amber" if fdr == 3 else "red")

FDR_MULTIPLIER = {1: 1.30, 2: 1.15, 3: 1.00, 4: 0.85, 5: 0.70}

ATTACKING_POS = {"F","FW","M","MF","ST","CF","SS","LW","RW",
                 "AM","CAM","LM","RM","FW/MF","MF/FW"}

def _get_opponent_defence(opp_id: int) -> int:
    """Opponent defence rating from their last 30 finished fixtures."""
    try:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT23:59:59Z")
        data = bsd_get(f"/teams/{opp_id}/fixtures/", params={
            "status": "finished", "limit": 30,
            "date_from": "2025-08-01T00:00:00Z", "date_to": now,
        })
        if not data:
            return 75
        raw = data if isinstance(data, list) else data.get("results", [])
        matches = []
        for fix in raw:
            is_home = fix.get("home_team_id") == opp_id
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

def _next_fixture(team_id: int) -> dict:
    """
    Get next upcoming fixture for a team.
    Key fix: uses date_from=today so BSD returns 2026/27 fixtures
    instead of drowning in finished 2025/26 results.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Strategy 1: notstarted + date_from today
    d = bsd_get(f"/teams/{team_id}/fixtures/", params={
        "status": "notstarted", "limit": 5, "date_from": today,
    })
    fixes = []
    if d:
        fixes = d if isinstance(d, list) else d.get("results", [])

    # Strategy 2: all fixtures from today (catches postponed/rescheduled)
    if not fixes:
        d2 = bsd_get(f"/teams/{team_id}/fixtures/", params={
            "limit": 10, "date_from": today,
        })
        if d2:
            fixes = d2 if isinstance(d2, list) else d2.get("results", [])

    # Strategy 3: /events/ endpoint with team_id + date_from
    if not fixes:
        d3 = bsd_get("/events/", params={
            "team_id": team_id, "date_from": today,
            "status": "notstarted", "limit": 5,
        })
        if d3:
            fixes = d3 if isinstance(d3, list) else d3.get("results", [])

    if not fixes:
        return {}

    fixes.sort(key=lambda f: f.get("event_date") or "")
    nf = fixes[0]

    is_home      = nf.get("home_team_id") == team_id
    opp_id       = nf.get("away_team_id" if is_home else "home_team_id") or 0
    opp_name     = nf.get("away_team" if is_home else "home_team", "Unknown")
    opp_def      = _get_opponent_defence(opp_id)
    fdr          = _fdr(opp_def, is_away=not is_home)
    try:
        dt       = datetime.fromisoformat(
            (nf.get("event_date") or "").replace("Z", "+00:00"))
        date_str = dt.strftime("%-d %b")
    except Exception:
        date_str = (nf.get("event_date") or "")[:10]

    return {
        "opponent":    opp_name,
        "venue":       "H" if is_home else "A",
        "date":        date_str,
        "fdr":         fdr,
        "fdr_label":   _fdr_label(fdr),
        "fdr_colour":  _fdr_colour(fdr),
        "multiplier":  FDR_MULTIPLIER.get(fdr, 1.0),
    }

def _player_stats(pid: int) -> dict:
    """Fetch last 5 match stats for a player, return scored averages."""
    data = bsd_get(f"/players/{pid}/stats/", params={"limit": 5})
    if not data:
        return {}
    stats = data if isinstance(data, list) else data.get("results", [])
    started = [s for s in stats if (s.get("minutes_played") or 0) >= 45]
    if not started:
        return {}
    n = len(started)
    avg_g   = sum(s.get("goals", 0) or 0           for s in started) / n
    avg_a   = sum(s.get("goal_assist", 0) or 0     for s in started) / n
    avg_sot = sum(s.get("shots_on_target", 0) or 0 for s in started) / n
    avg_r   = sum(s.get("rating", 0) or 0          for s in started) / n
    score   = avg_g * 6 + avg_a * 3 + avg_sot * 0.5 + avg_r * 0.3
    return {
        "score": round(score, 3), "apps": n,
        "avg_goals": round(avg_g, 2), "avg_assists": round(avg_a, 2),
        "avg_shots_on_target": round(avg_sot, 2), "avg_rating": round(avg_r, 2),
    }

def _reason(s: dict, fdr: int, fdr_label: str, opp: str, venue: str) -> str:
    parts = []
    if s["avg_goals"] >= 0.5:
        parts.append(f"{s['avg_goals']:.1f} goals/game")
    if s["avg_assists"] >= 0.4:
        parts.append(f"{s['avg_assists']:.1f} assists/game")
    if s["avg_shots_on_target"] >= 2.0:
        parts.append(f"{s['avg_shots_on_target']:.1f} SoT/game")
    if s["avg_rating"] >= 7.5:
        parts.append(f"rating {s['avg_rating']:.1f}")
    form = ", ".join(parts) if parts else "decent recent form"
    fix  = f"{fdr_label.lower()} fixture ({venue} vs {opp}, FDR {fdr})"
    return f"{form.capitalize()} · {fix}."

# ── Step 1: Fixture Ticker ────────────────────────────────────────────────────

@router.get("/fpl/fixtures")
def fixture_ticker(
    team: str = Query(..., description="Club name e.g. Arsenal, Liverpool"),
    gws:  int = Query(38, description="Gameweeks to show", ge=1, le=50),
):
    cache_key = f"fpl_fixtures_v4__{team.lower().replace(' ','_')}"
    cached    = cache_read(cache_key)
    if cached and cache_age(cached) < FDR_TTL:
        cached["cached"] = True
        return cached

    team_id, bsd_name = bsd_find_team(team)
    if not team_id:
        raise HTTPException(404, f"Team '{team}' not found.")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Try notstarted + date_from today first
    data = bsd_get(f"/teams/{team_id}/fixtures/", params={
        "status": "notstarted", "limit": min(gws + 10, 200),
        "date_from": today,
    })
    raw = data if isinstance(data, list) else (data or {}).get("results", [])

    # Fallback: all from today without status filter
    if not raw:
        data = bsd_get(f"/teams/{team_id}/fixtures/", params={
            "limit": min(gws + 10, 200), "date_from": today,
        })
        raw = data if isinstance(data, list) else (data or {}).get("results", [])

    # Final fallback: /events/ endpoint
    if not raw:
        data = bsd_get("/events/", params={
            "team_id": team_id, "date_from": today,
            "status": "notstarted", "limit": min(gws + 10, 200),
        })
        raw = data if isinstance(data, list) else (data or {}).get("results", [])

    if not raw:
        raise HTTPException(404,
            f"No upcoming fixtures found for '{team}'. "
            "The 2026/27 schedule may not be indexed in BSD yet — check back in early August.")

    raw.sort(key=lambda f: f.get("event_date") or "")
    upcoming = raw[:gws]

    fixtures_out = []
    for fix in upcoming:
        is_home    = fix.get("home_team_id") == team_id
        opp_id     = fix.get("away_team_id" if is_home else "home_team_id") or 0
        opp_name   = fix.get("away_team"    if is_home else "home_team", "Unknown")
        event_date = fix.get("event_date", "")
        round_num  = fix.get("round_number")
        try:
            dt           = datetime.fromisoformat(event_date.replace("Z", "+00:00"))
            date_display = dt.strftime("%-d %b")
        except Exception:
            date_display = event_date[:10]
        opp_def = _get_opponent_defence(opp_id)
        fdr     = _fdr(opp_def, is_away=not is_home)
        fixtures_out.append({
            "gameweek":    round_num,
            "date":        date_display,
            "date_iso":    event_date,
            "opponent":    opp_name,
            "venue":       "H" if is_home else "A",
            "opp_defence": opp_def,
            "fdr":         fdr,
            "fdr_label":   _fdr_label(fdr),
            "fdr_colour":  _fdr_colour(fdr),
        })

    # Shareable text — this is what users post on Twitter
    easy_count = sum(1 for f in fixtures_out if f["fdr_colour"] == "green")
    hard_count = sum(1 for f in fixtures_out if f["fdr_colour"] == "red")
    next_3     = fixtures_out[:3]
    next_3_str = " · ".join(
        f"{f['opponent']} ({f['fdr_label'][0]})" for f in next_3
    )
    if easy_count >= len(fixtures_out) * 0.6:
        insight = f"🟢 Great fixture run ahead"
    elif hard_count >= len(fixtures_out) * 0.6:
        insight = f"🔴 Tough fixtures coming up"
    else:
        insight = f"Mixed fixtures ahead"
    share_text = (
        f"📅 {team} Fixture Ticker via @TacticaEngine\n"
        f"Next 3: {next_3_str}\n"
        f"{insight} — {easy_count} easy, {hard_count} hard in next {len(fixtures_out)} GWs\n"
        f"Full analysis: app.tactica.com.ng/fpl #FPL #FPL{datetime.now().year}{datetime.now().year+1}"
    )

    result = {
        "team": team, "bsd_name": bsd_name, "team_id": team_id,
        "fixtures": fixtures_out,
        "share_text": share_text,
        "generated": datetime.now(timezone.utc).isoformat(),
        "cached": False, "_cached_at": time.time(),
    }
    cache_write(cache_key, result)
    return result


# ── Step 2: Captain Pick ──────────────────────────────────────────────────────

@router.get("/fpl/captain")
def captain_pick(
    team: str = Query(..., description="Club name"),
    top:  int = Query(5, ge=1, le=10),
):
    cache_key = f"fpl_captain_v3__{team.lower().replace(' ','_')}"
    cached    = cache_read(cache_key)
    if cached and cache_age(cached) < CAP_TTL:
        cached["cached"] = True
        return cached

    team_id, bsd_name = bsd_find_team(team)
    if not team_id:
        raise HTTPException(404, f"Team '{team}' not found.")

    squad_data   = bsd_get("/players/", params={"team_id": team_id, "limit": 100})
    players_raw  = squad_data if isinstance(squad_data, list) else (squad_data or {}).get("results", [])
    attackers    = [p for p in players_raw
                   if str(p.get("position","")).upper() in {"F","FW","M","MF"}
                   or str(p.get("specific_position","")).upper() in ATTACKING_POS]

    if not attackers:
        raise HTTPException(404, f"No attacking players found for {team}.")

    nf       = _next_fixture(team_id)
    fdr      = nf.get("fdr", 3)
    fdr_mult = FDR_MULTIPLIER.get(fdr, 1.0)

    candidates = []
    for p in attackers:
        pid  = p.get("id")
        name = p.get("name") or p.get("short_name") or "Unknown"
        if not pid:
            continue
        s = _player_stats(pid)
        if not s or s.get("apps", 0) == 0:
            continue
        candidates.append({
            "name":          name,
            "position":      p.get("specific_position") or p.get("position",""),
            "bsd_id":        pid,
            "market_value":  p.get("market_value_eur"),
            "form_score":    s["score"],
            "weighted_score": round(s["score"] * fdr_mult, 3),
            "apps_last5":    s["apps"],
            "avg_goals":     s["avg_goals"],
            "avg_assists":   s["avg_assists"],
            "avg_shots_on_target": s["avg_shots_on_target"],
            "avg_rating":    s["avg_rating"],
            "next_fixture":  nf,
            "reason":        _reason(s, fdr, _fdr_label(fdr),
                                     nf.get("opponent","Unknown"),
                                     nf.get("venue","H")),
        })

    candidates.sort(key=lambda c: c["weighted_score"], reverse=True)
    picks = candidates[:top]

    if not picks:
        raise HTTPException(404, f"No recent stats for {team}'s attackers in BSD.")

    rec  = picks[0]
    share_text = (
        f"🎯 My FPL captain this GW: {rec['name']} ({team})\n"
        f"{rec['avg_goals']:.1f} goals/game · {rec['avg_shots_on_target']:.1f} SoT/game\n"
        f"Next: {nf.get('venue','H')} vs {nf.get('opponent','?')} "
        f"(FDR {fdr} — {_fdr_label(fdr)})\n"
        f"via @TacticaEngine · app.tactica.com.ng/fpl #FPL"
    )

    result = {
        "team": team, "bsd_name": bsd_name,
        "next_fixture": nf,
        "recommendation": (
            f"Captain {rec['name']} — {rec['reason']} "
            f"Weighted score {rec['weighted_score']:.1f}."
        ),
        "picks": picks,
        "share_text": share_text,
        "cached": False, "_cached_at": time.time(),
    }
    cache_write(cache_key, result)
    return result


# ── Step 3: Transfer Recommender ──────────────────────────────────────────────
#
# GET /api/fpl/transfers?position=FW&budget=100&limit=10
#
# Finds the best value attackers/midfielders across ALL Premier League
# teams by combining:
#   - recent form score (goals × 6 + assists × 3 + SoT × 0.5 + rating × 0.3)
#   - next fixture FDR multiplier
#   - market value as FPL price proxy (lower = better value pick)
#
# Returns a ranked shortlist with a value_score that rewards
# high output relative to price — exactly how FPL managers think.

# 2026/27 Premier League — confirmed 20 clubs
# Relegated: Ipswich, Leicester City, Southampton
# Promoted:  Sunderland, Leeds United, Burnley
PL_TEAM_NAMES = [
    "Arsenal","Aston Villa","Bournemouth","Brentford","Brighton",
    "Burnley","Chelsea","Crystal Palace","Everton","Fulham",
    "Leeds United","Liverpool","Manchester City","Manchester United",
    "Newcastle United","Nottingham Forest","Sunderland",
    "Tottenham Hotspur","West Ham United","Wolverhampton",
]

# Max budget in €m for filtering (FPL prices roughly = market_value / 10M)
# A budget of 100 = no filter (show all)

@router.get("/fpl/transfers")
def transfer_recommender(
    position: str = Query("FW", description="FW = forwards, MF = midfielders, DF = defenders"),
    budget:   int = Query(100, description="Max market value in €m (default 100 = all)", ge=5, le=200),
    teams:    str = Query("", description="Comma-separated teams to search (empty = all PL)"),
    limit:    int = Query(10, description="Results to return", ge=3, le=25),
):
    """
    Returns the best transfer targets for the given position and budget,
    ranked by value_score = (weighted_form_score / market_value_m).
    """
    pos_upper = position.strip().upper()
    if pos_upper not in {"FW", "MF", "DF", "GK"}:
        raise HTTPException(400, "position must be FW, MF, DF, or GK")

    budget_eur = budget * 1_000_000

    cache_key = f"fpl_transfers_v1__{pos_upper}__{budget}__{teams}"
    cached    = cache_read(cache_key)
    if cached and cache_age(cached) < TRANS_TTL:
        cached["cached"] = True
        return cached

    # Which teams to search
    search_teams = [t.strip() for t in teams.split(",") if t.strip()] if teams else PL_TEAM_NAMES

    all_candidates = []

    for team_name in search_teams:
        team_id, bsd_name = bsd_find_team(team_name)
        if not team_id:
            continue

        # Fetch squad
        squad_data  = bsd_get("/players/", params={"team_id": team_id, "limit": 50})
        players_raw = squad_data if isinstance(squad_data, list) else (squad_data or {}).get("results", [])

        # Filter by position
        pos_players = [
            p for p in players_raw
            if str(p.get("position", "")).upper() == pos_upper
            or str(p.get("specific_position","")).upper() in {
                pos_upper, "ST","CF","SS","LW","RW"  # FW variants
            }
        ]

        # Budget filter
        if budget_eur < 100_000_000:   # only filter if budget is set below 100m
            pos_players = [
                p for p in pos_players
                if not p.get("market_value_eur")
                or (p.get("market_value_eur") or 0) <= budget_eur
            ]

        if not pos_players:
            continue

        # Get next fixture for this team
        nf       = _next_fixture(team_id)
        fdr      = nf.get("fdr", 3)
        fdr_mult = FDR_MULTIPLIER.get(fdr, 1.0)

        # Score each player
        for p in pos_players:
            pid  = p.get("id")
            name = p.get("name") or p.get("short_name") or "Unknown"
            if not pid:
                continue

            s = _player_stats(pid)
            if not s or s.get("apps", 0) == 0:
                continue

            weighted  = round(s["score"] * fdr_mult, 3)
            mval      = p.get("market_value_eur") or 0
            mval_m    = mval / 1_000_000 if mval else 0

            # Value score: output relative to price
            # Players with no market value get a moderate neutral score
            if mval_m > 0:
                value_score = round(weighted / mval_m * 10, 3)
            else:
                value_score = round(weighted * 0.5, 3)

            all_candidates.append({
                "name":          name,
                "team":          team_name,
                "position":      p.get("specific_position") or p.get("position",""),
                "bsd_id":        pid,
                "market_value":  mval,
                "market_value_m": round(mval_m, 1),
                "form_score":    s["score"],
                "weighted_score": weighted,
                "value_score":   value_score,
                "apps_last5":    s["apps"],
                "avg_goals":     s["avg_goals"],
                "avg_assists":   s["avg_assists"],
                "avg_shots_on_target": s["avg_shots_on_target"],
                "avg_rating":    s["avg_rating"],
                "next_fixture":  nf,
                "reason":        _reason(s, fdr, _fdr_label(fdr),
                                         nf.get("opponent","Unknown"),
                                         nf.get("venue","H")),
            })

    # Sort by value_score descending
    all_candidates.sort(key=lambda c: c["value_score"], reverse=True)
    top_picks = all_candidates[:limit]

    if not top_picks:
        raise HTTPException(404,
            f"No {pos_upper} transfer targets found. "
            "BSD may not have 2026/27 squad data indexed yet — check back in early August.")

    # Shareable output
    pos_label = {"FW":"Forward","MF":"Midfielder","DF":"Defender","GK":"Goalkeeper"}.get(pos_upper,"Player")
    top3      = top_picks[:3]
    top3_str  = " · ".join(
        f"{p['name']} ({p['team']}, €{p['market_value_m']}m)" for p in top3
    )
    share_text = (
        f"🔄 Top FPL {pos_label} transfers via @TacticaEngine\n"
        f"Budget: €{budget}m · Ranked by form + fixture value\n"
        f"1⃣ {top3_str[:200]}\n"
        f"Full list: app.tactica.com.ng/fpl #FPL #FPLTransfers"
    )

    result = {
        "position":    pos_upper,
        "budget_eur":  budget_eur,
        "total_found": len(all_candidates),
        "picks":       top_picks,
        "share_text":  share_text,
        "cached":      False,
        "_cached_at":  time.time(),
    }
    cache_write(cache_key, result)
    return result


# ── Step 4: Differential Finder ───────────────────────────────────────────────
#
# GET /api/fpl/differentials?position=FW&max_value=15&min_fdr_ease=2&limit=8
#
# The most shared content on FPL Twitter. Finds players that are:
#   1. Low market value (proxy for low FPL ownership %)
#   2. High recent form (goals, assists, shots)
#   3. Easy next fixture (FDR 1 or 2)
#
# This is the "under-the-radar" pick — someone most managers haven't
# transferred in yet, playing well, with an easy fixture coming.
# A successful differential is worth double in FPL (you gain vs rivals
# who don't have them). That's why this gets shared most on Twitter.
#
# Differential score = weighted_form_score × ease_bonus × value_bonus
#   ease_bonus:  FDR1=1.4, FDR2=1.2, FDR3=1.0 (only shows FDR 1-3)
#   value_bonus: (100 / market_value_m) → cheaper = bigger bonus
#                capped at 3.0 to prevent 0-value players dominating

DIFF_TTL = 1800  # 30 min

EASE_BONUS = {1: 1.40, 2: 1.20, 3: 1.00}

@router.get("/fpl/differentials")
def differential_finder(
    position:  str = Query("FW", description="FW, MF, or DF"),
    max_value: int = Query(25,  description="Max market value in €m", ge=1, le=100),
    limit:     int = Query(8,   description="Results to return", ge=3, le=20),
):
    """
    Returns the best differential picks — low ownership proxy (market value),
    strong recent form, easy next fixture. The FPL manager's secret weapon.
    """
    pos_upper = position.strip().upper()
    if pos_upper not in {"FW", "MF", "DF"}:
        raise HTTPException(400, "position must be FW, MF, or DF")

    max_eur = max_value * 1_000_000

    cache_key = f"fpl_diff_v1__{pos_upper}__{max_value}"
    cached    = cache_read(cache_key)
    if cached and cache_age(cached) < DIFF_TTL:
        cached["cached"] = True
        return cached

    all_candidates = []

    for team_name in PL_TEAM_NAMES:
        team_id, _ = bsd_find_team(team_name)
        if not team_id:
            continue

        # Get next fixture — only proceed if FDR is 1, 2, or 3 (easy/medium)
        nf  = _next_fixture(team_id)
        fdr = nf.get("fdr", 3)
        if fdr > 3:
            # Hard fixture — not a differential this week
            continue

        ease = EASE_BONUS.get(fdr, 1.0)

        # Fetch squad filtered by position
        squad_data  = bsd_get("/players/", params={"team_id": team_id, "limit": 50})
        players_raw = squad_data if isinstance(squad_data, list) else (squad_data or {}).get("results", [])

        pos_players = [
            p for p in players_raw
            if str(p.get("position", "")).upper() == pos_upper
        ]

        # Low value filter — proxy for low FPL ownership
        cheap = [
            p for p in pos_players
            if p.get("market_value_eur") is not None
            and 0 < (p.get("market_value_eur") or 0) <= max_eur
        ]

        for p in cheap:
            pid  = p.get("id")
            name = p.get("name") or p.get("short_name") or "Unknown"
            if not pid:
                continue

            s = _player_stats(pid)
            if not s or s.get("apps", 0) == 0:
                continue

            mval   = p.get("market_value_eur") or 0
            mval_m = mval / 1_000_000

            # Differential score — rewards output + easy fixture + cheap price
            value_bonus = min(100 / mval_m, 3.0) if mval_m > 0 else 1.0
            diff_score  = round(s["score"] * ease * value_bonus, 3)

            # Only include if showing actual attacking output
            if s["score"] < 1.0:
                continue

            all_candidates.append({
                "name":          name,
                "team":          team_name,
                "position":      p.get("specific_position") or p.get("position", ""),
                "bsd_id":        pid,
                "market_value":  mval,
                "market_value_m": round(mval_m, 1),
                "form_score":    s["score"],
                "diff_score":    diff_score,
                "weighted_score": round(s["score"] * FDR_MULTIPLIER.get(fdr, 1.0), 3),
                "apps_last5":    s["apps"],
                "avg_goals":     s["avg_goals"],
                "avg_assists":   s["avg_assists"],
                "avg_shots_on_target": s["avg_shots_on_target"],
                "avg_rating":    s["avg_rating"],
                "next_fixture":  nf,
                "ownership_proxy": "Low",  # market value confirms this
                "reason":        (
                    f"{_reason(s, fdr, _fdr_label(fdr), nf.get('opponent','?'), nf.get('venue','H'))} "
                    f"Low ownership proxy (€{mval_m:.0f}m) — ideal differential."
                ),
            })

    # Sort by differential score
    all_candidates.sort(key=lambda c: c["diff_score"], reverse=True)
    top_picks = all_candidates[:limit]

    if not top_picks:
        raise HTTPException(404,
            f"No differential {pos_upper}s found under €{max_value}m with easy fixtures. "
            "Try raising the budget or check back when 2026/27 fixtures are confirmed in BSD.")

    pos_label = {"FW": "Forward", "MF": "Midfielder", "DF": "Defender"}.get(pos_upper, "Player")

    # Shareable tweet — this is gold for FPL Twitter
    top3     = top_picks[:3]
    top3_str = " · ".join(
        f"{p['name']} ({p['team']}, FDR {p['next_fixture'].get('fdr','?')})" for p in top3
    )
    share_text = (
        f"💡 FPL Differentials GW1 — {pos_label}s under €{max_value}m\n"
        f"via @TacticaEngine · ranked by form + fixture + value\n\n"
        f"🔥 {top3_str}\n\n"
        f"Easy fixtures, low ownership, in form.\n"
        f"Full list: app.tactica.com.ng/fpl #FPL #FPLDifferentials #FPLCommunity"
    )

    result = {
        "position":    pos_upper,
        "max_value_eur": max_eur,
        "total_scanned": len(all_candidates),
        "picks":       top_picks,
        "share_text":  share_text,
        "cached":      False,
        "_cached_at":  time.time(),
    }
    cache_write(cache_key, result)
    return result
