"""
FPL Scout — Step 1: Fixture Ticker
GET /api/fpl/fixtures?team={name}

Returns the next 6 Premier League fixtures for a team with a
Fixture Difficulty Rating (FDR) for each:
  1-2 = Green  (easy)
  3   = Amber  (medium)
  4-5 = Red    (hard)

FDR formula (all data from BSD confirmed fields):
  base     = opponent's defence rating from /api/form (0-99)
  away_adj = +5 if this team is playing away (harder)
  fdr      = 1 + floor((base + away_adj) / 21)  → clipped 1-5

defence rating comes from _dynamic_ratings() in form.py which
uses goals conceded over last N matches — confirmed working.
"""
import time
from datetime import datetime, timezone
from math import floor
from fastapi import APIRouter, Query, HTTPException
from app.config import bsd_get, bsd_find_team, cache_read, cache_write, cache_age
from app.routers.form import _dynamic_ratings   # reuse confirmed helper

router  = APIRouter()
FDR_TTL = 3600   # 1 hour — fixture list changes rarely intra-day

# ── Helpers ───────────────────────────────────────────────────────────────────

def _fdr(defence: int, is_away: bool) -> int:
    """
    Convert opponent defence rating to 1-5 FDR.
    Higher defence = harder to score = higher FDR.
    Away games get +5 penalty on the base.
    """
    base = defence + (5 if is_away else 0)
    # Scale 0-104 → 1-5
    fdr = 1 + int(base // 21)
    return max(1, min(5, fdr))

def _fdr_label(fdr: int) -> str:
    if fdr <= 2: return "Easy"
    if fdr == 3: return "Medium"
    return "Hard"

def _fdr_colour(fdr: int) -> str:
    if fdr <= 2: return "green"
    if fdr == 3: return "amber"
    return "red"

def _get_opponent_defence(opp_id: int, opp_name: str) -> int:
    """
    Fetch opponent's defence rating using the form endpoint's
    _dynamic_ratings helper. Falls back to 75 if unavailable.
    """
    try:
        date_to   = datetime.now(timezone.utc).strftime("%Y-%m-%dT23:59:59Z")
        date_from = "2025-08-01T00:00:00Z"
        data = bsd_get(f"/teams/{opp_id}/fixtures/", params={
            "status":    "finished",
            "limit":     30,
            "date_from": date_from,
            "date_to":   date_to,
        })
        fixtures = []
        if data:
            raw = data if isinstance(data, list) else data.get("results", [])
            for fix in raw:
                is_home = fix.get("home_team_id") == opp_id
                scored    = fix.get("home_score" if is_home else "away_score") or 0
                conceded  = fix.get("away_score" if is_home else "home_score") or 0
                fixtures.append({
                    "scored":   scored,
                    "conceded": conceded,
                    "result":   "W" if scored > conceded else ("D" if scored == conceded else "L"),
                    "formation": "",
                })
        if fixtures:
            _, defence = _dynamic_ratings(fixtures)
            return defence
    except Exception:
        pass
    return 75   # sensible neutral fallback

# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.get("/fpl/fixtures")
def fixture_ticker(
    team: str = Query(..., description="Club name e.g. Arsenal, Liverpool"),
    gws:  int = Query(38, description="Number of gameweeks to show (max 50)", ge=1, le=50),
):
    """
    Returns next {gws} fixtures for the team with FDR for each.
    Opponent defence rating is fetched from BSD form data.
    Results cached 1 hour per team.
    """
    cache_key = f"fpl_fixtures_v3__{team.lower().replace(' ','_')}__{gws}"
    cached    = cache_read(cache_key)
    if cached and cache_age(cached) < FDR_TTL:
        cached["cached"] = True
        return cached

    # 1. Resolve team
    team_id, bsd_name = bsd_find_team(team)
    if not team_id:
        raise HTTPException(404, f"Team '{team}' not found in BSD. Try a slightly different spelling.")

    # Fetch upcoming fixtures — try notstarted first, fall back to all statuses
    # (During World Cup break, clubs may have no "notstarted" fixtures in BSD yet)
    data = bsd_get(f"/teams/{team_id}/fixtures/", params={
        "status": "notstarted",
        "limit":  min(gws + 10, 200),
    })
    raw = []
    if data:
        raw = data if isinstance(data, list) else data.get("results", [])

    # If empty, try without status filter (returns all including future)
    if not raw:
        data = bsd_get(f"/teams/{team_id}/fixtures/", params={
            "limit": min(gws + 10, 200),
        })
        if data:
            all_fix = data if isinstance(data, list) else data.get("results", [])
            now = datetime.now(timezone.utc).isoformat()
            # Keep only future fixtures
            raw = [f for f in all_fix if (f.get("event_date") or "") > now]

    # 3. Sort ascending by date, take first {gws}
    def _event_date(f):
        d = f.get("event_date", "") or ""
        return d[:19]   # ISO prefix for sort

    raw.sort(key=_event_date)
    upcoming = raw[:gws]

    if not upcoming:
        raise HTTPException(404, f"No upcoming fixtures found for '{team}'. Season may be between rounds.")

    # 4. Build FDR for each fixture
    fixtures_out = []
    for fix in upcoming:
        is_home    = fix.get("home_team_id") == team_id
        opp_id     = fix.get("away_team_id" if is_home else "home_team_id")
        opp_name   = fix.get("away_team"    if is_home else "home_team", "Unknown")
        event_date = fix.get("event_date", "")
        round_num  = fix.get("round_number")

        # Parse date for display
        try:
            dt = datetime.fromisoformat(event_date.replace("Z", "+00:00"))
            date_display = dt.strftime("%-d %b")   # e.g. "9 Aug"
        except Exception:
            date_display = event_date[:10]

        # Get opponent defence
        opp_defence = _get_opponent_defence(opp_id, opp_name)

        fdr = _fdr(opp_defence, is_away=not is_home)

        fixtures_out.append({
            "gameweek":       round_num,
            "date":           date_display,
            "date_iso":       event_date,
            "opponent":       opp_name,
            "venue":          "H" if is_home else "A",
            "opp_defence":    opp_defence,
            "fdr":            fdr,
            "fdr_label":      _fdr_label(fdr),
            "fdr_colour":     _fdr_colour(fdr),
        })

    result = {
        "team":        team,
        "bsd_name":    bsd_name,
        "team_id":     team_id,
        "fixtures":    fixtures_out,
        "generated":   datetime.now(timezone.utc).isoformat(),
        "cached":      False,
        "_cached_at":  time.time(),
    }
    cache_write(cache_key, result)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# FPL Scout — Step 2: Captain Pick
# GET /api/fpl/captain?team={name}
#
# Returns top 5 captain candidates from the team's attacking players, scored by:
#   form_score = avg_goals*6 + avg_assists*3 + avg_shots_on_target*0.5
#              + avg_rating*0.3
# Weighted by next fixture FDR (easy fixture = bonus multiplier).
# Only players who started (minutes_played >= 45) in last 5 games are scored.
# ─────────────────────────────────────────────────────────────────────────────

CAPTAIN_TTL = 1800   # 30 min — form can shift intra-day

FDR_MULTIPLIER = {1: 1.30, 2: 1.15, 3: 1.00, 4: 0.85, 5: 0.70}

ATTACKING_POSITIONS = {"F", "FW", "M", "MF", "ST", "CF", "SS",
                       "LW", "RW", "AM", "CAM", "LM", "RM",
                       "10", "SS", "FW/MF", "MF/FW"}


def _captain_score(stats: list[dict]) -> dict:
    """
    Score a player from their last N match stat dicts.
    Only counts appearances where minutes_played >= 45.
    Returns score + supporting averages.
    """
    started = [s for s in stats if (s.get("minutes_played") or 0) >= 45]
    if not started:
        return {"score": 0.0, "apps": 0, "avg_goals": 0,
                "avg_assists": 0, "avg_shots": 0, "avg_rating": 0}

    n = len(started)
    avg_g   = sum(s.get("goals", 0) or 0          for s in started) / n
    avg_a   = sum(s.get("goal_assist", 0) or 0    for s in started) / n
    avg_sot = sum(s.get("shots_on_target", 0) or 0 for s in started) / n
    avg_r   = sum(s.get("rating", 0) or 0         for s in started) / n

    raw = avg_g * 6.0 + avg_a * 3.0 + avg_sot * 0.5 + avg_r * 0.3
    return {
        "score":       round(raw, 3),
        "apps":        n,
        "avg_goals":   round(avg_g,   2),
        "avg_assists": round(avg_a,   2),
        "avg_shots":   round(avg_sot, 2),
        "avg_rating":  round(avg_r,   2),
    }


def _plain_reason(name: str, s: dict, fdr: int, fdr_label: str,
                  opponent: str, venue: str) -> str:
    """Generate a plain-English reason for the captain pick."""
    parts = []
    if s["avg_goals"] >= 0.5:
        parts.append(f"averaging {s['avg_goals']:.1f} goals per game")
    if s["avg_assists"] >= 0.4:
        parts.append(f"{s['avg_assists']:.1f} assists per game")
    if s["avg_shots"] >= 2.0:
        parts.append(f"{s['avg_shots']:.1f} shots on target per game")
    if s["avg_rating"] >= 7.5:
        parts.append(f"rating {s['avg_rating']:.1f} recently")

    form_str = ", ".join(parts) if parts else "decent recent form"
    fix_str  = f"{fdr_label.lower()} fixture ({venue} vs {opponent}, FDR {fdr})"
    return f"{form_str.capitalize()} · {fix_str}."


@router.get("/fpl/captain")
def captain_pick(
    team: str = Query(..., description="Club name e.g. Arsenal, Liverpool"),
    top:  int = Query(5, description="Number of candidates to return", ge=1, le=10),
):
    """
    Returns top attacking captain picks for the team, ranked by
    form score weighted by next fixture difficulty.
    """
    cache_key = f"fpl_captain_v2__{team.lower().replace(' ','_')}"
    cached    = cache_read(cache_key)
    if cached and cache_age(cached) < CAPTAIN_TTL:
        cached["cached"] = True
        return cached

    # 1. Resolve team
    team_id, bsd_name = bsd_find_team(team)
    if not team_id:
        raise HTTPException(404, f"Team '{team}' not found in BSD.")

    # 2. Get squad — fetch direct from BSD so we have BSD player IDs
    squad_data = bsd_get("/players/", params={"team_id": team_id, "limit": 100})
    if not squad_data:
        raise HTTPException(502, "Could not fetch squad from BSD.")

    players_raw = squad_data if isinstance(squad_data, list) else squad_data.get("results", [])

    # 3. Filter to attacking positions only (FW + MF)
    attackers = []
    for p in players_raw:
        pos  = str(p.get("position", "") or "").strip().upper()
        spec = str(p.get("specific_position", "") or "").strip().upper()
        if pos in {"F", "FW", "M", "MF"} or spec in ATTACKING_POSITIONS:
            attackers.append(p)

    if not attackers:
        raise HTTPException(404, f"No attacking players found in {team}'s BSD squad.")

    # 4. Get next fixture — try notstarted, fall back to all future dated fixtures
    next_fdr       = 3
    next_opponent  = "Unknown"
    next_venue     = "H"
    next_date      = ""

    def _fetch_next_fixture(tid: int):
        # Try notstarted first
        d = bsd_get(f"/teams/{tid}/fixtures/", params={"status": "notstarted", "limit": 5})
        fixes = []
        if d:
            fixes = d if isinstance(d, list) else d.get("results", [])
        # Fallback: all fixtures filtered to future dates
        if not fixes:
            d2 = bsd_get(f"/teams/{tid}/fixtures/", params={"limit": 50})
            if d2:
                now = datetime.now(timezone.utc).isoformat()
                all_f = d2 if isinstance(d2, list) else d2.get("results", [])
                fixes = [f for f in all_f if (f.get("event_date") or "") > now]
        fixes.sort(key=lambda f: (f.get("event_date") or ""))
        return fixes[0] if fixes else None

    nf = _fetch_next_fixture(team_id)
    if nf:
        is_home      = nf.get("home_team_id") == team_id
        opp_id       = nf.get("away_team_id" if is_home else "home_team_id")
        next_opponent= nf.get("away_team"    if is_home else "home_team", "Unknown")
        next_venue   = "H" if is_home else "A"
        opp_def      = _get_opponent_defence(opp_id, next_opponent)
        next_fdr     = _fdr(opp_def, is_away=not is_home)
        try:
            dt = datetime.fromisoformat(
                (nf.get("event_date") or "").replace("Z", "+00:00"))
            next_date = dt.strftime("%-d %b")
        except Exception:
            next_date = (nf.get("event_date") or "")[:10]

    fdr_mult = FDR_MULTIPLIER.get(next_fdr, 1.0)

    # 5. Score each attacker from last 5 match stats
    candidates = []
    for p in attackers:
        pid  = p.get("id")
        name = p.get("name") or p.get("short_name") or "Unknown"
        if not pid:
            continue

        stats_data = bsd_get(f"/players/{pid}/stats/", params={"limit": 5})
        if not stats_data:
            continue
        stats_list = (stats_data if isinstance(stats_data, list)
                      else stats_data.get("results", []))

        s = _captain_score(stats_list)
        if s["apps"] == 0:
            continue

        weighted = round(s["score"] * fdr_mult, 3)

        candidates.append({
            "name":          name,
            "position":      p.get("specific_position") or p.get("position", ""),
            "bsd_id":        pid,
            "market_value":  p.get("market_value_eur"),
            "form_score":    s["score"],
            "weighted_score": weighted,
            "apps_last5":    s["apps"],
            "avg_goals":     s["avg_goals"],
            "avg_assists":   s["avg_assists"],
            "avg_shots_on_target": s["avg_shots"],
            "avg_rating":    s["avg_rating"],
            "next_fixture": {
                "opponent": next_opponent,
                "venue":    next_venue,
                "date":     next_date,
                "fdr":      next_fdr,
                "fdr_label": _fdr_label(next_fdr),
                "fdr_colour": _fdr_colour(next_fdr),
                "multiplier": fdr_mult,
            },
            "reason": _plain_reason(
                name, s, next_fdr, _fdr_label(next_fdr),
                next_opponent, next_venue
            ),
        })

    # 6. Sort by weighted score descending
    candidates.sort(key=lambda c: c["weighted_score"], reverse=True)
    top_picks = candidates[:top]

    if not top_picks:
        raise HTTPException(404,
            f"No recent match data found for {team}'s attackers. "
            "Squad stats may not be indexed for this team in BSD yet.")

    # 7. Top pick recommendation
    rec = top_picks[0]
    recommendation = (
        f"Captain {rec['name']} — {rec['reason']} "
        f"Weighted score {rec['weighted_score']:.1f}."
    )

    result = {
        "team":           team,
        "bsd_name":       bsd_name,
        "next_fixture":   top_picks[0]["next_fixture"] if top_picks else {},
        "recommendation": recommendation,
        "picks":          top_picks,
        "cached":         False,
        "_cached_at":     time.time(),
    }
    cache_write(cache_key, result)
    return result
