# Reddit Toxic Report Bot: Composite Scoring Edition

This bot scans comments in one or more subreddits, scores them for toxicity, optionally reports to Reddit (mod-report). Also posts an optional weekly summary to Discord. It supports an ensemble (composite) score using multiple Toxicty models, as well as a periodic modlog refresh to reconcile outcomes, and detailed, configurable logging.

## Highlights

- **Composite scoring**: Detoxify + Offensiveness + Hate/Identity with configurable weights and threshold.
- **Rolling modlog refresh**: fetches mod actions regularly and updates outcomes without duplicates.
- **Weekly Discord summary**: precision-friendly metrics, trend deltas vs. prior week.
- **Verbose scan logs**: see composite and per-model components (and optionally comment text).

## Quick start (Ubuntu)

```bash
git clone git@github.com:RedditModBot/RedditToxicReportBot.git
cd RedditToxicReportBot
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env  # or paste your values into .env
nano .env             # fill in Reddit + Discord + options
python bot.py
```

If you see `Authenticated as u/None`, your Reddit env vars are missing or not loading. See **Troubleshooting**.

## Requirements

- Python 3.11+ recommended
- A Reddit account capable of reporting posts/comments in your subreddit(s)
- Optional: a Discord webhook for weekly summaries (or per-item pings if you enable them)

## How it works

1. **Comment stream**: the bot streams new comments from `SUBREDDITS`.
2. **Scoring**  
   - If `COMPOSITE_ENABLE=true`, it computes `COMPOSITE = w_detox*Detoxify + w_off*Offensive + w_hate*Hate`.
   - Otherwise it uses Detoxify alone.
3. **Decision**  
   - If the active threshold is hit, the bot reports (unless `DRY_RUN=true`) and writes a row to `STATE_PATH` (`reported_ids.jsonl`).
4. **Modlog refresh**  
   - Runs on a background schedule. It pulls mod actions for the lookback window and writes normalized rows to `DECISIONS_PATH` (`report_outcomes.jsonl`).
5. **Weekly summary**  
   - When due, reads both files, builds 7-day metrics with deltas vs prior week, and posts to Discord. State is tracked in `SUMMARY_STATE_PATH`.

## Configuration (`.env`)

Copy this template and adjust. Every option below matches the current bot.

```ini
# ========= Reddit auth =========
REDDIT_CLIENT_ID=
REDDIT_CLIENT_SECRET=
REDDIT_USERNAME=
REDDIT_PASSWORD=
REDDIT_USER_AGENT=tox-report-bot/1.3 by u/<your-user>

# ========= Subreddits =========
SUBREDDITS=ufos  # comma-separated for multiples, case-insensitive

# ========= Scoring / Models =========
DETOXIFY_VARIANT=unbiased  # original | unbiased | multilingual

# Composite controls
COMPOSITE_ENABLE=true      # true = use ensemble; false = Detoxify-only
COMPOSITE_WEIGHTS=0.45,0.35,0.20  # Detoxify, Offensiveness, Hate (sum need not be 1.0)
COMPOSITE_THRESHOLD=0.85   # 0..1 composite trigger threshold

# Optional logging for diagnostics
LOG_SHOW_COMPONENTS=true   # include per-model scores (Detox, Off, Hate) in SCAN log
LOG_SHOW_COMMENT=false     # print comment text in SCAN log (noisy; consider false in production)

# HuggingFace pipelines (you tested these locally)
OFFENSIVE_MODEL=unitary/toxic-bert
HATE_MODEL=Hate-speech-CNERG/dehatebert-mono-english
TEXT_NORMALIZE=false       # if true, light text normalization before scoring

# Lexicon (currently disabled in scoring; keep empty)
LEXICON_PATH=

# Confidence bands (used to decorate reasons/logs)
CONF_MEDIUM=0.85
CONF_HIGH=0.95
CONF_VERY_HIGH=0.98

# ========= Local model cache (optional) =========
# DETOXIFY_LOCAL_DIR=/path/to/models/detoxify-unbiased

# ========= Reporting to Reddit =========
REPORT_AS=moderator
REPORT_STYLE=simple
REPORT_REASON_TEMPLATE={verdict} (confidence: {confidence}).
REPORT_RULE_BUCKET=
ENABLE_REDDIT_REPORTS=true
DRY_RUN=false              # true = don’t actually report, still logs everything

# ========= Discord (per-item pings) =========
ENABLE_DISCORD=false
DISCORD_WEBHOOK=

# ========= Weekly summary (Discord) =========
ENABLE_WEEKLY_SUMMARY=true
SUMMARY_DISCORD_WEBHOOK=
SUMMARY_INTERVAL_DAYS=7
SUMMARY_STATE_PATH=summary_state.json
SUMMARY_INCLUDE_TOP_REASONS=false  # keep false to hide clutter you dislike

# ========= Outcomes + decision window =========
DECISIONS_PATH=report_outcomes.jsonl
DECISION_LAG_HOURS=12      # window before treating “left up” as decided

# ========= Modlog refresh (background) =========
MODLOG_LOOKBACK_DAYS=2
MODLOG_LIMIT=100000
MODLOG_REFRESH_INTERVAL_HOURS=4
MODLOG_REFRESH_JITTER_MIN=10
MODLOG_PER_REQUEST_SLEEP=0.05

# ========= Runtime =========
INTERVAL_SEC=20            # sleep between stream polls
LIMIT=120                  # PRAW listing page size
STATE_PATH=reported_ids.jsonl
LOG_LEVEL=INFO             # DEBUG | INFO | WARNING | ERROR
LOG_SCAN=1                 # 1 = log every scan; 0 = only important events
```

