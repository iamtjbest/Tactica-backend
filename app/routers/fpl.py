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
import unicodedata  # needed for diacritic‑insensitive normalisation


def _team_counts(squad: list[dict]) -> dict[int, int]:
    """Return a mapping of real‑life team_id to player count in the squad."""
    counts: dict[int, int] = {}
    for p in squad:
        team_id = p.get("team")
        if isinstance(team_id, int):
            counts[team_id] = counts.get(team_id, 0) + 1
    return counts

def _current_gameweek() -> int:
    """Fetch current gameweek from the FPL bootstrap data (cached)."""
    bootstrap = cache_read("fpl_bootstrap")
    if not bootstrap or cache_age(bootstrap) > FPL_TTL:
        bootstrap = bsd_get("https://fantasy.premierleague.com/api/bootstrap-static/")
        cache_write("fpl_bootstrap", bootstrap)
    # Assume events list is ordered; last element has latest gameweek
    return bootstrap.get("events", [{}])[-1].get("id", 0)

def _suggest_chip(gameweek: int) -> str | None:
    """Return a chip suggestion based on the current gameweek.
    Simple heuristic – can be refined later.
    """
    if 1 <= gameweek <= 3:
        return "Free Hit"
    if 4 <= gameweek <= 7:
        return "Bench Boost"
    if 8 <= gameweek <= 12:
        return "Triple Captain"
    return None

def _normalize_str(s: str) -> str:
    """Return a lower‑cased, diacritic‑free version of *s* for tolerant name matching."""
    return (
        unicodedata.normalize("NFKD", s)
        .encode("ASCII", "ignore")
        .decode("ASCII")
        .lower()
    )

router   = APIRouter()
FPL_URL  = "https://fantasy.premierleague.com/api/bootstrap-static/"
FPL_TTL  = 3600   # 1 hour — FPL data updates daily at most
FDR_TTL  = 3600
CAP_TTL  = 1800
TRANS_TTL= 3600
DIFF_TTL = 1800

# ── One canonical club registry, used everywhere ──────────────────────────────
# The confirmed 20 Premier League clubs for 2026/27. This is the single source
# of truth for display names — matches the frontend PL_TEAMS dropdown exactly.
PL_CANONICAL_TEAMS: list[str] = [
    "Arsenal", "Aston Villa", "Bournemouth", "Brentford", "Brighton & Hove Albion",
    "Chelsea", "Coventry City", "Crystal Palace", "Everton", "Fulham",
    "Hull City", "Ipswich Town", "Leeds United", "Liverpool", "Manchester City",
    "Manchester United", "Newcastle United", "Nottingham Forest", "Sunderland",
    "Tottenham Hotspur",
]

# Short/abbreviated forms that share no useful substring with the canonical
# name (so simple substring matching below won't catch them) — covers both
# FPL's abbreviated "name" field and common shorthand.
_CLUB_SHORT_ALIASES: dict[str, str] = {
    "man city":      "Manchester City",
    "man utd":       "Manchester United",
    "man united":    "Manchester United",
    "spurs":         "Tottenham Hotspur",
    "nott'm forest": "Nottingham Forest",
    "notts forest":  "Nottingham Forest",
    "forest":        "Nottingham Forest",
    "newcastle":     "Newcastle United",
    "leeds":         "Leeds United",
    "hull":          "Hull City",
    "ipswich":       "Ipswich Town",
    "coventry":      "Coventry City",
    "sunderland afc":"Sunderland",
    "brighton":      "Brighton & Hove Albion",
    "bournemouth":   "Bournemouth",
    "afc bournemouth":"Bournemouth",
}

def _normalize_club(s: str) -> str:
    s = (s or "").lower().strip()
    for junk in (" & ", " and ", "afc ", " afc", "f.c.", " fc"):
        s = s.replace(junk, " ")
    return " ".join(s.split())

def _canonical_club(raw: str) -> str:
    """Map any spelling/abbreviation (FPL's, BSD's, or ours) of a club name to
    our single canonical full name. Falls back to the input unchanged if no
    match is found, rather than silently returning 'Unknown'."""
    if not raw:
        return raw
    norm = _normalize_club(raw)
    if norm in _CLUB_SHORT_ALIASES:
        return _CLUB_SHORT_ALIASES[norm]
    for canon in PL_CANONICAL_TEAMS:
        cn = _normalize_club(canon)
        if norm == cn or norm in cn or cn in norm:
            return canon
    return raw

def _bsd_name(team_name: str) -> str:
    """Kept as a thin alias so existing call sites don't need touching."""
    return _canonical_club(team_name)

