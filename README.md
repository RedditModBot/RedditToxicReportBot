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
│  Step 3: ML SCORING                  │         │
│  ┌─────────────┐  ┌─────────────┐    │         │
│  │  Detoxify   │  │  External   │    │         │
│  │  (local)    │  │  APIs       │    │         │
│  └─────────────┘  └─────────────┘    │         │
│  Any triggering = send to AI         │         │
└──────────────────────────────────────┘         │
       │                                         │
       ├── Neither triggered ────── SKIP         │
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

**External API Modes** (for OpenAI Moderation and Perspective):
- `MODE=confirm` (default): Only call API if Detoxify triggers (saves API calls)
- `MODE=all`: Run on every comment (uses more API calls)
- `MODE=only`: Skip Detoxify, use only this API

### Step-by-Step Breakdown

**Step 1: Pattern Matching**

Checks `moderation_patterns.json` for known bad patterns:
- Slurs (racial, homophobic, ableist, etc.)
- Self-harm phrases ("kill yourself", "kys")
- Threat phrases ("I'll find you", "you're dead")
- Shill accusations directed at users ("you're a fed")
- Direct insults at users ("you're an idiot")

If ANY match → immediately send to AI for review (ML scores are still collected for context).

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
| insult | 0.40 | 0.65 |
| toxicity | 0.50 | 0.65 |
| obscene | 0.90 | 0.90 |

"Directed" = contains "you", "your", "OP", or is a reply (excluding "generic you" phrases like "you don't need to", "if you think about it"). See **Detection Thresholds** section for tuning.

**Optional: External Moderation APIs** (free supplements to Detoxify)

You can enable one or both of these APIs to work alongside Detoxify:

**OpenAI Moderation API** - Detects hate, harassment, self-harm, sexual, violence
```bash
OPENAI_API_KEY=sk-xxxxx
OPENAI_MODERATION_ENABLED=true
OPENAI_MODERATION_MODE=confirm  # "all", "confirm", or "only"
OPENAI_MODERATION_THRESHOLD=0.50
OPENAI_MODERATION_RPM=10
```

**Google Perspective API** - Detects toxicity, insult, threat, identity_attack
```bash
PERSPECTIVE_API_KEY=your_key_here
PERSPECTIVE_ENABLED=true
PERSPECTIVE_MODE=confirm  # "all", "confirm", or "only"
PERSPECTIVE_THRESHOLD=0.70
PERSPECTIVE_RPM=60
```

Get Perspective API key at: https://developers.google.com/codelabs/setup-perspective-api

Note: Perspective only supports certain languages (English, Spanish, French, German, etc.). Comments in unsupported languages are silently skipped.

**Step 4: AI Review**

Sends to Groq LLM with full context:
- Your moderation guidelines
- Whether it's a `[TOP-LEVEL]` or `[REPLY]`
- Post title and parent comment
- **ML detector scores** from Detoxify, OpenAI, and Perspective (with thresholds)
- Pattern match trigger reasons (if any)
- The comment text

The LLM sees which detectors flagged the comment and how confident they are, helping it make better decisions. For example, if OpenAI and Perspective both score high but Detoxify is borderline, the LLM knows it's likely truly toxic (not just profanity).

AI returns: `VERDICT: REPORT` or `VERDICT: BENIGN` with a reason.

**Model Fallback Chain** (configurable via `LLM_FALLBACK_CHAIN` in `.env`):

**Recommended: Use Reasoning Models**

Reasoning models "think" before answering, which improves accuracy on nuanced moderation decisions like distinguishing safety warnings from attacks, or sarcasm from genuine hostility.

```bash
# Best setup: Paid Grok primary + Free Groq reasoning fallbacks
LLM_MODEL=grok-4-0709
LLM_FALLBACK_CHAIN=openai/gpt-oss-120b,openai/gpt-oss-20b,qwen/qwen3-32b
GROQ_REASONING_EFFORT=high
```

**Reasoning Models (Recommended):**

| Model | Provider | Reasoning | Cost/Limits | Notes |
|-------|----------|-----------|-------------|-------|
| `grok-4-0709` | x.ai (paid) | ✅ Always | ~$2-5/M in | Best quality, always reasons |
| `openai/gpt-oss-120b` | Groq (free) | ✅ Configurable | 1K RPD, 200K TPD | Best free reasoning |
| `openai/gpt-oss-20b` | Groq (free) | ✅ Configurable | 1K RPD, 200K TPD | Smaller, still reasons |
| `qwen/qwen3-32b` | Groq (free) | ✅ Always | 1K RPD, 500K TPD | Good limits |

**Non-Reasoning Models (faster but less accurate on gray areas):**

