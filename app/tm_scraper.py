"""
app/tm_scraper.py — Transfermarkt scraper for national team data

Replaces BSD as the data source for the nations module.
Scrapes three things:
  1. Squad roster (23-man list + player details + market values)
  2. Player club season stats (raw performance metrics)
  3. Historical international match lineups (who started where)

All scraped data is cached to file to avoid hammering Transfermarkt.

URL patterns:
  Squad:    /COUNTRY/kader/verein/TEAM_ID/saison_id/YEAR/plus/1
  Player:   /PLAYER_SLUG/profil/spieler/PLAYER_ID
  Stats:    /PLAYER_SLUG/leistungsdaten/spieler/PLAYER_ID/saison/YEAR/plus/1
  Lineups:  /COUNTRY/spielplan/verein/TEAM_ID/saison_id/YEAR
"""

import os
import re
import json
import time
import logging
from datetime import datetime, date
from typing import Optional

import requests
from bs4 import BeautifulSoup

from app.config import cache_read, cache_write, cache_age

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

TM_BASE = "https://www.transfermarkt.com"
TM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Cache TTLs
SQUAD_TTL = 86400       # 24 hours — squads don't change mid-window
STATS_TTL = 43200       # 12 hours — player stats update less frequently
LINEUP_TTL = 604800     # 7 days — historical lineups are static

# Rate limiting: minimum seconds between requests to Transfermarkt
_last_request_time = 0.0
REQUEST_DELAY = 2.0     # 2 seconds between requests — be respectful