def _bsd_lookup(canonical_name: str):
    """Resolve a canonical club name to a BSD team id, retrying with looser
    forms if the exact full name doesn't match BSD's own naming convention.
    Returns (bsd_team_id, bsd_name) or (None, None)."""
    team_id, name = bsd_find_team(canonical_name)
    if team_id:
        return team_id, name
    # BSD may store this club under a shorter/different form — try the first
    # word alone (e.g. "Tottenham" instead of "Tottenham Hotspur").
    first_word = canonical_name.split()[0]
    if first_word != canonical_name:
        team_id, name = bsd_find_team(first_word)
        if team_id:
            return team_id, name
    return None, None

def _team_name(teams: dict, team_id) -> str:
    """teams.get(int) can miss if dict keys came back as strings after a cache
    round-trip — check both key forms before giving up. Always returns our
    canonical full name so display is consistent across every tab."""
    if team_id is None:
        return "Unknown"
    name = teams.get(team_id) or teams.get(str(team_id))
    if name is None:
        try:
            name = teams.get(int(team_id))
        except (TypeError, ValueError):
            name = None
    return _canonical_club(name) if name else "Unknown"

# FPL position codes
POS_MAP = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
POS_LABEL = {1: "Goalkeeper", 2: "Defender", 3: "Midfielder", 4: "Forward"}

# ── FPL data fetch ────────────────────────────────────────────────────────────

def _get_fpl_data() -> dict:
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

PL_LEAGUE_ID_TTL = 86400  # league IDs don't change; refresh daily just in case

def _get_pl_league_id() -> int | None:
    """Resolve the Premier League's real BSD league_id from /api/v2/leagues/
    instead of guessing a hardcoded number. Cached for a day."""
    cache_key = "bsd_pl_league_id_v1"
    cached    = cache_read(cache_key)
    if cached and cache_age(cached) < PL_LEAGUE_ID_TTL:
        return cached.get("league_id")
    try:
        data    = bsd_get("/leagues/", params={"country": "England", "limit": 200})
        leagues = data if isinstance(data, list) else (data or {}).get("results", [])
        for lg in leagues:
            if not lg.get("is_women") and (lg.get("name") or "").strip().lower() == "premier league":
                league_id = lg.get("id")
                cache_write(cache_key, {"league_id": league_id, "_cached_at": time.time()})
                return league_id
    except Exception:
        pass
    return None

def _is_pl(fix: dict) -> bool:
    """BSD v2 events only carry `league_id` (no `competition_id` field exists
    in the schema) — match it against the real, looked-up Premier League id."""
    pl_id = _get_pl_league_id()
    if pl_id is not None:
        return fix.get("league_id") == pl_id
    # Lookup failed (BSD unreachable etc.) — fall back to the old guess
    # rather than silently returning zero fixtures for everyone.
    return str(fix.get("league_id", "")) == "39"

def _fdr(defence: int, is_away: bool) -> int:
    """Map _dynamic_ratings defence score (10-90) to FPL's 1-5 FDR scale.

    Thresholds calibrated against FPL's own FDR ratings for 2026/27:
      ≤30 → FDR 1 (very easy)   e.g. Como, Viking
      ≤48 → FDR 2 (easy)        e.g. Coventry, Hull, newly promoted sides
      ≤63 → FDR 3 (medium)      e.g. Brentford, average mid-table
      ≤78 → FDR 4 (hard)        e.g. Arsenal, Chelsea, strong sides
      >78  → FDR 5 (very hard)  e.g. Man City, Liverpool, elite
    Away penalty reduced to +3 (was +5) — away games are harder but
    the old +5 was pushing nearly every fixture into FDR 4-5.
    """
    adjusted = defence + (3 if is_away else 0)
    if adjusted <= 30: return 1
    if adjusted <= 48: return 2
    if adjusted <= 63: return 3
    if adjusted <= 78: return 4
    return 5

def _fdr_label(fdr: int) -> str:
    return "Easy" if fdr <= 2 else ("Medium" if fdr == 3 else "Hard")

def _fdr_colour(fdr: int) -> str:
    return "green" if fdr <= 2 else ("amber" if fdr == 3 else "red")

FDR_MULTIPLIER = {1: 1.30, 2: 1.15, 3: 1.00, 4: 0.85, 5: 0.70}
EASE_BONUS     = {1: 1.40, 2: 1.20, 3: 1.00}

