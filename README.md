# KBO English

A Bluesky bot posting English-language KBO League (Korean baseball) updates.

Posts to [@kbo-english.bsky.social](https://bsky.app/profile/kbo-english.bsky.social).

## Post types

- schedule — a pre-game thread: tonight's matchups and start times (KST), with
  the probable starting pitchers threaded underneath (morning).
- results — a nightly final-scores digest, with a compact box score threaded per
  game (evening).
- standings — a daily rank / W-L / games-back table.
- leaders — a weekly season-leaders thread, top 3 in each core hitting and
  pitching stat (Mondays, a league off-day).

Data comes from Naver Sports' public JSON API and the KBO English site. Team
names use a stable 2-letter code; pitcher and leader names are romanised from the
KBO English pages and cached in kbo_roster.json. Dedup is by (mode, date) in
kbo_history.json, so each card posts at most once per day.

## Scripts

- kbo_post.py <mode> — post one card; mode is schedule, results, standings or
  leaders.
- kbo_roster_build.py — refresh kbo_roster.json, the pcode-to-English-name table.
- kbo_attendance_timing_check.py — a temporary verification harness (July 2026),
  to be removed once its question is answered.

## Setup

    pip install -r requirements.txt

    # Bluesky app password in the macOS Keychain:
    security add-generic-password -a "kbo-english.bsky.social" -s "kbobot-bluesky" -w

## Usage

    python3 kbo_post.py schedule            # or results / standings / leaders
    python3 kbo_post.py results --dry-run
    python3 kbo_roster_build.py             # refresh the roster name cache

## Notes

- kbo_history.json, kbo_results_history.json and kbo_roster.json are gitignored
  (rebuildable state/cache). The Bluesky credential lives in the macOS Keychain.
- No API keys: every data source is public and unauthenticated.

## License

MIT — see [LICENSE](LICENSE).
