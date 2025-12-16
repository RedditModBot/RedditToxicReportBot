# ToxicReportBot v2

An automated Reddit moderation bot that uses AI to detect and report toxic comments. Built for r/UFOs but configurable for any subreddit.

## How It Works

```
COMMENT ARRIVES
       │
       ▼
┌──────────────────────────────────────┐
│  Step 1: PATTERN MATCHING            │
│  Check for slurs, threats, insults   │
└──────────────────────────────────────┘
       │
       ├── Match found ──────────────────────────┐
       │                                         │
       ▼                                         │
┌──────────────────────────────────────┐         │
│  Step 2: BENIGN PHRASE CHECK         │         │
│  "holy shit", "this is fake", etc    │         │
└──────────────────────────────────────┘         │
       │                                         │
       ├── Benign + not directed ── SKIP         │
       │                                         │
       ▼                                         │
┌──────────────────────────────────────┐         │
│  Step 3: DETOXIFY ML SCORING         │         │
│  Score 0.0 to 1.0 on toxicity        │         │
└──────────────────────────────────────┘         │
       │                                         │
       ├── Below threshold ──────── SKIP         │
       │                                         │
       ▼                                         │
┌──────────────────────────────────────┐         │
│  Step 4: AI REVIEW  ◄──────────────────────────┘
│  Send to Groq LLM with context       │
└──────────────────────────────────────┘
       │
       ├── BENIGN ──────────────── No action (✅)
       │
       ▼
┌──────────────────────────────────────┐
│  REPORT TO REDDIT (🚨)               │
│  Track for accuracy                  │
└──────────────────────────────────────┘
       │
       ▼ (after 24h)
┌──────────────────────────────────────┐
│  ACCURACY CHECK                      │
│  Removed? ✓ True positive            │
│  Still up? ⚠️ False positive          │
└──────────────────────────────────────┘
```

### Step-by-Step Breakdown

**Step 1: Pattern Matching**

Checks `moderation_patterns.json` for known bad patterns:
- Slurs (racial, homophobic, ableist, etc.)
- Self-harm phrases ("kill yourself", "kys")
- Threat phrases ("I'll find you", "you're dead")
- Shill accusations directed at users ("you're a fed")
- Direct insults at users ("you're an idiot")

If ANY match → immediately send to AI for review.

**Step 2: Benign Phrase Check**

Checks if comment is clearly harmless:
- Excitement: "holy shit", "what the fuck", "no way"
- UFO context: "crazy footage", "insane video"
- Skepticism: "this is fake", "obviously a drone"

If matches AND not directed at a user → skip entirely (saves API calls).

**Step 3: Detoxify ML Scoring**

Local ML model scores comment 0.0 to 1.0. Default thresholds (configurable in `.env`):

| Label | Directed at User | Not Directed |
|-------|------------------|--------------|
| threat | 0.15 | 0.15 |
| severe_toxicity | 0.20 | 0.20 |
| identity_attack | 0.25 | 0.25 |
| insult | 0.40 | 0.60 |
| toxicity | 0.40 | 0.50 |
| obscene | 0.90 | 0.90 |

"Directed" = contains "you", "your", "OP", or is a reply. See **Detection Thresholds** section for tuning.

**Step 4: AI Review**

Sends to Groq LLM with full context:
- Your moderation guidelines
- Whether it's a `[TOP-LEVEL]` or `[REPLY]`
- Post title and parent comment
- The comment text

AI returns: `VERDICT: REPORT` or `VERDICT: BENIGN` with a reason.

**Model Fallback Chain** (if rate limited):
1. `groq/compound` (best quality)
2. `llama-3.3-70b-versatile`
3. `llama-4-scout-17b`
4. `llama-3.1-8b-instant`

---

## What Gets Reported vs Ignored

### ✅ Gets Reported
- Direct insults at other users ("you're an idiot", "what a moron")
- Slurs and hate speech (including obfuscated: "n1gger", "f4g")
- Threats ("I'll find you", "you're dead")
- Self-harm encouragement ("kill yourself", "kys")
- Shill/bot accusations at users ("you're a fed", "obvious bot")
- Calls for violence ("someone should shoot that", "laser the plane")

### ❌ Does NOT Get Reported
- Criticizing ideas ("that theory is nonsense", "this has been debunked")
- Criticizing public figures ("Corbell is a grifter", "Greer is a fraud")
- Profanity for emphasis ("holy shit that's crazy", "what the fuck")
- Skepticism ("this is obviously fake", "that's just Starlink")
- Venting about the subreddit ("this sub sucks", "mods are useless")
- Disagreement ("you're wrong", "I completely disagree")

