#!/usr/bin/env python3
"""
Builds kbo_card inputs from live Naver/KBO data and renders the three cards.

This is the adapter layer: kbo_post owns the API and the romanisation, kbo_card
owns pixels, and this maps one to the other. Run it to preview a real day:

    python3 kbo_card_preview.py 2026-07-18

Writes card_results.png, card_box.png and card_standings.png to the cwd.
Nothing here posts.
"""

import base64
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import kbo_card
import kbo_post as k

# Naver writes innings pitched with vulgar fractions: '6', '5 ⅔', '0 ⅓'.
INNING_FRACTIONS = {'⅓': 1 / 3, '⅔': 2 / 3}

# Club logos. Naver serves each as a 184px transparent PNG keyed by the same
# two-letter code the bot already uses. They are fetched once and cached on
# disk, never hotlinked at render time, and are third-party marks — hence
# gitignored rather than committed. Set USE_LOGOS = False to fall back to the
# team emoji, which every card still supports.
USE_LOGOS = True
LOGO_URL = 'https://sports-phinf.pstatic.net/team/kbo/default/{code}.png'
LOGO_DIR = Path(__file__).resolve().parent / 'logos'
_LOGO_CACHE = {}


def logo_uri(code):
    """A data: URI for one club's logo, fetching and caching it on first use.
    Returns '' if logos are off or the fetch fails, so the card falls back to
    the emoji rather than rendering a broken image."""
    if not USE_LOGOS or not code:
        return ''
    if code in _LOGO_CACHE:
        return _LOGO_CACHE[code]
    path = LOGO_DIR / f'{code}.png'
    if not path.exists():
        LOGO_DIR.mkdir(exist_ok=True)
        # curl, not urllib: Homebrew Python 3.13 fails cert verification here.
        subprocess.run(['curl', '-s', '--max-time', '30', '-o', str(path),
                        LOGO_URL.format(code=code)], check=False)
    try:
        data = base64.b64encode(path.read_bytes()).decode('ascii')
        uri = f'data:image/png;base64,{data}'
    except OSError:
        uri = ''
    _LOGO_CACHE[code] = uri
    return uri


def team_marks(code, prefix):
    """{'<prefix>_emoji': ..., '<prefix>_logo': ...} for one club, so callers
    build card input without caring which the renderer will use."""
    return {f'{prefix}_emoji': k.TEAM_EMOJI.get(code, ''),
            f'{prefix}_logo': logo_uri(code)}

# The postseason cut line only earns its place once the race is live, so it is
# drawn in the final month and not before. The 2026 regular season ends 6
# September (KBO's published calendar; Naver's schedule feed also stops there) —
# UPDATE THIS EACH SEASON, nothing derives it automatically.
SEASON_END = date(2026, 9, 6)
CUTLINE_DAYS = 31


def show_cutline(on):
    """True within CUTLINE_DAYS of the end of the regular season, and after it
    (so the final table still shows who made it)."""
    return (SEASON_END - on).days <= CUTLINE_DAYS


def card_date(date_str):
    """'2026-07-18' -> '18 July'. Cards spell the month out; kbo_post's own
    format_date stays abbreviated because the text posts are character-capped."""
    return f'{date.fromisoformat(date_str).day} ' \
           f'{date.fromisoformat(date_str):%B}'


# The game-winning hit arrives as one regular Korean string:
#   '안재석(7회 1사 만루서 우월 홈런)'  ->  An Jae Seok, grand slam to right, 7th
# Vocabulary below is the complete set observed across 302 game-winning hits
# from 1 May to 18 July 2026 (every one of which parsed). Anything outside it
# falls back to the bare name rather than guessing, so the card never prints
# Korean or an invented description.
GWH_PATTERN = re.compile(r'^(.+?)\((\d+)회 (\S+?)(?: (\S+?))?서 (\S+) (\S+)\)$')
GWH_NONE = '없음'                      # tie games have no game-winning hit

