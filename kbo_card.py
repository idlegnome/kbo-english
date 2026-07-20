#!/usr/bin/env python3
"""
Card renderer for KBO in English (@kbo-english.bsky.social).

Renders the bot's five post types as monospace "ink on cream" PNG cards.
Headless Google Chrome does the type and image layout; Pillow crops the result
to content, so no height is ever guessed.

    render_results_card()    the daily final-scores digest
    render_box_score_card()  one finished game, with a traditional line score
    render_schedule_card()   tonight's fixtures and their probable starters
    render_leaders_card()    one leaderboard's top three
    render_standings_card()  the league table, with the postseason cut line

Cards are rendered on a magenta sentinel background and cropped, so a 4-game day
and a 5-game day both come out tight. Corners are square on purpose: Bluesky
rounds image corners itself.

All five take plain dicts, not Naver API payloads — kbo_post owns the API and
the romanisation, this module owns pixels only. See __main__ for the shapes.

Raises CardRenderError on any failure so the poster can fall back to plaintext.
"""

import base64
import html
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

# Chrome does the layout. Look for it rather than hardcoding a macOS path, so
# the same renderer works on a Linux CI runner: KBO_CHROME wins if set, then
# anything on PATH under its various Linux names, then the standard macOS spot.
CHROME_ENV = 'KBO_CHROME'
CHROME_CANDIDATES = [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
]
CHROME_ON_PATH = ['google-chrome', 'google-chrome-stable', 'chromium',
                  'chromium-browser', 'chrome']


def find_chrome():
    """Path to a usable Chrome/Chromium, or None. Result is not cached: an
    hour-long bot run has no reason to hold a stale answer."""
    explicit = os.environ.get(CHROME_ENV)
    if explicit:
        return explicit if Path(explicit).exists() else None
    for name in CHROME_ON_PATH:
        found = shutil.which(name)
        if found:
            return found
    return next((c for c in CHROME_CANDIDATES if Path(c).exists()), None)


SENTINEL = 'FF00FF'          # page background; cropped away. Never appears in art.
SENTINEL_RGB = (255, 0, 255)
CARD_WIDTH = 620             # CSS px; device-scale 2 renders at 1240 px.
RENDER_HEIGHT = 1400         # generous CSS height; cropped to content after.

CREAM = '#faf7f1'
INK = '#1c2b45'              # navy: winners, scores, headings
RED = '#c8323f'              # top bar, winning score, postseason line
MUTED = '#8a8578'            # losers, labels, dates, footer
RULE = '#ded8cc'             # hairline row separators

# IBM Plex Mono, vendored in fonts/ and embedded in each rendered page rather
# than read from the system, so a card looks identical wherever it is rendered —
# this Mac today, a Linux CI runner later. Plex is SIL OFL 1.1 (see
# fonts/LICENSE.txt), which is what makes bundling it legitimate; the macOS
# stock monospaces are not redistributable and could not travel with the repo.
# Monospace throughout so score columns line up by construction.
FONT_STACK = "'IBM Plex Mono', monospace"
FONT_DIR = Path(__file__).resolve().parent / 'fonts'
FONT_FILES = [('IBMPlexMono-Regular.otf', 400), ('IBMPlexMono-Bold.otf', 700)]
_FONT_FACE_CSS = None


def _font_face_css():
    """@font-face rules with the fonts inlined as data URIs, built once. Returns
    '' if the files are missing, so rendering falls back to whatever monospace
    the system has rather than failing outright."""
    global _FONT_FACE_CSS
    if _FONT_FACE_CSS is None:
        rules = []
        for name, weight in FONT_FILES:
            path = FONT_DIR / name
            try:
                blob = base64.b64encode(path.read_bytes()).decode('ascii')
            except OSError:
                continue
            rules.append(
                f"@font-face{{font-family:'IBM Plex Mono';font-weight:{weight};"
                f"font-style:normal;src:url(data:font/otf;base64,{blob})"
                f" format('opentype')}}")
        _FONT_FACE_CSS = ''.join(rules)
    return _FONT_FACE_CSS


