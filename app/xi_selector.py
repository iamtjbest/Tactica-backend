"""
app/xi_selector.py — formation-aware Starting XI selection
"""
import re, difflib, json, os
from app.config import SPECIFIC_POS_MAP, GENERIC_POS_MAP

PLAYERS_PATH = os.environ.get("PLAYERS_PATH", "players.json")

def load_players() -> dict:
    try:
        return json.load(open(PLAYERS_PATH, encoding="utf-8"))
    except Exception:
        return {}

# Wide attacker specs — these players fill FW slots in wide formations
WIDE_ATT = {"RM","LM","RW","LW","RWF","LWF","AM","CAM","SS","WF"}

def get_pos(generic: str, specific: str) -> str:
    if specific:
        sp = specific.strip().upper()
        if sp in SPECIFIC_POS_MAP:
            return SPECIFIC_POS_MAP[sp]
    return GENERIC_POS_MAP.get((generic or "M").strip().upper(), "MF")

def _forwards_in_formation(formation: str) -> int:
    parts = [int(x) for x in re.findall(r"\d+", formation)]
    return parts[-1] if parts else 1

def select_xi(team_name: str, formation: str, players_db: dict = None) -> list[dict] | None:
    """
    Select the best starting XI for a given team and formation.

    Formation-aware winger logic:
    - 3+ forwards (4-3-3, 3-4-3 etc.): RM/LM/AM fill FW slots (they ARE the wide forwards)
    - 1-2 forwards (4-4-2, 5-3-2 etc.): RM/LM fill MF slots (wide midfielders)
    """
    if players_db is None:
        players_db = load_players()

    if team_name not in players_db:
        close = difflib.get_close_matches(team_name, players_db.keys(), n=1, cutoff=0.6)
        if close:
            team_name = close[0]
        else:
            return None

    parts     = [int(x) for x in re.findall(r"\d+", formation)]
    def_count = parts[0]
    att_count = parts[-1]
    mid_count = sum(parts[1:-1]) if len(parts) > 2 else parts[1]
    wide_go_fwd = att_count >= 3  # wide players fill FW slots in attacking formations

    # Resolve and clean roster
    raw = [
        p for p in players_db[team_name]
        if p.get("Name") and str(p["Name"]).strip() not in ("","None","null")
    ]
    roster = []
    for p in raw:
        p2 = dict(p)
        spec = str(p2.get("SpecPos","")).strip().upper()
        # Re-resolve position in case old data only has generic code
        p2["_resolved_pos"] = get_pos(p2.get("Pos","MF"), spec)
        p2["_is_wide"] = spec in WIDE_ATT
        roster.append(p2)

    roster = sorted(roster, key=lambda x: (x.get("Min",0), x.get("G_A",0)), reverse=True)

    xi, named = [], set()

    def draft(pos_check, n):
        drafted = 0
        for p in roster:
            if drafted >= n: break
            if p["Name"] in named: continue
            rp = p["_resolved_pos"]
            is_wide = p["_is_wide"]
            match = False
            if pos_check == "GK"  and rp == "GK": match = True
            elif pos_check == "DF" and rp == "DF": match = True
            elif pos_check == "MF":
                if rp == "MF" and not (is_wide and wide_go_fwd): match = True
            elif pos_check == "FW":
                if rp == "FW": match = True
                elif is_wide and wide_go_fwd: match = True  # wide players fill FW in 4-3-3 etc.
            if match:
                entry = {
                    "name":     p["Name"],
                    "pos":      pos_check,
                    "spec_pos": p.get("SpecPos",""),
                    "minutes":  p.get("Min",0),
                    "g_a":      p.get("G_A",0),
                    "fallback": False,
                }
                xi.append(entry)
                named.add(p["Name"])
                drafted += 1
        return drafted

    def draft_fallback(pos, n):
        """Second pass — relax wide-attacker constraint."""
        drafted = 0
        for p in roster:
            if drafted >= n: break
            if p["Name"] in named: continue
            if p["_resolved_pos"] == pos:
                xi.append({
                    "name": p["Name"], "pos": pos,
                    "spec_pos": p.get("SpecPos",""),
                    "minutes": p.get("Min",0), "g_a": p.get("G_A",0),
                    "fallback": False,
                })
                named.add(p["Name"])
                drafted += 1
        return drafted

    # Draft in positional order
    draft("GK", 1)
    n = draft("DF", def_count);  draft_fallback("DF", def_count - n) if n < def_count else None
    n = draft("MF", mid_count);  draft_fallback("MF", mid_count - n) if n < mid_count else None
    n = draft("FW", att_count);  draft_fallback("FW", att_count - n) if n < att_count else None

    # Emergency pad if still short (data gaps)
    for p in roster:
        if len(xi) >= 11: break
        if p["Name"] not in named:
            xi.append({
                "name": p["Name"], "pos": p["_resolved_pos"],
                "spec_pos": p.get("SpecPos",""),
                "minutes": p.get("Min",0), "g_a": p.get("G_A",0),
                "fallback": True,
            })
            named.add(p["Name"])

    return xi[:11]