# ── Transfermarkt national team IDs ──────────────────────────────────────────
# Maps our internal nation registry to Transfermarkt's verein IDs.
# These are stable and don't change.
TM_TEAM_IDS: dict[str, dict] = {
    # UEFA
    "France":        {"tm_id": 3377,  "slug": "frankreich"},
    "Italy":         {"tm_id": 3376,  "slug": "italien"},
    "Belgium":       {"tm_id": 3382,  "slug": "belgien"},
    "Türkiye":       {"tm_id": 3381,  "slug": "turkei"},
    "Turkey":        {"tm_id": 3381,  "slug": "turkei"},
    "England":       {"tm_id": 3299,  "slug": "england"},
    "Germany":       {"tm_id": 3262,  "slug": "deutschland"},
    "Spain":         {"tm_id": 3375,  "slug": "spanien"},
    "Portugal":      {"tm_id": 3300,  "slug": "portugal"},
    "Netherlands":   {"tm_id": 3379,  "slug": "niederlande"},
    "Croatia":       {"tm_id": 3556,  "slug": "kroatien"},
    "Switzerland":   {"tm_id": 3384,  "slug": "schweiz"},
    "Austria":       {"tm_id": 3383,  "slug": "osterreich"},
    "Scotland":      {"tm_id": 3380,  "slug": "schottland"},
    "Sweden":        {"tm_id": 3557,  "slug": "schweden"},
    "Norway":        {"tm_id": 3440,  "slug": "norwegen"},
    "Denmark":       {"tm_id": 3436,  "slug": "danemark"},
    "Poland":        {"tm_id": 3442,  "slug": "polen"},
    "Czechia":       {"tm_id": 3445,  "slug": "tschechien"},
    "Serbia":        {"tm_id": 3438,  "slug": "serbien"},
    "Ukraine":       {"tm_id": 3699,  "slug": "ukraine"},
    "Greece":        {"tm_id": 3378,  "slug": "griechenland"},
    "Hungary":       {"tm_id": 3468,  "slug": "ungarn"},
    "Romania":       {"tm_id": 3447,  "slug": "rumanien"},
    "Albania":       {"tm_id": 3561,  "slug": "albanien"},
    "Georgia":       {"tm_id": 3669,  "slug": "georgien"},
    "Slovakia":      {"tm_id": 3503,  "slug": "slowakei"},
    "Slovenia":      {"tm_id": 3588,  "slug": "slowenien"},
    "Bosnia and Herzegovina": {"tm_id": 3446, "slug": "bosnien-herzegowina"},
    "Wales":         {"tm_id": 3864,  "slug": "wales"},
    "Republic of Ireland": {"tm_id": 3509, "slug": "irland"},
    "Northern Ireland":    {"tm_id": 3508, "slug": "nordirland"},
    "Finland":       {"tm_id": 3443,  "slug": "finnland"},
    "Iceland":       {"tm_id": 3574,  "slug": "island"},
    "Montenegro":    {"tm_id": 3697,  "slug": "montenegro"},
    "North Macedonia": {"tm_id": 5765, "slug": "nordmazedonien"},
    "Bulgaria":      {"tm_id": 3441,  "slug": "bulgarien"},
    "Kosovo":        {"tm_id": 50059, "slug": "kosovo"},
    "Luxembourg":    {"tm_id": 3578,  "slug": "luxemburg"},
    "Moldova":       {"tm_id": 3671,  "slug": "moldau"},
    "Cyprus":        {"tm_id": 3448,  "slug": "zypern"},
    "Belarus":       {"tm_id": 3670,  "slug": "weissrussland"},
    "Estonia":       {"tm_id": 3572,  "slug": "estland"},
    "Latvia":        {"tm_id": 3573,  "slug": "lettland"},
    "Lithuania":     {"tm_id": 3575,  "slug": "litauen"},
    "Malta":         {"tm_id": 3576,  "slug": "malta"},
    "Armenia":       {"tm_id": 3668,  "slug": "armenien"},
    "Azerbaijan":    {"tm_id": 3667,  "slug": "aserbaidschan"},
    "Faroe Islands": {"tm_id": 3504,  "slug": "faroer"},
    "Gibraltar":     {"tm_id": 42417, "slug": "gibraltar"},
    "Andorra":       {"tm_id": 3560,  "slug": "andorra"},
    "Liechtenstein": {"tm_id": 3577,  "slug": "liechtenstein"},
    "San Marino":    {"tm_id": 3579,  "slug": "san-marino"},
    # CONMEBOL
    "Argentina":     {"tm_id": 3437,  "slug": "argentinien"},
    "Brazil":        {"tm_id": 3439,  "slug": "brasilien"},
    "Colombia":      {"tm_id": 3816,  "slug": "kolumbien"},
    "Uruguay":       {"tm_id": 3449,  "slug": "uruguay"},
    "Ecuador":       {"tm_id": 5750,  "slug": "ecuador"},
    "Paraguay":      {"tm_id": 3581,  "slug": "paraguay"},
    # CONCACAF
    "USA":           {"tm_id": 3505,  "slug": "vereinigte-staaten"},
    "Mexico":        {"tm_id": 6303,  "slug": "mexiko"},
    "Canada":        {"tm_id": 3510,  "slug": "kanada"},
    "Panama":        {"tm_id": 3562,  "slug": "panama"},
    "Haiti":         {"tm_id": 3607,  "slug": "haiti"},
    "Curaçao":       {"tm_id": 52444, "slug": "curacao"},
    # AFC
    "Japan":         {"tm_id": 3435,  "slug": "japan"},
    "South Korea":   {"tm_id": 3589,  "slug": "sudkorea"},
    "Australia":     {"tm_id": 3433,  "slug": "australien"},
    "Saudi Arabia":  {"tm_id": 3807,  "slug": "saudi-arabien"},
    "Iran":          {"tm_id": 3582,  "slug": "iran"},
    "Iraq":          {"tm_id": 3812,  "slug": "irak"},
    "Qatar":         {"tm_id": 3590,  "slug": "katar"},
    "Jordan":        {"tm_id": 3810,  "slug": "jordanien"},
    "Uzbekistan":    {"tm_id": 3591,  "slug": "usbekistan"},
    # CAF
    "Morocco":       {"tm_id": 3587,  "slug": "marokko"},
    "Senegal":       {"tm_id": 3499,  "slug": "senegal"},
    "Côte d'Ivoire": {"tm_id": 3591,  "slug": "elfenbeinkuste"},
    "Ghana":         {"tm_id": 3598,  "slug": "ghana"},
    "Egypt":         {"tm_id": 3672,  "slug": "agypten"},
    "South Africa":  {"tm_id": 3806,  "slug": "sudafrika"},
    "Algeria":       {"tm_id": 3614,  "slug": "algerien"},
    "Tunisia":       {"tm_id": 3670,  "slug": "tunesien"},
    "DR Congo":      {"tm_id": 3606,  "slug": "dr-kongo"},
    "Cabo Verde":    {"tm_id": 5637,  "slug": "kap-verde"},
    # OFC
    "New Zealand":   {"tm_id": 3593,  "slug": "neuseeland"},
}