# Team-mark sizes in CSS px, one per context. KBO's logos are wordmarks rather
# than simple icons, so they need more room than the emoji they replaced before
# they read as anything at all.
MARK_ROW = 28        # a fixture or result line
MARK_HEADLINE = 40   # the two team rows atop a box score
MARK_LINESCORE = 26  # the line score's row labels
MARK_HR = 24         # each team's home-run group
MARK_TABLE = 28      # standings and leaderboard rows


class CardRenderError(RuntimeError):
    """Rendering failed — caller should fall back to a plaintext post."""


def _esc(s):
    return html.escape(str(s) if s is not None else '', quote=True)


# --------------------------------------------------------------------------
# Rendering plumbing
# --------------------------------------------------------------------------

def _crop_to_content(raw_path, out_path):
    try:
        from PIL import Image, ImageChops
    except ImportError as e:
        raise CardRenderError(f'Pillow not available: {e}')
    with Image.open(raw_path) as im:
        im = im.convert('RGB')
        bg = Image.new('RGB', im.size, SENTINEL_RGB)
        bbox = ImageChops.difference(im, bg).getbbox()
        if not bbox:
            raise CardRenderError('rendered image was entirely background')
        cropped = im.crop(bbox)
        cropped.save(out_path)
        size = cropped.size
    return out_path, size