# ── Known baseline ratings ────────────────────────────────────────────────────
# Used when BSD has < 5 finished matches for a team (e.g. early season,
# UCL clubs whose league isn't scraped, newly promoted sides).
# Tuned against UEFA coefficients and 2025/26 form — update each preseason.
# Format: team_name → (attack, defence) on 10-90 scale.
KNOWN_RATINGS: dict[str, tuple[int, int]] = {
    # Pot 1 / elite
    "Real Madrid":            (88, 88), "Barcelona":           (87, 85),
    "Manchester City":        (87, 86), "Liverpool":           (85, 84),
    "Bayern Munich":          (86, 87), "Paris Saint-Germain": (85, 83),
    "Arsenal":                (82, 82), "Inter Milan":         (80, 85),
    "Atletico Madrid":        (78, 86),
    # Pot 2 / strong
    "Borussia Dortmund":      (80, 78), "Aston Villa":         (78, 76),
    "Manchester United":      (76, 78), "Porto":               (74, 75),
    "Roma":                   (74, 73), "Sporting CP":         (72, 74),
    "Club Brugge":            (68, 70), "Real Betis":          (70, 72),
    "PSV Eindhoven":          (72, 70),
    # Pot 3
    "Napoli":                 (76, 74), "Feyenoord":           (70, 68),
    "Lille":                  (68, 70), "RB Leipzig":          (74, 72),
    "Villarreal":             (72, 70), "Galatasaray":         (68, 66),
    "Fenerbahce":             (66, 65), "Shakhtar Donetsk":    (65, 68),
    "Fenerbahçe SK":          (66, 65), "Galatasaray SK":      (68, 66),
    # Pot 4 / UCL qualifiers
    "Celtic":                 (65, 63), "Slavia Prague":       (60, 62),
    "Sparta Prague":          (60, 62), "Stuttgart":           (70, 68),
    "Como":                   (55, 55), "RC Lens":             (65, 66),
    "AEK Athens":             (55, 58), "LASK":                (52, 55),
    "Slovan Bratislava":      (50, 52), "Viking":              (50, 50),
    "Bodo/Glimt":             (55, 55), "Sabah":               (52, 52),
    # PL clubs — real data takes over after GW5+
    "Chelsea":                (78, 78), "Tottenham Hotspur":   (76, 74),
    "Newcastle United":       (76, 78), "Brighton & Hove Albion": (72, 74),
    "Fulham":                 (68, 70), "Brentford":           (68, 68),
    "Crystal Palace":         (65, 66), "Everton":             (62, 65),
    "Bournemouth":            (65, 64), "Coventry City":       (58, 60),
    "Hull City":              (56, 58), "Ipswich Town":        (58, 58),
    "Leeds United":           (60, 60), "Sunderland":          (58, 58),
    "Nottingham Forest":      (62, 64),
}

# Minimum matches required before _dynamic_ratings is trusted over KNOWN_RATINGS.
# Below this threshold, too few data points → formula clamps everything to 90.
MIN_MATCHES_FOR_DYNAMIC = 5


def _baseline_defence(team_name: str) -> int:
    """Return the known-ratings defence for a team, or league-average fallback."""
    entry = KNOWN_RATINGS.get(team_name) or KNOWN_RATINGS.get(
        _canonical_club(team_name)
    )
    if entry:
        return entry[1]
    return 65  # generic mid-table fallback → FDR 3


def _get_opponent_defence(opp_id: int, opp_name: str = "") -> int:
    """Fetch real defence rating from BSD match history.

    Strategy:
      1. Fetch finished fixtures from BSD (any league — not just PL).
         This covers UCL sides (Bayern, PSG etc.) whose PL filter returns 0.
      2. If ≥ MIN_MATCHES_FOR_DYNAMIC matches found → use _dynamic_ratings().
         With fewer matches the formula clamps everything to 90 (1-match
         conceded-0 teams all come out DEF=90 — not meaningful).
      3. If < MIN_MATCHES_FOR_DYNAMIC → use KNOWN_RATINGS baseline instead.
    """
    try:
        now    = datetime.now(timezone.utc).strftime("%Y-%m-%dT23:59:59Z")
        # No league_id filter — fetch across all competitions so UCL/European
        # clubs return data even when they have no PL matches.
        params = {
            "status": "finished", "limit": 40,
            "date_from": "2025-08-01T00:00:00Z", "date_to": now,
        }
        data = bsd_get(f"/teams/{opp_id}/fixtures/", params=params)
        if not data:
            return _baseline_defence(opp_name)

        raw = data if isinstance(data, list) else data.get("results", [])
        matches = []
        for fix in raw:
            is_home  = fix.get("home_team_id") == opp_id
            scored   = fix.get("home_score" if is_home else "away_score")
            conceded = fix.get("away_score" if is_home else "home_score")
            if scored is not None and conceded is not None:
                matches.append({
                    "scored": scored, "conceded": conceded,
                    "result": "W" if scored > conceded else
                              ("D" if scored == conceded else "L"),
                    "formation": "",
                })

        if len(matches) >= MIN_MATCHES_FOR_DYNAMIC:
            _, defence = _dynamic_ratings(matches)
            return int(defence)

        # Not enough data — blend known baseline with whatever little we have.
        # This prevents a team that conceded 0 in GW1 from getting DEF=90.
        baseline = _baseline_defence(opp_name)
        if matches:
            _, dynamic = _dynamic_ratings(matches)
            # Weight: 80% baseline, 20% early-season dynamic
            blended = int(0.80 * baseline + 0.20 * dynamic)
            return max(10, min(90, blended))
        return baseline

    except Exception:
        pass
    return _baseline_defence(opp_name)

