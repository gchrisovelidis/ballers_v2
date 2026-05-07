#!/usr/bin/env python3
"""
Air Ballers — Basketaki scraper
- Results & Schedule: Basketaki API (reliable JSON)
- Player stats:       HTML scraper (only available as HTML)
Writes data.json to the repo root.

Run locally:  python scrape.py
Run on CI:    triggered by GitHub Actions (.github/workflows/scrape.yml)
"""

import json, re, sys
from datetime import datetime
from urllib.request import urlopen, Request
from urllib.error import URLError
from html.parser import HTMLParser

# ── CONFIG ─────────────────────────────────────────────────────────────────────
TEAM_ID      = 558                          # Air Ballers numeric ID
CURRENT_SEASON = "25/26"                    # Filter results to this season
BASE         = "https://www.basketaki.com"
CDN          = "https://basketaki-web.b-cdn.net"
API_PROFILE  = f"{BASE}/api/v1/teams/{TEAM_ID}/profile"
ROSTER_URL   = f"{BASE}/teams/air-ballers/roster"  # HTML scrape for stats only

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json",
    "Accept-Language": "el-GR,el;q=0.9,en;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
}

HTML_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept-Language": HEADERS["Accept-Language"],
}


# ── HTTP HELPERS ───────────────────────────────────────────────────────────────
def fetch_json(url):
    """Fetch URL and return parsed JSON dict, or None on error."""
    req = Request(url, headers=HEADERS)
    try:
        with urlopen(req, timeout=20) as r:
            raw = r.read().decode("utf-8", errors="replace")
            return json.loads(raw)
    except (URLError, json.JSONDecodeError) as e:
        print(f"  ERROR fetching JSON {url}: {e}", file=sys.stderr)
        return None


def fetch_html(url):
    """Fetch URL and return raw HTML string."""
    req = Request(url, headers=HTML_HEADERS)
    try:
        with urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8", errors="replace")
    except URLError as e:
        print(f"  ERROR fetching HTML {url}: {e}", file=sys.stderr)
        return ""


# ── NAME HELPERS ──────────────────────────────────────────────────────────────
def title_case_greek(s):
    """Capitalise first letter of each word, lowercase the rest."""
    def cap_word(w):
        return w[0].upper() + w[1:].lower() if w else w
    return " ".join(cap_word(w) for w in s.split())


def format_name(full):
    """
    Input:  'ΠΑΝΟΥΧΟΣ ΑΡΓ. ΝΙΚΟΛΑΟΣ' or 'Ανδρεαδάκης Εμμ. Νικόλαος'
    Output: { surname: 'Πανούχος', firstName: 'Νικόλαος', raw: '...' }
    Strips middle abbreviation (1-4 chars ending in dot).
    """
    parts   = full.strip().split()
    surname = title_case_greek(parts[0]) if parts else ""
    rest    = [p for p in parts[1:] if not re.match(r"^[Α-Ωα-ωA-Za-z]{1,4}\.$", p)]
    first   = title_case_greek(rest[-1]) if rest else ""
    return {"surname": surname, "firstName": first, "raw": full.strip()}


# ── API: RESULTS ───────────────────────────────────────────────────────────────
def parse_api_results(games_raw):
    """
    Parse the 'results' array from the API profile response.
    Each game object has: home_team, away_team, gameResult, tournament, etc.
    """
    now     = datetime.now()
    results = []

    for g in games_raw:
        # Season filter
        season_name = g.get("tournament", {}).get("season", {}).get("name", "")
        if season_name and season_name != CURRENT_SEASON:
            continue

        gr = g.get("gameResult")
        if not gr:
            continue  # No score yet — skip

        home_id   = g.get("home_team")
        away_id   = g.get("away_team")
        home_score = gr.get("home_score_final", 0) or 0
        away_score = gr.get("away_score_final", 0) or 0
        home_win   = bool(gr.get("home_win"))

        # Determine if we are home or away
        we_are_home = (home_id == TEAM_ID)
        if we_are_home:
            ts, os_ = home_score, away_score
            opp_info = g.get("teamAway", {})
            ha = "Home"
            win = home_win
        else:
            ts, os_ = away_score, home_score
            opp_info = g.get("teamHome", {})
            ha = "Away"
            win = not home_win

        opp_name = opp_info.get("name", "Unknown")
        opp_id   = opp_info.get("id", "")
        # Build slug from CDN pattern: we'll use the slug field if present
        opp_slug = opp_info.get("slug", "")

        date_str  = g.get("gameDateSimple", "")   # "26/04/2026"
        game_id   = str(g.get("id", ""))
        cat       = g.get("tournament", {}).get("league", {}).get("name", "")

        results.append({
            "date":     date_str,
            "opponent": opp_name,
            "oppSlug":  opp_slug,
            "ts":       int(ts),
            "os":       int(os_),
            "win":      win,
            "ha":       ha,
            "cat":      cat,
            "gameId":   game_id,
        })

    return results


