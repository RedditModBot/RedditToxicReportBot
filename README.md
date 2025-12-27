# ToxicReportBot v2

An automated Reddit moderation bot that uses AI to detect and report toxic comments. Built for r/UFOs but configurable for any subreddit.

## How It Works

```
                              COMMENT ARRIVES
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ LAYER 1: MUST-ESCALATE PATTERNS                                             │
│                                                                             │
│  Check for: slurs, self-harm, threats, violence, shill accusations          │
│  These ALWAYS send to LLM (no benign skip allowed)                         │
│                                                                             │
│  Also check: dismissive language + direct insults                          │
│  BUT skip if benign pattern matches (e.g., "bullshit argument")            │
│                                                                             │
│  If triggered → SEND TO LLM (with ML scores for context)                   │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                              Not triggered
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ LAYER 2: RUN ALL ML MODELS                                                  │
│                                                                             │
│  • Detoxify (local) - triggers on profanity/edgy content                   │
│  • OpenAI Moderation API - better context understanding                     │
│  • Google Perspective API - better context understanding                    │
│                                                                             │
│  Record which models triggered (exceeded their thresholds)                  │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ LAYER 3: DECISION LOGIC                                                     │
│                                                                             │
│  Case A: ONLY Detoxify triggered (OpenAI & Persp did NOT trigger)          │
│    └─► Check external scores (max of OpenAI, Perspective)                  │
│        • If external_max < 0.30 → SKIP (detox-only, not validated)         │
│        • If external_max >= 0.30 → SEND (external validates concern)       │
│                                                                             │
│  Case B: OpenAI OR Perspective triggered (with or without Detoxify)        │
│    └─► SEND TO LLM                                                         │
│                                                                             │
│  Case C: Nothing triggered                                                  │
│    └─► SKIP                                                                │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                              If SEND
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ LAYER 4: LLM REVIEW                                                         │
│                                                                             │
│  Send to Groq/Grok LLM with full context:                                  │
│  • Moderation guidelines                                                    │
│  • Post title, parent comment, grandparent context                         │
│  • All ML scores from Detoxify, OpenAI, Perspective                        │
│  • Pattern match trigger reasons                                            │
│                                                                             │
│  LLM returns: VERDICT: REPORT or VERDICT: BENIGN                           │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┴───────────────┐
                    │                               │
              BENIGN                            REPORT
                    │                               │
                    ▼                               ▼
            No action (✅)                  Report to Reddit (🚨)
                                                    │
                                                    ▼ (after 24h)
                                           ┌───────────────────┐
                                           │  ACCURACY CHECK   │
                                           │  Removed? ✓ TP    │
                                           │  Still up? ⚠️ FP   │
                                           └───────────────────┘
```

### Decision Examples

| Comment | Layer 1 | Detox | OpenAI | Persp | Result |
|---------|---------|-------|--------|-------|--------|
| "I think UFOs are real" | - | 0.00 | 0.01 | 0.02 | **SKIP** (nothing triggered) |
| "it's a fucking plane" | - | 0.95 ✅ | 0.10 | 0.15 | **SKIP** (detox-only, external < 0.30) |
| "Holy shit that's cool" | - | 0.90 ✅ | 0.05 | 0.10 | **SKIP** (detox-only, external < 0.30) |
| "Same old bullshit argument" | benign ✅ | - | - | - | **SKIP** (Layer 1 benign match) |
| "Edgy comment" | - | 0.80 ✅ | 0.10 | 0.35 | **SEND** (detox + external ≥ 0.30) |
| "You're an idiot" | - | 0.85 ✅ | 0.75 ✅ | 0.40 | **SEND** (OpenAI triggered) |
| "Hate speech" | - | 0.91 ✅ | 0.89 ✅ | 0.72 ✅ | **SEND** (multiple triggered) |
| "Kill yourself" | must_escalate ✅ | - | - | - | **SEND** (Layer 1 self-harm) |
| "You're a retard" | must_escalate ✅ | - | - | - | **SEND** (Layer 1 slur) |

### Key Insight: Why External Validation?

Detoxify triggers on **any profanity** regardless of context. "It's a fucking plane" scores 0.95+ toxicity even though it's not attacking anyone.

OpenAI and Perspective are **better at understanding context**. When Detoxify triggers alone but external APIs score low (< 0.30), it's usually a false positive.