def _next_fixture(bsd_team_id: int) -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fixes = []
    pl_id = _get_pl_league_id()

    param_sets = [
        {"status": "notstarted", "limit": 15, "date_from": today},
        {"limit": 20, "date_from": today},
    ]
    if pl_id is not None:
        for p in param_sets:
            p["league_id"] = pl_id

    for params in param_sets:
        d = bsd_get(f"/teams/{bsd_team_id}/fixtures/", params=params)
        if d:
            f = d if isinstance(d, list) else d.get("results", [])
            if f:
                # Python-side filtering using league_id
                pl_fixes = [x for x in f if _is_pl(x)]
                fixes = pl_fixes
                if fixes:
                    break
                
    if not fixes:
        return {}
        
    fixes.sort(key=lambda f: f.get("event_date") or "")
    nf      = fixes[0]
    is_home = nf.get("home_team_id") == bsd_team_id
    opp_id  = nf.get("away_team_id" if is_home else "home_team_id") or 0
    opp_name= nf.get("away_team" if is_home else "home_team", "Unknown")
    opp_def = _get_opponent_defence(opp_id, opp_name)
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
    """Season-aware FPL scoring.

    Early season (mins < 90): ep_next dominates — only GW1 data exists,
    so expected points next game is the most reliable signal.

    Mid season (90-899 mins, ~1-9 GWs): blend form + ppg + underlying
    stats. Form is the FPL rolling 5-GW average — reliable now.

    Established season (900+ mins, ~10+ GWs): full weighting on all
    metrics. xG/xA per 90 are meaningful with enough sample size.
    """
    ppg   = float(p.get("points_per_game") or 0)
    xg90  = float(p.get("expected_goals_per_90") or 0)
    xa90  = float(p.get("expected_assists_per_90") or 0)
    form  = float(p.get("form") or 0)
    ep    = float(p.get("ep_next") or 0)
    mins  = int(p.get("minutes") or 0)

    if mins >= 900:
        # 10+ GWs: full metric weighting
        score = ppg * 2.0 + xg90 * 3.0 + xa90 * 2.0 + form * 0.5 + ep * 0.3
    elif mins >= 90:
        # 1-9 GWs: form + ppg lead, xG/xA as supporting signals
        score = form * 1.5 + ppg * 1.5 + xg90 * 1.5 + xa90 * 1.0 + ep * 0.5
    else:
        # Pre-season / GW0: expected points only
        score = ep * 1.5 + ppg * 0.5
    return round(score, 3)