---

## Understanding moderation_patterns.json

This file contains word/phrase lists that the bot uses for fast pre-filtering BEFORE calling the AI. It's organized into categories:

### Slurs (Always escalate to AI)

```json
"slurs": {
  "racial": ["n-word variants", "..."],
  "homophobic_hard": ["f-word variants", "..."],
  "transphobic_hard": ["tranny", "..."],
  "ableist_hard": ["retard", "..."]
}
```

These are high-confidence bad words that almost always indicate a problem. Any match immediately sends the comment to the AI for review.

### Contextual Sensitive Terms (Escalate with additional signals)

```json
"contextual_sensitive_terms": {
  "racial_ambiguous": ["negro", "cracker", "gringo", "..."],
  "sexual_orientation": ["homo", "queer", "..."],
  "ideology_terms": ["white power", "nazi", "..."]
}
```

These words CAN be used in neutral contexts (historical discussion, quoting, reclaimed terms). They only escalate if:
- Directed at a user ("you're just a [term]"), OR
- Combined with high identity_attack score from Detoxify

### Insults (Escalate when directed at users)

```json
"insults_direct": {
  "intelligence": ["idiot", "moron", "dumbass", "..."],
  "character": ["loser", "pathetic", "scumbag", "..."],
  "mental_health": ["take your meds", "you're crazy", "..."]
}
```

These escalate when the comment appears to be targeting another user (contains "you", "your", "OP", or is a reply).

### Threats & Self-Harm (Always escalate)

```json
"threats": {
  "direct": ["I'll kill you", "you're dead", "..."],
  "implied": ["watch your back", "I know where you live", "..."]
},
"self_harm": {
  "direct": ["kill yourself", "kys", "..."],
  "indirect": ["world would be better without you", "..."]
}
```

High-priority patterns that always go to the AI.

### Benign Skip Phrases (Skip AI entirely)

```json
"benign_skip": {
  "excitement_phrases": ["holy shit", "what the fuck", "..."],
  "ufo_context_phrases": ["crazy footage", "insane video", "..."],
  "skeptic_phrases": ["this is fake", "obviously a drone", "..."]
}
```

When a short comment matches these AND isn't directed at a user, skip the AI entirely. Saves API calls on obviously-fine comments.

### How Pattern Matching Works

1. **Word boundary matching** - "cope" won't match "telescope", "pos" won't match "possessive"
2. **Normalization** - "n1gg3r" gets normalized to check against patterns
3. **Directedness check** - Many patterns only escalate when aimed at a user
4. **Context awareness** - Top-level comments vs replies are treated differently

---

## Features

- **Smart pre-filtering** - Only ~5% of comments use your API quota
- **Context-aware** - Knows if it's a reply vs top-level, who's being targeted
- **Public figure detection** - Understands UFO community figures (Grusch, Elizondo, Corbell, etc.)
- **Model fallback chain** - Automatically switches models if rate limited
- **Discord notifications** - Real-time alerts for reports, verdicts, and daily stats
- **Accuracy tracking** - Logs false positives for tuning
- **Dry run mode** - Test without actually reporting

---

## Requirements