**The rule:** Detox-only triggers require external validation (score ≥ 0.30) to send to LLM.

---

## External Moderation APIs

The bot uses three ML models for toxicity detection. Detoxify runs locally (free, unlimited). OpenAI and Perspective are external APIs that provide better context understanding.

### Google Perspective API (Recommended - Free)

**Setup:**
1. Go to https://developers.perspectiveapi.com/s/docs-get-started
2. Click "Get Started" and request API access
3. Access is typically granted within 1-2 business days
4. Once approved, create an API key in your Google Cloud Console

**Cost:** Free (quota-based, very generous limits)

```bash
PERSPECTIVE_API_KEY=your_key_here
PERSPECTIVE_ENABLED=true
PERSPECTIVE_MODE=all
PERSPECTIVE_THRESHOLD=0.70
PERSPECTIVE_RPM=60
```

Note: Perspective only supports certain languages (English, Spanish, French, German, etc.). Comments in unsupported languages are silently skipped.

### OpenAI Moderation API (Recommended - Free*)

**Setup:**
1. Create an account at https://platform.openai.com
2. Add a minimum of $5 credit to your account
3. Generate an API key

**Cost:** The Moderation API itself is **free** and doesn't consume credits. However, OpenAI now requires an account with credits to use any API endpoints, including free ones. The $5 minimum deposit is never used by the moderation endpoint - it just needs to be there.

```bash
OPENAI_API_KEY=sk-xxxxx
OPENAI_MODERATION_ENABLED=true
OPENAI_MODERATION_MODE=all
OPENAI_MODERATION_THRESHOLD=0.50
OPENAI_MODERATION_RPM=10
```

### API Mode Options

Both external APIs support three modes:

| Mode | Behavior | API Calls |
|------|----------|-----------|
| `all` | Run on every comment | High (recommended) |
| `confirm` | Only run if Detoxify triggers | Medium |
| `only` | Skip Detoxify, use only this API | Medium |

**Recommended:** Use `MODE=all` for both APIs. This ensures external validation is always available for the detox-only skip logic.

### Detoxify (Local - Free, Unlimited)

Detoxify runs locally using a pre-trained ML model. No API key needed.

**Thresholds** (configurable in `.env`):

| Label | Directed at User | Not Directed |
|-------|------------------|--------------|
| threat | 0.15 | 0.15 |
| severe_toxicity | 0.20 | 0.20 |
| identity_attack | 0.25 | 0.25 |
| insult | 0.40 | 0.65 |
| toxicity | 0.50 | 0.65 |
| obscene | 0.90 | 0.90 |

"Directed" = contains "you", "your", "OP", or is a reply.

**Detoxify Escalation Control:**

If Detoxify triggers too many false positives, you can disable it from triggering LLM review while still using it for scoring context:

```bash
# Detoxify provides scores but won't trigger LLM review on its own
DETOXIFY_CAN_ESCALATE=false
```

This way, the LLM still sees Detoxify scores for context, but only OpenAI/Perspective decisions matter.

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
- Self-deprecation ("I'm such an idiot", "maybe I'm just dumb")
- Third-party criticism ("the idiots who run the government")
- Situation criticism ("this is so stupid", "what a dumb rule")

### Benign Pattern Categories

The bot recognizes ~950 benign patterns across these categories:

| Category | Examples |
|----------|----------|
| `profanity_as_emphasis` | "it's a fucking", "fucking amazing", "cool as fuck" |
| `frustration_exclamations` | "fuck this", "goddammit", "what the hell" |
| `slang_expressions` | "no cap", "deadass", "talk shit" |
| `self_deprecating` | "I'm just dumb", "maybe I'm stupid" |
| `third_party_profanity` | "idiots who run", "these morons" |
| `disbelief_at_situation` | "this is bullshit", "total crap", "so stupid" |
| `ufo_skepticism_phrases` | "obviously fake", "clearly CGI", "just a balloon" |
| `excitement_phrases` | "holy shit", "what the fuck", "no way" |

---

## LLM Configuration

### Model Fallback Chain

The bot uses a primary model with fallbacks for rate limiting:

```bash
# Best setup: Paid Grok primary + Free Groq reasoning fallbacks
LLM_MODEL=grok-4-0709
LLM_FALLBACK_CHAIN=openai/gpt-oss-120b,openai/gpt-oss-20b,qwen/qwen3-32b
GROQ_REASONING_EFFORT=high
```

### Recommended Models

**Reasoning Models (Recommended for nuanced moderation):**

| Model | Provider | Cost | Notes |
|-------|----------|------|-------|
| `grok-4-0709` | x.ai (paid) | ~$2-5/M in | Best quality, always reasons |
| `openai/gpt-oss-120b` | Groq (free) | 1K RPD | Best free reasoning |
| `openai/gpt-oss-20b` | Groq (free) | 1K RPD | Smaller, still reasons |
| `qwen/qwen3-32b` | Groq (free) | 1K RPD | Good limits |

**Non-Reasoning Models (faster but less accurate):**

| Model | Provider | Limits | Notes |
|-------|----------|--------|-------|
| `llama-3.3-70b-versatile` | Groq (free) | 1K RPD | Good quality |
| `llama-3.1-8b-instant` | Groq (free) | 14.4K RPD | Fast fallback |

### LLM Context

The LLM receives comprehensive context for each decision:
- Your moderation guidelines
- Whether comment is `[TOP-LEVEL]` or `[REPLY]`
- Post title
- Parent comment text and author (with OP indicator)
- Grandparent comment text and author
- All ML scores from Detoxify, OpenAI, Perspective
- Pattern match trigger reasons

---

## Auto-Remove (Optional)

For high-confidence toxic comments, auto-remove to mod queue:

```bash
AUTO_REMOVE_ENABLED=true
AUTO_REMOVE_REQUIRE_MODELS=openai,perspective
AUTO_REMOVE_MIN_CONSENSUS=2
AUTO_REMOVE_OPENAI_MIN=0.80
AUTO_REMOVE_PERSPECTIVE_MIN=0.80
AUTO_REMOVE_ON_PATTERN_MATCH=false
```

| Scenario | Action |
|----------|--------|
| LLM=REPORT, OpenAI=0.85, Perspective=0.90 | **AUTO-REMOVE** ✅ |
| LLM=REPORT, OpenAI=0.60, Perspective=0.90 | Report only (OpenAI too low) |
| LLM=BENIGN | No action (LLM has final say) |

---

## Discord Notifications

The bot sends Discord notifications for:
- **Reports** (red) - Comment reported to Reddit
- **Auto-removes** (purple) - Comment auto-removed to mod queue
- **Startup** - Bot started with current stats
- **Daily stats** - Summary at midnight UTC
- **Weekly stats** - Detailed breakdown every 7 days

Configure with:
```bash
DISCORD_WEBHOOK=https://discord.com/api/webhooks/...
```

---

## Installation

### Prerequisites
- Python 3.9+
- Reddit account with mod permissions
- Groq API key (free): https://console.groq.com
- (Optional) OpenAI API key with $5+ credit
- (Optional) Google Perspective API key

### Quick Start

```bash
# Clone the repo
git clone https://github.com/yourusername/ToxicReportBot.git
cd ToxicReportBot

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure
cp env.template .env
nano .env  # Fill in your credentials

# Copy and customize guidelines
cp moderation_guidelines_template.txt moderation_guidelines.txt
nano moderation_guidelines.txt

# Test (make sure DRY_RUN=true)
python bot.py
```

### System Service Setup

```bash
sudo nano /etc/systemd/system/toxicreportbot.service
```

```ini
[Unit]
Description=ToxicReportBot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/ToxicReportBot
Environment=PATH=/home/ubuntu/ToxicReportBot/.venv/bin
ExecStart=/home/ubuntu/ToxicReportBot/.venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable toxicreportbot
sudo systemctl start toxicreportbot
```

### Useful Commands

```bash
# View live logs
sudo journalctl -u toxicreportbot -f

# Check status
sudo systemctl status toxicreportbot

# Restart after config changes
sudo systemctl restart toxicreportbot

# View recent logs
sudo journalctl -u toxicreportbot --since "1 hour ago"
```

---

## Understanding Log Output

