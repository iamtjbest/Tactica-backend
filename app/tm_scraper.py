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


# ── Position detection helpers ──────────────────────────────────────────────

_POS_KEYWORDS = [
    "back", "forward", "midfield", "winger", "goalkeeper",
    "striker", "keeper",
]

# TM CSS classes that indicate position category (on jersey number cells)
_CSS_POS_MAP = {
    "torwart": "Goalkeeper",
    "abwehr": "Centre-Back",
    "mittelfeld": "Central Midfield",
    "sturm": "Centre-Forward",
}


def _extract_position(row, cells):
    """
    Extract player position using multiple fallback strategies.
    TM embeds position in different ways depending on page version.
    """
    # Strategy 1: CSS classes on cells (most reliable)
    # TM uses bg_Torwart, bg_Abwehr, bg_Mittelfeld, bg_Sturm on cells
    all_classes = []
    for td in cells:
        all_classes.extend(td.get("class") or [])
    all_classes.extend(row.get("class") or [])
    class_str = " ".join(all_classes).lower()

    for css_key, pos_name in _CSS_POS_MAP.items():
        if css_key in class_str:
            return pos_name

    # Strategy 2: Text fragments in posrela cell
    pos_cell = row.select_one("td.posrela")
    if pos_cell:
        for text in pos_cell.stripped_strings:
            text_stripped = text.strip()
            # Exact match against known TM position labels
            if text_stripped in _POS_MAP:
                return text_stripped
            # Keyword match
            if any(kw in text_stripped.lower() for kw in _POS_KEYWORDS):
                return text_stripped

    # Strategy 3: Any cell containing exact position text (not in links)
    for td in cells:
        if td.select_one("a[href*='/profil/']") or td.select_one("a[href*='/verein/']"):
            continue
        for text in td.stripped_strings:
            if text in _POS_MAP:
                return text

    # Strategy 4: <small>, <span>, or nested elements with position text
    for td in cells:
        for tag in td.find_all(["small", "span", "td"]):
            text = tag.get_text(strip=True)
            if text in _POS_MAP:
                return text
            if any(kw in text.lower() for kw in _POS_KEYWORDS) and len(text) < 30:
                return text

    return ""


def _extract_age(cells):
    """
    Extract player age. TM shows DOB as 'Mon DD, YYYY (age)' or
    'DD.MM.YYYY (age)' — the bare digit scan used before was catching
    jersey numbers and caps.
    """
    # Strategy 1: Age in parentheses (most TM pages use this)
    for td in cells:
        text = td.get_text(strip=True)
        age_match = re.search(r'\((\d{1,2})\)', text)
        if age_match:
            val = int(age_match.group(1))
            if 14 <= val <= 55:
                return val

    # Strategy 2: Compute from birth year in date text
    for td in cells:
        text = td.get_text(strip=True)
        # Look for year in date-like context
        year_match = re.search(
            r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},?\s+((?:19|20)\d{2})'
            r'|(\d{1,2}\.\d{1,2}\.((?:19|20)\d{2}))'
            r'|((?:19|20)\d{2})-\d{2}-\d{2}',
            text
        )
        if year_match:
            birth_year_str = next(g for g in year_match.groups() if g)
            try:
                birth_year = int(birth_year_str)
                age = date.today().year - birth_year
                if 14 <= age <= 55:
                    return age
            except (ValueError, TypeError):
                pass

    # Strategy 3: Bare digit, but skip first cell (jersey) and cells with links/images
    for idx, td in enumerate(cells):
        if idx == 0:
            continue
        if td.find("a") or td.find("img"):
            continue
        text = td.get_text(strip=True)
        if text.isdigit():
            val = int(text)
            if 16 <= val <= 45:
                return val

    return 0