- Python 3.9+
- Reddit account with mod permissions (for moderator reports)
- Groq API key (free at https://console.groq.com)
- Discord webhook (optional, for notifications)

---

## Quick Start

### 1. Clone and install dependencies

```bash
git clone https://github.com/YOUR_USERNAME/toxic-report-bot.git
cd toxic-report-bot
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp env.template .env
# Edit .env with your credentials
```

### 3. Add your moderation guidelines

Edit `moderation_guidelines.txt` to customize what the AI should report.

### 4. Test in dry run mode

```bash
# Make sure DRY_RUN=true in .env
python bot.py
```

### 5. Go live

```bash
# Set DRY_RUN=false in .env
python bot.py
```

---

## Configuration

All configuration is done via environment variables in a `.env` file. 

### Setting Up Your .env File

1. Copy the template: `cp env.template .env`
2. Edit `.env` with your credentials
3. **Never commit `.env` to git** - it contains secrets!

The `env.template` file has detailed comments explaining each option. Here's a quick overview:

### Reddit Credentials

You need to create a Reddit "script" app to get these:

1. Go to https://www.reddit.com/prefs/apps
2. Click "create another app..."
3. Select "script"
4. Fill in name and redirect URI (use `http://localhost:8080`)
5. Copy the client ID (under the app name) and secret

| Variable | Description | Example |
|----------|-------------|---------|
| `REDDIT_CLIENT_ID` | OAuth app client ID | `Ab3CdEfGhIjKlM` |
| `REDDIT_CLIENT_SECRET` | OAuth app client secret | `xYz123AbC456DeF789` |
| `REDDIT_USERNAME` | Bot account username | `ToxicReportBot` |
| `REDDIT_PASSWORD` | Bot account password | `your_secure_password` |
| `REDDIT_USER_AGENT` | Identifies your bot to Reddit | `toxic-report-bot/2.0 by u/YourUsername` |
| `SUBREDDITS` | Comma-separated list | `UFOs` or `UFOs,aliens,UAP` |

### LLM Configuration (Groq)

Get a **free** API key at https://console.groq.com/

| Variable | Default | Description |
|----------|---------|-------------|
| `GROQ_API_KEY` | (required) | Your Groq API key (starts with `gsk_`) |
| `LLM_MODEL` | `groq/compound` | Primary model (see options below) |
| `LLM_FALLBACK_MODEL` | `llama-3.3-70b-versatile` | Backup when rate limited |
| `LLM_DAILY_LIMIT` | `240` | Switch to fallback after this many calls |
| `LLM_REQUESTS_PER_MINUTE` | `2` | Rate limit (1 request per 30 sec) |

**Model options (all free):**
| Model | Quality | Daily Limit | Best For |
|-------|---------|-------------|----------|
| `groq/compound` | Best | 250 | Default choice |
| `llama-3.3-70b-versatile` | Great | 1,000 | High volume fallback |
| `llama-3.1-8b-instant` | Good | 14,400 | Very high volume |

### Pre-filter Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `DETOXIFY_MODEL` | `original` | `original` (stricter) or `unbiased` (looser) |

### Detection Thresholds

Thresholds control how sensitive the pre-filter is. Scores range from 0.0 to 1.0. If ANY score exceeds its threshold, the comment gets sent to the AI.

- **Lower threshold** = More sensitive (catches more, but more API calls)
- **Higher threshold** = Less sensitive (fewer API calls, might miss some)

| Variable | Default | What it catches |
|----------|---------|-----------------|
| `THRESHOLD_THREAT` | `0.15` | Threats of violence/harm |
| `THRESHOLD_SEVERE_TOXICITY` | `0.20` | Extreme toxic content |
| `THRESHOLD_IDENTITY_ATTACK` | `0.25` | Slurs, hate speech |
| `THRESHOLD_INSULT_DIRECTED` | `0.40` | Insults aimed at users ("you're an idiot") |
| `THRESHOLD_INSULT_NOT_DIRECTED` | `0.60` | General insults not at a user |
| `THRESHOLD_TOXICITY_DIRECTED` | `0.40` | Toxic comments at users |
| `THRESHOLD_TOXICITY_NOT_DIRECTED` | `0.50` | General toxic comments |
| `THRESHOLD_OBSCENE` | `0.90` | Profanity (keep high - swearing isn't always toxic) |
| `THRESHOLD_BORDERLINE` | `0.35` | Threshold for "borderline skip" Discord alerts |

**When to adjust:**

| Problem | Solution |
|---------|----------|
| Missing real toxicity | Lower the relevant threshold |
| Too many API calls | Raise thresholds |
| False positives in `false_positives.json` | Raise `TOXICITY_*` or `INSULT_*` |
| Missing threats/slurs | Lower `THREAT` or `IDENTITY_ATTACK` |
| Profanity-heavy subreddit getting flagged | Raise `OBSCENE` to 0.95 |

### Reporting

| Variable | Default | Description |
|----------|---------|-------------|
| `REPORT_AS` | `moderator` | `moderator` (needs mod perms) or `user` |
| `ENABLE_REDDIT_REPORTS` | `true` | Master switch for reporting |
| `DRY_RUN` | `false` | `true` = log only, don't actually report |

**Important:** Start with `DRY_RUN=true` to test before going live!

### Discord (Optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `DISCORD_WEBHOOK` | (empty) | Webhook URL for notifications |

To create a webhook:
1. Go to your Discord server
2. Edit a channel → Integrations → Webhooks → New Webhook
3. Copy the URL

---

## Files

| File | Description |
|------|-------------|
| `bot.py` | Main bot code |
| `moderation_guidelines.txt` | Instructions for the AI on what to report (customize this!) |
| `moderation_guidelines_template.txt` | Annotated template with explanations for customization |
| `moderation_patterns.json` | Word lists for pre-filtering (slurs, insults, etc.) |
| `env.template` | Template for `.env` configuration |
| `requirements.txt` | Python dependencies |
| `reported_comments.json` | Auto-generated tracking of reported comments |
| `false_positives.json` | Auto-generated log of false positives |

---

## Discord Notifications

If configured, the bot sends these notifications:

| Notification | Color | Meaning |
|--------------|-------|---------|
| 🤖 Bot Started | Green | Bot is running |
| ⚪ Borderline Skip | Gray | Scored kinda high but not reviewed |
| 🔍 Analyzing | Blue | Sending to AI for review |
| ✅ BENIGN | Green | AI says it's fine |
| 🚨 REPORT | Red | AI flagged it, reporting |
| ⚠️ False Positive | Orange | Reported comment wasn't removed |
| 📈 Daily Stats | Varies | Daily summary at midnight UTC |

---

## Accuracy Tracking

The bot tracks every report to measure how well it's performing. This helps you tune the system over time.

### How It Works

1. **When a comment is reported**, it's logged to `reported_comments.json` with:
   - Comment ID and permalink
   - The comment text
   - Why it was reported (AI's reason)
   - Detoxify score
   - Timestamp

2. **Every 12 hours**, the bot checks each reported comment:
   - Fetches the comment from Reddit
   - If removed/deleted → **True Positive** (correctly reported)
   - If still visible → **False Positive** (incorrectly reported)

3. **False positives are logged** to `false_positives.json` for review

4. **Discord notifications** (if enabled):
   - ⚠️ Orange alert for each new false positive
   - 📈 Daily stats with accuracy percentage

### Understanding the JSON Files

**reported_comments.json** - All reported comments:
```json
{
  "comment_id": "t1_abc123",
  "permalink": "https://reddit.com/r/UFOs/comments/.../abc123/",
  "text": "You're an idiot...",
  "groq_reason": "Direct insult at user",
  "detoxify_score": 0.85,
  "reported_at": "2025-01-15T10:30:00Z",
  "outcome": "pending",      // or "removed" or "approved"
  "checked_at": ""
}
```

**false_positives.json** - Comments that weren't removed:
```json
{
  "comment_id": "t1_xyz789",
  "permalink": "https://reddit.com/r/UFOs/comments/.../xyz789/",
  "text": "This is obviously fake garbage",
  "groq_reason": "Direct insult at user",
  "detoxify_score": 0.62,
  "reported_at": "2025-01-15T08:00:00Z",
  "discovered_at": "2025-01-16T08:00:00Z"
}
```

### Reviewing False Positives

Regularly check `false_positives.json` to understand why the bot made mistakes:

| Common Cause | Solution |
|--------------|----------|
| Public figure criticism marked as attack | Add name to public figures list in guidelines |
| Sarcasm/quotes misunderstood | Add example to benign cases in guidelines |
| Domain-specific phrase flagged | Add to `benign_skip` in patterns.json |
| Threshold too aggressive | Raise thresholds in bot.py |

### Accuracy Metrics

The bot calculates:
- **Accuracy %** = (True Positives) / (True Positives + False Positives) × 100
- **Pending** = Reports not yet checked (less than 24h old)

A good target is **80%+ accuracy**. Below 60% means too many false positives.

### Data Retention

- `reported_comments.json`: Entries older than 7 days are automatically cleaned up
- `false_positives.json`: Kept indefinitely for review (manually delete when reviewed)

---

## Deploying on Ubuntu Server

The bot runs great on free cloud instances. It uses minimal resources (~200MB RAM) and can run 24/7 for free.

### Free Cloud Options

| Provider | Free Tier | Specs | Link |
|----------|-----------|-------|------|
| **Oracle Cloud** | Forever free | 1 CPU, 1GB RAM, 50GB disk | [cloud.oracle.com](https://cloud.oracle.com) |
| **Google Cloud** | Free e2-micro | 0.25 CPU, 1GB RAM | [cloud.google.com](https://cloud.google.com) |
| **AWS** | 12 months free | t2.micro, 1GB RAM | [aws.amazon.com](https://aws.amazon.com) |

Oracle Cloud's "Always Free" tier is recommended - it never expires and has plenty of resources.

### Step-by-Step Ubuntu Setup

#### 1. Create your cloud instance

- Choose **Ubuntu 22.04 or 24.04 LTS** (minimal/server image)
- Open port 22 (SSH) in your security rules
- Save your SSH key

#### 2. Connect via SSH

```bash
ssh ubuntu@YOUR_SERVER_IP
```

#### 3. Install system dependencies

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Python and pip
sudo apt install -y python3 python3-pip python3-venv git
```

#### 4. Clone and setup the bot

```bash
# Clone the repo
git clone https://github.com/RedditModBot/RedditToxicReportBot.git
cd RedditToxicReportBot

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies (this takes a few minutes on micro instances)
pip install --upgrade pip
pip install -r requirements.txt
```

#### 5. Configure the bot

```bash
# Create your config file
cp env.template .env

# Edit with your credentials
nano .env
```

Fill in your Reddit credentials, Groq API key, and Discord webhook (optional).

#### 6. Create moderation guidelines

```bash
# Copy the template
cp moderation_guidelines_template.txt moderation_guidelines.txt

# Customize for your subreddit
nano moderation_guidelines.txt
```

#### 7. Test the bot

```bash
# Make sure DRY_RUN=true in .env first!
source .venv/bin/activate
python bot.py
```

Watch the logs. If it connects and starts scanning comments, you're good.

#### 8. Set up as a system service

Create the service file:

```bash
sudo nano /etc/systemd/system/toxicreportbot.service
```

Paste this:

```ini
[Unit]
Description=ToxicReportBot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/RedditToxicReportBot
Environment=PATH=/home/ubuntu/RedditToxicReportBot/.venv/bin
ExecStart=/home/ubuntu/RedditToxicReportBot/.venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable toxicreportbot
sudo systemctl start toxicreportbot
```

#### 9. Go live

Once testing looks good:

```bash
# Edit .env and set DRY_RUN=false
nano .env

# Restart the service
sudo systemctl restart toxicreportbot
```

### Useful Commands

```bash
# View live logs
sudo journalctl -u toxicreportbot -f

# Check status
sudo systemctl status toxicreportbot

# Restart after config changes
sudo systemctl restart toxicreportbot

# Stop the bot
sudo systemctl stop toxicreportbot

# View recent logs
sudo journalctl -u toxicreportbot --since "1 hour ago"
```

### Updating the Bot

```bash
cd ~/RedditToxicReportBot
git pull
sudo systemctl restart toxicreportbot
```

### Memory Considerations

On 1GB RAM instances, the first startup takes ~60 seconds while Detoxify loads its ML model. After that, it uses ~200-300MB steadily. If you run into memory issues:

```bash
# Add swap space (one-time setup)
sudo fallocate -l 1G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

---

## Customization

### Moderation Guidelines

The `moderation_guidelines.txt` file is the "brain" of the bot - it tells the AI exactly what to report and what to ignore. This is where you customize behavior for your subreddit.

**Use `moderation_guidelines_template.txt` as a starting point** - it has detailed comments explaining each section.

Key sections to customize:

| Section | What to Change |
|---------|----------------|
| Subreddit name | Replace `r/YOUR_SUBREDDIT_HERE` |
| Public figures list | Add people commonly discussed in your community |
| Shill accusations | Add domain-specific accusations (e.g., "paid shill for [company]") |
| Dangerous acts | Add things specific to your topic (e.g., "laser aircraft" for UFOs) |
| Benign phrases | Add skepticism phrases common in your community |

**Example customizations by subreddit type:**

For **r/politics**:
```
PUBLIC FIGURES: Biden, Trump, AOC, Pelosi, McConnell, etc.
BENIGN: "both sides", "whataboutism", "fake news"
```

For **r/nba**:
```
PUBLIC FIGURES: LeBron, Curry, team owners, coaches
BENIGN: "refs are blind", "trade him", "bust"
```

For **r/cryptocurrency**:
```
PUBLIC FIGURES: CZ, SBF, Vitalik, crypto influencers
SHILL ACCUSATIONS: "paid by [coin]", "bag holder"
BENIGN: "FUD", "shill coin", "rug pull"
```

### Pattern Lists

Edit `moderation_patterns.json` to add/remove:
- Slurs and hate speech terms
- Insult words and phrases
- Threat phrases
- Benign skip phrases
- Public figure names

---

## Troubleshooting

### Bot not reporting anything
- Check `DRY_RUN` is `false`
- Check `ENABLE_REDDIT_REPORTS` is `true`
- Check bot has mod permissions in the subreddit

### Rate limited constantly
- Lower `LLM_REQUESTS_PER_MINUTE`
- Check Groq dashboard for usage
- Fallback models should kick in automatically

### Discord notifications not working
- Check webhook URL is correct
- Check for "Discord embed post failed" in logs
- Test webhook with curl

### High false positive rate
- Review `false_positives.json`
- Adjust thresholds in `bot.py`
- Update `moderation_guidelines.txt`

---

## License

MIT License - Don't be a menice

## Credits

- [Detoxify](https://github.com/unitaryai/detoxify) for local toxicity scoring
- [Groq](https://groq.com) LLM 
- [PRAW](https://praw.readthedocs.io) Reddit API