| Model | Provider | Limits | Notes |
|-------|----------|--------|-------|
| `groq/compound` | Groq (free) | 250 RPD, unlimited TPD | Smart routing, no reasoning |
| `llama-3.3-70b-versatile` | Groq (free) | 1K RPD, 100K TPD | Good quality |
| `llama-3.1-8b-instant` | Groq (free) | 14.4K RPD, 500K TPD | Fast fallback |

**Reasoning Effort Settings:**
```bash
# For Groq's gpt-oss models (low/medium/high)
GROQ_REASONING_EFFORT=high

# For x.ai's grok-3-mini only (grok-4 always reasons)
#XAI_REASONING_EFFORT=high
```

**Smart Cooldown System**: When rate limited, the bot remembers which models are unavailable and skips them automatically. Cooldowns include a 60-second buffer to ensure rate limits fully reset.

**Detoxify Escalation Control**

If Detoxify triggers too many false positives, you can disable it from triggering LLM review while still using it for scoring context:

```bash
# Detoxify provides scores but won't trigger LLM review on its own
DETOXIFY_CAN_ESCALATE=false

# Let OpenAI and Perspective decide what gets sent to LLM
OPENAI_MODERATION_MODE=all
PERSPECTIVE_MODE=all
```

This way, the LLM still sees Detoxify scores for context, but only OpenAI/Perspective trigger analysis.

**Auto-Remove (Optional)**

For high-confidence toxic comments, you can auto-remove them (sends to mod queue for review):

```bash
AUTO_REMOVE_ENABLED=true

# Which models must agree (comma-separated): detoxify, openai, perspective
AUTO_REMOVE_REQUIRE_MODELS=openai,perspective

# How many must pass their threshold (2 = both must agree)
AUTO_REMOVE_MIN_CONSENSUS=2

# Minimum scores required
AUTO_REMOVE_OPENAI_MIN=0.80
AUTO_REMOVE_PERSPECTIVE_MIN=0.80

# Auto-remove on pattern matches (slurs, threats)?
AUTO_REMOVE_ON_PATTERN_MATCH=false
```