GWH_DIRECTION = {
    '좌월': 'to left', '중월': 'to centre', '우월': 'to right',
    '좌중월': 'to left-centre', '우중월': 'to right-centre',
    '좌전': 'to left', '중전': 'up the middle', '우전': 'to right',
    '좌중간': 'to left-centre', '우중간': 'to right-centre',
    '좌익수': 'to left', '중견수': 'to centre', '우익수': 'to right',
    '유격수': 'to short', '1루수': 'to first', '2루수': 'to second',
    '3루수': 'to third', '투수': 'to the pitcher',
}
GWH_TYPE = {
    '홈런': 'home run', '안타': 'single', '2루타': 'double', '3루타': 'triple',
    '희생플라이': 'sacrifice fly', '땅볼': 'groundout',
    '4구': 'walk', '사구': 'hit by pitch',
}
# '밀어내기' means the run was forced in, which in English is carried by the
# phrase itself rather than by a direction.
GWH_FORCED = '밀어내기'
GWH_LOADED = '만루'


def ordinal(n):
    """1 -> '1st'. Innings only, so the teens case is academic but cheap."""
    if 10 <= n % 100 <= 20:
        suffix = 'th'
    else:
        suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return f'{n}{suffix}'


def describe_gwh(raw):
    """'안재석(7회 1사 만루서 우월 홈런)' -> ('안재석', 'grand slam to right, 7th').
    Returns (korean_name, description) or (None, '') if it doesn't parse."""
    if not raw or raw == GWH_NONE:
        return None, ''
    m = GWH_PATTERN.match(raw)
    if not m:
        return None, ''
    name, inning, _outs, runners, direction, kind = m.groups()
    hit = GWH_TYPE.get(kind)
    if not hit:
        return name, ''                 # unknown feat: fall back to the name
    if direction == GWH_FORCED:
        phrase = f'bases-loaded {hit}'
    else:
        # A bases-loaded home run is a grand slam, and that is what a box score
        # calls it — the only case where the runners change the noun.
        if kind == '홈런' and runners == GWH_LOADED:
            hit = 'grand slam'
        where = GWH_DIRECTION.get(direction)
        phrase = f'{hit} {where}' if where else hit
    return name, f'{phrase}, {ordinal(int(inning))}'


def innings_pitched(raw):
    """'5 ⅔' -> 5.667. Naver gives innings as a string, so parse rather than
    compare text."""
    total = 0.0
    for token in str(raw or '').split():
        if token in INNING_FRACTIONS:
            total += INNING_FRACTIONS[token]
        else:
            try:
                total += float(token)
            except ValueError:
                pass
    return total


def game_note(record):
    """A one-word tag under the score, for genuine feats only: a no-hitter or a
    complete game. Blowouts get nothing — a rout is an opinion, these are facts.

    A complete game means one pitcher covered every inning the opposition
    batted, which is what the `inn` arrays measure (a home side that wins
    without batting in the ninth has a short array, so its pitcher still needs
    the full nine)."""
    if not record:
        return ''
    sb = record.get('scoreBoard') or {}
    inn, rheb = sb.get('inn') or {}, sb.get('rheb') or {}
    pitchers = record.get('pitchersBoxscore') or {}
    if not rheb:
        return ''
    for side, opp in (('away', 'home'), ('home', 'away')):
        staff = pitchers.get(side) or []
        needed = len(inn.get(opp) or [])
        alone = (len(staff) == 1 and needed
                 and innings_pitched(staff[0].get('inn')) >= needed)
        if rheb.get(opp, {}).get('h') == 0:
            return 'no-hitter' if alone else 'combined no-hitter'
        if alone:
            return 'complete game'
    return ''


def results_input(games, records):
    out = []
    for g in k.by_start(games):
        a, h = g['awayTeamScore'], g['homeTeamScore']
        out.append({
            **team_marks(g['awayTeamCode'], 'away'),
            'away_name': k.TEAMS.get(g['awayTeamCode'], g['awayTeamCode']),
            'away_score': a,
            **team_marks(g['homeTeamCode'], 'home'),
            'home_name': k.TEAMS.get(g['homeTeamCode'], g['homeTeamCode']),
            'home_score': h,
            'note': game_note(records.get(g['gameId'])),
        })
    return out


def line_input(game, record):
    """The scoreBoard block -> kbo_card's line-score shape, or None if Naver
    didn't return one (older games occasionally lack it)."""
    sb = record.get('scoreBoard') or {}
    inn, rheb = sb.get('inn') or {}, sb.get('rheb') or {}
    if not inn.get('away') or not rheb.get('away'):
        return None
    return {
        **team_marks(game['awayTeamCode'], 'away'),
        **team_marks(game['homeTeamCode'], 'home'),
        'away_inn': inn.get('away') or [],
        'home_inn': inn.get('home') or [],
        'away_rhe': (rheb['away'].get('r', 0), rheb['away'].get('h', 0),
                     rheb['away'].get('e', 0)),
        'home_rhe': (rheb['home'].get('r', 0), rheb['home'].get('h', 0),
                     rheb['home'].get('e', 0)),
    }