def _build_player(p: dict, teams: dict) -> dict:
    return {
        "id":          p.get("id"),
        "name":        p.get("known_name") or p.get("web_name") or
                       f"{p.get('first_name','')} {p.get('second_name','')}".strip(),
        "team_id":     p.get("team"),
        "team":        _team_name(teams, p.get("team")),
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
        parts.append(f"{player['ppg']} pts/game this season")
    if player["xg90"] >= 0.3:
        parts.append(f"{player['xg90']:.2f} xG/90")
    if player["xa90"] >= 0.2:
        parts.append(f"{player['xa90']:.2f} xA/90")
    if player["form"] > 0:
        parts.append(f"form {player['form']}")
    form_str = ", ".join(parts) if parts else "consistent performer"
    fix_str  = f"{fdr_label.lower()} fixture ({venue} vs {opp}, FDR {fdr})"
    return f"{form_str.capitalize()} · {fix_str}."


# ── Player list (for squad-builder search UI) ─────────────────────────────────

@router.get("/fpl/players")
def player_list():
    """Minimal player list for client-side search — id, name, team, position, price."""
    cache_key = "fpl_player_list_v1"
    cached    = cache_read(cache_key)
    if cached and cache_age(cached) < FPL_TTL:
        cached["cached"] = True
        return cached

    fpl   = _get_fpl_data()
    teams = fpl["teams"]
    out = []
    for p in fpl["players"]:
        display_name = (p.get("known_name") or p.get("web_name") or
                        f"{p.get('first_name','')} {p.get('second_name','')}".strip())
        out.append({
            "id":          p.get("id"),
            "name":        display_name,
            "search_name": _normalize_str(display_name),
            "team":        _team_name(teams, p.get("team")),
            "position":    POS_MAP.get(p.get("element_type"), "UNK"),
            "price":       round((p.get("now_cost") or 0) / 10, 1),
            "status":      p.get("status", "a"),
        })
    result = {"players": out, "cached": False, "_cached_at": time.time()}
    cache_write(cache_key, result)
    return result


# ── Step 1: Fixture Ticker ────────────────────────────────────────────────────

@router.get("/fpl/fixtures")
def fixture_ticker(
    team:  str  = Query(..., description="Club name e.g. Arsenal, Liverpool"),
    gws:   int  = Query(38, description="Gameweeks to show", ge=1, le=50),
    debug: bool = Query(False, description="Return raw unfiltered BSD fixture data for diagnosis"),
):
    team_id, bsd_name = _bsd_lookup(_canonical_club(team))
    if not team_id:
        raise HTTPException(404, f"Team '{team}' not found.")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw   = []
    pl_id = _get_pl_league_id()

    param_sets = [
        {"status": "notstarted", "limit": min(gws+30, 200), "date_from": today},
        {"limit": min(gws+30, 200), "date_from": today},
        {"team_id": team_id, "date_from": today, "status":"notstarted","limit":min(gws+30, 200)},
    ]
    if pl_id is not None:
        for p in param_sets:
            p["league_id"] = pl_id

    for params in param_sets:
        path = f"/teams/{team_id}/fixtures/" if "team_id" not in params else "/events/"
        d    = bsd_get(path, params=params)
        if d:
            r = d if isinstance(d, list) else d.get("results", [])
            if r:
                raw = r
                break

    if debug:
        # Return the first 2 raw fixtures completely unfiltered so we can see
        # exactly which fields BSD populates on not-yet-started fixtures.
        return {
            "team": team, "bsd_team_id": team_id, "bsd_name": bsd_name,
            "resolved_pl_league_id": pl_id,
            "raw_count": len(raw), "sample": raw[:2],
        }

    # Bumped to v6 to immediately flush out your 404 cached error
    cache_key = f"fpl_fixtures_v7__{team.lower().replace(' ','_')}"
    cached    = cache_read(cache_key)
    if cached and cache_age(cached) < FDR_TTL:
        cached["cached"] = True
        return cached

    # Filtering exclusively by league ID locally
    pl_matches = [fix for fix in raw if _is_pl(fix)]
    raw = pl_matches

    if not raw:
        raise HTTPException(404, f"No upcoming Premier League fixtures for '{team}'.")

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
            
        opp_def = _get_opponent_defence(opp_id, opp_name)
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


# ── Step 2: Captain Pick ──────────────────────────────────────────────────────

@router.get("/fpl/captain")
def captain_pick(
    team: str = Query(..., description="FPL club name e.g. Arsenal"),
    top:  int = Query(5, ge=1, le=10),
):
    cache_key = f"fpl_captain_v6__{team.lower().replace(' ','_')}"
    cached    = cache_read(cache_key)
    if cached and cache_age(cached) < CAP_TTL:
        cached["cached"] = True
        return cached

    fpl   = _get_fpl_data()
    teams = fpl["teams"]

    fpl_team_id = None
    # Normalized matching using diacritic‑insensitive helper and short aliases
    normalized_target = _normalize_str(team)
    # First pass: exact or startswith match on canonical names
    for tid, tname in teams.items():
        canonical = _bsd_name(tname)
        norm_canonical = _normalize_str(canonical)
        if norm_canonical == normalized_target or norm_canonical.startswith(normalized_target[:4]):
            fpl_team_id = tid
            break
    # Second pass: fallback to substring match
    if not fpl_team_id:
        for tid, tname in teams.items():
            canonical = _bsd_name(tname)
            norm_canonical = _normalize_str(canonical)
            if normalized_target in norm_canonical or norm_canonical in normalized_target:
                fpl_team_id = tid
                break
        # Check short alias map if still not found
        if not fpl_team_id:
            for alias, full in _CLUB_SHORT_ALIASES.items():
                if normalized_target == _normalize_str(alias) or normalized_target in _normalize_str(full):
                    for tid, tname in teams.items():
                        if _normalize_str(_bsd_name(tname)) == _normalize_str(full):
                            fpl_team_id = tid
                            break
                    if fpl_team_id:
                        break
    if not fpl_team_id:
        raise HTTPException(404, f"'{team}' not found in FPL data.")

    # teams.items() keys can come back as strings after a cache round-trip;
    # player records always use int team ids — normalize before comparing.
    try:
        fpl_team_id = int(fpl_team_id)
    except (TypeError, ValueError):
        pass

    team_name = _team_name(teams, fpl_team_id)

    bsd_team_id, _ = _bsd_lookup(_bsd_name(team_name))
    nf  = _next_fixture(bsd_team_id) if bsd_team_id else {}
    fdr = nf.get("fdr", 3)

    squad = [
        _build_player(p, teams)
        for p in fpl["players"]
        if int(p.get("team") or -1) == fpl_team_id
        and p.get("element_type") in {3, 4}
        and p.get("status") != "u"
        # Pre-season every player shows 0 minutes — don't require minutes
        # already played, just that they're an active squad player.
        and (p.get("status") == "a" or int(p.get("minutes") or 0) > 0)
    ]

    if not squad:
        raise HTTPException(404, f"No attacking players found for {team_name}.")

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


# ── Step 3: Transfer Recommender ──────────────────────────────────────────────

@router.get("/fpl/transfers")
def transfer_recommender(
    position: str = Query("FWD", description="GKP, DEF, MID, or FWD"),
    min_price: float = Query(0.0,  description="Min price in £m", ge=0, le=20),
    max_price: float = Query(15.0, description="Max price in £m", ge=3, le=30),
    limit:     int   = Query(10,   description="Results to return", ge=3, le=25),
):
    pos_upper = position.strip().upper()
    pos_id_map = {"GKP":1,"DEF":2,"MID":3,"FWD":4}
    if pos_upper not in pos_id_map:
        raise HTTPException(400, "position must be GKP, DEF, MID, or FWD")
    pos_id = pos_id_map[pos_upper]

    cache_key = f"fpl_trans_v4__{pos_upper}__{min_price}__{max_price}"
    cached    = cache_read(cache_key)
    if cached and cache_age(cached) < TRANS_TTL:
        cached["cached"] = True
        return cached

    fpl   = _get_fpl_data()
    teams = fpl["teams"]

    candidates_raw = [
        p for p in fpl["players"]
        if p.get("element_type") == pos_id
        and p.get("status") != "u"
        and min_price <= (p.get("now_cost") or 0) / 10 <= max_price
        and (p.get("status") == "a" or int(p.get("minutes") or 0) > 0)
    ]

    team_fixtures: dict[int, dict] = {}
    results = []

    for p in candidates_raw:
        tid  = p.get("team")
        if tid not in team_fixtures:
            bsd_id, _ = _bsd_lookup(_bsd_name(_team_name(teams, tid)))
            team_fixtures[tid] = _next_fixture(bsd_id) if bsd_id else {}
        nf      = team_fixtures[tid]
        fdr     = nf.get("fdr", 3)
        player  = _build_player(p, teams)
        player["next_fixture"]   = nf
        player["weighted_score"] = round(player["fpl_score"] * FDR_MULTIPLIER.get(fdr,1.0), 3)
        player["reason"]         = _reason_fpl(player, fdr, _fdr_label(fdr),
                                                nf.get("opponent","?"), nf.get("venue","H"))
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


# ── Step 4: Differential Finder ───────────────────────────────────────────────

@router.get("/fpl/differentials")
def differential_finder(
    position:      str   = Query("FWD", description="GKP, DEF, MID, or FWD"),
    max_ownership: float = Query(15.0, description="Max ownership %", ge=0.5, le=50),
    max_price:     float = Query(8.0,  description="Max price in £m", ge=3, le=20),
    limit:         int   = Query(8,    description="Results to return", ge=3, le=20),
):
    pos_upper = position.strip().upper()
    pos_id_map = {"GKP":1,"DEF":2,"MID":3,"FWD":4}
    if pos_upper not in pos_id_map:
        raise HTTPException(400, "position must be GKP, DEF, MID, or FWD")
    pos_id = pos_id_map[pos_upper]

    cache_key = f"fpl_diff_v4__{pos_upper}__{max_ownership}__{max_price}"
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
        and (
            p.get("status") == "a"
            or (int(p.get("minutes") or 0) > 450 and float(p.get("points_per_game") or 0) >= 3.0)
        )
    ]

    team_fixtures: dict[int, dict] = {}
    results = []

    for p in candidates_raw:
        tid = p.get("team")
        if tid not in team_fixtures:
            bsd_id, _ = _bsd_lookup(_bsd_name(_team_name(teams, tid)))
            team_fixtures[tid] = _next_fixture(bsd_id) if bsd_id else {}
        nf  = team_fixtures[tid]
        fdr = nf.get("fdr", 3)
        if fdr > 3:
            continue

        player  = _build_player(p, teams)
        ease    = EASE_BONUS.get(fdr, 1.0)
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


