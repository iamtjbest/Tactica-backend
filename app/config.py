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
LEAGUE_NAMES = {
    17:"Premier League", 8:"La Liga", 5:"Bundesliga", 11:"Serie A",
    4:"Ligue 1", 2:"Champions League", 3:"Europa League",
    848:"Conference League", 88:"Eredivisie", 94:"Primeira Liga",
    39:"Scottish Premiership", 144:"Belgian Pro League",
    203:"Süper Lig", 197:"Austrian Bundesliga",
}

# BSD sometimes has a women's-team entry that shares the exact same name
# as the men's team with zero distinguishing text (e.g. bsd_name comes
# back as plain "Real Sociedad" for the women's side too) — no name-based
# filter can catch that. league_id is a much stronger, name-independent
# signal. Confirmed live via /form's _debug block on 2026-09-17: team_id
# 924 ("Real Sociedad") returned 33 fixtures ALL tagged league_id 36,
# against opponents including Atlético Madrid and Barcelona — both clubs
# with strong women's sides — confirming league_id 36 is Spain's Liga F
# (women's top flight), not the men's La Liga (league_id 8).
WOMENS_LEAGUE_IDS = {36}

# Captures the raw /teams/ search results from the most recent
# bsd_find_team() call, for debugging team-resolution issues without
# needing to change bsd_find_team's return signature everywhere it's
# called. Read via get_last_team_search_debug().
_last_team_search_debug: dict = {}

def get_last_team_search_debug() -> dict:
    return _last_team_search_debug

# ── League quality weight (for national team rating calc) ────────────────────
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


# ── Position mapping from BSD specific_position ──────────────────────────────
# Exact abbreviation matches (fast path)
SPECIFIC_POS_MAP = {
    "GK":"GK",
    "CB":"DF","RB":"DF","LB":"DF","RWB":"DF","LWB":"DF","SW":"DF",
    "CM":"MF","CDM":"MF","DM":"MF","CAM":"MF","AM":"MF",
    # Wide players → FW in modern football (Saka = RM, Mbappe = LW, etc.)
    "RM":"FW","LM":"FW","RW":"FW","LW":"FW","RWF":"FW","LWF":"FW",
    "ST":"FW","CF":"FW","SS":"FW",
}
GENERIC_POS_MAP = {"G":"GK","D":"DF","M":"MF","F":"FW"}

# Fallback keyword matching for full-word labels some providers send
# instead of abbreviations (e.g. "Goalkeeper", "Centre Back", "Right Wing").
# Checked as substrings, most specific first, so "Defensive Midfielder"
# matches MIDFIELD before "Defensive" could wrongly suggest DF.
_POSITION_KEYWORDS = (
    ("GOALKEEPER", "GK"), ("KEEPER", "GK"),
    ("WING BACK", "DF"), ("WINGBACK", "DF"), ("BACK", "DF"), ("DEFENDER", "DF"), ("DEFENCE", "DF"), ("DEFENSE", "DF"),
    ("MIDFIELD", "MF"),
    ("WING", "FW"), ("WINGER", "FW"), ("STRIKER", "FW"), ("FORWARD", "FW"), ("ATTACK", "FW"),
)

def _keyword_position(text: str) -> str | None:
    t = text.strip().upper()
    for kw, pos in _POSITION_KEYWORDS:
        if kw in t:
            return pos
    return None

def resolve_position(generic: str, specific: str) -> str:
    """Return internal position (GK/DF/MF/FW). Tries, in order: exact
    specific_position abbreviation, keyword match on specific_position
    (handles full-word labels), exact generic abbreviation, keyword match
    on generic, then MF as a last-resort default."""
    if specific:
        sp = specific.strip().upper()
        if sp in SPECIFIC_POS_MAP:
            return SPECIFIC_POS_MAP[sp]
        kw = _keyword_position(sp)
        if kw:
            return kw
    if generic:
        g = generic.strip().upper()
        if g in GENERIC_POS_MAP:
            return GENERIC_POS_MAP[g]
        kw = _keyword_position(g)
        if kw:
            return kw
    return "MF"

