#!/usr/bin/env python3
"""
Build/refresh kbo_roster.json — a local pcode -> {name, foreign} table of
official English pitcher names, so kbo_post.py can romanise probable starters
without touching the KBO English site at post time.

How it works: it reads the probable-starter pcodes from Naver's game previews
for the target dates (default today + tomorrow, KST), and for any pcode not
already in the table, fetches that player's KBO English page once to read the
official English name and whether they're a foreign import (salary currency:
$ = import, ￦ = domestic). The table only ever grows, and only for pitchers who
actually start games — no full-league enumeration needed.

Run it on its own schedule (e.g. daily, well before the schedule post). If the
KBO site is unreachable, it simply adds nothing and exits cleanly; kbo_post.py
falls back to the Korean name for any pcode still missing, so the nightly post
never depends on this succeeding.

Names are stored raw (KBO's surname-first, ALL-CAPS surname form, e.g.
"KIM Min Jun"); kbo_post.py applies display ordering at read time.

Usage:
    python3 kbo_roster_build.py                 # today + tomorrow (KST)
    python3 kbo_roster_build.py 2026-07-17       # a specific date
    python3 kbo_roster_build.py 2026-07-17 2026-07-18
"""

import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')
ROSTER = Path(__file__).parent / 'kbo_roster.json'

SCHED_API = ('https://api-gw.sports.naver.com/schedule/games'
             '?upperCategoryId=kbaseball&categoryId=kbo&fromDate={d}&toDate={d}')
PREVIEW_API = 'https://api-gw.sports.naver.com/schedule/games/{gid}/preview'
KBO_PITCHER = 'https://eng.koreabaseball.com/Teams/PlayerInfoPitcher/Summary.aspx?pcode={pc}'


def fetch_text(url):
    r = subprocess.run(
        ['curl', '-s', '--max-time', '30', '-H', 'User-Agent: Mozilla/5.0', url],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f'curl failed ({r.returncode}) for {url}')
    return r.stdout


def fetch_json(url):
    return json.loads(fetch_text(url))


def starter_pcodes(date_str):
    """Every probable-starter pcode for a date (from Naver previews)."""
    pcodes = set()
    try:
        games = fetch_json(SCHED_API.format(d=date_str)).get('result', {}).get('games', [])
    except (RuntimeError, json.JSONDecodeError):
        return pcodes
    for g in games:
        if g.get('cancel'):
            continue
        try:
            pd = fetch_json(PREVIEW_API.format(gid=g['gameId']))
            pd = pd.get('result', {}).get('previewData', {})
        except (RuntimeError, json.JSONDecodeError):
            continue
        for side in ('awayStarter', 'homeStarter'):
            pc = ((pd.get(side) or {}).get('playerInfo') or {}).get('pCode')
            if pc:
                pcodes.add(str(pc))
    return pcodes


def fetch_player(pcode):
    """(english_name, is_foreign) from the KBO English player page, or None."""
    try:
        html = fetch_text(KBO_PITCHER.format(pc=pcode))
    except RuntimeError:
        return None
    m = re.search(r'<b>Name</b>\s*:\s*([^<]+)<', html)
    if not m:
        return None
    name = m.group(1).strip()
    sal = re.search(r'<b>Salary</b>\s*:\s*([^<]+)<', html)
    foreign = bool(sal and '$' in sal.group(1))
    return name, foreign


def main():
    dates = sys.argv[1:]
    if not dates:
        today = datetime.now(KST)
        dates = [today.strftime('%Y-%m-%d'),
                 (today + timedelta(days=1)).strftime('%Y-%m-%d')]

    roster = json.loads(ROSTER.read_text()) if ROSTER.exists() else {}
    before = len(roster)

    pcodes = set()
    for d in dates:
        pcodes |= starter_pcodes(d)
    missing = sorted(pc for pc in pcodes if pc not in roster)
    print(f'Dates {dates}: {len(pcodes)} starter pcodes, {len(missing)} new to fetch.')

    added = 0
    for pc in missing:
        got = fetch_player(pc)
        if not got:
            print(f'  · {pc}: no KBO page (skipped)')
            continue
        name, foreign = got
        roster[pc] = {'name': name, 'foreign': foreign}
        added += 1
        if foreign:
            # Flag every new import: it'll be flipped to first-last by default,
            # which is wrong for an East-Asian import (Japanese/Taiwanese/
            # Chinese) — those must be added to KEEP_SURNAME_FIRST in kbo_post.py.
            print(f'  + {pc}: {name}  ⚠ NEW IMPORT — if East-Asian, add {pc} to '
                  f'KEEP_SURNAME_FIRST in kbo_post.py')
        else:
            print(f'  + {pc}: {name}')

    if added:
        ROSTER.write_text(json.dumps(roster, ensure_ascii=False, indent=2, sort_keys=True))
    print(f'Roster: {before} -> {len(roster)} pitchers ({added} added).')


if __name__ == '__main__':
    main()
