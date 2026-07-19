#!/usr/bin/env python3
"""TEMPORARY verification harness (added 2026-07-19).

Question: does KBO publish same-night per-game attendance on its official
crowd page before the KBO bot's results job fires (23:30 KST, 00:45 fallback)?

This samples https://www.koreabaseball.com/Record/Crowd/GraphDaily.aspx at
19:00, 23:25 and 00:40 KST and appends to a log what attendance is present for
today's and yesterday's games, alongside how many of those games are final
(per Naver). Run for a couple of nights, then read the log and decide whether
attendance is reliable enough to add to the box scores.

Remove this script and its launchd job (com.chrisstanford.kbobot-atttest)
once the question is answered.
"""
import subprocess
import re
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')
LOG = '/Users/christopherstanford/Documents/Scans/kbo_attendance_timing.log'
CROWD_URL = 'https://www.koreabaseball.com/Record/Crowd/GraphDaily.aspx'
SCHED = ('https://api-gw.sports.naver.com/schedule/games'
         '?upperCategoryId=kbaseball&categoryId=kbo&fromDate={d}&toDate={d}')


def get(url):
    r = subprocess.run(
        ['curl', '-s', '--compressed', '-A', 'Mozilla/5.0',
         '-H', 'Referer: https://www.koreabaseball.com/', '--max-time', '30', url],
        capture_output=True)
    return r.stdout.decode('utf-8', 'replace')


def crowd_rows(html):
    """(date 'YYYY/MM/DD', dow, home, away, stadium, attendance) per game."""
    return re.findall(
        r'<td[^>]*>\s*(\d{4}/\d\d/\d\d)\s*</td>\s*'
        r'<td[^>]*>\s*([^<]+?)\s*</td>\s*'
        r'<td[^>]*>\s*([^<]+?)\s*</td>\s*'
        r'<td[^>]*>\s*([^<]+?)\s*</td>\s*'
        r'<td[^>]*>\s*([^<]+?)\s*</td>\s*'
        r'<td[^>]*>\s*([\d,]+)\s*</td>', html)


def sched_status(datestr):
    """(games, finals) for a KST date, or (None, None) on failure."""
    try:
        games = json.loads(get(SCHED.format(d=datestr)))['result']['games']
    except Exception:
        return (None, None)
    return (len(games), sum(1 for g in games if g.get('statusCode') == 'RESULT'))


def main():
    now = datetime.now(KST)
    rows = crowd_rows(get(CROWD_URL))
    out = [f'[{now:%Y-%m-%d %H:%M KST}] crowd-page game rows total={len(rows)}']
    for label, day in (('today', now), ('yesterday', now - timedelta(days=1))):
        dslash = day.strftime('%Y/%m/%d')
        ddash = day.strftime('%Y-%m-%d')
        n, final = sched_status(ddash)
        att = [(a, h, c) for (d, dow, h, a, s, c) in rows if d == dslash]
        out.append(f'  {label} {ddash}: games={n} final={final} '
                   f'attendance_rows={len(att)}')
        for a, h, c in att:
            out.append(f'      {a} @ {h}: {c}')
    with open(LOG, 'a') as f:
        f.write('\n'.join(out) + '\n')
    print('\n'.join(out))


if __name__ == '__main__':
    main()