# Canonical display-slot labels — always one of this fixed set, regardless
# of whatever raw format BSD sends (abbreviation, full word, mixed case).
# Order matters: checked most-specific-first so e.g. "Right Wing Back"
# matches RWB before the generic "BACK" -> DF keyword could misfire.
_CANONICAL_SLOT_KEYWORDS = (
    ("GOALKEEPER", "GK"), ("KEEPER", "GK"), ("GK", "GK"),
    ("RIGHT WING BACK", "RWB"), ("RIGHT WINGBACK", "RWB"), ("RWB", "RWB"),
    ("LEFT WING BACK", "LWB"), ("LEFT WINGBACK", "LWB"), ("LWB", "LWB"),
    ("RIGHT BACK", "RB"), ("RB", "RB"),
    ("LEFT BACK", "LB"), ("LB", "LB"),
    ("CENTRE BACK", "CB"), ("CENTER BACK", "CB"), ("CB", "CB"), ("SWEEPER", "CB"), ("SW", "CB"),
    ("DEFENSIVE MID", "DM"), ("CDM", "DM"), ("DM", "DM"),
    ("ATTACKING MID", "AM"), ("CAM", "AM"), ("AM", "AM"),
    ("CENTRE MID", "CM"), ("CENTER MID", "CM"), ("CM", "CM"),
    ("RIGHT WING", "RW"), ("RW", "RW"), ("RM", "RW"), ("RIGHT MID", "RW"),
    ("LEFT WING", "LW"), ("LW", "LW"), ("LM", "LW"), ("LEFT MID", "LW"),
    ("STRIKER", "ST"), ("CENTRE FORWARD", "ST"), ("CENTER FORWARD", "ST"), ("ST", "ST"), ("CF", "ST"), ("SS", "ST"),
    # Broad fallbacks if nothing more specific matched
    ("DEFENDER", "CB"), ("DEFENCE", "CB"), ("DEFENSE", "CB"), ("BACK", "CB"),
    ("MIDFIELD", "CM"),
    ("FORWARD", "ST"), ("ATTACK", "ST"), ("WING", "RW"),
)

