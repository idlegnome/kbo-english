#!/usr/bin/env python3
"""
Post English-language KBO League updates to Bluesky (@kbo-english.bsky.social).

Four post types:

  schedule   A pre-game thread: (1) tonight's matchups and start times (KST),
             with (2) the probable starting pitchers threaded underneath.
  results    A nightly final-scores digest (every game's final in one post),
             then a compact box score threaded underneath per game.
  standings  A daily rank / W-L / games-back table (from the KBO English site).
  leaders    A weekly season-leaders thread: a lead post, then one reply per
             leaderboard (top 3), romanised via the KBO English player pages.

Every post also carries a rendered PNG card of the same information, built by
kbo_card via kbo_card_data. Cards are strictly additive: the text is unchanged
and complete on its own, each card is described in alt text, and a rendering
failure drops the image rather than the post.

schedule/results/leaders draw their game and stat data from Naver Sports' public
API; standings and the leaders' name romanisation read the KBO English site.
schedule runs in the morning, results in the evening, standings daily, leaders
weekly on Monday (a league off-day). Dedup is by (mode, date) in
kbo_history.json, so each card posts at most once per day.

Data sources (unauthenticated JSON, KST timestamps):
    .../schedule/games?categoryId=kbo&fromDate=...   scores, matchups, times
    .../schedule/games/{gameId}/preview              probable starters (Korean)
    .../schedule/games/{gameId}/record               box score (line score, W/L/S)
    .../statistics/.../top-players?playerType=...     season stat leaders

Team names come from the stable 2-letter TeamCode (see TEAMS), never from the
API's TeamName field, which flip-flops between Korean and English. Starting
pitchers are posted in Korean unless the pcode is in kbo_roster.json; leaderboard
names are romanised from the KBO English player pages and cached into that same
table, falling back to Korean on a miss.

Requires (only for a real post, not --dry-run):
    pip install atproto
    security add-generic-password -a "kbo-english.bsky.social" -s "kbobot-bluesky" -w

Usage:
    python3 kbo_post.py schedule  --dry-run          # tonight's games (today KST)
    python3 kbo_post.py results   --dry-run           # tonight's finals
    python3 kbo_post.py standings --dry-run           # today's standings
    python3 kbo_post.py leaders   --dry-run           # season stat leaders
    python3 kbo_post.py results   --dry-run --date 2026-07-16
    python3 kbo_post.py results   --dry-run --all      # ignore history (re-show)
    python3 kbo_post.py schedule                        # post for real (needs atproto)
"""

import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')

HANDLE = 'kbo-english.bsky.social'
KEYCHAIN_SERVICE = 'kbobot-bluesky'
HISTORY = Path(__file__).parent / 'kbo_history.json'
STATE = Path(__file__).parent / 'kbo_state.json'
ROSTER = Path(__file__).parent / 'kbo_roster.json'
RESULTS_ARCHIVE = Path(__file__).parent / 'kbo_results_history.json'

# KBO sends the top 5 to the postseason; the standings post draws a line there.
PLAYOFF_SPOTS = 5

API = ('https://api-gw.sports.naver.com/schedule/games'
       '?upperCategoryId=kbaseball&categoryId=kbo&fromDate={d}&toDate={d}')
PREVIEW_API = 'https://api-gw.sports.naver.com/schedule/games/{gid}/preview'
RECORD_API = 'https://api-gw.sports.naver.com/schedule/games/{gid}/record'

# Season stat leaders (weekly "leaders" post). One call per player type returns a
# fixed set of leaderboards; each ranking row also carries the stat value under a
# key matching the category type (e.g. row['hitterHra'] is the batting average).
TOP_PLAYERS_API = ('https://api-gw.sports.naver.com/statistics/categories/kbo/'
                   'seasons/{season}/top-players'
                   '?playerType={pt}&rankFlag=Y&limit={limit}&includeFields={fields}')

# KBO English player pages, by pcode (== Naver pcode). Used only by the leaders
# post to romanise leaderboard names it hasn't cached yet — hitters and pitchers
# live on different pages, so the lookup is keyed by which one the player is.
KBO_PLAYER_PAGE = {
    True:  'https://eng.koreabaseball.com/Teams/PlayerInfoPitcher/Summary.aspx?pcode={pc}',
    False: 'https://eng.koreabaseball.com/Teams/PlayerInfoHitter/Summary.aspx?pcode={pc}',
}

MAX_POST_CHARS = 290   # packing target: a conservative code-point buffer used to
                       # split threads (see pack_lines / compose_schedule).
BLUESKY_LIMIT = 300    # Bluesky's real per-post ceiling, counted in GRAPHEMES —
                       # the hard gate emit() checks (a flag emoji is 2 code
                       # points but 1 grapheme, so code-point length over-counts).

# Naver's stable 2-letter team codes -> full club names. These codes do not
# change even when a franchise rebrands (SK stayed "SK" after the SK Wyverns
# became the SSG Landers; OB stayed "OB" for the Doosan Bears), which is exactly
# why we key off them instead of the inconsistent TeamName field.
TEAMS = {
    'HT': 'KIA Tigers', 'SK': 'SSG Landers', 'LG': 'LG Twins', 'KT': 'KT Wiz',
    'LT': 'Lotte Giants', 'SS': 'Samsung Lions', 'OB': 'Doosan Bears',
    'NC': 'NC Dinos', 'WO': 'Kiwoom Heroes', 'HH': 'Hanwha Eagles',
}