# ── HTTP helper ──────────────────────────────────────────────────────────────

def _tm_get(path: str) -> Optional[BeautifulSoup]:
    """
    Fetch a Transfermarkt page with rate limiting.
    Returns a BeautifulSoup object or None on error.
    """
    global _last_request_time

    # Rate limit
    elapsed = time.time() - _last_request_time
    if elapsed < REQUEST_DELAY:
        time.sleep(REQUEST_DELAY - elapsed)

    url = f"{TM_BASE}{path}"
    try:
        resp = requests.get(url, headers=TM_HEADERS, timeout=15)
        _last_request_time = time.time()

        if resp.status_code == 200:
            return BeautifulSoup(resp.text, "lxml")
        elif resp.status_code == 404:
            logger.warning(f"TM 404: {url}")
            return None
        else:
            logger.warning(f"TM {resp.status_code}: {url}")
            return None
    except Exception as e:
        logger.error(f"TM request error: {e}")
        _last_request_time = time.time()
        return None


# ── Position mapping ─────────────────────────────────────────────────────────

_POS_MAP = {
    "Goalkeeper": "GK",
    "Centre-Back": "DF", "Left-Back": "DF", "Right-Back": "DF",
    "Defensive Midfield": "MF", "Central Midfield": "MF",
    "Attacking Midfield": "MF",
    "Left Midfield": "MF", "Right Midfield": "MF",
    "Left Winger": "FW", "Right Winger": "FW",
    "Centre-Forward": "FW", "Second Striker": "FW",
}

_SPECIFIC_POS_MAP = {
    "Goalkeeper": "GK",
    "Centre-Back": "CB", "Left-Back": "LB", "Right-Back": "RB",
    "Defensive Midfield": "DM", "Central Midfield": "CM",
    "Attacking Midfield": "AM",
    "Left Midfield": "LM", "Right Midfield": "RM",
    "Left Winger": "LW", "Right Winger": "RW",
    "Centre-Forward": "ST", "Second Striker": "SS",
}


def _resolve_pos(tm_position: str) -> str:
    """Map Transfermarkt position string to GK/DF/MF/FW."""
    if not tm_position:
        return "MF"
    for key, val in _POS_MAP.items():
        if key.lower() in tm_position.lower():
            return val
    return "MF"


def _resolve_specific_pos(tm_position: str) -> str:
    """Map Transfermarkt position string to specific slot (CB, LW, ST etc)."""
    if not tm_position:
        return "CM"
    for key, val in _SPECIFIC_POS_MAP.items():
        if key.lower() in tm_position.lower():
            return val
    return "CM"


# ── 1. SQUAD SCRAPER ────────────────────────────────────────────────────────