# ── Step 5: Squad Analysis ("My Squad" mode) ──────────────────────────────────
# Standard FPL squad composition — always 15 players, this exact split.
SQUAD_COMPOSITION = {"GKP": 2, "DEF": 5, "MID": 5, "FWD": 3}
SQUAD_TTL = 900
UPGRADE_THRESHOLD = 1.15   # candidate must score 15%+ higher to be worth flagging
BAD_STATUS = {"i", "s", "u", "n"}  # injured / suspended / unavailable / not eligible

def _team_next_fixture_cached(team_id: int, teams: dict, cache: dict) -> dict:
    if team_id not in cache:
        bsd_id, _ = _bsd_lookup(_bsd_name(_team_name(teams, team_id)))
        cache[team_id] = _next_fixture(bsd_id) if bsd_id else {}
    return cache[team_id]

def _score_player(p: dict, teams: dict, fixture_cache: dict) -> dict:
    """Build a player dict with next_fixture + weighted_score attached."""
    player = _build_player(p, teams)
    nf     = _team_next_fixture_cached(player["team_id"], teams, fixture_cache)
    fdr    = nf.get("fdr", 3)
    player["next_fixture"]   = nf
    player["weighted_score"] = round(player["fpl_score"] * FDR_MULTIPLIER.get(fdr, 1.0), 3)
    player["reason"] = _reason_fpl(player, fdr, _fdr_label(fdr),
                                    nf.get("opponent", "?"), nf.get("venue", "H"))
    return player