# Team code -> emoji, keyed to the club nickname. Eight map cleanly to the
# animal/character in the name; Landers (🚀, evokes a landing craft) and Giants
# (🗿) are looser choices — swap freely, they carry no data meaning.
TEAM_EMOJI = {
    'HT': '🐯', 'SK': '🚀', 'LG': '👯', 'KT': '🧙', 'LT': '🗿',
    'SS': '🦁', 'OB': '🐻', 'NC': '🦖', 'WO': '🦸', 'HH': '🦅',
}

# Hashtags appended to the tagged post. #KBO is the community tag KBO fans
# follow; #baseball reaches the broader English-speaking baseball audience the
# bot exists to serve. Team tags are omitted since every post is a league-wide
# digest.
HASHTAGS = ['KBO', 'baseball']

# Short names for the compact standings table (full club names used elsewhere).
SHORT_NAMES = {
    'HT': 'KIA', 'SK': 'SSG', 'LG': 'LG', 'KT': 'KT', 'LT': 'Lotte',
    'SS': 'Samsung', 'OB': 'Doosan', 'NC': 'NC', 'WO': 'Kiwoom', 'HH': 'Hanwha',
}

# Standings come from the KBO official English site (authoritative order incl.
# tiebreakers), fetched once for the daily standings post — the one place the
# bot reads KBO English at post time (schedule/results stay Naver-only). If the
# page is unreachable the standings post simply skips.
STANDINGS_URL = 'https://eng.koreabaseball.com/Standings/TeamStandings.aspx'
STANDINGS_TEAM_CODE = {
    'SAMSUNG': 'SS', 'LG': 'LG', 'KT': 'KT', 'KIA': 'HT', 'DOOSAN': 'OB',
    'HANWHA': 'HH', 'NC': 'NC', 'LOTTE': 'LT', 'SSG': 'SK', 'KIWOOM': 'WO',
}

# KBO lists every player surname-first ("WELLS Lachlan"); we flip Western
# imports to first-last ("Lachlan Wells"). Foreign is known from the roster
# table (salary currency). East-Asian imports (Japanese/Taiwanese/Chinese) are
# also foreign but their names are already correctly surname-first, so they are
# kept via this pcode set — maintain by hand when a new one arrives (rare).
# Seeded 2026-07-17: 54843 Shirakawa Keisho (JP), 56719 Wang Yan-Cheng (TW).
KEEP_SURNAME_FIRST = {'54843', '56719'}

# Naver statusCode values: BEFORE (scheduled), STARTED/READY (in progress),
# RESULT (final), CANCEL (postponed).
FINAL = 'RESULT'

# Leaders post — (API category key, display label), rendered top 3 each. The key
# is both the leaderboard's `type` and the stat field on each row. includeFields
# nudges the API to include these; it returns a fixed default set regardless.
HITTING_LEADERS = [('hitterHra', 'Batting average'),
                   ('hitterHr', 'Home runs'),
                   ('hitterRbi', 'R.B.I.s')]
PITCHING_LEADERS = [('pitcherEra', 'E.R.A.'),
                    ('pitcherWin', 'Wins'),
                    ('pitcherSave', 'Saves'),
                    ('pitcherKk', 'Strikeouts')]
LEADER_FIELDS = {'HITTER': 'offenseHra,offenseHr,offenseRbi',
                 'PITCHER': 'defenseEra,defenseWin,defenseSave,defenseKk'}
# Rate-stat boards are limited to qualified players (batting-title / ERA-title);
# counting-stat boards (HR, RBI, W, SV, K) include everyone.
QUALIFIED_ONLY = {'hitterHra', 'pitcherEra'}


def team_label(code):
    """'HT' -> '🐯 KIA Tigers' (emoji + name), or just the name if no emoji."""
    name = TEAMS.get(code, code)
    emoji = TEAM_EMOJI.get(code)
    return f'{emoji} {name}' if emoji else name


def fetch_text(url):
    """GET a URL as text via curl (not urllib — Homebrew Python 3.13's urllib
    fails TLS cert verification on this machine)."""
    result = subprocess.run(
        ['curl', '-s', '--max-time', '30',
         '-H', 'User-Agent: Mozilla/5.0', '-H', 'Accept: application/json', url],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f'curl failed ({result.returncode}) fetching {url}')
    return result.stdout


def fetch_games(date_str):
    """The day's KBO games as a list of dicts."""
    try:
        data = json.loads(fetch_text(API.format(d=date_str)))
    except json.JSONDecodeError:
        raise RuntimeError('Non-JSON from schedule API')
    if not data.get('success'):
        raise RuntimeError(f'Schedule API failure: {data.get("code")}')
    return data.get('result', {}).get('games', []) or []


def fetch_starters(game_id):
    """(away, home) probable starters for one game, each a dict with the Korean
    name and season w / l / era, or None if not yet announced. Returns
    (None, None) on any failure so a missing preview degrades to no pitcher
    line rather than crashing the run."""
    try:
        pd = json.loads(fetch_text(PREVIEW_API.format(gid=game_id)))
        pd = pd.get('result', {}).get('previewData', {})
    except (RuntimeError, json.JSONDecodeError):
        return None, None

    def starter(side):
        s = pd.get(side) or {}
        info = s.get('playerInfo') or {}
        name = (info.get('name') or '').strip()
        if not name:
            return None
        st = s.get('currentSeasonStats') or {}
        return {'name_ko': name, 'pcode': info.get('pCode'),
                'w': st.get('w'), 'l': st.get('l'), 'era': st.get('era')}

    return starter('awayStarter'), starter('homeStarter')