def scrape_squad(nation_name: str, season: int = None) -> Optional[dict]:
    """
    Scrape the full squad roster for a national team from Transfermarkt.

    Returns:
    {
        "team": "France",
        "season": 2026,
        "squad_count": 26,
        "total_market_value": "€1.2B",
        "players": [
            {
                "name": "Kylian Mbappé",
                "tm_id": 342229,
                "tm_slug": "kylian-mbappe",
                "position": "FW",
                "specific_position": "LW",
                "tm_position": "Left Winger",
                "age": 27,
                "club": "Real Madrid",
                "club_country": "Spain",
                "market_value": "€180M",
                "caps": 85,
                "goals": 48,
                "jersey_number": 10,
            },
            ...
        ]
    }
    """
    if season is None:
        season = date.today().year

    tm_info = TM_TEAM_IDS.get(nation_name)
    if not tm_info:
        logger.warning(f"No TM ID for '{nation_name}'")
        return None

    cache_key = f"tm_squad__{nation_name.lower().replace(' ','_')}__{season}"
    cached = cache_read(cache_key)
    if cached and cache_age(cached) < SQUAD_TTL:
        return cached

    tm_id = tm_info["tm_id"]
    slug = tm_info["slug"]
    path = f"/{slug}/kader/verein/{tm_id}/saison_id/{season}/plus/1"

    soup = _tm_get(path)
    if not soup:
        return cached  # return stale cache if available

    players = []
    # Transfermarkt squad table uses class "items"
    table = soup.find("table", class_="items")
    if not table:
        logger.warning(f"No squad table found for {nation_name}")
        return cached

    rows = table.select("tbody tr")
    for row in rows:
        # Skip separator/header rows
        if "bg_blau_20" in (row.get("class") or []):
            continue

        cells = row.find_all("td", recursive=False)
        if len(cells) < 4:
            continue

        player = _parse_squad_row(cells, row)
        if player:
            players.append(player)

    result = {
        "_cached_at": time.time(),
        "team": nation_name,
        "season": season,
        "squad_count": len(players),
        "players": players,
    }

    cache_write(cache_key, result)
    return result


def _parse_squad_row(cells, row) -> Optional[dict]:
    """Parse one player row from the Transfermarkt squad table."""
    try:
        # Jersey number: first cell
        jersey = 0
        jersey_cell = cells[0]
        jersey_text = jersey_cell.get_text(strip=True)
        if jersey_text.isdigit():
            jersey = int(jersey_text)

        # Player name + link: typically in the second or third cell
        # Look for the main player link
        player_link = row.select_one("a.spielprofil_tooltip") or row.select_one("td.hauptlink a")
        if not player_link:
            # Fallback: find any link that goes to a player profile
            for a in row.find_all("a"):
                href = a.get("href", "")
                if "/profil/spieler/" in href:
                    player_link = a
                    break

        if not player_link:
            return None

        name = player_link.get_text(strip=True)
        href = player_link.get("href", "")

        # Extract TM player ID from href like /player-slug/profil/spieler/12345
        tm_player_id = 0
        tm_player_slug = ""
        match = re.search(r"/profil/spieler/(\d+)", href)
        if match:
            tm_player_id = int(match.group(1))
        slug_match = re.search(r"^/([^/]+)/profil/", href)
        if slug_match:
            tm_player_slug = slug_match.group(1)

        # Position: look for the position text (usually in a cell with class "posrela" or nearby)
        tm_position = ""
        pos_cell = row.select_one("td.posrela")
        if pos_cell:
            pos_text = pos_cell.find("tr", class_="")
            if pos_text:
                tm_position = pos_text.get_text(strip=True)
        if not tm_position:
            # Alternative: look for position in small text
            for td in cells:
                small = td.find("small")
                if small:
                    text = small.get_text(strip=True)
                    if any(pos_word in text for pos_word in
                           ["Back", "Forward", "Midfield", "Winger", "Goalkeeper", "Striker"]):
                        tm_position = text
                        break

        # Age
        age = 0
        for td in cells:
            text = td.get_text(strip=True)
            if text.isdigit() and 15 < int(text) < 50:
                age = int(text)
                break

        # Club: look for club link
        club = ""
        club_country = ""
        club_links = []
        for td in cells:
            for a in td.find_all("a"):
                href = a.get("href", "")
                if "/verein/" in href and "/profil/" not in href:
                    club_links.append(a)

        if club_links:
            # Last club link is usually the actual club (not national team)
            club = club_links[-1].get("title", "") or club_links[-1].get_text(strip=True)
            # Club country flag is usually an img near the club cell
            parent_td = club_links[-1].find_parent("td")
            if parent_td:
                flag = parent_td.find("img", class_="flaggenrahmen")
                if flag:
                    club_country = flag.get("title", "")

        # Market value: look for "rechts hauptlink" cell
        market_value = ""
        mv_cell = row.select_one("td.rechts.hauptlink")
        if mv_cell:
            market_value = mv_cell.get_text(strip=True)

        # Caps and goals: typically in the last few cells
        caps = 0
        goals = 0
        # Caps/goals cells are usually the rightmost numeric cells
        numeric_cells = []
        for td in cells:
            text = td.get_text(strip=True).replace("-", "0")
            if text.isdigit():
                numeric_cells.append(int(text))

        # In the extended squad view (+1), caps and goals are typically
        # the last two numeric columns before market value
        if len(numeric_cells) >= 3:
            # Pattern: jersey, age, caps, goals (sometimes more)
            # The first is jersey, second is usually age
            # Then caps and goals
            caps = numeric_cells[-2] if len(numeric_cells) >= 2 else 0
            goals = numeric_cells[-1] if len(numeric_cells) >= 1 else 0

        return {
            "name": name,
            "tm_id": tm_player_id,
            "tm_slug": tm_player_slug,
            "position": _resolve_pos(tm_position),
            "specific_position": _resolve_specific_pos(tm_position),
            "tm_position": tm_position,
            "age": age,
            "club": club,
            "club_country": club_country,
            "market_value": market_value,
            "caps": caps,
            "goals": goals,
            "jersey_number": jersey,
        }
    except Exception as e:
        logger.error(f"Error parsing squad row: {e}")
        return None


