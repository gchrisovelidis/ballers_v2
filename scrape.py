#!/usr/bin/env python3
"""
Air Ballers — Basketaki scraper
Fetches results, schedule and roster from basketaki.com
and writes data.json to the repo root.
Run locally:  python scrape.py
Run on CI:    triggered by GitHub Actions
"""

import json, re, sys
from datetime import datetime
from urllib.request import urlopen, Request
from urllib.error import URLError
from html.parser import HTMLParser

BASE  = "https://www.basketaki.com"
TEAM  = "air-ballers"
CDN   = "https://basketaki-web.b-cdn.net"
PAGES = {
    "results":  f"{BASE}/teams/{TEAM}/results",
    "schedule": f"{BASE}/teams/{TEAM}/schedule",
    "roster":   f"{BASE}/teams/{TEAM}/roster",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "el-GR,el;q=0.9,en;q=0.8",
}


def fetch(url):
    req = Request(url, headers=HEADERS)
    try:
        with urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8", errors="replace")
    except URLError as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return ""


# ── Minimal HTML table parser ──────────────────────────────────────────────────
class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables = []          # list of tables; each table = list of rows; each row = list of cells
        self._in_table = False
        self._in_row   = False
        self._in_cell  = False
        self._cell_buf = []
        self._row_buf  = []
        self._tbl_buf  = []
        self._links    = {}       # cell_index → href
        self._cell_idx = 0
        self._cur_href = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            self._in_table = True
            self._tbl_buf  = []
        elif tag == "tr" and self._in_table:
            self._in_row  = True
            self._row_buf = []
            self._cell_idx = 0
        elif tag in ("td", "th") and self._in_row:
            self._in_cell  = True
            self._cell_buf = []
        elif tag == "a" and self._in_cell:
            self._cur_href = a.get("href", "")
            if self._cur_href and not self._cur_href.startswith("http"):
                self._cur_href = BASE + self._cur_href
        elif tag == "img" and self._in_cell:
            src = a.get("src", "")
            # store img src as pseudo-text so we can extract team slugs
            self._cell_buf.append(f"[IMG:{src}]")

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
            text = " ".join("".join(self._cell_buf).split())
            self._row_buf.append(text)
            self._cell_idx += 1
            self._cur_href = None
        elif tag == "a":
            self._cur_href = None

    def handle_data(self, data):
        if self._in_cell:
            self._cell_buf.append(data)


def parse_tables(html):
    p = TableParser()
    p.feed(html)
    return p.tables


def slug_from_img(cell_text):
    m = re.search(r"\[IMG:.*?/teams/([^.]+)\.png\]", cell_text)
    return m.group(1) if m else ""


def title_case_greek(s):
    def cap_word(w):
        if not w:
            return w
        return w[0].upper() + w[1:].lower()
    return " ".join(cap_word(w) for w in s.split())


def format_name(full):
    """
    Input:  'ΠΑΝΟΥΧΟΣ ΑΡΓ. ΝΙΚΟΛΑΟΣ' or 'Ανδρεαδάκης Εμμ. Νικόλαος'
    Output: { surname: 'Πανούχος', firstName: 'Νικόλαος' }
    Strips middle abbreviation (2-4 chars ending in dot).
    """
    parts   = full.strip().split()
    surname = title_case_greek(parts[0]) if parts else ""
    rest    = [p for p in parts[1:] if not re.match(r"^[Α-Ωα-ωA-Za-z]{1,4}\.$", p)]
    first   = title_case_greek(rest[-1]) if rest else ""
    return {"surname": surname, "firstName": first, "raw": full.strip()}


# ── RESULTS ────────────────────────────────────────────────────────────────────
def parse_results(html):
    tables = parse_tables(html)
    # Look for the table that has W/L values
    result_table = None
    for t in tables:
        for row in t:
            if len(row) >= 5 and row[2].strip() in ("W", "L"):
                result_table = t
                break
        if result_table:
            break

    if not result_table:
        return []

    results = []
    for row in result_table:
        if len(row) < 5:
            continue
        wl = row[2].strip()
        if wl not in ("W", "L"):
            continue

        date     = row[0].strip()
        opp_raw  = row[1]
        opp_slug = slug_from_img(opp_raw)
        # Clean opponent name — remove [IMG:...] tokens
        opp_name = re.sub(r"\[IMG:[^\]]+\]", "", opp_raw).strip()
        score    = row[3].strip()   # "63 - 47"
        ha       = row[4].strip()   # Home / Away
        cat      = row[5].strip() if len(row) > 5 else ""
        # game link — look for /games/NNNNN in remaining cells
        game_id  = ""
        for cell in row[6:]:
            m = re.search(r"/games/(\d+)", cell)
            if m:
                game_id = m.group(1)
                break

        sm = re.match(r"(\d+)\s*-\s*(\d+)", score)
        ts, os_ = 0, 0
        if sm:
            if ha.lower() == "home":
                ts, os_ = int(sm.group(1)), int(sm.group(2))
            else:
                ts, os_ = int(sm.group(2)), int(sm.group(1))

        results.append({
            "date":     date,
            "opponent": opp_name,
            "oppSlug":  opp_slug,
            "ts":       ts,
            "os":       os_,
            "win":      wl == "W",
            "ha":       ha,
            "cat":      cat,
            "gameId":   game_id,
        })

    return results


