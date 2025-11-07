# ToxicReportBot

A lightweight Reddit moderator helper that scans new comments with a toxicity model, reports those above a threshold you configure, and posts a optional weekly Discord summary with week‑over‑week deltas. It also refreshes report outcomes daily by reading your subreddit’s modlog (removals vs approvals) so the summary reflects what actually happened to items the bot reported.

## What it does

- Streams new comments from one or more subreddits
- Scores toxicity using [`detoxify`](https://github.com/unitaryai/detoxify) (PyTorch)
- Logs every scan (`reported_ids.jsonl`)
- Reports comments at or above `THRESHOLD` with a configurable report reason
- Refreshes outcomes daily by scanning the modlog (which reported comments were approved or removed)
- Posts a weekly summary to Discord with deltas vs the previous week

## What it **doesn’t** do

- Ban, mute, or anything beyond standard “report” actions
- Track outcomes for items it didn’t report

---

## Quick start

### 1) Requirements

- Python 3.10+ (3.12 recommended)
- A Reddit script app (client id/secret) with a mod account
- Optional: a Discord channel webhook URL (for per‑item pings and/or weekly summary)

### 2) Create and activate a venv

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3) Install dependencies

```bash
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
```

### 4) Configure environment

Copy the template and edit:

```bash
cp .env.example .env
nano .env
```

All options are documented below.

### 5) Run the bot

```bash
python bot.py
```

You should see logs like:

```text
YYYY-mm-dd HH:MM:SS | INFO | Authenticated as u/<yourbot>
YYYY-mm-dd HH:MM:SS | INFO | Building weekly summary (if due)...
YYYY-mm-dd HH:MM:SS | INFO | Starting ToxicReportBot; tracking <N> previously-reported items
YYYY-mm-dd HH:MM:SS | INFO | SCAN t1_abcd123 | 0.9342 | HIGH | ufos
```

If a comment crosses the threshold, the bot logs the scan and writes a second entry setting `reported: true` and issues a Reddit report (unless `DRY_RUN=true`).

## Files it writes

### `reported_ids.jsonl`

Every scan, plus a second record for items the bot reported. Keys:

- `id` (t1_xxx or t3_xxx)
- `subreddit`
- `verdict` (`TOXIC` or `NOT TOXIC`)
- `tox` (float)
- `reported` (bool)
- `ts` (Unix seconds)

### `report_outcomes.jsonl`

Outcomes the daily refresher finds in the modlog for items the bot reported. Keys:

- `id` (t1_/t3_ or bare id)
- `subreddit`
- `ts` (action timestamp)
- `action` (`removed` or `approved`)
- `raw_action` (exact modlog action)
- `details`, `description` (best‑effort fields if available)

### `summary_state.json`

Stores `{"last_post_ts": <unix-seconds>}` for the weekly summary throttle.

## Weekly summary (Discord)

The weekly summary compares the current full interval to the previous one and excludes any “top reasons” list on purpose. It includes:

- Current threshold
- Average toxicity this week (all scanned) plus delta vs prior week
- Total reported comments plus delta
- Reported comments removed by mods plus delta
  - (only counts items the bot reported)
- Reports ignored (left up) plus delta
  - (reported and older than the decision lag with no removal/approval)
- % of reports aligned with mod removal plus delta
- Pending decisions within your configured lag window

Posting is controlled by:

- `ENABLE_WEEKLY_SUMMARY`
- `SUMMARY_DISCORD_WEBHOOK`
- `SUMMARY_INTERVAL_DAYS`
- `SUMMARY_STATE_PATH`

You can force a re‑calculation/post by deleting `summary_state.json` and restarting the bot.

## Outcome refresh (daily)

A background thread runs roughly every `OUTCOMES_REFRESH_HOURS` hours:

1. Looks back `MODLOG_LOOKBACK_DAYS`
2. Reads up to `MODLOG_MAX_ACTIONS` mod actions
3. Matches only against items the bot reported in that window
4. Writes outcomes to `report_outcomes.jsonl`
5. Sleeps `MODLOG_SLEEP_SECS` every `MODLOG_SLEEP_EVERY` actions to avoid rate limits

If your subreddit is very large, raise `MODLOG_MAX_ACTIONS` and consider a longer lookback only if you need it. More actions means more API requests.

## Configuration (`.env`)

All values are read from `.env`. Booleans accept `true/false/1/0/yes/no`.

### Reddit credentials

```ini
REDDIT_CLIENT_ID=
REDDIT_CLIENT_SECRET=
REDDIT_USERNAME=
REDDIT_PASSWORD=
REDDIT_USER_AGENT=tox-report-bot/1.0 by u/yourname
```

The account must have mod permissions in the target sub(s) to read modlog and report.

### Subreddits

```ini
SUBREDDITS=
```

Comma‑separated list, no spaces: `sub1,sub2`.

### Scoring

```ini
DETOXIFY_VARIANT=unbiased  # original | unbiased | multilingual
THRESHOLD=0.85             # report threshold
CONF_MEDIUM=0.86           # log label only
CONF_HIGH=0.90
CONF_VERY_HIGH=0.95
```

Optional offline model directory to avoid internet access:

```ini
DETOXIFY_LOCAL_DIR=/path/to/models/detoxify-unbiased
```

If set and valid, the bot enables offline mode for transformers/HF.

### Reporting

