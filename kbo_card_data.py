#!/usr/bin/env python3
"""
Turns live Naver/KBO data into kbo_card inputs, and into the alt text that
describes each card.

This is the adapter layer: kbo_post owns the API and the romanisation, kbo_card
owns pixels, and this maps one to the other. Both the bot and the preview CLI
import it, so it must stay free of anything that posts.

Run it directly to preview a real day without posting anything:

    python3 kbo_card_data.py 2026-07-18 [TEAM]

which writes card_*.png to the cwd — the digest, one box score, the fixtures,
seven leaderboards and the standings.
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
                           'team_name': k.TEAMS.get(code, code),
                           'names': ', '.join(names)})
    return groups


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


# --------------------------------------------------------------------------
# Alt text. A card is a PNG, so everything it says is invisible to a screen
# reader unless it is also said here. These read the same data the cards do,
# so the two cannot drift.
# --------------------------------------------------------------------------

def results_alt(date_label, rows):
    parts = [f'Final scores for {date_label}.']
    for r in rows:
        line = (f'{r["away_name"]} {r["away_score"]}, '
                f'{r["home_name"]} {r["home_score"]}')
        if r.get('note'):
            line += f' ({r["note"]})'
        parts.append(line + '.')
    return ' '.join(parts)


def plural(n, word):
    """'1 error', '2 errors' — alt text is read aloud, so it should read."""
    return f'{n} {word}' if n == 1 else f'{n} {word}s'


def box_alt(date_label, game):
    parts = [f'Box score for {date_label}.',
             f'{game["away_name"]} {game["away_score"]}, '
             f'{game["home_name"]} {game["home_score"]}.']
    line = game.get('line')
    if line:
        innings = max(len(line['away_inn']), len(line['home_inn']))
        for side, name in (('away', game['away_name']),
                           ('home', game['home_name'])):
            got = line[f'{side}_inn']
            by_inn = ' '.join(
                str(got[i]) if i < len(got) else ('X' if side == 'home' else '')
                for i in range(innings)).strip()
            r, h, e = line[f'{side}_rhe']
            parts.append(f'{name} by inning: {by_inn}. '
                         f'{plural(r, "run")}, {plural(h, "hit")}, '
                         f'{plural(e, "error")}.')
    for code, name, detail in game.get('pitchers') or ():
        word = {'W': 'Winning pitcher', 'L': 'Losing pitcher',
                'S': 'Save'}.get(code, code)
        parts.append(f'{word}: {name} ({detail}).')
    groups = game.get('hr') or ()
    if groups:
        # One 'Home runs:' for the lot, each team's batters named after it —
        # the card attributes them by logo, which alt text cannot.
        listed = '; '.join(f'{g.get("team_name", "")} {g["names"]}'.strip()
                           for g in groups)
        parts.append(f'Home runs: {listed}.')
    return ' '.join(parts)


def schedule_alt(date_label, rows, subtitle):
    parts = [f"Tonight's games, {date_label}."]
    if subtitle:
        # 'All games start at 6:30 p.m.' already ends in a stop.
        parts.append(subtitle if subtitle.endswith('.') else subtitle + '.')
    for r in rows:
        line = f'{r["away_name"]} at {r["home_name"]}'
        if r.get('time'):
            line += f', {r["time"]}'
        starters = [s for s in (r.get('away_starter'), r.get('home_starter')) if s]
        if len(starters) == 2:
            line += f'. Probable starters {starters[0]} and {starters[1]}'
        parts.append(line + '.')
    return ' '.join(parts)


def leaders_alt(date_label, title, rows):
    parts = [f'{title}, season leaders, {date_label}.']
    parts += [f'{r["rank"]}. {r["name"]}, {r["value"]}.' for r in rows]
    return ' '.join(parts)


def standings_alt(date_label, rows, cut_after):
    parts = [f'KBO standings, {date_label}.']
    for i, r in enumerate(rows, start=1):
        gb = f", {r['gb']} games back" if r.get('gb') else ''
        parts.append(f'{i}. {r["name"]}, {r["w"]}-{r["l"]}{gb}.')
        if cut_after and i == cut_after and i < len(rows):
            parts.append('Postseason line.')
    return ' '.join(parts)


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