# ── API: SCHEDULE ──────────────────────────────────────────────────────────────
def parse_api_schedule(games_raw):
    """
    Parse the 'schedule' array from the API profile response.
    Only includes games without a result yet (upcoming).
    """
    now   = datetime.now()
    games = []

    for g in games_raw:
        game_date_str = g.get("game_date")  # ISO: "2026-05-09T22:00:00.000+03:00"

        # Parse game datetime
        game_dt = None
        is_future = False
        if game_date_str and game_date_str != "-":
            try:
                # Strip timezone offset for naive comparison
                dt_clean = re.sub(r"\.\d+[+-]\d{2}:\d{2}$", "", game_date_str)
                game_dt  = datetime.fromisoformat(dt_clean)
                is_future = game_dt > now
            except ValueError:
                pass

        # Home or away
        home_id   = g.get("home_team")
        away_id   = g.get("away_team")
        we_home   = (home_id == TEAM_ID)

        opp_info  = g.get("teamAway" if we_home else "teamHome", {})
        opp_name  = opp_info.get("name", "TBD")
        opp_slug  = opp_info.get("slug", "")
        ha        = "Home" if we_home else "Away"

        court     = g.get("court") or {}
        venue     = court.get("name", "") if court else ""
        cat       = g.get("tournament", {}).get("league", {}).get("name", "")

        # Formatted date/time
        date_simple = g.get("gameDateSimple", "-")   # "09/05/2026"
        time_str    = g.get("gameTime", "-")          # "22:00"
        date_time   = f"{date_simple} {time_str}" if date_simple != "-" else "-"

        games.append({
            "dateTime": date_time,
            "date":     date_simple,
            "time":     time_str,
            "opponent": opp_name,
            "oppSlug":  opp_slug,
            "ha":       ha,
            "venue":    venue,
            "cat":      cat,
            "isFuture": is_future,
            "ts":       game_dt.isoformat() if game_dt else None,
        })

    # Sort by date ascending
    games.sort(key=lambda x: x["ts"] or "9999")
    return games


# ── HTML SCRAPER: PLAYER STATS ─────────────────────────────────────────────────
class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables     = []
        self._in_table  = False
        self._in_row    = False
        self._in_cell   = False
        self._cell_buf  = []
        self._row_buf   = []
        self._tbl_buf   = []

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._in_table = True
            self._tbl_buf  = []
        elif tag == "tr" and self._in_table:
            self._in_row  = True
            self._row_buf = []
        elif tag in ("td", "th") and self._in_row:
            self._in_cell  = True
            self._cell_buf = []

    def handle_endtag(self, tag):
        if tag == "table":
            self._in_table = False
            self.tables.append(self._tbl_buf)
            self._tbl_buf = []
        elif tag == "tr" and self._in_row:
            self._in_row = False
            if any(c.strip() for c in self._row_buf):
                self._tbl_buf.append(self._row_buf)
        elif tag in ("td", "th") and self._in_cell:
            self._in_cell = False
            self._row_buf.append(" ".join("".join(self._cell_buf).split()))

    def handle_data(self, data):
        if self._in_cell:
            self._cell_buf.append(data)


