#!/usr/bin/env python3
"""
Air Ballers — Basketaki scraper
- Results & Schedule: Basketaki API (reliable JSON)
- Player stats:       HTML scraper (only available as HTML)
                      Combines stats across ALL leagues/tables (weighted averages)
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
TEAM_ID        = 558
CURRENT_SEASON = "25/26"
BASE           = "https://www.basketaki.com"
CDN            = "https://basketaki-web.b-cdn.net"
API_PROFILE    = f"{BASE}/api/v1/teams/{TEAM_ID}/profile"
ROSTER_URL     = f"{BASE}/teams/air-ballers/roster"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":           "application/json",
    "Accept-Language":  "el-GR,el;q=0.9,en;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
}

HTML_HEADERS = {
    "User-Agent":      HEADERS["User-Agent"],
    "Accept-Language": HEADERS["Accept-Language"],
}


# ── HTTP HELPERS ───────────────────────────────────────────────────────────────
def fetch_json(url):
    req = Request(url, headers=HEADERS)
    try:
        with urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except (URLError, json.JSONDecodeError) as e:
        print(f"  ERROR fetching JSON {url}: {e}", file=sys.stderr)
        return None


def fetch_html(url):
    req = Request(url, headers=HTML_HEADERS)
    try:
        with urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8", errors="replace")
    except URLError as e:
        print(f"  ERROR fetching HTML {url}: {e}", file=sys.stderr)
        return ""


# ── NAME HELPERS ───────────────────────────────────────────────────────────────
def title_case_greek(s):
    def cap_word(w):
        return w[0].upper() + w[1:].lower() if w else w
    return " ".join(cap_word(w) for w in s.split())


def format_name(full):
    parts   = full.strip().split()
    surname = title_case_greek(parts[0]) if parts else ""
    rest    = [p for p in parts[1:] if not re.match(r"^[Α-Ωα-ωA-Za-z]{1,4}\.$", p)]
    first   = title_case_greek(rest[-1]) if rest else ""
    return {"surname": surname, "firstName": first, "raw": full.strip()}


def safe_float(s):
    try:
        return float(str(s).replace(",", ".").strip())
    except (ValueError, TypeError):
        return 0.0


def fmt(val):
    """Format a float nicely — no trailing zeros."""
    if val == int(val):
        return str(int(val))
    return f"{val:.1f}"


# ── API: RESULTS ───────────────────────────────────────────────────────────────
def parse_api_results(games_raw):
    results = []
    for g in games_raw:
        season_name = g.get("tournament", {}).get("season", {}).get("name", "")
        if season_name and season_name != CURRENT_SEASON:
            continue
        gr = g.get("gameResult")
        if not gr:
            continue

        home_id    = g.get("home_team")
        home_score = gr.get("home_score_final", 0) or 0
        away_score = gr.get("away_score_final", 0) or 0
        home_win   = bool(gr.get("home_win"))
        we_home    = (home_id == TEAM_ID)

        if we_home:
            ts, os_, opp_info, ha, win = home_score, away_score, g.get("teamAway", {}), "Home", home_win
        else:
            ts, os_, opp_info, ha, win = away_score, home_score, g.get("teamHome", {}), "Away", not home_win

        results.append({
            "date":     g.get("gameDateSimple", ""),
            "opponent": opp_info.get("name", "Unknown"),
            "oppSlug":  opp_info.get("slug", ""),
            "ts":       int(ts),
            "os":       int(os_),
            "win":      win,
            "ha":       ha,
            "cat":      g.get("tournament", {}).get("league", {}).get("name", ""),
            "gameId":   str(g.get("id", "")),
        })
    return results


# ── API: SCHEDULE ──────────────────────────────────────────────────────────────
def parse_api_schedule(games_raw):
    now, games = datetime.now(), []
    for g in games_raw:
        game_date_str = g.get("game_date")
        game_dt, is_future = None, False
        if game_date_str and game_date_str != "-":
            try:
                dt_clean = re.sub(r"\.\d+[+-]\d{2}:\d{2}$", "", game_date_str)
                game_dt  = datetime.fromisoformat(dt_clean)
                is_future = game_dt > now
            except ValueError:
                pass

        we_home  = (g.get("home_team") == TEAM_ID)
        opp_info = g.get("teamAway" if we_home else "teamHome", {})
        court    = g.get("court") or {}
        date_s   = g.get("gameDateSimple", "-")
        time_s   = g.get("gameTime", "-")

        games.append({
            "dateTime": f"{date_s} {time_s}" if date_s != "-" else "-",
            "date":     date_s,
            "time":     time_s,
            "opponent": opp_info.get("name", "TBD"),
            "oppSlug":  opp_info.get("slug", ""),
            "ha":       "Home" if we_home else "Away",
            "venue":    court.get("name", "") if court else "",
            "cat":      g.get("tournament", {}).get("league", {}).get("name", ""),
            "isFuture": is_future,
            "ts":       game_dt.isoformat() if game_dt else None,
        })

    games.sort(key=lambda x: x["ts"] or "9999")
    return games


# ── HTML TABLE PARSER ──────────────────────────────────────────────────────────
class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables    = []
        self._in_table = self._in_row = self._in_cell = False
        self._cell_buf = []
        self._row_buf  = []
        self._tbl_buf  = []

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._in_table = True; self._tbl_buf = []
        elif tag == "tr" and self._in_table:
            self._in_row = True; self._row_buf = []
        elif tag in ("td", "th") and self._in_row:
            self._in_cell = True; self._cell_buf = []

    def handle_endtag(self, tag):
        if tag == "table":
            self._in_table = False
            self.tables.append(self._tbl_buf); self._tbl_buf = []
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


def parse_table_rows(table):
    """
    Extract player stats from one HTML table.
    Returns dict: name_raw → stat dict with raw totals and averages.
    Column mapping (confirmed from browser console):
      [0]=name [1]=GP  [3]=total_mins [4]=mins_avg
      [5]=total_pts  [6]=PPG
      [9]=total_reb  [12]=RPG
      [13]=total_ast [14]=APG
      [15]=total_stl [16]=SPG
      [21]=FT m/a    [22]=FT%
      [23]=2P m/a    [24]=2P%
      [25]=3P m/a    [26]=3P%
    """
    stats = {}
    for row in table:
        if len(row) < 10:
            continue
        name_raw = row[0].strip()
        if not name_raw or name_raw in ("Παίχτης", "Player"):
            continue

        gp = int(safe_float(row[1]))
        if gp == 0:
            continue  # skip zero-GP rows (other season breakdown tables)

        # Raw totals (for weighted combination)
        total_pts = safe_float(row[5]) if len(row) > 5  else 0
        total_reb = safe_float(row[9]) if len(row) > 9  else 0
        total_ast = safe_float(row[13]) if len(row) > 13 else 0
        total_stl = safe_float(row[15]) if len(row) > 15 else 0

        # Shooting: parse "made/attempted" strings
        def parse_shooting(cell):
            m = re.match(r"(\d+)/(\d+)", cell.strip())
            if m:
                return int(m.group(1)), int(m.group(2))
            return 0, 0

        ft_m,  ft_a  = parse_shooting(row[21]) if len(row) > 21 else (0, 0)
        p2_m,  p2_a  = parse_shooting(row[23]) if len(row) > 23 else (0, 0)
        p3_m,  p3_a  = parse_shooting(row[25]) if len(row) > 25 else (0, 0)

        stats[name_raw] = {
            "gp":        gp,
            "total_pts": total_pts,
            "total_reb": total_reb,
            "total_ast": total_ast,
            "total_stl": total_stl,
            "ft_m":      ft_m,  "ft_a":  ft_a,
            "p2_m":      p2_m,  "p2_a":  p2_a,
            "p3_m":      p3_m,  "p3_a":  p3_a,
        }
    return stats


def combine_stats(all_table_stats):
    """
    Combine player stats across multiple tables (leagues).
    Sums GP and raw totals, recalculates weighted averages.
    Returns list of player dicts ready for data.json.
    """
    # Merge: name → combined totals
    combined = {}

    for table_stats in all_table_stats:
        for name_raw, s in table_stats.items():
            if name_raw not in combined:
                combined[name_raw] = {
                    "gp": 0,
                    "total_pts": 0, "total_reb": 0,
                    "total_ast": 0, "total_stl": 0,
                    "ft_m": 0, "ft_a": 0,
                    "p2_m": 0, "p2_a": 0,
                    "p3_m": 0, "p3_a": 0,
                }
            c = combined[name_raw]
            c["gp"]        += s["gp"]
            c["total_pts"] += s["total_pts"]
            c["total_reb"] += s["total_reb"]
            c["total_ast"] += s["total_ast"]
            c["total_stl"] += s["total_stl"]
            c["ft_m"]      += s["ft_m"];  c["ft_a"]  += s["ft_a"]
            c["p2_m"]      += s["p2_m"];  c["p2_a"]  += s["p2_a"]
            c["p3_m"]      += s["p3_m"];  c["p3_a"]  += s["p3_a"]

    # Build output list
    players = []
    for name_raw, c in combined.items():
        gp = c["gp"]
        if gp == 0:
            continue

        ppg = c["total_pts"] / gp
        rpg = c["total_reb"] / gp
        apg = c["total_ast"] / gp
        spg = c["total_stl"] / gp

        def pct(made, att):
            return f"{round(made/att*100)}%" if att > 0 else "0%"

        name_info = format_name(name_raw)
        players.append({
            "raw":       name_raw,
            "surname":   name_info["surname"],
            "firstName": name_info["firstName"],
            "gp":        str(gp),
            "ppg":       fmt(ppg),
            "rpg":       fmt(rpg),
            "apg":       fmt(apg),
            "spg":       fmt(spg),
            "ftp":       pct(c["ft_m"], c["ft_a"]),
            "twop":      pct(c["p2_m"], c["p2_a"]),
            "thrp":      pct(c["p3_m"], c["p3_a"]),
        })

    # Sort by PPG descending
    players.sort(key=lambda p: safe_float(p["ppg"]), reverse=True)
    return players


def parse_player_stats(html):
    """
    Parse all non-zero tables from the roster page and combine stats.
    """
    parser = TableParser()
    parser.feed(html)

    if not parser.tables:
        print("  WARNING: No tables found in roster HTML", file=sys.stderr)
        return []

    all_table_stats = []
    for i, table in enumerate(parser.tables):
        table_stats = parse_table_rows(table)
        if table_stats:
            print(f"  Table {i}: {len(table_stats)} players with stats")
            all_table_stats.append(table_stats)
        else:
            print(f"  Table {i}: skipped (all zeros or empty)")

    players = combine_stats(all_table_stats)
    return players


# ── MAIN ────────────────────────────────────────────────────────────────────────
def main():
    print("Air Ballers scraper starting...")
    print(f"  Timestamp: {datetime.now().isoformat()}")

    # 1. API — results + schedule
    print(f"  Fetching API: {API_PROFILE}")
    api_data = fetch_json(API_PROFILE)
    if not api_data:
        print("  ERROR: Could not fetch API data. Aborting.", file=sys.stderr)
        sys.exit(1)

    results_raw  = api_data.get("results", [])
    schedule_raw = api_data.get("schedule", [])
    print(f"  API returned: {len(results_raw)} total results, {len(schedule_raw)} scheduled games")

    results  = parse_api_results(results_raw)
    schedule = parse_api_schedule(schedule_raw)
    print(f"  Parsed: {len(results)} results ({CURRENT_SEASON}), {len(schedule)} schedule entries")

    # 2. HTML — player stats (combined across all leagues)
    print(f"  Fetching player stats: {ROSTER_URL}")
    roster_html = fetch_html(ROSTER_URL)
    players     = parse_player_stats(roster_html)
    print(f"  Combined: {len(players)} players (across all leagues)")

    # 3. Summary stats
    valid  = [r for r in results if r["ts"] > 0 or r["os"] > 0]
    wins   = sum(1 for r in valid if r["win"])
    losses = len(valid) - wins
    pct    = round(wins / len(valid) * 100) if valid else 0

    streak_type, streak_count = "neutral", 0
    if results:
        cur = "win" if results[0]["win"] else "loss"
        streak_type = cur
        for r in results:
            if (r["win"] and cur == "win") or (not r["win"] and cur == "loss"):
                streak_count += 1
            else:
                break

    # 4. Next game
    future    = [g for g in schedule if g["isFuture"]]
    next_game = future[0] if future else None

    # 5. Write data.json
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