# ── 2. PLAYER CLUB STATS SCRAPER ────────────────────────────────────────────

def scrape_player_stats(
    tm_player_id: int,
    tm_player_slug: str,
    season: int = None,
    position: str = "MF",
) -> Optional[dict]:
    """
    Scrape a player's club season stats from Transfermarkt.

    Returns position-appropriate raw metrics:
      Attackers (FW): goals, assists, minutes, yellow/red cards, appearances
      Midfielders (MF): goals, assists, minutes, yellow/red cards, appearances
      Defenders (DF): goals, assists, minutes, yellow/red cards, appearances, clean_sheets
      GK: goals_conceded, clean_sheets, minutes, appearances

    NOTE: Transfermarkt's free pages show basic stats (goals, assists, minutes,
    cards, appearances). Advanced stats like xG, xA, chances created, tackles,
    interceptions etc. would require FBref or a paid API. The rating engine
    adjusts weights accordingly.
    """
    if season is None:
        season = date.today().year

    cache_key = f"tm_pstats__{tm_player_id}__{season}"
    cached = cache_read(cache_key)
    if cached and cache_age(cached) < STATS_TTL:
        return cached

    if not tm_player_slug:
        tm_player_slug = "player"

    # Transfermarkt player stats page
    path = f"/{tm_player_slug}/leistungsdaten/spieler/{tm_player_id}/saison/{season}/plus/1"
    soup = _tm_get(path)
    if not soup:
        return cached

    stats = {
        "_cached_at": time.time(),
        "player_id": tm_player_id,
        "season": season,
        "appearances": 0,
        "goals": 0,
        "assists": 0,
        "minutes": 0,
        "yellow_cards": 0,
        "red_cards": 0,
        "clean_sheets": 0,
        "goals_conceded": 0,
        "goals_per_90": 0.0,
        "assists_per_90": 0.0,
        "g_a_per_90": 0.0,
        "minutes_per_appearance": 0.0,
    }

    # The stats page has a summary table at the top or a detailed breakdown
    # Look for the stats summary row
    table = soup.find("table", class_="items")
    if not table:
        # Try the footer/summary row of the detailed stats table
        tables = soup.find_all("table")
        for t in tables:
            tfoot = t.find("tfoot")
            if tfoot:
                table = t
                break

    if table:
        # Look for the total/summary row (tfoot)
        tfoot = table.find("tfoot")
        if tfoot:
            cells = tfoot.find_all("td")
            stats = _parse_stats_footer(cells, stats)
        else:
            # Count from individual match rows
            rows = table.select("tbody tr")
            stats = _aggregate_match_stats(rows, stats)

    # Compute per-90 metrics
    if stats["minutes"] > 0:
        stats["goals_per_90"] = round((stats["goals"] / stats["minutes"]) * 90, 3)
        stats["assists_per_90"] = round((stats["assists"] / stats["minutes"]) * 90, 3)
        stats["g_a_per_90"] = round(
            ((stats["goals"] + stats["assists"]) / stats["minutes"]) * 90, 3
        )
    if stats["appearances"] > 0:
        stats["minutes_per_appearance"] = round(
            stats["minutes"] / stats["appearances"], 1
        )

    cache_write(cache_key, stats)
    return stats