def _best_starting_xi(squad: list[dict]) -> tuple[list[dict], list[dict], str]:
    """Pick the highest-scoring valid FPL formation (1 GKP + 10 outfield,
    DEF 3-5 / MID 2-5 / FWD 1-3) from a 15-man squad."""
    by_pos = {"GKP": [], "DEF": [], "MID": [], "FWD": []}
    for p in squad:
        by_pos.setdefault(p["position"], []).append(p)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda p: p["weighted_score"], reverse=True)

    best_gkp = by_pos["GKP"][0] if by_pos["GKP"] else None
    bench_gkp = by_pos["GKP"][1] if len(by_pos["GKP"]) > 1 else None

    best_total   = -1.0
    best_combo   = None
    best_formation = ""
    for d in range(3, 6):
        for m in range(2, 6):
            f = 10 - d - m
            if f < 1 or f > 3:
                continue
            if d > len(by_pos["DEF"]) or m > len(by_pos["MID"]) or f > len(by_pos["FWD"]):
                continue
            defs = by_pos["DEF"][:d]
            mids = by_pos["MID"][:m]
            fwds = by_pos["FWD"][:f]
            total = sum(p["weighted_score"] for p in defs + mids + fwds)
            if total > best_total:
                best_total   = total
                best_combo   = (defs, mids, fwds)
                best_formation = f"{d}-{m}-{f}"

    if not best_combo or not best_gkp:
        raise HTTPException(400, "Squad doesn't have enough players in a valid position "
                                  "split to build a starting XI (need at least 1 GKP, "
                                  "3 DEF, 2 MID, 1 FWD).")

    defs, mids, fwds = best_combo
    starting = [best_gkp] + defs + mids + fwds
    starting_ids = {p["id"] for p in starting}
    bench_outfield = sorted(
        [p for p in squad if p["id"] not in starting_ids and p["position"] != "GKP"],
        key=lambda p: p["weighted_score"], reverse=True,
    )
    bench = ([bench_gkp] if bench_gkp else []) + bench_outfield
    return starting, bench, best_formation