### What each group does

- **Reddit auth**: required. All five must be non-empty, or you will see `Authenticated as u/None`.
- **Subreddits**: target subreddits to monitor. Comma-separated.
- **Scoring / Models**:  
  - `COMPOSITE_ENABLE=true` turns on the ensemble. If false, only Detoxify is used and the legacy threshold would apply. The ensemble is recommended.
  - `COMPOSITE_WEIGHTS` and `COMPOSITE_THRESHOLD` control the decision boundary.
  - `LOG_SHOW_COMPONENTS=true` prints per-model parts of the score for debugging.
  - `LOG_SHOW_COMMENT=true` prints the actual comment text in logs. Useful for audits; noisy and potentially sensitive.
  - `OFFENSIVE_MODEL` and `HATE_MODEL` are HuggingFace pipeline models. Defaults match local tests.
  - `TEXT_NORMALIZE` allows a light normalizer before scoring. Off by default to preserve raw semantics.
- **Lexicon**: `LEXICON_PATH` is ignored by the current bot. Leave it empty.
- **Reporting**: `DRY_RUN=true` does everything except report to Reddit. `REPORT_REASON_TEMPLATE` supports `{verdict}` and `{confidence}`.
- **Discord**: per-item pings are off by default. Weekly summary uses `SUMMARY_DISCORD_WEBHOOK`.
- **Weekly summary**: posts when `SUMMARY_INTERVAL_DAYS` has elapsed since the prior summary time stored in `SUMMARY_STATE_PATH`. If `SUMMARY_STATE_PATH` is missing or unreadable the bot assumes a first run and posts.
- **Outcomes**: `DECISION_LAG_HOURS` is the window before “left up” is treated as decided.
- **Modlog refresh**: background task that pulls mod actions every `MODLOG_REFRESH_INTERVAL_HOURS` plus small random jitter to avoid thundering herds. It looks back `MODLOG_LOOKBACK_DAYS` and writes normalized rows to `DECISIONS_PATH`. Actions are deduped by `(target_fullname, action, timestamp)`. `MODLOG_PER_REQUEST_SLEEP` reduces API errors and rate limiting.
- **Runtime**: stream pacing and log verbosity. Set `LOG_LEVEL=DEBUG` for deeper diagnostics.

## Files and formats

- **`STATE_PATH`** (default `reported_ids.jsonl`)  
  Append-only JSONL. One row per scanned item; includes `target_fullname`, tox or composite, verdict, reported flag, and timestamp.
- **`DECISIONS_PATH`** (default `report_outcomes.jsonl`)  
  Append-only JSONL of mod actions (approve/remove) normalized to the target fullname. Idempotent writes; safe across restarts.
- **`SUMMARY_STATE_PATH`** (default `summary_state.json`)  
  JSON with `last_run` timestamp and a few cached counters. If you delete it, the next run posts immediately.

All IDs are normalized to `target_fullname` like `t1_abcdefg` for comments and `t3_xyz` for submissions.

## Metrics (weekly summary)

- **Average toxicity (all scanned)**: mean over all scanned items in the 7-day window. In composite mode it displays the composite mean; Detox-only mode uses Detoxify.
- **Average toxicity (reported)**: mean of items actually reported.
- **Total reported comments**: count of rows in `STATE_PATH` with `reported=true` in the window.
- **Removed / Approved**: from `DECISIONS_PATH` where mod action matches and target falls in the window.
- **Left up (past lag)**: reported items older than `DECISION_LAG_HOURS` without an approve/remove action.
- **Pending (within lag)**: reported items newer than `DECISION_LAG_HOURS`.
- **% aligned**: `removed / reported`, clamped to 0–100%.
- Each metric also shows delta vs prior week when prior data exists. If not, the report shows `(+∞% vs prior)` for new series.

## Forcing a weekly summary

Pick one:

- Delete or rename `SUMMARY_STATE_PATH` then run the bot.
- Temporarily set `SUMMARY_INTERVAL_DAYS=0`, run once, then set it back.
- Manually edit `summary_state.json` and set `last_run` to a very old timestamp.

## Logging

Every scan line looks like:

```
2025-11-07 20:17:19 | INFO | SCAN t1_nnnkvb8 | COMP=0.936 (detox=0.88 off=0.74 hate=0.12) | LOW | UFOs | "Nobody knows"...
```

