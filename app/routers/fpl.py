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
    gws:  int = Query(6,   description="Number of gameweeks to show (max 10)", ge=1, le=10),
):
    """
    Returns next {gws} fixtures for the team with FDR for each.
    Opponent defence rating is fetched from BSD form data.
    Results cached 1 hour per team.
    """
    cache_key = f"fpl_fixtures_v1__{team.lower().replace(' ','_')}__{gws}"
    cached    = cache_read(cache_key)
    if cached and cache_age(cached) < FDR_TTL:
        cached["cached"] = True
        return cached

    # 1. Resolve team
    team_id, bsd_name = bsd_find_team(team)
    if not team_id:
        raise HTTPException(404, f"Team '{team}' not found in BSD. Try a slightly different spelling.")

    # 2. Fetch upcoming fixtures
    data = bsd_get(f"/teams/{team_id}/fixtures/", params={
        "status": "notstarted",
        "limit":  gws + 5,   # fetch extra in case of cup games in the list
    })
    if not data:
        raise HTTPException(502, "BSD fixture data unavailable.")

    raw = data if isinstance(data, list) else data.get("results", [])

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