def pitcher_decisions(record, roster, added):
    """The W/L/S decisions as [(code, name, detail), ...] for one card row —
    only the decisions, since a 12-pitcher game will not fit. Name and record
    are kept apart so the card can bold the name alone."""
    by_result = {p.get('wls'): p for p in record.get('pitchingResult', [])}
    parts = []
    for code in ('W', 'L', 'S'):
        p = by_result.get(code)
        if not p:
            continue
        name = k.resolve_name(p.get('pCode'), p.get('name', ''), True,
                              roster, added)
        detail = (p.get('s', 0) if code == 'S'
                  else f'{p.get("w", 0)}–{p.get("l", 0)}')
        parts.append((code, name, str(detail)))
    return parts


def hr_groups(game, record, roster, added):
    """Home runs as one group per team, each carrying that club's mark once
    rather than repeating it per batter:
        [{team_emoji/team_logo, 'names': 'Park Chan Ho, An Jae Seok (2)'}]"""
    groups = []
    for side, code in (('away', game['awayTeamCode']),
                       ('home', game['homeTeamCode'])):
        names = []
        for b in record.get('battersBoxscore', {}).get(side, []):
            if b.get('hr', 0) > 0:
                name = k.resolve_name(b.get('playerCode'), b.get('name', ''),
                                      False, roster, added)
                names.append(name + (f' ({b["hr"]})' if b['hr'] > 1 else ''))
        if names:
            groups.append({**team_marks(code, 'team'),
                           'names': ', '.join(names)})
    return groups


def gw_line(game, record, roster, added):
    """'🐻 An Jae Seok — grand slam to right, 7th', or '' if the game had no
    game-winning hit (a tie) or the string didn't parse.

    etcRecords gives only a Korean name with no player code, so the pcode comes
    from this game's own batter box score, which carries both."""
    raw = next((e.get('result') for e in record.get('etcRecords') or []
                if e.get('how') == '결승타'), '')
    name_ko, description = describe_gwh(raw)
    if not name_ko:
        return ''
    for side, code in (('away', game['awayTeamCode']),
                       ('home', game['homeTeamCode'])):
        for b in record.get('battersBoxscore', {}).get(side, []):
            if b.get('name') == name_ko:
                name = k.resolve_name(b.get('playerCode'), name_ko, False,
                                      roster, added)
                emoji = k.TEAM_EMOJI.get(code, '')
                tail = f' — {description}' if description else ''
                return f'{emoji} {name}{tail}'.strip()
    return ''                           # name not in this game's box score


def box_input(game, record, roster, added):
    return {
        **team_marks(game['awayTeamCode'], 'away'),
        'away_name': k.TEAMS.get(game['awayTeamCode'], game['awayTeamCode']),
        'away_score': game['awayTeamScore'],
        **team_marks(game['homeTeamCode'], 'home'),
        'home_name': k.TEAMS.get(game['homeTeamCode'], game['homeTeamCode']),
        'home_score': game['homeTeamScore'],
        'line': line_input(game, record),
        'pitchers': pitcher_decisions(record, roster, added),
        'hr': hr_groups(game, record, roster, added),
    }


def starter_text(starter, roster):
    """'James Naile (5-5, 3.77)' — name plus season W-L and E.R.A. when the API
    has them. '' when the starter hasn't been announced; the card prints TBD."""
    if not starter:
        return ''
    text = k.display_name(starter, roster)
    if starter['w'] is not None and starter['l'] is not None and starter['era']:
        text += f' ({starter["w"]}-{starter["l"]}, {starter["era"]})'
    return text


def schedule_input(games, roster):
    """Fixtures with probable starters. Returns (rows, subtitle): when every
    game starts at the same time the subtitle carries it once and the rows drop
    it, matching what compose_schedule does for the text post."""
    games = k.by_start(games)
    times = {k.format_time(g['gameDateTime']) for g in games}
    uniform = len(times) == 1 and len(games) > 1
    rows = []
    for g in games:
        away, home = k.fetch_starters(g['gameId'])
        rows.append({
            **team_marks(g['awayTeamCode'], 'away'),
            'away_name': k.TEAMS.get(g['awayTeamCode'], g['awayTeamCode']),
            'away_starter': starter_text(away, roster),
            **team_marks(g['homeTeamCode'], 'home'),
            'home_name': k.TEAMS.get(g['homeTeamCode'], g['homeTeamCode']),
            'home_starter': starter_text(home, roster),
            'time': '' if uniform else k.format_time(g['gameDateTime']),
        })
    subtitle = (f'All games start at {next(iter(times))}' if uniform
                else '')
    return rows, subtitle