- Enable components with `LOG_SHOW_COMPONENTS=true`.
- Enable full text with `LOG_SHOW_COMMENT=true`.

Reports look like:

```
Reported comment t1_abc123 @ 0.96:
https://www.reddit.com/r/<sub>/comments/<post>/<comment_id>/
```

If you only see “tracking N previously-reported items,” give it a moment to connect to the stream. Use `LOG_LEVEL=DEBUG` to see the paging.

## Rate limits and performance

- The modlog task sleeps `MODLOG_PER_REQUEST_SLEEP` between page fetches and respects `MODLOG_LIMIT`. With large subs, consider:
  - `MODLOG_LOOKBACK_DAYS=1..2`
  - `MODLOG_REFRESH_INTERVAL_HOURS=2..6`
  - `MODLOG_LIMIT=100000` only if you truly need it. Start smaller if you hit 429s.
- The scanner uses `INTERVAL_SEC` and `LIMIT` to keep stream polling civil.

## Upgrading from older bot versions

- The bot now normalizes IDs to `target_fullname` everywhere. Legacy lines with only `"id": "xxxxx"` are still read, but new writes include `target_fullname`.
- You do not need to delete state files. The bot de-duplicates outcomes on ingest and skips duplicate writes on restart.
- Weekly summary state is preserved. If you want to re-post immediately, follow **Forcing a weekly summary**.

## Find the permalink for a comment

```bash
curl -s "https://www.reddit.com/api/info.json?id=t1_nnj5yk7" | jq -r '.data.children[0].data.permalink'
```

## Systemd (optional)

Create `/etc/systemd/system/toxic-report-bot.service`:

```ini
[Unit]
Description=Toxic Report Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/ubuntu/report-bot
ExecStart=/home/ubuntu/report-bot/.venv/bin/python /home/ubuntu/report-bot/bot.py
Environment="PYTHONUNBUFFERED=1"
Restart=always
RestartSec=5
User=ubuntu

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable toxic-report-bot
sudo systemctl start toxic-report-bot
journalctl -u toxic-report-bot -f
```

## Example `.env.example`

```ini
# Reddit
REDDIT_CLIENT_ID=
REDDIT_CLIENT_SECRET=
REDDIT_USERNAME=
REDDIT_PASSWORD=
REDDIT_USER_AGENT=tox-report-bot/1.3 by u/YourUser

# Subreddits
SUBREDDITS=ufos

# Scoring
DETOXIFY_VARIANT=unbiased
COMPOSITE_ENABLE=true
COMPOSITE_WEIGHTS=0.45,0.35,0.20
COMPOSITE_THRESHOLD=0.85
OFFENSIVE_MODEL=unitary/toxic-bert
HATE_MODEL=Hate-speech-CNERG/dehatebert-mono-english
TEXT_NORMALIZE=false
LOG_SHOW_COMPONENTS=true
LOG_SHOW_COMMENT=false
LEXICON_PATH=
CONF_MEDIUM=0.85
CONF_HIGH=0.95
CONF_VERY_HIGH=0.98

# Reporting
REPORT_AS=moderator
REPORT_STYLE=simple
REPORT_REASON_TEMPLATE={verdict} (confidence: {confidence}).
REPORT_RULE_BUCKET=
ENABLE_REDDIT_REPORTS=true
DRY_RUN=false

# Discord per-item
ENABLE_DISCORD=false
DISCORD_WEBHOOK=

# Weekly summary
ENABLE_WEEKLY_SUMMARY=true
SUMMARY_DISCORD_WEBHOOK=
SUMMARY_INTERVAL_DAYS=7
SUMMARY_STATE_PATH=summary_state.json
SUMMARY_INCLUDE_TOP_REASONS=false

# Outcomes + lag
DECISIONS_PATH=report_outcomes.jsonl
DECISION_LAG_HOURS=12

# Modlog refresh
MODLOG_LOOKBACK_DAYS=2
MODLOG_LIMIT=100000
MODLOG_REFRESH_INTERVAL_HOURS=4
MODLOG_REFRESH_JITTER_MIN=10
MODLOG_PER_REQUEST_SLEEP=0.05

# Runtime
INTERVAL_SEC=20
LIMIT=120
STATE_PATH=reported_ids.jsonl
LOG_LEVEL=INFO
LOG_SCAN=1
```

## Notes on precision and thresholds

Your live experience suggests Detoxify ≥ 0.95 is relatively reliable with some false positives. The ensemble helps filter intent and offensiveness. Start with:

- `COMPOSITE_WEIGHTS=0.45,0.35,0.20`
- `COMPOSITE_THRESHOLD=0.85`

Raise to `0.88–0.90` if you still see drift; lower to `0.82–0.84` if you are missing obvious insults.

Keep `LOG_SHOW_COMPONENTS=true` while tuning to understand why the composite passed or failed.
