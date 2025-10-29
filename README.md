# RedditToxicReportBot

Files Reddit comments in your subreddit as **moderator reports** when they cross a toxicity threshold. Primary score from **Detoxify** (CPU-friendly). Optional secondary score from a **Hugging Face** classifier to mirror your older “HF” signal. De-dupes so you don’t spam reports. Optional Discord notifications.

---

## Features

- Monitor one or more subreddits for new comments (lightweight polling).
- Score text with **Detoxify** (`original` or `unbiased`).
- Optional **Hugging Face** classifier (default `unitary/unbiased-toxic-roberta`).
- Create **moderator reports** with a short verdict like `ToxicReportBot: TOXIC (confidence: high).`
- De-duplicate using `reported_ids.jsonl`.
- Optional Discord webhook summaries.
- Systemd unit provided for server use.

---

## Requirements

- Linux with **Python 3.10** (Ubuntu 22.04+ is fine)
- A Reddit bot account that is a **moderator** of your target subreddit(s), with **Posts and Comments** permissions
- A Reddit **script** app (client id/secret)
- Small VM friendly: CPU-only install, no CUDA required

---

## Quick Start

### 1) Clone and set up venv

```bash
# SSH (recommended)
git clone git@github.com:RedditModBot/RedditToxicReportBot.git
cd RedditToxicReportBot

# Or HTTPS
# git clone https://github.com/RedditModBot/RedditToxicReportBot.git
# cd RedditToxicReportBot

python3.10 -m venv .venv310
source .venv310/bin/activate
pip install -U pip wheel
pip install -r requirements.txt
```

### 2) Configure `.env`

Create a file named `.env` in the repo root using this template:

```dotenv
# =========================
# Reddit credentials
# =========================
REDDIT_CLIENT_ID=your_client_id
REDDIT_CLIENT_SECRET=your_client_secret
REDDIT_USERNAME=YourBotUser
REDDIT_PASSWORD=your_bot_password
REDDIT_USER_AGENT=tox-report-bot/1.1 by u/YourBotUser

# =========================
# Subreddits to monitor (comma-separated, no spaces)
# =========================
SUBREDDITS=ToxicReportBotTest

# =========================
# Scoring & thresholds
# =========================
# Detoxify checkpoint: 'original' (more aggressive) or 'unbiased' (more conservative)
DETOXIFY_VARIANT=original

# Optional Hugging Face classifier (extra signal)
ENABLE_HF=true
HF_MODEL_ID=unitary/unbiased-toxic-roberta
HF_LABEL=OFFENSIVE
HF_MAX_SEQ_LEN=256

# Primary toxicity threshold for filing a report
THRESHOLD=0.71

# Confidence buckets for verdict text
CONF_MEDIUM=0.80
CONF_HIGH=0.90
CONF_VERY_HIGH=0.95

# =========================
# Reporting behavior
# =========================
# moderator = file a moderator report (requires mod permissions)
# user      = file a standard user report
REPORT_AS=moderator

# Final report reason shown in Reddit UI
# Tokens: {verdict}, {confidence}
REPORT_STYLE=simple
REPORT_REASON_TEMPLATE=ToxicReportBot: {verdict} (confidence: {confidence}).

# Master switch for Reddit reporting
ENABLE_REDDIT_REPORTS=true

# Optional UI routing bucket (leave blank to skip). Examples: Harassment, Hate
REPORT_RULE_BUCKET=

# =========================
# Discord (optional)
# =========================
ENABLE_DISCORD=false
DISCORD_WEBHOOK=

# =========================
# Runtime controls
# =========================
INTERVAL_SEC=20
LIMIT=120
STATE_PATH=reported_ids.jsonl
LOG_LEVEL=INFO

# If true, log verdicts but DO NOT file reports
DRY_RUN=false
```

### 3) Run the bot

```bash
source .venv310/bin/activate
python bot.py
```

You should see logs like:

```
INFO | Logged in as u/YourBotUser
INFO | Monitoring: r/ToxicReportBotTest
INFO | Reported t1_abcd1234 | detox=0.93 hf=0.71 | reason="ToxicReportBot: TOXIC (confidence: high)."
```

---

## How scoring works

- **Detoxify** is the primary decision signal. We use its `toxicity` probability and compare it to `THRESHOLD`.
- **Hugging Face** classifier is optional and informational. Default model `unitary/unbiased-toxic-roberta` with label `OFFENSIVE`. This number does not gate reporting unless you add your own logic.
- Confidence wording:
  - `< CONF_MEDIUM` → “medium”
  - `>= CONF_HIGH` → “high”
  - `>= CONF_VERY_HIGH` → “very high”

Final reason example:

```
ToxicReportBot: TOXIC (confidence: high).
```

---

## De-duplication

The bot tracks already reported items in `STATE_PATH` (JSON Lines). If an ID exists, it won’t be reported again.

- Do not commit `reported_ids.jsonl`.
- Delete it only if you understand that older items may be re-reported.

---

## Discord webhook (optional)

Set these and the bot will post a compact summary in your channel:

```dotenv
ENABLE_DISCORD=true
DISCORD_WEBHOOK=https://discord.com/api/webhooks/...
```

---

## Systemd service (optional)

Create `/etc/systemd/system/reddit-tox.service`:

```ini
[Unit]
Description=Reddit Toxic Report Bot
After=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/RedditToxicReportBot
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=/home/ubuntu/RedditToxicReportBot/.env
ExecStart=/home/ubuntu/RedditToxicReportBot/.venv310/bin/python /home/ubuntu/RedditToxicReportBot/bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable reddit-tox.service
sudo systemctl start reddit-tox.service
sudo systemctl status reddit-tox.service
```

---

## Troubleshooting

### 401 from Reddit after it worked before
- Verify the bot user can still log in via web (no captcha/lockout).
- Ensure the **script** app is the one you’re using, not an installed app type.
- Check `.env` vars are actually loaded in the running context (`EnvironmentFile` in systemd, or exported in your shell).
- Rotate the app secret if compromised.

### Hugging Face number looks “different” than the old bot
- Detoxify and HF models are trained differently and are not numerically comparable. That’s normal. We trust Detoxify for the main decision. HF is an extra signal for continuity.

### It’s downloading giant GPU things
- This project is CPU-only. If you see CUDA junk, you installed the wrong wheels. Recreate venv and install from `requirements.txt` shipped here.

---

## Git hygiene

A `.gitignore` is included to keep the repo tiny:

```
# venvs
.venv*/
venv*/

# Python cruft
__pycache__/
*.pyc
*.pyo
*.pyd
*.egg-info/

# Local config / secrets / state
.env
.env.*
reported_ids.jsonl
*.log

# Model + cache directories (keep this repo light)
.cache/
cache/
data/
models/
checkpoints/

# Tool caches
.pytest_cache/
.ruff_cache/
.mypy_cache/

# OS
.DS_Store
```

---

## License

MIT. Don’t be a menace.