def canonical_slot_label(generic: str, specific: str) -> str:
    """Return one clean, consistent slot label (GK/RB/CB/LB/RWB/LWB/DM/CM/AM/RW/LW/ST)
    for DISPLAY purposes, regardless of whatever raw format the source uses."""
    for text in (specific, generic):
        if not text:
            continue
        t = text.strip().upper()
        for kw, label in _CANONICAL_SLOT_KEYWORDS:
            if kw == t or kw in t:
                return label
    return "CM"


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
    Filters out reserve/youth/women teams unless specifically requested.
    """
    # Short/ambiguous — must appear as a whole word (space-prefixed) to avoid
    # false positives inside ordinary club names.
    RESERVE_WORD_KEYWORDS = (" B", " II", " 2", " U21", " U23", " U19", " U18", " LFC", " YOUTH")

    # Women's-team indicators — long and unambiguous enough that a plain
    # substring check is safe, and it catches real-world formatting BSD uses
    # that a space-prefix check would miss, e.g. "Real Madrid (Women)",
    # "Real Madrid Femenina" (vs. "Femenino"), "Real Madrid CF Femenino".
    RESERVE_SUBSTRING_KEYWORDS = (
        "WOMEN", "WOMAN", "LADIES", "FEMENINO", "FEMENINA", "FEMENÍ",
        "FEMALE", "FEMMINILE", "FÉMININE", "FEMININE", "DAMEN",
    )

    def _is_reserve(tname: str) -> bool:
        t_upper = tname.upper()
        # Only treat as reserve if query didn't ask for reserve/youth keywords
        q_upper = name.upper()
        for kw in RESERVE_WORD_KEYWORDS:
            if kw in t_upper and kw not in q_upper:
                return True
        for kw in RESERVE_SUBSTRING_KEYWORDS:
            if kw in t_upper and kw not in q_upper:
                return True
        return False

    def _search_and_match(query: str) -> tuple[int | None, str | None]:
        data = bsd_get("/teams/", params={"name": query, "limit": 1000})
        if not data:
            return None, None
        results = data.get("results", [])
        if not results:
            return None, None

        # Filter out reserve/youth/women teams unless no main team exists.
        # Two passes: name-based (_is_reserve) catches entries like "Real
        # Madrid (Women)" that have distinguishing text; league_id-based
        # catches entries that don't (plain "Real Sociedad" for the women's
        # team too) — see WOMENS_LEAGUE_IDS for the evidence behind this.
        main_teams = [t for t in results
                      if not _is_reserve(t["name"])
                      and t.get("league_id") not in WOMENS_LEAGUE_IDS]
        candidates = main_teams if main_teams else results

        global _last_team_search_debug
        _last_team_search_debug = {
            "query": query,
            "raw_results": [{"id": t.get("id"), "name": t.get("name"), "league_id": t.get("league_id")} for t in results],
            "main_teams_after_filter": [{"id": t.get("id"), "name": t.get("name"), "league_id": t.get("league_id")} for t in main_teams],
            "used_fallback_to_unfiltered": not main_teams,
        }

        # Prioritize entries in major leagues (LEAGUE_NAMES) to avoid picking Women/Youth team entries
        candidates.sort(key=lambda t: 0 if t.get("league_id") in LEAGUE_NAMES else 1)

        # Filter out dead/broken/empty team IDs — BSD sometimes has duplicate
        # stub entries for the same name that respond 200 with zero fixtures.
        # A non-error response isn't enough; require at least one real fixture.
        active_candidates = []
        for t in candidates:
            fix_check = bsd_get(f"/teams/{t['id']}/fixtures/", params={"limit": 1})
            if fix_check is not None and len(fix_check.get("results", [])) > 0:
                active_candidates.append(t)
        candidates = active_candidates if active_candidates else candidates

        # 1. Look for EXACT match (case-insensitive)
        for t in candidates:
            if t["name"].lower() == name.lower():
                return t["id"], t["name"]

        # 2. Look for close match using difflib
        names_lower = [t["name"].lower() for t in candidates]
        best = difflib.get_close_matches(name.lower(), names_lower, n=1, cutoff=0.30)
        if best:
            for t in candidates:
                if t["name"].lower() == best[0]:
                    return t["id"], t["name"]

        # Fallback: return top candidate
        return candidates[0]["id"], candidates[0]["name"]

    # Strategy 1: full name
    tid, bname = _search_and_match(name)
    if tid:
        return tid, bname

    # Strategy 2: first significant word only
    words = [w for w in name.split() if w.upper() not in ("FC", "AS", "AC", "SC", "SV", "VFB", "RB", "CF", "RCD", "ESTAC", "AFC", "LOSC", "OGC", "SK", "US", "TSG", "VFB", "FSV")]
    if words:
        tid, bname = _search_and_match(words[0])
        if tid:
            return tid, bname

    # Strategy 3: first two significant words
    if len(words) >= 2:
        tid, bname = _search_and_match(f"{words[0]} {words[1]}")
        if tid:
            return tid, bname

    # Strategy 4: common suffixes/prefixes
    suffixes = [" FC", " United", " City", " AFC"]
    for suff in suffixes:
        tid, bname = _search_and_match(name + suff)
        if tid:
            return tid, bname
    prefixes = ["FC ", "AFC "]
    for pref in prefixes:
        tid, bname = _search_and_match(pref + name)
        if tid:
            return tid, bname

    return None, None

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

def clear_cache(prefix: str = "") -> int:
    """Delete cached entries. With no prefix, wipes everything in CACHE_DIR.
    With a prefix (e.g. 'form_v7__'), only deletes matching keys.
    Returns the number of files deleted."""
    deleted = 0
    for fname in os.listdir(CACHE_DIR):
        if not fname.endswith(".json"):
            continue
        if prefix and not fname.startswith(prefix):
            continue
        try:
            os.remove(os.path.join(CACHE_DIR, fname))
            deleted += 1
        except OSError:
            pass
    return deleted