def parse_player_stats(html):
    """
    Scrape player stats from the HTML roster page.
    Uses Table 0 (cumulative season stats).
    Column mapping (confirmed):
      [0]=name [1]=GP [6]=PPG [12]=RPG [14]=APG [16]=SPG
      [22]=FT% [24]=2P% [26]=3P%
    """
    p = TableParser()
    p.feed(html)
    if not p.tables:
        return []

    first   = p.tables[0]
    players = []

    for row in first:
        if len(row) < 10:
            continue
        name_raw = row[0].strip()
        if not name_raw or name_raw in ("Παίχτης", "Player"):
            continue
        # Skip all-zero rows (other season tables)
        try:
            if all(float(v.replace(",", ".") or "0") == 0
                   for v in row[3:7]
                   if v.replace(",", ".").replace(".", "").replace("-","").isdigit()):
                continue
        except Exception:
            pass

        name_info = format_name(name_raw)
        players.append({
            "raw":       name_raw,
            "surname":   name_info["surname"],
            "firstName": name_info["firstName"],
            "gp":        row[1].strip(),
            "ppg":       row[6].strip()              if len(row) > 6  else "—",
            "rpg":       row[12].strip()             if len(row) > 12 else "—",
            "apg":       row[14].strip()             if len(row) > 14 else "—",
            "spg":       row[16].strip()             if len(row) > 16 else "—",
            "ftp":       row[22].strip()             if len(row) > 22 else "—",
            "twop":      row[24].strip()             if len(row) > 24 else "—",
            "thrp":      row[26].strip()             if len(row) > 26 else "—",
        })

    return players


# ── MAIN ────────────────────────────────────────────────────────────────────────
def main():
    print("Air Ballers scraper starting...")
    print(f"  Timestamp: {datetime.now().isoformat()}")

    # ── 1. API call — results + schedule ──────────────────────────────────────
    print(f"  Fetching API: {API_PROFILE}")
    api_data = fetch_json(API_PROFILE)

    if not api_data:
        print("  ERROR: Could not fetch API data. Aborting.", file=sys.stderr)
        sys.exit(1)

    results_raw  = api_data.get("results", [])
    schedule_raw = api_data.get("schedule", [])

    print(f"  API returned: {len(results_raw)} results, {len(schedule_raw)} scheduled games")

    results  = parse_api_results(results_raw)
    schedule = parse_api_schedule(schedule_raw)

    print(f"  Parsed: {len(results)} results ({CURRENT_SEASON}), {len(schedule)} schedule entries")

    # ── 2. HTML scrape — player stats only ────────────────────────────────────
    print(f"  Fetching player stats HTML: {ROSTER_URL}")
    roster_html = fetch_html(ROSTER_URL)
    players     = parse_player_stats(roster_html)
    print(f"  Parsed: {len(players)} players")

    # ── 3. Compute summary stats ───────────────────────────────────────────────
    valid   = [r for r in results if r["ts"] > 0 or r["os"] > 0]
    wins    = sum(1 for r in valid if r["win"])
    losses  = len(valid) - wins
    pct     = round(wins / len(valid) * 100) if valid else 0

    streak_type, streak_count = "neutral", 0
    if results:
        cur = "win" if results[0]["win"] else "loss"
        streak_type = cur
        for r in results:
            if (r["win"] and cur == "win") or (not r["win"] and cur == "loss"):
                streak_count += 1
            else:
                break

    # ── 4. Next game ──────────────────────────────────────────────────────────
    future    = [g for g in schedule if g["isFuture"]]
    next_game = future[0] if future else None

    # ── 5. Write data.json ────────────────────────────────────────────────────
    data = {
        "updated":  datetime.now().isoformat(),
        "record":   {"wins": wins, "losses": losses, "pct": pct},
        "streak":   {"type": streak_type, "count": streak_count},
        "nextGame": next_game,
        "schedule": schedule,
        "results":  results,
        "players":  players,
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print("  ✓ data.json written successfully")
    print(f"  Record: {wins}W – {losses}L ({pct}%)")
    if next_game:
        print(f"  Next game: vs {next_game['opponent']} on {next_game['dateTime']}")
    else:
        print("  No upcoming games found")


if __name__ == "__main__":
    main()