def _extract_club_info(cells):
    """Extract club name and club country from squad row."""
    club = ""
    club_country = ""

    club_links = []
    for td in cells:
        for a in td.find_all("a"):
            href = a.get("href", "")
            if "/verein/" in href and "/profil/" not in href:
                club_links.append(a)

    if not club_links:
        return club, club_country

    # Last club link is usually the actual club
    club_link = club_links[-1]
    club = club_link.get("title", "") or club_link.get_text(strip=True)

    # Club country: look for flag images near the club
    parent_td = club_link.find_parent("td")
    if parent_td:
        # Try flaggenrahmen class first
        flag = parent_td.find("img", class_="flaggenrahmen")
        # Try any small flag image with a title
        if not flag:
            for img in parent_td.find_all("img"):
                title = img.get("title", "")
                # Skip if title matches club name (that's the club logo, not country flag)
                if title and title != club and len(title) < 30:
                    flag = img
                    break
        # Try adjacent cells (TM sometimes puts flag in the next cell)
        if not flag:
            next_td = parent_td.find_next_sibling("td")
            if next_td:
                flag = next_td.find("img", class_="flaggenrahmen")
                if not flag:
                    for img in next_td.find_all("img"):
                        title = img.get("title", "")
                        if title and title != club and len(title) < 30:
                            flag = img
                            break

        if flag:
            club_country = flag.get("title", "")

    # Fallback: infer club_country from known club-to-country mappings
    if not club_country and club:
        club_country = _infer_club_country(club)

    return club, club_country


# Quick club→country lookup for major clubs (fallback when flag scraping fails)
_CLUB_COUNTRY_MAP = {
    # England
    "Arsenal": "England", "Chelsea": "England", "Liverpool": "England",
    "Manchester City": "England", "Manchester United": "England",
    "Tottenham": "England", "Tottenham Hotspur": "England",
    "Newcastle": "England", "Newcastle United": "England",
    "Aston Villa": "England", "West Ham": "England", "West Ham United": "England",
    "Brighton": "England", "Crystal Palace": "England", "Brentford": "England",
    "Brentford FC": "England", "Everton": "England", "Fulham": "England",
    "Wolverhampton": "England", "Nottingham Forest": "England",
    "Bournemouth": "England", "Leicester": "England", "Leicester City": "England",
    # Spain
    "Real Madrid": "Spain", "Barcelona": "Spain", "FC Barcelona": "Spain",
    "Atletico Madrid": "Spain", "Atlético de Madrid": "Spain",
    "Real Sociedad": "Spain", "Athletic Bilbao": "Spain",
    "Real Betis": "Spain", "Villarreal": "Spain", "Sevilla": "Spain",
    "Valencia": "Spain", "Girona": "Spain",
    # Germany
    "Bayern Munich": "Germany", "FC Bayern München": "Germany",
    "Borussia Dortmund": "Germany", "RB Leipzig": "Germany",
    "Bayer Leverkusen": "Germany", "Bayer 04 Leverkusen": "Germany",
    "Eintracht Frankfurt": "Germany", "VfB Stuttgart": "Germany",
    "VfL Wolfsburg": "Germany", "Borussia Mönchengladbach": "Germany",
    "SC Freiburg": "Germany", "1.FC Nuremberg": "Germany",
    "1.FC Nürnberg": "Germany",
    # Italy
    "Inter Milan": "Italy", "AC Milan": "Italy", "Juventus": "Italy",
    "Juventus FC": "Italy", "SSC Napoli": "Italy", "AS Roma": "Italy",
    "SS Lazio": "Italy", "Atalanta": "Italy", "Atalanta BC": "Italy",
    "ACF Fiorentina": "Italy", "Bologna FC 1909": "Italy",
    "Torino FC": "Italy", "Cagliari Calcio": "Italy", "Como 1907": "Italy",
    "US Sassuolo": "Italy", "AC Monza": "Italy", "Udinese": "Italy",
    "Hellas Verona": "Italy", "Genoa CFC": "Italy", "Empoli FC": "Italy",
    "US Lecce": "Italy", "Parma Calcio": "Italy",
    # France
    "Paris Saint-Germain": "France", "PSG": "France",
    "Olympique Marseille": "France", "AS Monaco": "France",
    "Olympique Lyon": "France", "LOSC Lille": "France",
    "OGC Nice": "France", "RC Lens": "France", "Stade Rennais": "France",
    "Stade Brestois 29": "France", "Paris FC": "France",
    # Portugal
    "Sporting CP": "Portugal", "SL Benfica": "Portugal", "FC Porto": "Portugal",
    "SC Braga": "Portugal",
    # Netherlands
    "Ajax": "Netherlands", "PSV": "Netherlands", "PSV Eindhoven": "Netherlands",
    "Feyenoord": "Netherlands", "AZ Alkmaar": "Netherlands",
    # Other
    "Celtic FC": "Scotland", "Rangers FC": "Scotland",
    "Red Bull Salzburg": "Austria", "Galatasaray": "Turkey",
    "Fenerbahce": "Turkey", "Besiktas": "Turkey",
}


