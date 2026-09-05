"""
GET /api/form?team={name}
Fetches last 5 finished matches for a team.
Returns: { team, matches[], attack, defence, best_formation }
1-hour cache per team.

Date window: March 1 2026 → today
This covers the full end of the 2025-26 European season (Mar-May/Jun 2026)
while excluding older data from December 2025 and earlier.

BSD calls:
  1 x GET /api/v2/teams/?name={name}            → team_id
  1 x GET /api/v2/teams/{id}/fixtures/?...      → up to 20 finished fixtures
  ≤5 x GET /api/v2/events/{id}/lineups/         → formation used (top 5 only)
"""
import time
import os
from datetime import datetime, timezone, timedelta
USE_DUMMY_DATA = os.getenv('USE_DUMMY_DATA', 'False') == 'True'
from fastapi import APIRouter, Query, HTTPException
from app.config import (bsd_get, bsd_find_team, cache_read, cache_write,
                        cache_age, LEAGUE_NAMES)

def _get_team_primary_league(team_id: int) -> int | None:
    """Return the primary league_id for a team (e.g., Premier League).
    If the API fails or the team has fewer than 5 fixtures in that league, return None so the caller can fall back.
    """
    try:
        data = bsd_get(f"/teams/{team_id}/")
        league_id = data.get("league_id")
        if not league_id:
            leagues = data.get("leagues") or []
            if leagues:
                league_id = leagues[0].get("id")
        return int(league_id) if league_id is not None else None
    except Exception:
        return None

router   = APIRouter()
FORM_TTL = 3600   # 1 hour

# Window covers the full 2025-26 season so ratings (attack/defence) reflect
# the whole season's form. limit=50 ensures BSD returns enough fixtures that
# after sorting DESC we reliably hit the 5 most recent regardless of how many
# games a team has played since August.
# NOTE: BSD returns fixtures ASCENDING by default — we sort DESC in Python.
# With limit=20 and a wide window, BSD returned the oldest 20 (Aug-Nov) and
# the most recent May fixtures were never fetched. Raising to 50 fixes this.
SEASON_START = "2025-08-01T00:00:00Z"  # widened to full 2025/26 season
FORM_LIMIT   = 80

# League IDs that are preseason friendlies or cups — never used for ratings.
# BSD uses league_id=79 for preseason/friendly fixtures across multiple teams.
# Add any other IDs found in practice.
# Minimum finished matches required to trust the dynamic rating over the
# hardcoded _KNOWN_RATINGS baseline. Used both when deciding whether the
# primary-league-only fixture set is enough on its own, and later when
# deciding whether to use the dynamic rating at all — these two checks must
# stay in sync, or a team can get filtered down to a small league-only set
# that then fails the dynamic threshold even though richer data existed
# before filtering.
_MIN_MATCHES = 5

FRIENDLY_LEAGUE_IDS = {79, 0}  # 0 = unknown/unset


def _dynamic_ratings(matches: list) -> tuple[int, int]:
    """Calculate attack and defence ratings using Dixon-Coles style.

    • Uses up to the 10 most recent matches.
    • Binary decay weighting: weight 1.0 for the 5 newest, 0.5 for the next 5.
    • Expected league-average goals per game = 1.4.
    • Sigmoid scaling prevents extreme single-match results from clamping
      the output to 90 — a team scoring 3 goals once shouldn't become
      ATT=90 when they also scored 0 twice.
    • Final ratings clamped to 30-85 range (not 10-90) so dynamic ratings
      stay in a realistic band — KNOWN_RATINGS handles the true elite/weak
      extremes. This prevents early-season noise from producing 90/90.
    """
    import math
    if not matches:
        return 72, 72  # mid-table default

    weighted_scored = 0.0
    weighted_conceded = 0.0
    total_weight = 0.0
    expected_goals = 1.4

    for idx, m in enumerate(matches[:10]):
        weight = 1.0 if idx < 5 else 0.5
        weighted_scored   += m["scored"]   * weight
        weighted_conceded += m["conceded"] * weight
        total_weight      += weight

    weighted_conceded = max(weighted_conceded, 0.3 * total_weight)
    avg_scored    = weighted_scored   / total_weight
    avg_conceded  = weighted_conceded / total_weight

    # Ratio vs league average — capped at 2.0 so extreme results don't dominate
    att_ratio = min(avg_scored   / expected_goals, 2.0)
    def_ratio = min(expected_goals / avg_conceded, 2.0)

    # Sigmoid scaling: maps 0-2 ratio → 30-82 rating
    # ratio=1.0 (average team) → ~56, ratio=2.0 (dominant) → ~82
    def _scale(ratio: float) -> int:
        # sigmoid centred at ratio=1.0, output range 30-82
        sig = 1.0 / (1.0 + math.exp(-3.5 * (ratio - 1.0)))
        return int(30 + sig * 52)

    attack  = _scale(att_ratio)
    defence = _scale(def_ratio)

    # Clamp to realistic dynamic range — KNOWN_RATINGS handles extremes
    attack  = max(30, min(82, attack))
    defence = max(30, min(82, defence))
    return attack, defence


