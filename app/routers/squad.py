"""
GET /api/squad?team={name}
Fetches squad from BSD, saves to cache + players.json
Returns: { team_name, bsd_name, count, players[] }
"""
import time, json, os
from fastapi import APIRouter, Query, HTTPException
from app.config import bsd_get, bsd_find_team, cache_read, cache_write, cache_age
from app.config import SPECIFIC_POS_MAP, GENERIC_POS_MAP, resolve_position

router   = APIRouter()
PLAYERS  = os.environ.get("PLAYERS_PATH", "players.json")
SQUAD_TTL = 604800  # 7 days

def _load_players():
    try: return json.load(open(PLAYERS, encoding="utf-8"))
    except: return {}

def _save_players(db):
    try: json.dump(db, open(PLAYERS,"w",encoding="utf-8"), indent=2, ensure_ascii=False)
    except: pass

@router.get("/squad")
def squad(team: str = Query(..., description="Team name (any European club or national team)")):
    cache_key = f"squad__{team.lower().replace(' ','_')}"
    cached    = cache_read(cache_key)

    if cached and cache_age(cached) < SQUAD_TTL:
        return {
            "team_name": team,
            "bsd_name":  cached.get("bsd_name", team),
            "count":     len(cached.get("players",[])),
            "players":   cached.get("players",[]),
            "cached":    True,
        }

    # Resolve team_id
    team_id, bsd_name = bsd_find_team(team)
    if not team_id:
        raise HTTPException(status_code=404,
            detail=f"Team '{team}' not found in BSD. Try a slightly different spelling.")

    # GET /api/v2/players/?team_id={id}&limit=100
    data = bsd_get("/players/", params={"team_id": team_id, "limit": 100})
    if not data:
        raise HTTPException(status_code=502, detail="BSD API error fetching squad.")

    players = []
    for p in data.get("results", []):
        name = p.get("name") or p.get("short_name","")
        if not name or name.strip() in ("","None","null"):
            continue
        spec = str(p.get("specific_position","")).strip().upper()
        gen  = str(p.get("position","M")).strip().upper()
        players.append({
            "Name":    name.strip(),
            "Pos":     resolve_position(gen, spec),
            "SpecPos": spec or gen,
            "Min":     0,
            "G_A":     0,
        })

    # Save to cache and players.json
    entry = {"_cached_at": time.time(), "bsd_name": bsd_name, "players": players}
    cache_write(cache_key, entry)

    db = _load_players()
    db[team] = players
    _save_players(db)

    return {
        "team_name": team,
        "bsd_name":  bsd_name,
        "count":     len(players),
        "players":   players,
        "cached":    False,
    }