def _infer_club_country(club_name: str) -> str:
    """Infer club country from club name when flag scraping fails."""
    if not club_name:
        return ""
    # Exact match
    if club_name in _CLUB_COUNTRY_MAP:
        return _CLUB_COUNTRY_MAP[club_name]
    # Partial match
    club_lower = club_name.lower()
    for known_club, country in _CLUB_COUNTRY_MAP.items():
        if known_club.lower() in club_lower or club_lower in known_club.lower():
            return country
    return ""


def _extract_caps_goals(cells, jersey: int, age: int):
    """
    Extract international caps and goals from the rightmost numeric cells.
    Avoids confusion with jersey number and age by excluding known values.
    """
    # Collect (index, value) for all numeric cells
    numeric_entries = []
    for idx, td in enumerate(cells):
        text = td.get_text(strip=True).replace("-", "0").replace(".", "")
        if text.isdigit():
            numeric_entries.append((idx, int(text)))

    # Filter out jersey (first cell) and known age
    filtered = []
    for idx, val in numeric_entries:
        if idx == 0:
            continue  # skip jersey cell
        # Don't skip by value — multiple cells can have the same number
        filtered.append((idx, val))

    # Caps and goals are the LAST two numeric values (rightmost columns)
    if len(filtered) >= 2:
        caps = filtered[-2][1]
        goals = filtered[-1][1]
    elif len(filtered) == 1:
        caps = filtered[0][1]
        goals = 0
    else:
        caps = 0
        goals = 0

    return caps, goals


# ── 1. SQUAD SCRAPER ────────────────────────────────────────────────────────