def _parse_stats_footer(cells: list, stats: dict) -> dict:
    """Parse the tfoot summary row of a Transfermarkt stats table."""
    # Typical column order in the detailed stats view:
    # Competition, Apps, Goals, Assists, Yellow, 2nd Yellow, Red, Minutes
    # But the exact order varies; we parse by position
    numerics = []
    for cell in cells:
        text = cell.get_text(strip=True).replace(".", "").replace("'", "").replace("-", "0")
        if text.isdigit():
            numerics.append(int(text))
        else:
            numerics.append(None)

    # Try to map known patterns
    # In the extended view, common patterns are:
    # [apps, goals, assists, yellows, second_yellows, reds, subs_on, subs_off, minutes]
    if len(numerics) >= 3:
        vals = [n for n in numerics if n is not None]
        if len(vals) >= 2:
            stats["appearances"] = vals[0] if vals[0] and vals[0] < 100 else 0
            stats["goals"] = vals[1] if len(vals) > 1 else 0
            stats["assists"] = vals[2] if len(vals) > 2 else 0
            if len(vals) > 3:
                stats["yellow_cards"] = vals[3]
            # Minutes is typically the last number (often large, 1000+)
            if vals and vals[-1] > 100:
                stats["minutes"] = vals[-1]

    return stats


def _aggregate_match_stats(rows: list, stats: dict) -> dict:
    """Aggregate stats from individual match rows."""
    total_goals = 0
    total_assists = 0
    total_minutes = 0
    total_apps = 0
    total_yellows = 0
    total_reds = 0

    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        total_apps += 1

        for cell in cells:
            text = cell.get_text(strip=True)
            # Goals cell often has a football icon or specific class
            # Minutes cell is usually the last numeric one
            if text.replace("'", "").isdigit():
                val = int(text.replace("'", ""))
                if val > 100:  # likely minutes
                    total_minutes += val

    stats["appearances"] = total_apps
    stats["goals"] = total_goals
    stats["assists"] = total_assists
    stats["minutes"] = total_minutes
    stats["yellow_cards"] = total_yellows
    stats["red_cards"] = total_reds
    return stats


# ── 3. HISTORICAL LINEUPS SCRAPER ───────────────────────────────────────────

def scrape_match_lineups(
    nation_name: str,
    season: int = None,
    limit: int = 10,
) -> Optional[list[dict]]:
    """
    Scrape historical international match lineups for a national team.

    Returns a list of match lineup records:
    [
        {
            "match_date": "2026-09-05",
            "opponent": "Belgium",
            "competition": "Nations League",
            "score": "1-0",
            "formation": "4-3-3",
            "starters": [
                {"name": "Maignan", "position": "GK", "minutes": 90},
                {"name": "Kounde", "position": "RB", "minutes": 90},
                ...
            ],
            "subs": [
                {"name": "Griezmann", "position": "AM", "minutes": 25, "replaced": "Olise"},
                ...
            ]
        },
        ...
    ]
    """
    if season is None:
        season = date.today().year

    tm_info = TM_TEAM_IDS.get(nation_name)
    if not tm_info:
        return None

    cache_key = f"tm_lineups__{nation_name.lower().replace(' ','_')}__{season}"
    cached = cache_read(cache_key)
    if cached and cache_age(cached) < LINEUP_TTL:
        return cached

    tm_id = tm_info["tm_id"]
    slug = tm_info["slug"]

    # First get the fixture list for this season
    path = f"/{slug}/spielplan/verein/{tm_id}/saison_id/{season}"
    soup = _tm_get(path)
    if not soup:
        return cached

    # Find match links
    match_links = []
    for a in soup.find_all("a"):
        href = a.get("href", "")
        if "/spielbericht/spielbericht/" in href or "/index/spielbericht/" in href:
            if href not in [m["href"] for m in match_links]:
                match_links.append({"href": href})
                if len(match_links) >= limit:
                    break

    lineups = []
    for match in match_links:
        # Get the match report page which contains lineup info
        match_soup = _tm_get(match["href"].replace("/spielbericht/", "/aufstellung/"))
        if not match_soup:
            continue

        lineup = _parse_match_lineup(match_soup, nation_name)
        if lineup:
            lineups.append(lineup)

    result = {
        "_cached_at": time.time(),
        "team": nation_name,
        "season": season,
        "matches": lineups,
    }

    cache_write(cache_key, result)
    return result