| Scenario | Action |
|----------|--------|
| LLM=REPORT, OpenAI=0.85, Perspective=0.90 | **AUTO-REMOVE** ✅ |
| LLM=REPORT, OpenAI=0.60, Perspective=0.90 | Report only (OpenAI too low) |
| LLM=REPORT, OpenAI disabled | Report only (can't reach consensus) |
| LLM=BENIGN | No action (LLM has final say) |

Auto-removed comments:
- Go to mod queue immediately
- Stay there until a mod approves or confirms removal
- Get a special Discord notification (purple) with all ML scores

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
  "frustration_exclamations": ["holy shit", "what the fuck", "this shit", "..."],
  "profanity_as_emphasis": ["fucking ridiculous", "so fucking", "..."],
  "slang_expressions": ["full of shit", "fake and gay", "copium", "..."],
  "ufo_context_phrases": ["crazy footage", "insane video", "..."],
  "skeptic_phrases": ["this is fake", "obviously a drone", "..."],
  "genuine_questions": ["what exactly is a", "how is he a", "..."]
}
```

When a comment matches these AND isn't directed at a user, skip the AI entirely. Saves API calls on obviously-fine comments. The bot currently has **400+ benign phrases** covering common expressions, internet slang, and profanity used for emphasis.

### Generic "You" Detection

The bot distinguishes between personal attacks and generic statements:
- **"You're an idiot"** → Personal attack = escalate
- **"You can't just ignore this"** → Generic = don't escalate

Currently has **170+ generic "you" phrases** including:
- Hypotheticals: "if you think", "when you look at"
- Generic advice: "you don't need", "you can't expect"
- Rhetorical: "wouldn't you", "don't you think"
- All with apostrophe variants: "dont", "cant", "wouldnt"

### How Pattern Matching Works

1. **Word boundary matching** - "cope" won't match "telescope", "pos" won't match "possessive"
2. **Normalization** - "n1gg3r" gets normalized to check against patterns
3. **Directedness check** - Many patterns only escalate when aimed at a user
4. **Context awareness** - Top-level comments vs replies are treated differently

---

## Features

- **Smart pre-filtering** - Only ~5% of comments use your API quota
- **400+ benign phrases** - Automatically skips common expressions, slang, and profanity-as-emphasis
- **170+ generic "you" phrases** - Distinguishes "you're an idiot" from "you don't need to be an expert"
- **1,600+ total patterns** - Comprehensive coverage of slurs, threats, insults, and variations
- **Misspelling/variant detection** - Catches "stoopid", "ur", "dont", leetspeak like "n1gg3r"
- **Quote detection** - Understands Reddit quote blocks (lines starting with ">")
- **Context-aware** - Knows if it's a reply vs top-level, who's being targeted
- **Public figure detection** - Understands UFO community figures (Grusch, Elizondo, Corbell, etc.)
- **Multi-model consensus** - Combines Detoxify, OpenAI, and Perspective for better accuracy
- **ML scores sent to LLM** - AI sees detector scores and thresholds for informed decisions
- **Auto-remove option** - Automatically remove high-confidence toxic comments (configurable consensus)
- **Detoxify escalation control** - Optionally disable Detoxify from triggering LLM review
- **Model fallback chain** - Automatically switches models if rate limited
- **Discord notifications** - Real-time alerts with trigger reasons (e.g., `detoxify:insult=0.72`, `perspective:TOXICITY=0.85`)
- **Accuracy tracking** - Logs false positives for tuning
- **Dry run mode** - Test without actually reporting

---

## Requirements

- Python 3.9+
- Reddit account with mod permissions (for moderator reports)
- Groq API key (free at https://console.groq.com)
- x.ai API key (optional, paid - for Grok models at https://console.x.ai)
- OpenAI API key (optional, for Moderation API)
- Google Perspective API key (optional, free at https://developers.google.com/codelabs/setup-perspective-api)
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
| `LLM_MODEL` | `groq/compound` | Primary model to try first |
| `LLM_FALLBACK_CHAIN` | (see below) | Comma-separated list of fallback models |
| `LLM_REQUESTS_PER_MINUTE` | `2` | Rate limit (1 request per 30 sec) |
| `LLM_DAILY_LIMIT` | `14000` | Legacy setting (less relevant with fallback chain) |

**Default fallback chain:**
```
LLM_FALLBACK_CHAIN=llama-3.3-70b-versatile,meta-llama/llama-4-scout-17b-16e-instruct,meta-llama/llama-4-maverick-17b-128e-instruct,llama-3.1-8b-instant
```

**Available models (all free on Groq):**

| Model | Quality | Requests/Day | Tokens/Day | Best For |
|-------|---------|--------------|------------|----------|
| `groq/compound` | Great | 250 | Unlimited | **Recommended primary** |
| `llama-3.3-70b-versatile` | Excellent | 1K | 100K | Highest quality |
| `meta-llama/llama-4-scout-17b-16e-instruct` | Good | 1K | 500K | Good balance |
| `meta-llama/llama-4-maverick-17b-128e-instruct` | Good | 1K | 500K | Good balance |
| `llama-3.1-8b-instant` | Decent | 14.4K | 500K | Last resort |

**Not recommended:** 
- `llama-guard-4-12b` - Classifier model, returns "safe/unsafe" instead of following instructions
- `qwen/qwen3-32b` - Outputs `<think>` tags, doesn't follow instructions cleanly

**How fallback works:**
1. Bot tries your `LLM_MODEL` first
2. If rate limited, it sets a cooldown (API wait time + 60s buffer)
3. Tries next model in `LLM_FALLBACK_CHAIN`
4. Skips models on cooldown automatically
5. Cooldowns clear when time expires

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
| `DETOXIFY_THRESHOLD` | `0.50` | Main pre-filter - comments below this skip label checks |
| `THRESHOLD_THREAT` | `0.15` | Threats of violence/harm |
| `THRESHOLD_SEVERE_TOXICITY` | `0.20` | Extreme toxic content |
| `THRESHOLD_IDENTITY_ATTACK` | `0.25` | Slurs, hate speech |
| `THRESHOLD_INSULT_DIRECTED` | `0.40` | Insults aimed at users ("you're an idiot") |
| `THRESHOLD_INSULT_NOT_DIRECTED` | `0.65` | General insults not at a user |
| `THRESHOLD_TOXICITY_DIRECTED` | `0.50` | Toxic comments at users |
| `THRESHOLD_TOXICITY_NOT_DIRECTED` | `0.65` | General toxic comments |
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
| `moderation_patterns.json` | Word lists for pre-filtering (slurs, insults, benign phrases, etc.) |
| `env.template` | Template for `.env` configuration |
| `requirements.txt` | Python dependencies |
| `reported_comments.json` | Auto-generated tracking of reported comments and outcomes |
| `false_positives.json` | Auto-generated log of false positives (reported but not removed) |
| `benign_analyzed.json` | Auto-generated log of comments sent to LLM that were benign |

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
  "detoxify_scores": {"toxicity": 0.85, "insult": 0.78, "severe_toxicity": 0.42},
  "openai_scores": {"harassment": 0.72, "hate": 0.15},
  "perspective_scores": {"TOXICITY": 0.91, "INSULT": 0.88, "PROFANITY": 0.65},
  "is_top_level": false,
  "reported_at": "2025-01-15T10:30:00Z",
  "outcome": "pending",      // or "removed" or "approved"
  "checked_at": ""
}
```