def leaders_input(rows):
    """kbo_post's (rank, name, teamCode, value) tuples -> card rows."""
    return [{'rank': rank, 'name': name, 'value': value,
             **team_marks(team, 'team')}
            for rank, name, team, value in rows]


def slug(label):
    """'R.B.I.s' -> 'rbis', for one filename per leaderboard."""
    return re.sub(r'-+', '-', re.sub(r'[^a-z0-9]+', '-',
                                     label.lower().replace('.', ''))).strip('-')


def standings_input(rows):
    out = []
    for r in rows:
        code = k.STANDINGS_TEAM_CODE.get(r['team'], r['team'])
        out.append({
            **team_marks(code, 'team'),
            'name': k.TEAMS.get(code, r['team'].title()),
            'w': r['w'], 'l': r['l'],
            'gb': '' if r['gb'] in ('0.0', '0', '-') else r['gb'],
        })
    return out


def main(argv):
    date_str = argv[1] if len(argv) > 1 else str(date.today() - timedelta(days=1))
    roster, added = k.load_roster(), []

    games = [g for g in k.fetch_games(date_str) if g.get('statusCode') == k.FINAL]
    if not games:
        print(f'no finished games on {date_str}')
        return 1
    label = card_date(date_str)

    # One fetch per game, shared: the digest needs box scores too now, to spot
    # a no-hitter or complete game. The bot already fetches these for its
    # box-score replies, so wiring this in costs no extra calls.
    records = {}
    for g in games:
        rec = k.fetch_box_score(g['gameId'])
        if rec:
            records[g['gameId']] = rec

    print(kbo_card.render_results_card(label, results_input(games, records),
                                       'card_results.png'))

    # Box score: the game named by a second argument (a team code such as OB,
    # or a full gameId), else the first game of the day that had a home run.
    want = argv[2].upper() if len(argv) > 2 else ''
    pick = record = None
    for g in k.by_start(games):
        rec = records.get(g['gameId'])
        if not rec:
            continue
        hit = (want in (g['awayTeamCode'], g['homeTeamCode'])
               or want == g['gameId']) if want else \
            bool(hr_groups(g, rec, roster, added))
        if hit:
            pick, record = g, rec
            break
    if not record:                       # nothing matched — fall back to game 1
        pick = k.by_start(games)[0]
        record = records.get(pick['gameId'])
    if not record:
        print('no box score available')
        return 1
    print(kbo_card.render_box_score_card(label, box_input(pick, record, roster,
                                                          added),
                                         'card_box.png'))

    # Tonight's games: the fixtures for the day after the results being shown,
    # which is the pairing the bot posts (yesterday's results, today's card).
    next_day = date.fromisoformat(date_str) + timedelta(days=1)
    fixtures = k.fetch_games(str(next_day))
    if fixtures:
        rows, subtitle = schedule_input(fixtures, roster)
        print(kbo_card.render_schedule_card(card_date(str(next_day)), rows,
                                            'card_schedule.png',
                                            subtitle=subtitle))
    else:
        print(f'no fixtures on {next_day} (KBO rests on Mondays) — skipped')

    # Season leaders: one card per leaderboard, seven in all.
    data = k.fetch_leaders(date.fromisoformat(date_str).year)
    for key, label in k.HITTING_LEADERS + k.PITCHING_LEADERS:
        top = k.leader_rows(key, data.get(key, []), roster, added)
        if not top:
            print(f'no data for {label} — skipped')
            continue
        print(kbo_card.render_leaders_card(card_date(date_str), label,
                                           leaders_input(top),
                                           f'card_leaders_{slug(label)}.png'))

    rows = k.fetch_standings()
    if rows:
        as_of = date.fromisoformat(date_str) + timedelta(days=1)
        print(kbo_card.render_standings_card(
            card_date(str(as_of)), standings_input(rows), 'card_standings.png',
            cut_after=k.PLAYOFF_SPOTS if show_cutline(as_of) else None))
    else:
        print('standings unavailable — skipped')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