def fetch_box_score(game_id):
    """A finished game's box-score record (Naver /record), or None on any
    failure so a missing box score drops that game's reply rather than
    crashing the results thread."""
    try:
        data = json.loads(fetch_text(RECORD_API.format(gid=game_id)))
    except (RuntimeError, json.JSONDecodeError):
        return None
    if not data.get('success'):
        return None
    return data.get('result', {}).get('recordData') or None


def format_date(date_str):
    """'2026-07-16' -> '16 Jul' (UK day-month)."""
    d = datetime.strptime(date_str, '%Y-%m-%d')
    return f'{d.day} {d:%b}'


def format_time(dt_iso):
    """'2026-07-17T18:30:00' -> '6:30 p.m.'; on-the-hour times drop the ':00'
    -> '6 p.m.' (KST; lowercase a.m./p.m.)."""
    t = datetime.strptime(dt_iso, '%Y-%m-%dT%H:%M:%S')
    hour = t.hour % 12 or 12
    meridiem = 'a.m.' if t.hour < 12 else 'p.m.'
    return f'{hour} {meridiem}' if t.minute == 0 else f'{hour}:{t.minute:02d} {meridiem}'


def final_innings(status_info):
    """Innings played for a finished game, from statusInfo ('9회말' -> 9).
    A normal game ends at 9; less means rain-shortened, more means extras."""
    m = re.search(r'(\d+)\s*회', status_info or '')
    return int(m.group(1)) if m else None


def by_start(games):
    return sorted(games, key=lambda g: g.get('gameDateTime', ''))


def result_line(game):
    """'🐯 KIA Tigers 0 @ 🚀 SSG Landers 6', with an inning tag if the game
    didn't go a regulation 9 (rain-shortened or extras)."""
    a, h = game['awayTeamScore'], game['homeTeamScore']
    line = (f'{team_label(game["awayTeamCode"])} {a} @ '
            f'{team_label(game["homeTeamCode"])} {h}')
    inn = final_innings(game.get('statusInfo'))
    if inn and inn != 9:
        line += f' ({inn})'
    return line


def schedule_line(game, show_time=True):
    """'🐯 KIA Tigers @ 🚀 SSG Landers · 6:30 p.m.' — the time is dropped when the
    header already states a single shared start time."""
    line = f'{team_label(game["awayTeamCode"])} @ {team_label(game["homeTeamCode"])}'
    if show_time:
        line += f' · {format_time(game["gameDateTime"])}'
    return line


def load_roster():
    if ROSTER.exists():
        return json.loads(ROSTER.read_text())
    return {}


def order_name(raw, foreign, pcode):
    """KBO's ALL-CAPS surname-first form -> display form. Surname is title-cased
    ('KIM' -> 'Kim'); Western imports get the surname moved to the end
    ('WELLS Lachlan' -> 'Lachlan Wells'). Korean players and East-Asian imports
    (KEEP_SURNAME_FIRST) stay surname-first."""
    parts = raw.split()
    if parts:
        parts[0] = parts[0].capitalize()
    if foreign and pcode not in KEEP_SURNAME_FIRST and len(parts) >= 2:
        parts = parts[1:] + parts[:1]
    return ' '.join(parts)


def display_name(starter, roster):
    """Romanised name from the roster table, or the Korean name if the pcode
    isn't in the table yet (so the post never depends on a live KBO lookup)."""
    pcode = str(starter.get('pcode') or '')
    entry = roster.get(pcode)
    if entry:
        return order_name(entry['name'], entry.get('foreign', False), pcode)
    return starter['name_ko']


def starter_label(code, starter, roster):
    """'🐯 Shirakawa Keisho (2-3, 4.88)' — emoji, name, season W-L and ERA.
    Stats are appended only when present; an unannounced starter shows TBD."""
    emoji = TEAM_EMOJI.get(code, '')
    if not starter:
        return f'{emoji} TBD'.strip()
    text = display_name(starter, roster)
    if starter['w'] is not None and starter['l'] is not None and starter['era']:
        text += f' ({starter["w"]}-{starter["l"]}, {starter["era"]})'
    return f'{emoji} {text}'.strip()


def starters_line(item, roster):
    """'🐯 Shirakawa Keisho (2-3, 4.88) vs 🚀 Kim Min Jun (2-1, 4.18)'"""
    g = item['game']
    return (f'{starter_label(g["awayTeamCode"], item["away"], roster)} vs '
            f'{starter_label(g["homeTeamCode"], item["home"], roster)}')