@router.get("/fpl/squad")
def squad_analysis(
    player_ids: str = Query(..., description="15 comma-separated FPL player IDs"),
    bank:       float = Query(0.0, description="Money left in the bank, £m", ge=0, le=50),
):
    try:
        ids = [int(x.strip()) for x in player_ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "player_ids must be a comma-separated list of integers.")
    if len(ids) != 15 or len(set(ids)) != 15:
        raise HTTPException(400, f"Expected exactly 15 unique player IDs, got {len(set(ids))}.")

    cache_key = f"fpl_squad_v1__{'_'.join(map(str, sorted(ids)))}__{bank}"
    cached    = cache_read(cache_key)
    if cached and cache_age(cached) < SQUAD_TTL:
        cached["cached"] = True
        return cached

    fpl   = _get_fpl_data()
    teams = fpl["teams"]
    by_id = {p["id"]: p for p in fpl["players"]}

    missing = [i for i in ids if i not in by_id]
    if missing:
        raise HTTPException(404, f"Unknown FPL player ID(s): {missing}")

    fixture_cache: dict = {}
    squad = [_score_player(by_id[i], teams, fixture_cache) for i in ids]

    counts = {"GKP": 0, "DEF": 0, "MID": 0, "FWD": 0}
    for p in squad:
        counts[p["position"]] = counts.get(p["position"], 0) + 1
    if counts != SQUAD_COMPOSITION:
        raise HTTPException(
            400,
            f"Squad must be 2 GKP / 5 DEF / 5 MID / 3 FWD. Got "
            f"{counts['GKP']} GKP / {counts['DEF']} DEF / {counts['MID']} MID / {counts['FWD']} FWD."
        )

    starting, bench, formation = _best_starting_xi(squad)
    ranked_starting = sorted(starting, key=lambda p: p["weighted_score"], reverse=True)
    captain    = ranked_starting[0]
    vice       = ranked_starting[1] if len(ranked_starting) > 1 else None

    # ── Transfer suggestions ──────────────────────────────────────────────────
    pos_id_map = {"GKP": 1, "DEF": 2, "MID": 3, "FWD": 4}
    squad_ids  = set(ids)
    transfer_suggestions = []

    # Prepare team count mapping for 3‑player rule
    team_counts = _team_counts(squad)
    for out_p in squad:
        pos_id      = pos_id_map[out_p["position"]]
        max_budget  = round(out_p["price"] + bank, 1)
        is_flagged  = out_p["status"] in BAD_STATUS

        raw_candidates = [
            c for c in fpl["players"]
            if c.get("element_type") == pos_id
            and c.get("id") not in squad_ids
            and c.get("status") == "a"
            and (c.get("now_cost") or 0) / 10 <= max_budget
        ]
        # cheap pre-sort by raw fpl_score, only fixture-score the top few
        raw_candidates.sort(key=lambda c: _fpl_score(c), reverse=True)
        top_raw = raw_candidates[:8]
        scored_candidates = [_score_player(c, teams, fixture_cache) for c in top_raw]
        scored_candidates.sort(key=lambda p: p["weighted_score"], reverse=True)

        if not scored_candidates:
            continue
        best_candidate = scored_candidates[0]

        is_upgrade = best_candidate["weighted_score"] >= out_p["weighted_score"] * UPGRADE_THRESHOLD
        # Enforce max 3 players per real‑life team
        candidate_team = best_candidate.get("team")
        if candidate_team is not None and team_counts.get(candidate_team, 0) >= 3:
            # Skip candidate that would exceed team limit
            continue
        if not (is_flagged or is_upgrade):
            continue

        if is_flagged:
            status_label = {"i": "injured", "s": "suspended", "u": "unavailable", "n": "not eligible"}
            reason = (f"{out_p['name']} is {status_label.get(out_p['status'], 'flagged')} "
                      f"({out_p['news'] or 'no fitness update'}). ")
        else:
            reason = f"{out_p['name']}'s score ({out_p['weighted_score']}) is below reach. "
        reason += (f"{best_candidate['name']} ({best_candidate['team']}, £{best_candidate['price']}m) "
                   f"scores {best_candidate['weighted_score']} with a "
                   f"{_fdr_label(best_candidate['next_fixture'].get('fdr', 3)).lower()} fixture "
                   f"and fits your budget (£{max_budget}m).")

        transfer_suggestions.append({
            "out": out_p,
            "in": best_candidate,
            "reason": reason,
            "flagged": is_flagged,
            "suggested_chip": _suggest_chip(_current_gameweek()),
        })

    transfer_suggestions.sort(key=lambda t: (not t["flagged"],
                               -(t["in"]["weighted_score"] - t["out"]["weighted_score"])))

    squad_value = round(sum(p["price"] for p in squad), 1)
    cap_text = (f"🎯 My Squad Captain Pick: {captain['name']} ({captain['team']})\n"
                f"{formation} · squad value £{squad_value}m + £{bank}m bank\n")
    if transfer_suggestions:
        t = transfer_suggestions[0]
        cap_text += f"Suggested transfer: {t['out']['name']} ➡ {t['in']['name']}\n"
    cap_text += "via @TacticaEngine · app.tactica.com.ng/fpl #FPL #MySquad"

    # Chip suggestion based on current gameweek
    current_gw = fpl.get("current_event") or 0
    if current_gw <= 2:
        chip = "Wildcard"
    elif current_gw >= 20:
        chip = "Bench Boost"
    else:
        chip = None

    result = {
        "formation": formation,
        "starting_xi": starting,
        "bench": bench,
        "captain": captain,
        "vice_captain": vice,
        "squad_value": squad_value,
        "bank": bank,
        "transfer_suggestions": transfer_suggestions,
        "share_text": cap_text,
        "chip_suggestion": chip,
        "cached": False, "_cached_at": time.time(),
    }
    cache_write(cache_key, result)
    return result
