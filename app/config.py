"""
app/config.py — shared constants, BSD client, position helpers
"""
import os, json, time, difflib
import requests as _requests

# ── API keys (set as Railway environment variables) ──────────────────────────
BSD_KEY     = os.environ.get("BSD_API_KEY", "")
GEMINI_KEY  = os.environ.get("GEMINI_API_KEY", "")
BSD_BASE    = "https://sports.bzzoiro.com/api/v2"
BSD_HEADERS = {"Authorization": f"Token {BSD_KEY}"}

# ── Formation map (code → name) ───────────────────────────────────────────────
FORMATIONS = {
    0:"3-4-3",  1:"3-5-2",  2:"3-4-1-2", 3:"3-2-4-1", 4:"3-4-2-1",
    5:"3-3-1-3",6:"4-2-3-1",7:"4-3-3",   8:"4-4-2",   9:"4-4-2 Diamond",
    10:"4-1-4-1",11:"4-3-2-1",12:"4-2-2-2",13:"5-3-2",14:"5-4-1",
    15:"5-2-2-1",16:"5-2-3",
}
FORMATION_NAME_TO_CODE = {v: k for k, v in FORMATIONS.items()}

# ── League ID → name (BSD events list returns league_id only) ─────────────────
LEAGUE_WEIGHTS: dict[str, float] = {
    # England (Premier League, Championship)
    "ENG": 1.00,
    # Spain (La Liga)
    "ESP": 0.97,
    # Germany (Bundesliga)
    "GER": 0.95,
    # Italy (Serie A)
    "ITA": 0.94,
    # France (Ligue 1)
    "FRA": 0.91,
    # Portugal (Primeira Liga)
    "POR": 0.88,
    # Netherlands (Eredivisie)
    "NED": 0.87,
    # Belgium (Jupiler Pro League)
    "BEL": 0.85,
    # Turkey (Süper Lig)
    "TUR": 0.84,
    # Russia / Ukraine / Greece
    "RUS": 0.82,
    "UKR": 0.82,
    "GRE": 0.81,
    # Scotland, Czech Republic, Austria
    "SCO": 0.80,
    "CZE": 0.80,
    "AUT": 0.79,
    # Brazil (Brasileirão)
    "BRA": 0.84,
    # Argentina (Liga Profesional)
    "ARG": 0.82,
    # Mexico (Liga MX)
    "MEX": 0.80,
    # USA (MLS)
    "USA": 0.78,
    # Saudi Arabia (Pro League)
    "KSA": 0.76,
    "SAU": 0.76,
    # Japan (J-League)
    "JPN": 0.77,
    # South Korea (K-League)
    "KOR": 0.77,
    # All other countries outside top leagues
    "__default__": 0.74,
}

# ── League quality weight (for national team rating calc) ────────────────────
LEAGUE_WEIGHTS = {
    "Premier League": 1.00, "La Liga": 0.98, "Bundesliga": 0.96,
    "Serie A": 0.95, "Ligue 1": 0.93, "Champions League": 1.05,
    "Europa League": 0.97, "Eredivisie": 0.88, "Primeira Liga": 0.87,
    "Scottish Premiership": 0.82, "Belgian Pro League": 0.84,
    "Süper Lig": 0.85, "Austrian Bundesliga": 0.80,
}

# ── Position mapping from BSD specific_position ──────────────────────────────
SPECIFIC_POS_MAP = {
    "GK":"GK",
    "CB":"DF","RB":"DF","LB":"DF","RWB":"DF","LWB":"DF","SW":"DF",
    "CM":"MF","CDM":"MF","DM":"MF","CAM":"MF","AM":"MF",
    # Wide players → FW in modern football (Saka = RM, Mbappe = LW, etc.)
    "RM":"FW","LM":"FW","RW":"FW","LW":"FW","RWF":"FW","LWF":"FW",
    "ST":"FW","CF":"FW","SS":"FW",
}
GENERIC_POS_MAP = {"G":"GK","D":"DF","M":"MF","F":"FW"}

def resolve_position(generic: str, specific: str) -> str:
    """Return internal position (GK/DF/MF/FW) using specific_position first."""
    if specific:
        sp = specific.strip().upper()
        if sp in SPECIFIC_POS_MAP:
            return SPECIFIC_POS_MAP[sp]
    return GENERIC_POS_MAP.get((generic or "M").strip().upper(), "MF")

# ── BSD HTTP helpers ──────────────────────────────────────────────────────────
def bsd_get(path: str, params: dict = None) -> dict | None:
    """GET from BSD API. Returns parsed JSON or None on error."""
    try:
        r = _requests.get(
            f"{BSD_BASE}{path}",
            headers=BSD_HEADERS,
            params=params,
            timeout=12,
        )
        if r.status_code == 200:
            return r.json()
        return None
    except Exception:
        return None

def bsd_find_team(name: str) -> tuple[int | None, str | None]:
    """
    Search BSD for a team by name. Returns (team_id, matched_name) or (None, None).
    Uses GET /api/v2/teams/?name={name}&limit=3  (partial, case-insensitive)
    """
    data = bsd_get("/teams/", params={"name": name, "limit": 3})
    if not data:
        return None, None
    results = data.get("results", [])
    if not results:
        return None, None
    # Fuzzy-match the closest name
    names_lower = [t["name"].lower() for t in results]
    best = difflib.get_close_matches(name.lower(), names_lower, n=1, cutoff=0.35)
    if best:
        for t in results:
            if t["name"].lower() == best[0]:
                return t["id"], t["name"]
    return results[0]["id"], results[0]["name"]

# ── Cache helpers (file-based, Railway persists /app volume) ─────────────────
CACHE_DIR = os.environ.get("CACHE_DIR", "/tmp/tactica_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

def cache_read(key: str) -> dict | None:
    path = os.path.join(CACHE_DIR, f"{key}.json")
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return None

def cache_write(key: str, data: dict):
    path = os.path.join(CACHE_DIR, f"{key}.json")
    try:
        json.dump(data, open(path, "w", encoding="utf-8"), indent=2)
    except Exception:
        pass

def cache_age(entry: dict) -> float:
    """Return seconds since entry was cached."""
    return time.time() - entry.get("_cached_at", 0)