# ── SCHEDULE ───────────────────────────────────────────────────────────────────
def parse_schedule(html):
    tables = parse_tables(html)
    now    = datetime.now()
    games  = []

    for table in tables:
        for row in table:
            if len(row) < 4:
                continue
            # TD[1] = "09/05/2026 22:00"
            dt_raw = row[1].strip()
            m = re.match(r"(\d{2})/(\d{2})/(\d{4})\s+(\d{2}):(\d{2})", dt_raw)
            if not m:
                continue

            opp_raw  = row[2]
            opp_slug = slug_from_img(opp_raw)
            opp_name = re.sub(r"\[IMG:[^\]]+\]", "", opp_raw).strip()
            ha       = row[3].strip()
            cat      = row[4].strip() if len(row) > 4 else ""
            venue    = row[7].strip() if len(row) > 7 else ""

            game_dt = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)),
                               int(m.group(4)), int(m.group(5)))
            is_future = game_dt > now

            games.append({
                "dateTime": dt_raw,
                "date":     f"{m.group(1)}/{m.group(2)}/{m.group(3)}",
                "time":     f"{m.group(4)}:{m.group(5)}",
                "opponent": opp_name,
                "oppSlug":  opp_slug,
                "ha":       ha,
                "venue":    venue,
                "cat":      cat,
                "isFuture": is_future,
                "ts":       game_dt.isoformat(),
            })
        if games:
            break

    return games


# ── ROSTER / STATS ──────────────────────────────────────────────────────────────
def parse_roster(html):
    tables = parse_tables(html)
    if not tables:
        return []

    # First table has real cumulative stats (others have zeros)
    first = tables[0]
    players = []

    for row in first:
        if len(row) < 10:
            continue
        name_raw = row[0].strip()
        # Skip header rows
        if not name_raw or name_raw in ("Παίχτης", "Player"):
            continue
        # Skip rows that are all zeros
        try:
            if all(float(v.replace(",", ".")) == 0 for v in row[1:5] if v.replace(",", ".").replace(".", "").isdigit()):
                continue
        except:
            pass

        gp   = row[1].strip()
        ppg  = row[6].strip()   # Ποντοι ΜΟ
        rpg  = row[12].strip()  # ΡΜΠ ΜΟ
        apg  = row[14].strip()  # ΑΣΙ ΜΟ
        spg  = row[16].strip()  # ΚΛΨ ΜΟ
        ftp  = row[22].strip() if len(row) > 22 else "—"
        twop = row[24].strip() if len(row) > 24 else "—"
        thrp = row[26].strip() if len(row) > 26 else "—"

        name_info = format_name(name_raw)

        players.append({
            "raw":       name_raw,
            "surname":   name_info["surname"],
            "firstName": name_info["firstName"],
            "gp":        gp,
            "ppg":       ppg,
            "rpg":       rpg,
            "apg":       apg,
            "spg":       spg,
            "ftp":       ftp,
            "twop":      twop,
            "thrp":      thrp,
        })

    return players


# ── MAIN ────────────────────────────────────────────────────────────────────────
def main():
    print("Air Ballers scraper starting...")
    print(f"  Timestamp: {datetime.now().isoformat()}")

    print("  Fetching results...")
    results_html  = fetch(PAGES["results"])
    print("  Fetching schedule...")
    schedule_html = fetch(PAGES["schedule"])
    print("  Fetching roster...")
    roster_html   = fetch(PAGES["roster"])

    results  = parse_results(results_html)
    schedule = parse_schedule(schedule_html)
    players  = parse_roster(roster_html)

    print(f"  Parsed: {len(results)} results, {len(schedule)} games, {len(players)} players")

    # Compute summary stats
    valid_results = [r for r in results if r["ts"] > 0 or r["os"] > 0]
    wins   = sum(1 for r in valid_results if r["win"])
    losses = len(valid_results) - wins
    pct    = round(wins / len(valid_results) * 100) if valid_results else 0

    streak_type, streak_count = "neutral", 0
    if results:
        cur = "win" if results[0]["win"] else "loss"
        streak_type = cur
        for r in results:
            if (r["win"] and cur == "win") or (not r["win"] and cur == "loss"):
                streak_count += 1
            else:
                break

    # Next game
    future = [g for g in schedule if g["isFuture"]]
    next_game = future[0] if future else None

    data = {
        "updated":      datetime.now().isoformat(),
        "record":       {"wins": wins, "losses": losses, "pct": pct},
        "streak":       {"type": streak_type, "count": streak_count},
        "nextGame":     next_game,
        "schedule":     schedule,
        "results":      results,
        "players":      players,
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print("  ✓ data.json written successfully")
    print(f"  Record: {wins}W – {losses}L ({pct}%)")
    if next_game:
        print(f"  Next game: vs {next_game['opponent']} on {next_game['dateTime']}")


if __name__ == "__main__":
    main()