def _parse_match_lineup(soup: BeautifulSoup, nation_name: str) -> Optional[dict]:
    """Parse a single match's lineup from its match report page."""
    try:
        # Match header info
        header = soup.find("div", class_="sb-spielbericht")
        if not header:
            header = soup.find("div", class_="box-header")

        match_date = ""
        opponent = ""
        competition = ""
        score = ""
        formation = ""

        # Try to extract match info from the header
        date_span = soup.find("span", class_="sb-datum")
        if date_span:
            match_date = date_span.get_text(strip=True)

        # Formation
        formation_div = soup.find("div", class_="aufstellung-formation")
        if formation_div:
            formation = formation_div.get_text(strip=True)

        # Starting XI
        starters = []
        lineup_tables = soup.find_all("div", class_="aufstellung-spieler-container")
        if not lineup_tables:
            lineup_tables = soup.find_all("table", class_="items")

        for container in lineup_tables:
            player_links = container.find_all("a")
            for a_tag in player_links:
                href = a_tag.get("href", "")
                if "/profil/spieler/" in href:
                    player_name = a_tag.get_text(strip=True)
                    if player_name and player_name not in [s["name"] for s in starters]:
                        starters.append({
                            "name": player_name,
                            "position": "",  # Will be enriched from squad data
                            "minutes": 90,   # Default, refined from sub data
                        })
                        if len(starters) >= 11:
                            break
            if len(starters) >= 11:
                break

        if not starters:
            return None

        return {
            "match_date": match_date,
            "opponent": opponent,
            "competition": competition,
            "score": score,
            "formation": formation,
            "starters": starters[:11],
            "subs": [],  # Subs parsing can be added later
        }
    except Exception as e:
        logger.error(f"Error parsing match lineup: {e}")
        return None


# ── CONVENIENCE: scrape everything for a nation ─────────────────────────────

def scrape_nation_full(nation_name: str, season: int = None) -> Optional[dict]:
    """
    Full scrape for a national team: squad + player stats + lineups.
    This is the main entry point for the rating engine.

    Returns combined data ready for the XI predictor and rating engine.
    """
    if season is None:
        season = date.today().year

    cache_key = f"tm_full__{nation_name.lower().replace(' ','_')}__{season}"
    cached = cache_read(cache_key)
    if cached and cache_age(cached) < SQUAD_TTL:
        return cached

    # 1. Squad roster
    squad = scrape_squad(nation_name, season)
    if not squad or not squad.get("players"):
        # Try previous season
        squad = scrape_squad(nation_name, season - 1)
        if not squad or not squad.get("players"):
            return None

    # 2. Player stats for each squad member
    for player in squad["players"]:
        if player.get("tm_id"):
            stats = scrape_player_stats(
                tm_player_id=player["tm_id"],
                tm_player_slug=player.get("tm_slug", ""),
                season=season,
                position=player.get("position", "MF"),
            )
            if stats:
                player["club_stats"] = stats
            else:
                player["club_stats"] = None
        else:
            player["club_stats"] = None

    # 3. Historical lineups (for XI predictor)
    lineups = scrape_match_lineups(nation_name, season, limit=10)
    if not lineups:
        lineups = scrape_match_lineups(nation_name, season - 1, limit=10)

    result = {
        "_cached_at": time.time(),
        "team": nation_name,
        "season": season,
        "squad": squad,
        "lineups": lineups,
    }

    cache_write(cache_key, result)
    return result