def _most_used_formation(matches: list) -> str | None:
    counts: dict[str, int] = {}
    for m in matches:
        f = m.get("formation", "Unknown")
        if f and f != "Unknown":
            counts[f] = counts.get(f, 0) + 1
    return max(counts, key=counts.get) if counts else None


def _fixture_date(fix: dict) -> str:
    """Try known BSD date field names. Empty string sorts last under reverse=True."""
    return (
        fix.get("event_date")
        or fix.get("date")
        or fix.get("kickoff_time")
        or fix.get("starting_at")
        or ""
    )


_KNOWN_RATINGS: dict[str, tuple[int, int]] = {
    "Real Madrid":(88,88),"Barcelona":(87,85),"FC Barcelona":(87,85),"Manchester City":(87,86),
    "Liverpool":(85,84),"Liverpool FC":(85,84),"Bayern Munich":(86,87),"FC Bayern München":(86,87),
    "Paris Saint-Germain":(85,83),"Arsenal":(82,82),"Inter Milan":(80,85),"Inter":(80,85),
    "Atletico Madrid":(78,86),"Atlético Madrid":(78,86),"Borussia Dortmund":(80,78),
    "Aston Villa":(78,76),"Manchester United":(76,78),"Chelsea":(78,78),
    "Tottenham Hotspur":(76,74),"Tottenham":(76,74),"Newcastle United":(76,78),
    "Everton":(62,65),"Brighton":(72,74),"Brighton & Hove Albion":(72,74),
    "Fulham":(68,70),"Brentford":(68,68),"Crystal Palace":(65,66),
    "Bournemouth":(65,64),"Nottingham Forest":(62,64),"West Ham United":(66,66),
    "Wolves":(62,64),"Wolverhampton Wanderers":(62,64),
}