def fetch_standings():
    """Parse the KBO English standings table into ranked rows, or [] on failure
    (so a bad fetch skips the post rather than crashing)."""
    try:
        html = fetch_text(STANDINGS_URL)
    except RuntimeError:
        return []
    rows = []
    for tr in re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.S):
        cells = [re.sub(r'<[^>]+>', '', c).strip()
                 for c in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', tr, re.S)]
        cells = [c for c in cells if c]
        # standings rows look like: rank, TEAM, games, W, L, D, .PCT, GB, streak
        if len(cells) >= 8 and cells[0].isdigit() and re.match(r'0?\.\d+$', cells[6]):
            rows.append({'rank': int(cells[0]), 'team': cells[1].upper(),
                         'w': cells[3], 'l': cells[4], 'gb': cells[7]})
    return rows


def compose_standings(date_str, rows):
    """Ranked standings post: '1. 🦁 Samsung 52-32', GB after a middot, and a
    postseason cutline after 5th (KBO sends its top 5 to the playoffs).

    The cutline only appears in the final month of the regular season, matching
    the card that accompanies this post — the two are read together, so they
    must agree."""
    import kbo_card_data
    cutline = kbo_card_data.show_cutline(
        datetime.strptime(date_str, '%Y-%m-%d').date())
    lines = []
    for r in rows:
        code = STANDINGS_TEAM_CODE.get(r['team'], r['team'])
        emoji = TEAM_EMOJI.get(code, '')
        name = SHORT_NAMES.get(code, r['team'].title())
        gb = '' if r['gb'] in ('0.0', '0', '-') else f' · {r["gb"]}'
        lines.append(f'{r["rank"]}. {emoji} {name} {r["w"]}-{r["l"]}{gb}'.strip())
        if r['rank'] == PLAYOFF_SPOTS and cutline:
            lines.append('— postseason —')
    body = (f'🇰🇷⚾ Standings · {format_date(date_str)}\n(W-L · games back)\n\n'
            + '\n'.join(lines) + '\n\n')
    return [(body, HASHTAGS)]


def fetch_leaders(season):
    """{category_key: [ranking rows]} for the season-leaders post, from Naver's
    top-players endpoint (one call per player type). Empty on failure so the post
    skips rather than crashing."""
    out = {}
    for pt, cats in (('HITTER', HITTING_LEADERS), ('PITCHER', PITCHING_LEADERS)):
        url = TOP_PLAYERS_API.format(season=season, pt=pt, limit=6,
                                     fields=LEADER_FIELDS[pt])
        try:
            data = json.loads(fetch_text(url))
        except (RuntimeError, json.JSONDecodeError):
            continue
        by_type = {c['type']: c.get('rankings', [])
                   for c in data.get('result', {}).get('topPlayers', [])}
        for key, _label in cats:
            if by_type.get(key):
                out[key] = by_type[key]
    return out


def fetch_kbo_name(pcode, is_pitcher):
    """{'name', 'foreign'} from the KBO English player page, or None. Name is the
    raw ALL-CAPS surname-first form; foreign is inferred from salary currency
    ($ = import). Hitters and pitchers live on different pages."""
    try:
        html = fetch_text(KBO_PLAYER_PAGE[is_pitcher].format(pc=pcode))
    except RuntimeError:
        return None
    m = re.search(r'<b>Name</b>\s*:\s*([^<]+?)\s*<', html)
    if not m:
        return None
    sal = re.search(r'<b>Salary</b>\s*:\s*([^<]+)<', html)
    return {'name': m.group(1).strip(), 'foreign': bool(sal and '$' in sal.group(1))}


def resolve_name(pcode, name_ko, is_pitcher, roster, added):
    """Romanised display name for a leaderboard player, fetching + caching into
    the roster on a miss (appending (pcode, entry) to `added`), or the Korean
    name if the KBO lookup fails."""
    pcode = str(pcode or '')
    entry = roster.get(pcode)
    if entry is None and pcode:
        entry = fetch_kbo_name(pcode, is_pitcher)
        if entry:
            roster[pcode] = entry
            added.append((pcode, entry))
    if entry:
        return order_name(entry['name'], entry.get('foreign', False), pcode)
    return name_ko


def fmt_leader_value(key, value):
    """Batting average as '.360', E.R.A. as '2.19', counting stats as integers."""
    if key == 'hitterHra':
        return f'{float(value):.3f}'.lstrip('0')
    if key == 'pitcherEra':
        return f'{float(value):.2f}'
    return str(int(round(float(value))))


def leader_rows(key, rankings, roster, added):
    """Up to three (rank, name, teamCode, value) tuples for one leaderboard,
    filtering rate stats to qualified players."""
    is_pitcher = key.startswith('pitcher')
    rows = []
    for r in rankings:
        if key in QUALIFIED_ONLY and not r.get('isQualified'):
            continue
        name = resolve_name(r.get('playerId'), r.get('playerName', ''),
                            is_pitcher, roster, added)
        rows.append((r.get('ranking'), name, r.get('teamId', ''),
                     fmt_leader_value(key, r.get(key))))
        if len(rows) == 3:
            break
    return rows


def leader_block(label, rows):
    """One leaderboard as a text block: a label then three ranked lines, each
    'rank. TEAM Player · value' with the team as its short name (e.g. Lotte).
    Names carry the team plainly rather than by emoji, so a reader who doesn't
    know the club emojis can still tell who's who."""
    lines = [label]
    for rank, name, team, val in rows:
        abbr = SHORT_NAMES.get(team, team)
        lines.append(f'{rank}. {abbr} {name} · {val}'.strip())
    return '\n'.join(lines)


def compose_results(date_str, finals, cancelled=()):
    """Final-scores digest, with a Postponed section listing any cancelled games
    rather than dropping them."""
    parts = [f'🇰🇷⚾ Final scores · {format_date(date_str)}']
    if finals:
        parts.append('\n'.join(result_line(g) for g in by_start(finals)))
    if cancelled:
        pp = '\n'.join(f'{team_label(g["awayTeamCode"])} @ {team_label(g["homeTeamCode"])}'
                       for g in by_start(cancelled))
        parts.append(f'Postponed:\n{pp}')
    return [('\n\n'.join(parts) + '\n\n', HASHTAGS)]


def hits_errors_line(record):
    """'Hits: 18–5 · Errors: 2–1' (away–home), or '' if the line score is
    missing. En-dash separates the two team totals."""
    r = record.get('scoreBoard', {}).get('rheb', {})
    a, h = r.get('away'), r.get('home')
    if not a or not h:
        return ''
    return (f'Hits: {a.get("h", 0)}–{h.get("h", 0)} · '
            f'Errors: {a.get("e", 0)}–{h.get("e", 0)}')


def decision_line(record, roster, added):
    """'W: Naile (6-5) · L: Hatch (1-4) · S: Lee Young-ha (14)' — the winning,
    losing and (if any) saving pitcher, romanised, with season W-L or save
    count. Holds are omitted. '' if no decision parses."""
    by_result = {p.get('wls'): p for p in record.get('pitchingResult', [])}
    parts = []
    for code, tag in (('W', 'W'), ('L', 'L'), ('S', 'S')):
        p = by_result.get(code)
        if not p:
            continue
        name = resolve_name(p.get('pCode'), p.get('name', ''), True, roster, added)
        detail = p.get('s', 0) if code == 'S' else f'{p.get("w", 0)}-{p.get("l", 0)}'
        parts.append(f'{tag}: {name} ({detail})')
    return ' · '.join(parts)


def hr_labels(game, record, roster, added):
    """Every batter with a home run, as '🐯 Kim Do-yeong' labels (team emoji +
    romanised name; a multi-homer game shows the count), away side first."""
    labels = []
    for side, code in (('away', game['awayTeamCode']),
                       ('home', game['homeTeamCode'])):
        emoji = TEAM_EMOJI.get(code, '')
        for b in record.get('battersBoxscore', {}).get(side, []):
            if b.get('hr', 0) > 0:
                name = resolve_name(b.get('playerCode'), b.get('name', ''),
                                    False, roster, added)
                label = f'{emoji} {name}'.strip()
                if b['hr'] > 1:
                    label += f' ({b["hr"]})'
                labels.append(label)
    return labels


def box_score_body(game, record, roster, added):
    """One game's compact box score as a post body: the matchup and final
    (with a non-regulation inning tag), then hits/errors, the pitching
    decision, and any home runs. The HR list is trimmed to keep the post under
    Bluesky's limit, appending '(+N more)' when batters are dropped (a slugfest
    with long names could otherwise overflow)."""
    a, h = game['awayTeamScore'], game['homeTeamScore']
    head = (f'{team_label(game["awayTeamCode"])} {a} @ '
            f'{team_label(game["homeTeamCode"])} {h}')
    inn = final_innings(game.get('statusInfo'))
    if inn and inn != 9:
        head += f' ({inn})'
    base = [head, '']
    for line in (hits_errors_line(record),
                 decision_line(record, roster, added)):
        if line:
            base.append(line)
    labels = hr_labels(game, record, roster, added)

    for n in range(len(labels), -1, -1):        # try all HRs, then trim from end
        if n == len(labels) and labels:
            hr = 'HR: ' + ', '.join(labels)
        elif n > 0:
            hr = 'HR: ' + ', '.join(labels[:n]) + f' (+{len(labels) - n} more)'
        else:
            hr = ''                             # n == 0: drop the HR line
        body = '\n'.join(base + ([hr] if hr else [])) + '\n\n'
        if grapheme_len(plain_text(body, [])) <= BLUESKY_LIMIT:
            return body
    return '\n'.join(base) + '\n\n'             # base alone over limit (unreachable)


def tags_footer(tags):
    """The rendered hashtag line appended to a post, or '' if no tags. A blank
    line separates it from the body regardless of the body's trailing newlines."""
    return ('\n\n' + ' '.join(f'#{t}' for t in tags)) if tags else ''


def pack_lines(lines, header, reserve=0):
    """Pack lines into as few post bodies as fit under the char limit, split as
    evenly as possible (so 5 lines become 3+2, not 4+1). The first post carries
    `header`; continuation posts carry just lines. `reserve` leaves room for a
    footer (e.g. hashtags) appended to every post at render time."""
    def build(chunks):
        return [('' if i else header) + '\n'.join(c) for i, c in enumerate(chunks)]

    def fits(bodies):
        return all(len(b) + reserve <= MAX_POST_CHARS for b in bodies)

    for n in range(1, len(lines) + 1):
        size = -(-len(lines) // n)                      # ceil(len/n)
        chunks = [lines[i:i + size] for i in range(0, len(lines), size)]
        bodies = build(chunks)
        if len(chunks) <= n and fits(bodies):
            return bodies
    return build([[ln] for ln in lines])                # fallback: one per post


def compose_schedule(date_str, items, roster):
    """Schedule thread: a matchups post, plus one or more probable-starters
    replies (chunked to fit) if any starter is announced. Only the matchups post
    carries the hashtags. Returns a list of (body, tags) segments."""
    items = sorted(items, key=lambda it: it['game'].get('gameDateTime', ''))
    games = [it['game'] for it in items]
    # If every game starts at the same time, say so once in the header and drop
    # the per-line times; otherwise show the time on each line.
    times = {format_time(g['gameDateTime']) for g in games}
    uniform = len(times) == 1 and len(games) > 1
    head = f'🇰🇷⚾ Tonight’s games · {format_date(date_str)}'
    if uniform:
        head += f' (all games start at {next(iter(times))})'
    lines = '\n'.join(schedule_line(g, show_time=not uniform) for g in games)
    matchups = f'{head}\n\n{lines}\n\n'
    segments = [(matchups, HASHTAGS)]

    if any(it['away'] or it['home'] for it in items):
        pitch_lines = [starters_line(it, roster) for it in items]
        header = '🇰🇷⚾ Probable starters\n(W-L, E.R.A.)\n\n'
        # Starters replies carry no hashtags — only the matchups post is tagged.
        for body in pack_lines(pitch_lines, header):
            segments.append((body, []))
    return segments


def plain_text(body, tags):
    return body.rstrip('\n') + tags_footer(tags)


def grapheme_len(s):
    """Grapheme-cluster count, matching how Bluesky measures a post's length.
    Handles the multi-scalar emoji we actually use — regional-indicator flag
    pairs (🇰🇷), ZWJ sequences, variation selectors and skin-tone modifiers —
    so a flag counts as 1, not the 2 code points len() would report."""
    n = 0
    prev_ri = prev_zwj = False
    for ch in s:
        cp = ord(ch)
        if cp == 0x200D:                              # ZWJ joins the next scalar
            prev_zwj = True
            continue
        if cp == 0xFE0F or 0x1F3FB <= cp <= 0x1F3FF:  # VS16 / skin tone: combines
            continue
        if prev_zwj:
            prev_zwj = False
            continue
        if 0x1F1E6 <= cp <= 0x1F1FF:                  # regional indicator
            if prev_ri:                               # 2nd half of a flag pair
                prev_ri = False
                continue
            prev_ri = True
            n += 1
            continue
        prev_ri = False
        n += 1
    return n


def keychain_password(account, service):
    result = subprocess.run(
        ['security', 'find-generic-password', '-a', account, '-s', service, '-w'],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(
            f'No Keychain password for account="{account}" service="{service}".\n'
            f'Add it with:\n'
            f'  security add-generic-password -a "{account}" -s "{service}" -w'
        )
    return result.stdout.strip()


def build_tb(body, tags):
    from atproto import client_utils
    tb = client_utils.TextBuilder()
    tb.text(body.rstrip('\n'))
    if tags:
        tb.text('\n\n')
        for i, tag in enumerate(tags):
            if i:
                tb.text(' ')
            tb.tag(f'#{tag}', tag)
    return tb


# --------------------------------------------------------------------------
# Cards. Each post can carry a rendered PNG of the same information. Rendering
# needs Chrome and Pillow, so it is always attempted inside build_card(): if
# anything fails the post still goes out as plain text, which is what it was
# before cards existed. A missed image is not worth a missed post.
# --------------------------------------------------------------------------

def build_card(render, alt):
    """Render one card and return {'png', 'alt', 'size'}, or None if rendering
    failed for any reason. `render` is a zero-argument callable that writes a
    PNG to the path it is given and returns (path, (w, h))."""
    import tempfile
    try:
        import kbo_card
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / 'card.png')
            _, size = render(path)
            return {'png': Path(path).read_bytes(), 'alt': alt, 'size': size}
    except Exception as exc:                # noqa: BLE001 - never block a post
        print(f'  (card not rendered: {exc.__class__.__name__}: {exc})')
        return None


def seg_parts(segment):
    """Segments are (body, tags) or (body, tags, card). Normalise to three."""
    body, tags = segment[0], segment[1]
    card = segment[2] if len(segment) > 2 else None
    return body, tags, card


def with_card(segments, index, card):
    """Return `segments` with `card` attached to the segment at `index`."""
    if not card:
        return segments
    out = list(segments)
    body, tags, _ = seg_parts(out[index])
    out[index] = (body, tags, card)
    return out


def attach_results_card(date_str, finals, segments):
    """The final-scores digest gets a card of the same slate."""
    import kbo_card
    import kbo_card_data as data
    records = {g['gameId']: fetch_box_score(g['gameId']) for g in finals}
    rows = data.results_input(finals, {k: v for k, v in records.items() if v})
    label = data.card_date(date_str)
    card = build_card(
        lambda path: kbo_card.render_results_card(label, rows, path),
        data.results_alt(label, rows))
    return with_card(segments, 0, card)


def box_score_segments(finals, roster, added):
    """A box-score reply per finished game, each carrying its own card."""
    import kbo_card
    import kbo_card_data as data
    segments = []
    for g in by_start(finals):
        record = fetch_box_score(g['gameId'])
        if not record:
            continue
        body = box_score_body(g, record, roster, added)
        game = data.box_input(g, record, roster, added)
        label = data.card_date(f'{g["gameId"][:4]}-{g["gameId"][4:6]}-{g["gameId"][6:8]}')
        card = build_card(
            lambda path, game=game, label=label:
                kbo_card.render_box_score_card(label, game, path),
            data.box_alt(label, game))
        segments.append((body, [], card))
    return segments


def attach_schedule_card(date_str, playable, roster, segments):
    """Tonight's fixtures and their probable starters, on one card. The text
    post splits those across a post and a reply; the card holds both."""
    import kbo_card
    import kbo_card_data as data
    rows, subtitle = data.schedule_input(playable, roster)
    label = data.card_date(date_str)
    card = build_card(
        lambda path: kbo_card.render_schedule_card(label, rows, path,
                                                   subtitle=subtitle),
        data.schedule_alt(label, rows, subtitle))
    return with_card(segments, 0, card)


def attach_standings_card(date_str, rows, segments):
    import kbo_card
    import kbo_card_data as data
    card_rows = data.standings_input(rows)
    label = data.card_date(date_str)
    on = datetime.strptime(date_str, '%Y-%m-%d').date()
    cut = PLAYOFF_SPOTS if data.show_cutline(on) else None
    card = build_card(
        lambda path: kbo_card.render_standings_card(label, card_rows, path,
                                                    cut_after=cut),
        data.standings_alt(label, card_rows, cut))
    return with_card(segments, 0, card)


def leaders_segments(date_str, raw, roster, added):
    """A lead post, then one reply per leaderboard carrying that board's card.

    Each board appears once. Where a card renders, the reply is the card and
    its title, with the standings themselves in the alt text; where rendering
    failed, the reply falls back to the old text block so the numbers still go
    out. Returns [] if no board has data."""
    import kbo_card
    import kbo_card_data as data
    label = data.card_date(date_str)
    boards = []
    for key, title in HITTING_LEADERS + PITCHING_LEADERS:
        top = leader_rows(key, raw.get(key, []), roster, added)
        if not top:
            continue
        rows = data.leaders_input(top)
        card = build_card(
            lambda path, title=title, rows=rows:
                kbo_card.render_leaders_card(label, title, rows, path),
            data.leaders_alt(label, title, rows))
        if card:
            boards.append((f'{title}\n\n', [], card))
        else:
            boards.append((leader_block(title, top) + '\n\n', [], None))
    if not boards:
        return []
    lead = f'🇰🇷⚾ KBO season leaders · {format_date(date_str)}\n\n'
    return [(lead, HASHTAGS)] + boards


def post_thread(segments):
    """Post one or more segments as a Bluesky thread (each replies to the last).
    atproto is imported lazily so --dry-run runs without the dependency."""
    from atproto import Client, models

    password = keychain_password(HANDLE, KEYCHAIN_SERVICE)
    bsky = Client()
    bsky.login(HANDLE, password)

    root_ref = parent_ref = None
    for segment in segments:
        body, tags, card = seg_parts(segment)
        reply = None
        if root_ref is not None:
            reply = models.AppBskyFeedPost.ReplyRef(root=root_ref, parent=parent_ref)
        if card:
            width, height = card['size']
            resp = bsky.send_image(
                text=build_tb(body, tags), image=card['png'],
                image_alt=card['alt'], reply_to=reply,
                image_aspect_ratio=models.AppBskyEmbedDefs.AspectRatio(
                    width=width, height=height))
        else:
            resp = bsky.send_post(text=build_tb(body, tags), reply_to=reply)
        ref = models.create_strong_ref(resp)
        if root_ref is None:
            root_ref = ref
        parent_ref = ref


def load_history():
    if HISTORY.exists():
        return json.loads(HISTORY.read_text())
    return {}


def archive_results(date_str, finals, cancelled):
    """Append the day's finals + postponements to kbo_results_history.json, a
    structured season archive for a future standings/results page."""
    arch = json.loads(RESULTS_ARCHIVE.read_text()) if RESULTS_ARCHIVE.exists() else {}
    arch[date_str] = {
        'finals': [{'away': g['awayTeamCode'], 'home': g['homeTeamCode'],
                    'away_score': g['awayTeamScore'], 'home_score': g['homeTeamScore'],
                    'innings': final_innings(g.get('statusInfo'))}
                   for g in by_start(finals)],
        'postponed': [{'away': g['awayTeamCode'], 'home': g['homeTeamCode']}
                      for g in by_start(cancelled)],
    }
    RESULTS_ARCHIVE.write_text(json.dumps(arch, ensure_ascii=False, indent=2, sort_keys=True))


def results_candidates(argv):
    """Dates to try for the results digest, newest first: an explicit --date, or
    [today, yesterday] so a late-night run can catch a game that finished after
    the main run held (the date having rolled past midnight)."""
    if '--date' in argv:
        return [argv[argv.index('--date') + 1]]
    now = datetime.now(KST)
    return [now.strftime('%Y-%m-%d'), (now - timedelta(days=1)).strftime('%Y-%m-%d')]


def evaluate_results(candidates, history, ignore_history):
    """First candidate date with a postable slate — all games final, at least one
    final or postponement — that isn't already posted. A date with games still in
    progress is held (skipped). Returns (date, finals, cancelled) or None."""
    for d in candidates:
        if f'results:{d}' in history and not ignore_history:
            return None                     # newest unposted date is done; stop
        games = fetch_games(d)
        cancelled = [g for g in games if g.get('cancel')]
        finals = [g for g in games if g.get('statusCode') == FINAL and not g.get('cancel')]
        live = [g for g in games if g.get('statusCode') != FINAL and not g.get('cancel')]
        if live:
            print(f'{d}: {len(live)} game(s) still unfinished — holding.')
            continue
        if finals or cancelled:
            return d, finals, cancelled
        print(f'{d}: no games.')
    return None


def emit(mode, date_str, segments, dry_run, history, count):
    """Print each segment, and (unless dry-run) post the thread and record it.
    Returns True if it actually posted."""
    for i, segment in enumerate(segments):
        body, tags, card = seg_parts(segment)
        text = plain_text(body, tags)
        length = grapheme_len(text)
        flag = '  ⚠ OVER LIMIT' if length > BLUESKY_LIMIT else ''
        label = f'post {i + 1}/{len(segments)}' if len(segments) > 1 else 'post'
        print(f'\n{mode} {label} ({length} chars){flag}\n{"-"*40}\n{text}\n{"-"*40}')
        if card:
            w, h = card['size']
            print(f'  + card {w}x{h}, {len(card["png"])//1024} KB\n'
                  f'    alt: {card["alt"]}')
        else:
            print('  (no card — text only)')
    if dry_run:
        print('\n(dry run — nothing posted, history untouched)')
        return False
    post_thread(segments)
    history[f'{mode}:{date_str}'] = {
        'posted_at': datetime.now(timezone.utc).isoformat(), 'games': count}
    HISTORY.write_text(json.dumps(history, ensure_ascii=False, indent=2))
    print('Posted.')
    return True


def main():
    argv = sys.argv[1:]
    mode = ('schedule' if 'schedule' in argv
            else 'standings' if 'standings' in argv
            else 'leaders' if 'leaders' in argv else 'results')
    dry_run = '--dry-run' in argv
    ignore_history = '--all' in argv
    history = load_history()

    if mode == 'standings':
        date_str = (argv[argv.index('--date') + 1] if '--date' in argv
                    else datetime.now(KST).strftime('%Y-%m-%d'))
        if f'standings:{date_str}' in history and not ignore_history:
            print(f'standings for {date_str} already posted — skipping.')
            return
        rows = fetch_standings()
        if not rows:
            print('standings unavailable (KBO site) — skipping.')
            return
        segments = compose_standings(date_str, rows)
        segments = attach_standings_card(date_str, rows, segments)
        emit('standings', date_str, segments, dry_run, history, len(rows))
        return

    if mode == 'leaders':
        date_str = (argv[argv.index('--date') + 1] if '--date' in argv
                    else datetime.now(KST).strftime('%Y-%m-%d'))
        if f'leaders:{date_str}' in history and not ignore_history:
            print(f'leaders for {date_str} already posted — skipping.')
            return
        data = fetch_leaders(date_str[:4])
        roster = load_roster()
        added = []
        segments = leaders_segments(date_str, data, roster, added)
        if added:
            if not dry_run:
                ROSTER.write_text(json.dumps(roster, ensure_ascii=False,
                                             indent=2, sort_keys=True))
            for pc, entry in added:
                warn = ('  ⚠ NEW IMPORT — if East-Asian, add to KEEP_SURNAME_FIRST'
                        if entry.get('foreign') else '')
                print(f'  + roster {pc}: {entry["name"]}{warn}')
        if not segments:
            print('leaders unavailable (no leader data) — skipping.')
            return
        emit('leaders', date_str, segments, dry_run, history, len(segments))
        return

    if mode == 'schedule':
        date_str = (argv[argv.index('--date') + 1] if '--date' in argv
                    else datetime.now(KST).strftime('%Y-%m-%d'))
        if f'schedule:{date_str}' in history and not ignore_history:
            print(f'schedule card for {date_str} already posted — skipping.')
            return
        playable = [g for g in fetch_games(date_str) if not g.get('cancel')]
        if not playable:
            print(f'{date_str}: no games scheduled — nothing to post.')
            return
        roster = load_roster()
        items = []
        for g in by_start(playable):
            away, home = fetch_starters(g['gameId'])
            items.append({'game': g, 'away': away, 'home': home})
        segments = compose_schedule(date_str, items, roster)
        segments = attach_schedule_card(date_str, playable, roster, segments)
        emit('schedule', date_str, segments, dry_run, history, len(playable))
        return

    # results
    picked = evaluate_results(results_candidates(argv), history, ignore_history)
    if not picked:
        print('No results to post.')
        return
    date_str, finals, cancelled = picked
    print(f'KBO {date_str} · results: {len(finals)} final, {len(cancelled)} postponed.')
    roster = load_roster()
    added = []
    segments = compose_results(date_str, finals, cancelled)
    segments = attach_results_card(date_str, finals, segments)
    segments += box_score_segments(finals, roster, added)
    if added:
        if not dry_run:
            ROSTER.write_text(json.dumps(roster, ensure_ascii=False,
                                         indent=2, sort_keys=True))
        for pc, entry in added:
            warn = ('  ⚠ NEW IMPORT — if East-Asian, add to KEEP_SURNAME_FIRST'
                    if entry.get('foreign') else '')
            print(f'  + roster {pc}: {entry["name"]}{warn}')
    if emit('results', date_str, segments, dry_run, history, len(finals)):
        archive_results(date_str, finals, cancelled)


def record_run(mode):
    """Heartbeat: note that a run completed without error, whether or not it
    had anything to post. Off-days and the whole off-season are legitimately
    postless, so 'last posted' cannot tell a broken bot from a quiet one --
    'last completed a run' can. Never let this break a run that already
    succeeded."""
    try:
        state = json.loads(STATE.read_text()) if STATE.exists() else {}
        state['last_run_at'] = datetime.now(timezone.utc).isoformat()
        state['last_run_mode'] = mode
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True))
    except Exception as exc:                    # noqa: BLE001 - heartbeat is best-effort
        print(f'(could not write heartbeat: {exc})')


if __name__ == '__main__':
    main()
    # Only real runs count as a heartbeat; a manual --dry-run should not make a
    # stalled bot look alive.
    if '--dry-run' not in sys.argv[1:]:
        record_run(next((m for m in ('schedule', 'standings', 'leaders')
                         if m in sys.argv[1:]), 'results'))