def _shoot(doc, out_path):
    """Render an HTML doc to a content-cropped PNG. Returns (out_path, (w, h))."""
    chrome = find_chrome()
    if not chrome:
        raise CardRenderError(
            'no Chrome or Chromium found (set KBO_CHROME to its path)')
    out_path = str(out_path)
    with tempfile.TemporaryDirectory() as td:
        html_path = Path(td) / 'card.html'
        raw_png = Path(td) / 'raw.png'
        html_path.write_text(doc, encoding='utf-8')
        cmd = [
            chrome, '--headless=new', '--disable-gpu', '--hide-scrollbars',
            '--force-device-scale-factor=2',
            f'--window-size={CARD_WIDTH},{RENDER_HEIGHT}',
            f'--default-background-color={SENTINEL}FF',
            f'--screenshot={raw_png}', f'file://{html_path}',
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if not raw_png.exists():
            raise CardRenderError(
                f'Chrome produced no image (exit {r.returncode}): '
                f'{(r.stderr or r.stdout or "").strip()[:200]}')
        _, size = _crop_to_content(raw_png, out_path)
    return out_path, size


BASE_CSS = f"""
html,body{{margin:0;background:#{SENTINEL}}}
.card{{width:{CARD_WIDTH}px;box-sizing:border-box;background:{CREAM};color:{INK};
  border-top:5px solid {RED};padding:26px 30px 18px;font-family:{FONT_STACK}}}
.top{{display:flex;align-items:baseline;justify-content:space-between}}
.top .t{{font-size:19px;font-weight:700}}
.top .d{{font-size:13px;color:{MUTED}}}
.sub{{margin-top:6px;font-size:12px;color:{MUTED}}}
.hr{{border-bottom:2px solid {INK};margin:14px 0 0}}
.foot{{margin-top:14px;padding-top:12px;border-top:1px solid {RULE};
  font-size:11px;color:{MUTED};letter-spacing:0.06em}}
/* Club logos centre on the text's middle rather than sitting on its baseline:
   they are much taller than the type, so a baseline leaves them riding high.
   Unlike a fixed em offset, this holds as the MARK_* sizes change. */
img.lg{{vertical-align:middle;object-fit:contain}}
"""

FOOTER = ('<div class="foot">KBO IN ENGLISH &middot; '
          '@kbo-english.bsky.social</div>')


def _document(css, body):
    return (f'<!doctype html><html><head><meta charset="utf-8"><style>'
            f'{_font_face_css()}{BASE_CSS}{css}</style></head><body>{body}'
            f'</body></html>')


def _mark(item, prefix, size):
    """A team's mark at `size` px: its logo if the caller supplied one, else its
    emoji. Callers pass a dict and a field prefix ('away' -> away_logo /
    away_emoji), so a card renders logos or emoji without knowing which."""
    logo = item.get(f'{prefix}_logo')
    if logo:
        return (f'<img class="lg" src="{_esc(logo)}" '
                f'style="height:{size}px;width:{size}px">')
    return _esc(item.get(f'{prefix}_emoji') or '')


def _head(title, date_label, emoji='🇰🇷 ⚾', subtitle=''):
    sub = f'<div class="sub">{_esc(subtitle)}</div>' if subtitle else ''
    return (f'<div class="top"><div class="t">{_esc(emoji)} {_esc(title)}</div>'
            f'<div class="d">{_esc(date_label)}</div></div>{sub}'
            f'<div class="hr"></div>')


# --------------------------------------------------------------------------
# Final scores digest
# --------------------------------------------------------------------------

RESULTS_CSS = f"""
.g{{padding:16px 0 14px}}
.g + .g{{border-top:1px solid {RULE}}}
.row{{display:grid;grid-template-columns:1fr auto 1fr;align-items:baseline;
  column-gap:16px;font-size:17px}}
.row .a{{text-align:right}}
.row .h{{text-align:left}}
.row .a,.row .h{{font-weight:700}}
.row .s{{color:{MUTED};white-space:nowrap;letter-spacing:0.04em}}
.note{{margin-top:9px;text-align:center;font-size:12px;color:{MUTED};
  letter-spacing:0.04em}}
"""


def _game_block(g):
    away = (f'<span class="a">{_mark(g, "away", MARK_ROW)} '
            f'{_esc(g["away_name"])}</span>')
    home = (f'<span class="h">{_mark(g, "home", MARK_ROW)} '
            f'{_esc(g["home_name"])}</span>')
    score = (f'<span class="s">{g["away_score"]} &mdash; '
             f'{g["home_score"]}</span>')
    note = (f'<div class="note">{_esc(g["note"])}</div>'
            if g.get('note') else '')
    return f'<div class="g"><div class="row">{away}{score}{home}</div>{note}</div>'


def render_results_card(date_label, games, out_path, title='Final Scores'):
    """The daily digest. `games` is a list of dicts:
        {away_emoji, away_name, away_score, home_emoji, home_name, home_score,
         note}  — note is a short tag ('rout', 'shutout') or '' for none.
    Returns (path, (w, h))."""
    if not games:
        raise CardRenderError('no games to render')
    body = (f'<div class="card">{_head(title, date_label)}'
            f'{"".join(_game_block(g) for g in games)}{FOOTER}</div>')
    return _shoot(_document(RESULTS_CSS, body), out_path)


# --------------------------------------------------------------------------
# Tonight's games + probable starters
# --------------------------------------------------------------------------

SCHEDULE_CSS = f"""
.g{{padding:16px 0 14px}}
.g + .g{{border-top:1px solid {RULE}}}
.row{{display:grid;grid-template-columns:1fr auto 1fr;align-items:baseline;
  column-gap:16px;font-size:17px}}
.row .a{{text-align:right;font-weight:700}}
.row .h{{text-align:left;font-weight:700}}
.row .mid{{color:{MUTED};white-space:nowrap;font-size:13px}}
.sp{{display:grid;grid-template-columns:1fr auto 1fr;column-gap:16px;
  margin-top:7px;font-size:12px;color:{MUTED}}}
.sp .a{{text-align:right}}
.sp .h{{text-align:left}}
.sp .mid{{white-space:nowrap}}
.sp .sep{{color:{RED}}}
"""


def _fixture_block(g):
    away = (f'<span class="a">{_mark(g, "away", MARK_ROW)} '
            f'{_esc(g["away_name"])}</span>')
    home = (f'<span class="h">{_mark(g, "home", MARK_ROW)} '
            f'{_esc(g["home_name"])}</span>')
    mid = f'<span class="mid">{_esc(g.get("time") or "@")}</span>'
    starters = ''
    if g.get('away_starter') or g.get('home_starter'):
        # A middot, not a second '@': the matchup row above already says it.
        starters = (f'<div class="sp"><span class="a">'
                    f'{_esc(g.get("away_starter") or "TBD")}</span>'
                    f'<span class="mid sep">&middot;</span><span class="h">'
                    f'{_esc(g.get("home_starter") or "TBD")}</span></div>')
    return (f'<div class="g"><div class="row">{away}{mid}{home}</div>'
            f'{starters}</div>')


def render_schedule_card(date_label, games, out_path, title="Tonight's Games",
                         subtitle=''):
    """Tonight's fixtures with probable starters. `games` is a list of dicts:
        {away_emoji/away_logo, away_name, away_starter, home_..., time}
    `time` is '6:30 p.m.' per fixture, or '' when every game starts together and
    the caller has put the time in `subtitle` instead. A starter that hasn't
    been announced shows TBD. Returns (path, (w, h))."""
    if not games:
        raise CardRenderError('no fixtures to render')
    body = (f'<div class="card">{_head(title, date_label, subtitle=subtitle)}'
            f'{"".join(_fixture_block(g) for g in games)}{FOOTER}</div>')
    return _shoot(_document(SCHEDULE_CSS, body), out_path)


# --------------------------------------------------------------------------
# Box score
# --------------------------------------------------------------------------

# Label gutter, sized to the longest label ('Home runs', 9 chars at 11px with
# 0.06em tracking, plus its 10px right padding). Value column takes what is left
# of the card after its 30px side padding. Plex's advance is exactly 0.6em
# (measured), so a monospace line's width is char_count * 0.6 * font_size and
# the fit below is arithmetic, not a guess. Menlo, which this replaced, was
# 0.6021, so the constant very slightly under-read it.
KV_LABEL_WIDTH = 80
KV_VALUE_WIDTH = CARD_WIDTH - 60 - KV_LABEL_WIDTH
MONO_ADVANCE = 0.6


BOX_CSS = f"""
/* Centre the mark against the club name rather than sitting it on the text
   baseline: the logos are far taller than the type, so a baseline puts them
   visibly high. Flex centring holds at any MARK_HEADLINE size. */
.tm{{display:flex;align-items:center;justify-content:space-between;
  margin:18px 0}}
.tm .n{{display:flex;align-items:center;gap:16px;font-size:22px;
  font-weight:700}}
.tm .sc{{font-size:26px;font-weight:700}}
.hr2{{border-bottom:2px solid {INK};margin:4px 0 0}}
table.ls{{width:100%;border-collapse:collapse;margin:16px 0 4px;
  font-size:13px;table-layout:fixed}}
table.ls th,table.ls td{{text-align:right;padding:5px 0;font-weight:400}}
table.ls th{{color:{MUTED};font-size:11px;border-bottom:1px solid {RULE}}}
table.ls td.lab,table.ls th.lab{{text-align:left;width:40px}}
table.ls td.t,table.ls th.t{{font-weight:700;width:34px}}
table.ls .t.first{{border-left:1px solid {RULE};padding-left:10px}}
table.ls .li{{padding-right:10px}}   /* keep the last inning off the divider */
table.ls tr.r td{{border-bottom:1px solid {RULE}}}
.kv{{display:flex;align-items:flex-start;padding:11px 0;font-size:14px}}
.kv + .kv{{border-top:1px solid {RULE}}}
.kv .k{{color:{MUTED};font-size:11px;letter-spacing:0.06em;
  width:{KV_LABEL_WIDTH}px;box-sizing:border-box;padding:3px 10px 0 0;
  flex:none}}
.kv .v{{line-height:1.5}}
.kv .sep{{color:{RED}}}
/* Home runs list one team per line, the way a box score prints them. */
.hrg + .hrg{{margin-top:5px}}
"""


def _line_score_table(line):
    """A traditional line score: runs by inning, then R/H/E. `line` is
        {away_emoji, home_emoji, away_inn:[...], home_inn:[...],
         away_rhe:(r,h,e), home_rhe:(r,h,e)}
    A home side that didn't bat in the last inning has a short list, and gets
    the conventional X in that cell."""
    innings = max(len(line['away_inn']), len(line['home_inn']))
    last = f' class="li"'                 # last inning column: gets right padding
    head = ''.join(f'<th{last if i == innings else ""}>{i}</th>'
                   for i in range(1, innings + 1))
    head = (f'<tr><th class="lab"></th>{head}'
            f'<th class="t first">R</th><th class="t">H</th>'
            f'<th class="t">E</th></tr>')

    rows = ''
    for side in ('away', 'home'):
        cells = ''
        for i in range(innings):
            got = line[f'{side}_inn']
            # Short home list = didn't bat (walk-off or home win); print X.
            val = got[i] if i < len(got) else ('X' if side == 'home' else '')
            cells += f'<td{last if i == innings - 1 else ""}>{_esc(val)}</td>'
        r, h, e = line[f'{side}_rhe']
        rows += (f'<tr class="r"><td class="lab">{_mark(line, side, MARK_LINESCORE)}</td>'
                 f'{cells}<td class="t first">{r}</td><td class="t">{h}</td>'
                 f'<td class="t">{e}</td></tr>')
    return f'<table class="ls">{head}{rows}</table>'


def _kv(key, value, style=''):
    style = f' style="{style}"' if style else ''
    return (f'<div class="kv"><div class="k">{_esc(key)}</div>'
            f'<div class="v"{style}>{value}</div></div>')


def _fit_size(text, base=14, floor=11, width=KV_VALUE_WIDTH):
    """Largest whole-px size in [floor, base] that keeps `text` on one line."""
    if not text:
        return base
    for size in range(base, floor - 1, -1):
        if len(text) * MONO_ADVANCE * size <= width:
            return size
    return floor


def render_box_score_card(date_label, game, out_path, title='Final'):
    """One finished game. `game` is a dict:
        {away_emoji, away_name, away_score, home_emoji, home_name, home_score,
         line:   {...} for the line score (see _line_score_table), or None
         pitchers: [('W', 'Takada', '1-1'), ('S', 'Lee Young Ha', '14'), ...]
                   — only the name is bold; the code, the record and the red
                   separators between entries are all regular weight
         hr:     [{emoji/logo per team, 'names': 'Park Chan Ho, An Jae Seok (2)'}]
         extra:  optional [(label, value)] rows appended after HR}
    Returns (path, (w, h))."""
    away = (f'<div class="tm"><div class="n">{_mark(game, "away", MARK_HEADLINE)}'
            f'{_esc(game["away_name"])}</div>'
            f'<div class="sc">{game["away_score"]}</div></div>')
    home = (f'<div class="tm"><div class="n">{_mark(game, "home", MARK_HEADLINE)}'
            f'{_esc(game["home_name"])}</div>'
            f'<div class="sc">{game["home_score"]}</div></div>')

    parts = [_head(title, date_label), away, home, '<div class="hr2"></div>']
    if game.get('line'):
        parts.append(_line_score_table(game['line']))
    if game.get('pitchers'):
        # All three decisions share one row, shrinking a little if the names are
        # long, rather than wrapping a lone '(14)' onto a second line. Bold and
        # regular Plex share an advance width, so the fit maths is unaffected
        # by emboldening the W/L/S codes.
        pitchers = game['pitchers']
        plain = ' · '.join(f'{code} {name} ({detail})'
                           for code, name, detail in pitchers)
        marked = '<span class="sep"> · </span>'.join(
            f'{_esc(code)} <b>{_esc(name)}</b> ({_esc(detail)})'
            for code, name, detail in pitchers)
        parts.append(_kv('Pitchers', marked,
                         f'font-size:{_fit_size(plain)}px;white-space:nowrap'))
    if game.get('hr'):
        groups = ''.join(f'<div class="hrg">{_mark(g, "team", MARK_HR)} '
                         f'{_esc(g["names"])}</div>' for g in game['hr'])
        parts.append(_kv('Home runs', groups))
    for label, value in game.get('extra') or ():
        parts.append(_kv(label, _esc(value)))
    parts.append(FOOTER)
    return _shoot(_document(BOX_CSS, f'<div class="card">{"".join(parts)}</div>'),
                  out_path)


# --------------------------------------------------------------------------
# Standings
# --------------------------------------------------------------------------

STANDINGS_CSS = f"""
table.st{{width:100%;border-collapse:collapse;margin-top:4px;font-size:16px}}
table.st th{{color:{MUTED};font-size:11px;font-weight:400;letter-spacing:0.06em;
  text-align:right;padding:10px 0 6px}}
table.st td{{padding:11px 0;border-bottom:1px solid {RULE}}}
table.st tr:last-child td{{border-bottom:0}}
table.st td.rk{{color:{MUTED};font-size:12px;width:26px}}
table.st td.tm{{font-weight:700}}
table.st td.wl{{text-align:right;font-weight:700;white-space:nowrap}}
table.st td.gb{{text-align:right;color:{MUTED};width:64px}}
tr.cut td{{border-bottom:0;padding:0}}
.cutline{{display:flex;align-items:center;gap:10px;color:{RED};font-size:11px;
  letter-spacing:0.1em;padding:7px 0}}
.cutline::before,.cutline::after{{content:"";flex:1;
  border-bottom:1px dashed {RED}}}
"""


LEADERS_CSS = f"""
table.ld{{width:100%;border-collapse:collapse;margin-top:6px;font-size:18px}}
table.ld td{{padding:16px 0;border-bottom:1px solid {RULE}}}
table.ld tr:last-child td{{border-bottom:0}}
table.ld td.rk{{color:{MUTED};font-size:12px;width:26px}}
table.ld td.nm{{font-weight:700}}
table.ld td.vl{{text-align:right;font-weight:700;white-space:nowrap;
  font-size:22px}}
"""


def render_leaders_card(date_label, title, rows, out_path,
                        subtitle='Season leaders'):
    """One leaderboard: a stat's top three. `rows` is a list of dicts:
        {rank, team_emoji/team_logo, name, value}
    Ranks come from the API as-is, so a three-way tie prints 1, 1, 1 rather
    than being renumbered. Returns (path, (w, h))."""
    if not rows:
        raise CardRenderError('no leaderboard rows to render')
    body = ''
    for r in rows:
        body += (f'<tr><td class="rk">{_esc(r["rank"])}</td>'
                 f'<td class="nm">{_mark(r, "team", MARK_TABLE)} {_esc(r["name"])}</td>'
                 f'<td class="vl">{_esc(r["value"])}</td></tr>')
    card = (f'<div class="card">'
            f'{_head(title, date_label, subtitle=subtitle)}'
            f'<table class="ld">{body}</table>{FOOTER}</div>')
    return _shoot(_document(LEADERS_CSS, card), out_path)


def render_standings_card(date_label, rows, out_path, cut_after=5,
                          title='Standings'):
    """The league table. `rows` is a list of dicts in standings order:
        {emoji, name, w, l, gb}  — gb is '' for the leader, else '2.5'.
    A dashed POSTSEASON LINE is drawn after `cut_after` rows (None for none).
    Returns (path, (w, h))."""
    if not rows:
        raise CardRenderError('no standings rows to render')
    body = ('<tr><th class="rk"></th><th></th><th class="wl">W&ndash;L</th>'
            '<th class="gb">GB</th></tr>')
    for i, r in enumerate(rows, start=1):
        body += (f'<tr><td class="rk">{i}</td>'
                 f'<td class="tm">{_mark(r, "team", MARK_TABLE)} {_esc(r["name"])}</td>'
                 f'<td class="wl">{r["w"]}&ndash;{r["l"]}</td>'
                 f'<td class="gb">{_esc(r.get("gb", ""))}</td></tr>')
        if cut_after and i == cut_after and i < len(rows):
            body += ('<tr class="cut"><td colspan="4">'
                     '<div class="cutline">POSTSEASON LINE</div></td></tr>')
    card = (f'<div class="card">{_head(title, date_label)}'
            f'<table class="st">{body}</table>{FOOTER}</div>')
    return _shoot(_document(STANDINGS_CSS, card), out_path)
