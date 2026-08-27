"""
GET /api/live?home={team}&away={team}
Returns: { match_found, home_team, away_team, home_score, away_score,
           minute, competition, status, cached, last_updated }

BSD live endpoint: GET /api/v2/events/live/
Response: { count, events: [{home_team, away_team, home_score, away_score,
            current_minute, league_name, status, last_updated}] }
Redis TTL on BSD side = 30s — no point polling faster than that.
"""
import time
import difflib
from fastapi import APIRouter, Query, HTTPException
from app.config import bsd_get, cache_read, cache_write, cache_age

router = APIRouter()
LIVE_TTL = 30  # seconds — matches BSD Redis TTL

def _fuzzy_match(query: str, candidate: str) -> bool:
    q, c = query.lower().strip(), candidate.lower().strip()
    return q in c or c in q or bool(difflib.get_close_matches(q, [c], n=1, cutoff=0.6))

@router.get("/live")
def live(
    home: str = Query(..., description="Home team name"),
    away: str = Query(..., description="Away team name"),
):
    cache_key = f"live__{home.lower()}__vs__{away.lower()}"
    cached    = cache_read(cache_key)

    # Return cache if fresh enough
    if cached and cache_age(cached) < LIVE_TTL:
        cached["cached"] = True
        return cached

    # Call BSD
    data = bsd_get("/events/live/")
    if data is None:
        # BSD unreachable — return stale cache or 503
        if cached:
            cached["cached"] = True
            cached["stale"]  = True
            return cached
        raise HTTPException(status_code=503, detail="BSD API unreachable.")

    events = data.get("events", [])

    for match in events:
        hn = match.get("home_team","")
        an = match.get("away_team","")
        home_hit = _fuzzy_match(home, hn) or _fuzzy_match(home, an)
        away_hit = _fuzzy_match(away, hn) or _fuzzy_match(away, an)

        if home_hit and away_hit:
            result = {
                "_cached_at":  time.time(),
                "match_found": True,
                "home_team":   hn,
                "away_team":   an,
                "home_score":  match.get("home_score") or 0,
                "away_score":  match.get("away_score") or 0,
                "minute":      match.get("current_minute") or 0,
                "competition": match.get("league_name","Unknown"),
                "status":      match.get("status","inprogress"),
                "last_updated":match.get("last_updated",""),
                "cached":      False,
            }
            cache_write(cache_key, result)
            return result

    # No match found — cache the negative result too
    result = {
        "_cached_at":   time.time(),
        "match_found":  False,
        "live_count":   len(events),
        "cached":       False,
    }
    cache_write(cache_key, result)
    return result