@router.get("/form")
def form(
    team: str = Query(..., description="Team name"),
    refresh: bool = Query(True, description="Deprecated no-op — BSD is now always fetched fresh; cache is only used as a fallback if BSD fails"),
):
    cache_key = f"form_v7__{team.lower().replace(' ', '_')}"

    # Resolve team_id — always hit BSD live, never trust cache for this
    team_id, bsd_name = bsd_find_team(team)
    if not team_id and USE_DUMMY_DATA:
        # Fallback for test environment – provide dummy IDs for known teams
        _fallback = {
            "Arsenal": (1, "Arsenal"),
            "Manchester United": (2, "Manchester United"),
        }
        team_id, bsd_name = _fallback.get(team, (None, None))
    if not team_id:
        # BSD lookup itself failed (or team truly doesn't exist) — fall back
        # to a cached copy if we have one rather than hard-failing.
        cached = cache_read(cache_key)
        if cached:
            cached["cached"] = True
            cached["_served_stale_reason"] = "bsd_team_lookup_failed"
            return cached
        raise HTTPException(status_code=404, detail=f"Team '{team}' not found in BSD.")

    date_to = datetime.now(timezone.utc).strftime("%Y-%m-%dT23:59:59Z")
    if USE_DUMMY_DATA and team_id in (1, 2):
        dummy_fixtures = []
        for i in range(5):
            dummy_fixtures.append({
                "id": i + 1,
                "home_team_id": team_id,
                "away_team_id": 999,
                "home_score": i % 3,
                "away_score": (i + 1) % 3,
                "league_id": 17,
                "event_date": (datetime.now(timezone.utc) - timedelta(days=i * 7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "home_team": {"name": team},
                "away_team": {"name": "Dummy Opponent"},
            })
        data = {"results": dummy_fixtures}
    else:
        data = bsd_get(f"/teams/{team_id}/fixtures/", params={
            "status":    "finished",
            "limit":     FORM_LIMIT,
            "date_from": SEASON_START,
            "date_to":   date_to,
        })
    if not data:
        # BSD fixtures fetch failed (timeout/error/rate-limit) — fall back
        # to a cached copy if we have one rather than hard-failing.
        cached = cache_read(cache_key)
        if cached:
            cached["cached"] = True
            cached["_served_stale_reason"] = "bsd_fixtures_fetch_failed"
            return cached
        raise HTTPException(status_code=502, detail="BSD API error fetching fixtures.")

    fixtures = data.get("results", [])
    _raw_fixture_count = len(fixtures)

    # Always strip friendly/preseason fixtures first
    fixtures = [f for f in fixtures if f.get("league_id") not in FRIENDLY_LEAGUE_IDS]
    _after_friendly_strip = len(fixtures)

    # Filter to primary league if available
    primary_league = _get_team_primary_league(team_id)
    if primary_league is not None:
        league_fixtures = [f for f in fixtures if f.get("league_id") == primary_league]
        fixtures = league_fixtures if len(league_fixtures) >= _MIN_MATCHES else fixtures
    _after_league_filter = len(fixtures)

    fixtures.sort(key=_fixture_date, reverse=True)
    fixtures = fixtures[:10]   # up to 10 most recent for decay weighting

    matches = []
    for fix in fixtures:
        fid        = fix.get("id", 0)
        home_id    = fix.get("home_team_id", 0)
        home_score = fix.get("home_score") or 0
        away_score = fix.get("away_score") or 0
        is_home    = (home_id == team_id)
        scored     = home_score if is_home else away_score
        conceded   = away_score if is_home else home_score

        opp_val    = fix.get("away_team") if is_home else fix.get("home_team")
        if isinstance(opp_val, dict):
            opp = opp_val.get("name", "?")
        else:
            opp = str(opp_val or "?")

        league_id   = fix.get("league_id", 0)
        competition = LEAGUE_NAMES.get(league_id, f"League {league_id}")
        result      = "W" if scored > conceded else ("D" if scored == conceded else "L")

        formation = "Unknown"
        ld = bsd_get(f"/events/{fid}/lineups/")
        if ld:
            status  = ld.get("lineup_status", "unavailable")
            lineups = ld.get("lineups")
            if status != "unavailable" and lineups:
                side      = "home" if is_home else "away"
                formation = (lineups.get(side) or {}).get("formation", "Unknown")

        matches.append({
            "fixture_id":   fid,
            "opponent":     opp,
            "competition":  competition,
            "scored":       scored,
            "conceded":     conceded,
            "result":       result,
            "formation":    formation,
            "event_date":   _fixture_date(fix),
        })

    # ── Rating calculation ────────────────────────────────────────────────────
    raw_att, raw_dfc = _dynamic_ratings(matches)
    if len(matches) >= _MIN_MATCHES:
        att, dfc = raw_att, raw_dfc
    else:
        baseline = _KNOWN_RATINGS.get(team) or _KNOWN_RATINGS.get(bsd_name)
        if baseline:
            b_att, b_dfc = baseline
            if matches:
                att = max(10, min(90, int(0.80 * b_att + 0.20 * raw_att)))
                dfc = max(10, min(90, int(0.80 * b_dfc + 0.20 * raw_dfc)))
            else:
                att, dfc = b_att, b_dfc
        else:
            att = max(10, min(90, raw_att))
            dfc = max(10, min(90, raw_dfc))

    best_form = _most_used_formation(matches)

    result_doc = {
        "_cached_at":     time.time(),
        "team":           team,
        "bsd_name":       bsd_name,
        "matches":        matches,
        "attack":         att,
        "defence":        dfc,
        "win_percentage": max(0, min(100, int(((att - 10) / 80) * 100))),
        "best_formation": best_form,
        "cached":         False,
        "_debug": {
            "team_id":               team_id,
            "primary_league":        primary_league,
            "raw_fixture_count":     _raw_fixture_count,
            "after_friendly_strip":  _after_friendly_strip,
            "after_league_filter":   _after_league_filter,
            "used_dynamic_rating":   len(matches) >= _MIN_MATCHES,
        },
    }
    cache_write(cache_key, result_doc)
    return result_doc