**benign_analyzed.json** - Comments sent to LLM that came back benign:
```json
{
  "comment_id": "t1_def456",
  "permalink": "https://reddit.com/r/UFOs/comments/.../def456/",
  "text": "This looks fake, you can see the reflection",
  "llm_reason": "Brief skepticism with reasoning",
  "detoxify_score": 0.35,
  "detoxify_scores": {"toxicity": 0.35, "insult": 0.22},
  "openai_scores": {"harassment": 0.08, "hate": 0.02},
  "perspective_scores": {"TOXICITY": 0.28, "INSULT": 0.19},
  "is_top_level": true,
  "prefilter_trigger": "detoxify:toxicity=0.35",
  "analyzed_at": "2025-01-15T10:30:00Z"
}
```

**false_positives.json** - Comments that weren't removed:
```json
{
  "comment_id": "t1_xyz789",
  "permalink": "https://reddit.com/r/UFOs/comments/.../xyz789/",
  "text": "This is obviously fake garbage",
  "groq_reason": "Low-effort hostile dismissal",
  "detoxify_score": 0.62,
  "detoxify_scores": {"toxicity": 0.62, "insult": 0.45},
  "openai_scores": {"harassment": 0.38, "hate": 0.05},
  "perspective_scores": {"TOXICITY": 0.58, "INSULT": 0.42},
  "is_top_level": true,
  "reported_at": "2025-01-15T08:00:00Z",
  "discovered_at": "2025-01-16T08:00:00Z"
}
```

The ML scores help you understand why each API flagged the comment:
- **detoxify_scores**: Detoxify model categories (toxicity, insult, severe_toxicity, etc.)
- **openai_scores**: OpenAI Moderation categories (harassment, hate, violence, etc.)
- **perspective_scores**: Google Perspective categories (TOXICITY, INSULT, PROFANITY, etc.)

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

### Understanding Log Output

The bot logs show ML scores from all APIs for each comment:

```
# Benign comment - shows all API scores
PREFILTER | SKIP | Detox:0.08 | OpenAI:0.02 | Persp:0.05 | 'Agree its a satellite...'

# Flagged comment - shows what triggered and scores
PREFILTER | SEND (detoxify:toxicity=0.85 + openai:harassment) [directed, reply] | Detox:0.85 | OpenAI:0.72 | Persp:0.91 | 'you idiot...'

# API calls (shown before scores)
HTTP Request: POST https://api.openai.com/v1/moderations "HTTP/1.1 200 OK"
HTTP Request: POST https://commentanalyzer.googleapis.com/v1alpha1/comments:analyze
```

Score meanings:
- **Detox**: Detoxify toxicity score (0-1)
- **OpenAI**: Max of harassment/hate from OpenAI Moderation (0-1)
- **Persp**: Max of TOXICITY/INSULT from Google Perspective (0-1)

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
- Check Groq dashboard for usage: https://console.groq.com/settings/organization/usage
- The fallback chain should handle this automatically
- If ALL models are exhausted, add more models to `LLM_FALLBACK_CHAIN`
- Consider adding x.ai Grok models as fallback (requires `XAI_API_KEY`)
- Lower `LLM_REQUESTS_PER_MINUTE` to `1` for slower but safer operation
- Look for "Skipping [model] - on cooldown" in logs to see what's happening

### Falling back to llama-3.1-8b-instant too often
This means better models are rate limited. Solutions:
- Wait - daily limits reset at midnight UTC
- Add more mid-tier models to your fallback chain
- The bot tracks cooldowns, so it won't keep hammering rate-limited models

### Discord notifications not working
- Check webhook URL is correct
- Check for "Discord embed post failed" in logs
- Test webhook with curl

### High false positive rate
- Review `false_positives.json` to see patterns
- Update `moderation_guidelines.txt` with more examples
- Add common benign phrases to `benign_skip` in `moderation_patterns.json`
- Raise thresholds in `.env` (e.g., `THRESHOLD_INSULT_DIRECTED=0.50`)

### Reddit 502/504 errors
These are Reddit API hiccups - the bot handles them automatically with retries. If you see them constantly for several minutes, Reddit may be having issues.

---

## License

MIT License - feel free to use and modify.

## Credits

- [Detoxify](https://github.com/unitaryai/detoxify) for local toxicity scoring
- [Groq](https://groq.com) for fast, free LLM inference
- [x.ai](https://x.ai) for Grok API (optional paid alternative)
- [OpenAI Moderation API](https://platform.openai.com/docs/guides/moderation) (optional)
- [Google Perspective API](https://perspectiveapi.com) (optional)
- [PRAW](https://praw.readthedocs.io) for Reddit API access