```
# Normal comment - all scores low, skipped
PREFILTER | SKIP | Detox:0.02 | OpenAI:0.01 | Persp:0.03 | 'I think UFOs are real...'

# Detox triggered but external low - skipped
PREFILTER | SKIP (detox-only, external APIs low: OpenAI=0.10, Persp=0.15) | Detox:0.95 | OpenAI:0.10 | Persp:0.15 | 'it's a fucking plane...'

# Pattern match with benign skip
PREFILTER | SKIP (benign pattern) | 'Same old bullshit argument...'

# Sent to LLM - external APIs triggered
PREFILTER | SEND (detoxify:toxicity=0.91 + openai:harassment=0.89) [directed, reply] | Detox:0.91 | OpenAI:0.89 | Persp:0.72 | 'you're pathetic...'

# Must-escalate pattern (slur, threat, etc.)
PREFILTER | MUST_ESCALATE (must_escalate:slur) | 'you retard...'
```

---

## Configuration Reference

### Essential Settings

```bash
# Reddit
REDDIT_CLIENT_ID=xxxxx
REDDIT_CLIENT_SECRET=xxxxx
REDDIT_USERNAME=YourBotAccount
REDDIT_PASSWORD=xxxxx
SUBREDDIT=UFOs

# LLM
GROQ_API_KEY=gsk_xxxxx
LLM_MODEL=grok-4-0709
LLM_FALLBACK_CHAIN=openai/gpt-oss-120b,qwen/qwen3-32b

# Operation
DRY_RUN=false
ENABLE_REDDIT_REPORTS=true
```

### External APIs

```bash
# OpenAI Moderation (free, requires $5 deposit)
OPENAI_API_KEY=sk-xxxxx
OPENAI_MODERATION_ENABLED=true
OPENAI_MODERATION_MODE=all
OPENAI_MODERATION_THRESHOLD=0.50

# Google Perspective (free, requires access request)
PERSPECTIVE_API_KEY=xxxxx
PERSPECTIVE_ENABLED=true
PERSPECTIVE_MODE=all
PERSPECTIVE_THRESHOLD=0.70
```

### Thresholds

```bash
# Detoxify thresholds
THRESHOLD_THREAT=0.15
THRESHOLD_SEVERE_TOXICITY=0.20
THRESHOLD_IDENTITY_ATTACK=0.25
THRESHOLD_INSULT_DIRECTED=0.40
THRESHOLD_INSULT_NOT_DIRECTED=0.65
THRESHOLD_TOXICITY_DIRECTED=0.50
THRESHOLD_TOXICITY_NOT_DIRECTED=0.65
THRESHOLD_OBSCENE=0.90
```

---

## Troubleshooting

### Bot not reporting anything
- Check `DRY_RUN` is `false`
- Check `ENABLE_REDDIT_REPORTS` is `true`
- Check bot has mod permissions in the subreddit

### Too many false positives
- Review `false_positives.json` for patterns
- Add benign phrases to `moderation_patterns.json`
- Raise thresholds in `.env`
- Enable both OpenAI and Perspective with `MODE=all`

### Rate limited constantly
- Add more models to `LLM_FALLBACK_CHAIN`
- Lower `LLM_REQUESTS_PER_MINUTE`
- Check Groq dashboard: https://console.groq.com

### Memory issues (1GB RAM)
```bash
# Add swap space
sudo fallocate -l 1G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

---

## Files Overview

| File | Purpose |
|------|---------|
| `bot.py` | Main bot code |
| `moderation_patterns.json` | Pattern lists (slurs, insults, benign phrases) |
| `moderation_guidelines.txt` | LLM instructions (customize for your sub) |
| `.env` | Configuration (API keys, thresholds) |
| `bot_stats.json` | Persistent statistics |
| `pending_reports.json` | Reports awaiting accuracy check |
| `false_positives.json` | Logged false positives for review |
| `benign_analyzed.json` | Comments sent to LLM that returned BENIGN |

---

## License

MIT License - feel free to use and modify.

## Credits

- [Detoxify](https://github.com/unitaryai/detoxify) for local toxicity scoring
- [Groq](https://groq.com) for fast, free LLM inference
- [x.ai](https://x.ai) for Grok API (optional paid alternative)
- [OpenAI Moderation API](https://platform.openai.com/docs/guides/moderation) (free with account)
- [Google Perspective API](https://perspectiveapi.com) (free with access request)
- [PRAW](https://praw.readthedocs.io) for Reddit API access