def scrape_squad(nation_name: str, season: int = None) -> Optional[dict]:
    """
    Scrape the full squad roster for a national team from Transfermarkt.

    Returns dict with team info and player list.
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
    table = soup.find("table", class_="items")
    if not table:
        logger.warning(f"No squad table found for {nation_name}")
        return cached

    rows = table.select("tbody tr")
    for row_idx, row in enumerate(rows):
        # Skip separator/header rows
        if "bg_blau_20" in (row.get("class") or []):
            continue

        cells = row.find_all("td", recursive=False)
        if len(cells) < 4:
            continue

        # Debug: log first 2 rows so we can see actual HTML structure
        if row_idx < 2:
            logger.info(f"[TM DEBUG] {nation_name} row {row_idx}: {len(cells)} cells")
            logger.info(f"[TM DEBUG] Row classes: {row.get('class')}")
            for ci, c in enumerate(cells):
                cls = c.get("class", [])
                txt = c.get_text(strip=True)[:80]
                logger.info(f"[TM DEBUG]   Cell {ci}: classes={cls} text='{txt}'")

        player = _parse_squad_row(cells, row)
        if player:
            # Log first player's extracted data
            if row_idx < 2:
                logger.info(
                    f"[TM DEBUG] Parsed: {player['name']} | "
                    f"pos={player['tm_position']}→{player['position']} | "
                    f"age={player['age']} | club={player['club']} | "
                    f"country={player['club_country']} | "
                    f"caps={player['caps']} goals={player['goals']}"
                )
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
        jersey_text = cells[0].get_text(strip=True)
        if jersey_text.isdigit():
            jersey = int(jersey_text)

        # Player name + link
        player_link = (
            row.select_one("a.spielprofil_tooltip")
            or row.select_one("td.hauptlink a")
        )
        if not player_link:
            for a in row.find_all("a"):
                href = a.get("href", "")
                if "/profil/spieler/" in href:
                    player_link = a
                    break
        if not player_link:
            return None

        name = player_link.get_text(strip=True)
        href = player_link.get("href", "")

        # Extract TM player ID + slug
        tm_player_id = 0
        tm_player_slug = ""
        match = re.search(r"/profil/spieler/(\d+)", href)
        if match:
            tm_player_id = int(match.group(1))
        slug_match = re.search(r"^/([^/]+)/profil/", href)
        if slug_match:
            tm_player_slug = slug_match.group(1)

        # Position (multi-strategy)
        tm_position = _extract_position(row, cells)

        # Age (DOB-aware)
        age = _extract_age(cells)

        # Club + country
        club, club_country = _extract_club_info(cells)

        # Market value
        market_value = ""
        mv_cell = row.select_one("td.rechts.hauptlink")
        if mv_cell:
            market_value = mv_cell.get_text(strip=True)

        # Caps and goals
        caps, goals = _extract_caps_goals(cells, jersey, age)

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
    """
    Parse the tfoot summary row of a Transfermarkt stats table.

    The footer in the plus/1 (extended) view typically has:
    [empty/label, Apps, Goals, Assists, Yellow, 2ndYellow, Red, SubOn, SubOff, Minutes]

    We extract all non-None numerics and use position logic:
    - First value (< 100) = appearances
    - Last value (usually the largest) = minutes
    - After appearances: goals, assists, yellows (in order)
    """
    numerics = []
    for cell in cells:
        text = cell.get_text(strip=True).replace(".", "").replace("'", "").replace("-", "0")
        if text.isdigit():
            numerics.append(int(text))
        else:
            numerics.append(None)

    vals = [n for n in numerics if n is not None]
    if len(vals) < 2:
        return stats

    # Minutes is the last value (typically > 100 for any player with apps)
    if vals[-1] > 80:
        stats["minutes"] = vals[-1]
        vals = vals[:-1]  # remove minutes from the list

    # First value = appearances
    if vals and vals[0] < 100:
        stats["appearances"] = vals[0]

    # Remaining values: goals, assists, yellows (in that order)
    if len(vals) > 1:
        stats["goals"] = vals[1]
    if len(vals) > 2:
        stats["assists"] = vals[2]
    if len(vals) > 3:
        stats["yellow_cards"] = vals[3]
    # 2nd yellow at index 4, red at index 5 — less important but let's capture reds
    if len(vals) > 5:
        stats["red_cards"] = vals[5]

    return stats


def _aggregate_match_stats(rows: list, stats: dict) -> dict:
    """
    Aggregate stats from individual match rows in the TM performance table.

    TM's detailed stats table (leistungsdaten, plus/1 view) has columns:
    [Competition, Matchday, Date, Venue, Opponent, Result, Pos, Goals, Assists,
     Yellow, 2ndYellow, Red, SubOn, SubOff, Minutes]

    The exact column count and order can vary, so we parse each row by
    identifying the numeric values and their positions relative to the end
    of the row (goals/assists/cards/minutes are always the rightmost columns).
    """
    total_goals = 0
    total_assists = 0
    total_minutes = 0
    total_apps = 0
    total_yellows = 0
    total_reds = 0

    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 6:
            continue

        # Skip rows that are separators (e.g. competition headers)
        row_classes = row.get("class") or []
        if "bg_blau_20" in row_classes or "extrarow" in row_classes:
            continue

        total_apps += 1

        # Parse the rightmost numeric cells.
        # TM's column layout (from right to left): Minutes, SubOff, SubOn,
        # Red, 2ndYellow, Yellow, Assists, Goals, Pos, Result, ...
        # We grab all numeric values from the right side.
        numerics_from_right = []
        for cell in reversed(cells):
            text = (cell.get_text(strip=True)
                    .replace("'", "")   # minutes often have tick mark
                    .replace(".", "")   # thousands separator
                    .replace("-", "0")) # dash = 0
            if text.isdigit():
                numerics_from_right.append(int(text))
            elif numerics_from_right:
                # Stop once we hit a non-numeric cell after finding numbers
                break

        # numerics_from_right is [minutes, subOff, subOn, red, 2ndYellow, yellow, assists, goals]
        # (reading from right to left in the table)
        if len(numerics_from_right) >= 1:
            # Last column is minutes (usually the largest number)
            total_minutes += numerics_from_right[0]
        if len(numerics_from_right) >= 7:
            total_assists += numerics_from_right[6]
        if len(numerics_from_right) >= 8:
            total_goals += numerics_from_right[7]
        if len(numerics_from_right) >= 6:
            total_yellows += numerics_from_right[5]
        if len(numerics_from_right) >= 4:
            total_reds += numerics_from_right[3]

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