```ini
REPORT_AS=moderator          # display only (not used by APIs)
REPORT_STYLE=simple          # display only
REPORT_REASON_TEMPLATE=ToxicReportBot: {verdict} (confidence: {confidence}).
REPORT_RULE_BUCKET=          # optional bucket/rule name to include in reason
ENABLE_REDDIT_REPORTS=true   # set false to only log
DRY_RUN=false                # true: never call Reddit.report()
```

### Discord

Per‑item pings (optional):

```ini
ENABLE_DISCORD=false
DISCORD_WEBHOOK=
```

Weekly summary:

```ini
ENABLE_WEEKLY_SUMMARY=true
SUMMARY_DISCORD_WEBHOOK=https://discord.com/api/webhooks/...
SUMMARY_INTERVAL_DAYS=7
SUMMARY_STATE_PATH=summary_state.json
```

### Outcome tracking

```ini
DECISIONS_PATH=report_outcomes.jsonl
DECISION_LAG_HOURS=72         # “pending” window in the summary
ENABLE_MOD_REASON_LOOKUP=true # not used for reasons list; harmless to leave true
MODLOG_LOOKBACK_DAYS=14       # daily refresher lookback
MODLOG_MAX_ACTIONS=100000     # ceiling on actions to fetch
MODLOG_SLEEP_EVERY=250        # pace the API calls
MODLOG_SLEEP_SECS=2.0
OUTCOMES_REFRESH_HOURS=24     # cadence of the background refresher
```

### Runtime

```ini
INTERVAL_SEC=20      # polite pacing between stream cycles
LIMIT=120            # not used by the stream, kept for compatibility
STATE_PATH=reported_ids.jsonl
LOG_LEVEL=INFO       # DEBUG, INFO, WARNING, ERROR
```

Set `LOG_LEVEL=INFO` to see one line for every scanned comment.

## Tips & troubleshooting

### No weekly summary yet

It only posts when the last post time is older than `SUMMARY_INTERVAL_DAYS`. Delete `summary_state.json` to force a re‑post.

### Numbers look “too high” or “too low”

The summary includes only outcomes for items the bot actually reported in the window. “Left up” means reported, older than `DECISION_LAG_HOURS`, and still no removal/approval found.

### Heavy subs / rate limits

Increase `MODLOG_SLEEP_SECS` or lower `MODLOG_SLEEP_EVERY`. If you’re missing outcomes, raise `MODLOG_MAX_ACTIONS` and keep `MODLOG_LOOKBACK_DAYS` at 14.

### Offline model

Put the Detoxify checkpoint/config in `DETOXIFY_LOCAL_DIR`. The bot will set `TRANSFORMERS_OFFLINE=1` and avoid network calls.

## Run as a service (optional)

Create `/etc/systemd/system/toxicreportbot.service`:

```ini
[Unit]
Description=ToxicReportBot
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/ubuntu/report-bot
Environment="PYTHONUNBUFFERED=1"
ExecStart=/home/ubuntu/report-bot/.venv/bin/python /home/ubuntu/report-bot/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now toxicreportbot
journalctl -u toxicreportbot -f
```

## Data retention

All logs are local JSONL files. Rotate or archive them on your own schedule. Deleting `report_outcomes.jsonl` removes historic decisions; deleting `summary_state.json` only resets the weekly‑post timer.

## Safety

- The bot issues reports only. It never removes content.
- Use `DRY_RUN=true` to validate behavior without touching Reddit.
- Keep your `.env` out of version control.

---

### `.env.example`

```dotenv
# ---------- Reddit ----------
REDDIT_CLIENT_ID=
REDDIT_CLIENT_SECRET=
REDDIT_USERNAME=
REDDIT_PASSWORD=
REDDIT_USER_AGENT=tox-report-bot/1.0 by u/yourname

# ---------- Subreddits ----------
SUBREDDITS=ufos

# ---------- Scoring ----------
DETOXIFY_VARIANT=unbiased # original | unbiased | multilingual
THRESHOLD=0.85
CONF_MEDIUM=0.86
CONF_HIGH=0.90
CONF_VERY_HIGH=0.95

# Optional: point to a local Detoxify checkpoint to run offline
DETOXIFY_LOCAL_DIR=

# ---------- Reporting ----------
REPORT_AS=moderator
REPORT_STYLE=simple
REPORT_REASON_TEMPLATE=ToxicReportBot: {verdict} (confidence: {confidence}).
REPORT_RULE_BUCKET=
ENABLE_REDDIT_REPORTS=true
DRY_RUN=false

# ---------- Discord (per-item pings; optional) ----------
ENABLE_DISCORD=false
DISCORD_WEBHOOK=

# ---------- Weekly summary (Discord) ----------
ENABLE_WEEKLY_SUMMARY=true
SUMMARY_DISCORD_WEBHOOK=
SUMMARY_INTERVAL_DAYS=7
SUMMARY_STATE_PATH=summary_state.json

# ---------- Outcome tracking ----------
DECISIONS_PATH=report_outcomes.jsonl
DECISION_LAG_HOURS=72
ENABLE_MOD_REASON_LOOKUP=true
MODLOG_LOOKBACK_DAYS=14
MODLOG_MAX_ACTIONS=100000
MODLOG_SLEEP_EVERY=250
MODLOG_SLEEP_SECS=2.0
OUTCOMES_REFRESH_HOURS=24

# ---------- Runtime ----------
INTERVAL_SEC=20
LIMIT=120
STATE_PATH=reported_ids.jsonl
LOG_LEVEL=INFO
```
