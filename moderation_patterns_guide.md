# Moderation Patterns Guide

This guide explains how `moderation_patterns.json` works and how to customize it for your subreddit.

## Overview

The patterns file controls the **pre-filter** stage - fast pattern matching that happens BEFORE sending comments to the AI. This saves API calls and catches obvious cases quickly.

```
Comment arrives
      │
      ▼
┌─────────────────────────────┐
│  PATTERN MATCHING           │  ◄── moderation_patterns.json
│  (slurs, threats, insults)  │
└─────────────────────────────┘
      │
      ├── Slur/threat found ────► Send to AI immediately
      ├── Benign phrase found ──► SKIP (no API call)
      │
      ▼
┌─────────────────────────────┐
│  DETOXIFY ML SCORING        │
└─────────────────────────────┘
      │
      ▼
┌─────────────────────────────┐
│  AI REVIEW (if needed)      │
└─────────────────────────────┘
```

## File Structure

The JSON file has these main sections:

```json
{
  "slurs": { ... },                      // Always escalate to AI
  "self_harm": { ... },                  // Always escalate to AI
  "threats": { ... },                    // Always escalate to AI
  "shill_accusations": { ... },          // Escalate when directed at user
  "insults_direct": { ... },             // Escalate when directed at user
  "dismissive_hostile": { ... },         // Escalate when directed at user
  "contextual_sensitive_terms": { ... }, // Escalate with additional signals
  "benign_skip": { ... },                // Skip AI entirely
  "public_figures": { ... },             // Names for context
  "obfuscation_map": { ... },            // Character substitutions
  "regex_patterns": { ... }              // Advanced patterns
}
```

---

## Section Details

### 1. Slurs (Always Escalate)

High-confidence bad words that almost always indicate a problem. Any match immediately sends to AI.

```json
"slurs": {
  "racial": ["nigger", "nigga", "chink", "spic", "wetback", "..."],
  "homophobic_hard": ["faggot", "fag", "dyke", "..."],
  "transphobic_hard": ["tranny", "..."],
  "ableist_hard": ["retard", "retarded", "..."]
}
```

**Customization tips:**
- Add slurs specific to your community
- Include common misspellings and variations
- The bot automatically checks for obfuscation (n1gg3r, f@g, etc.)

### 2. Self-Harm (Always Escalate)

Phrases encouraging self-harm. These are high-priority safety issues.

```json
"self_harm": {
  "direct": ["kill yourself", "kys", "end yourself", "..."],
  "indirect": ["world would be better without you", "do everyone a favor and disappear", "..."]
}
```

**Important:** These patterns require word boundaries, so "kys" won't match "Tokyo" or "keys".

### 3. Threats (Always Escalate)

Direct and implied threats of violence.

```json
"threats": {
  "direct": ["i'll kill you", "you're dead", "i will find you", "..."],
  "implied": ["watch your back", "sleep with one eye open", "..."]
}
```

### 4. Shill Accusations (Escalate When Directed)

Accusations that someone is a paid agent, bot, etc. Only escalates when aimed at a Reddit user.

```json
"shill_accusations": {
  "terms": ["shill", "bot", "paid actor", "fed", "glowie", "disinfo agent", "..."]
}
```

**Customization tips:**
- Add domain-specific accusations for your subreddit
- Examples: "bag holder" (crypto), "astroturfer" (politics), "corporate shill" (tech)

### 5. Direct Insults (Escalate When Directed)

Insult words that escalate when aimed at another user (contains "you", "your", "OP", or is a reply).

```json
"insults_direct": {
  "intelligence": ["idiot", "moron", "dumbass", "stupid", "braindead", "..."],
  "character": ["loser", "pathetic", "scumbag", "trash", "..."],
  "profane": ["asshole", "dipshit", "douchebag", "..."],
  "mental_health": ["crazy", "insane", "lunatic", "psycho", "..."],
  "dismissive": ["clown", "joke", "troll", "..."]
}
```

**How directedness works:**
- "He's an idiot" → About third party = NOT escalated
- "You're an idiot" → At user = ESCALATED
- "OP is an idiot" → At user = ESCALATED

### 6. Dismissive/Hostile (Escalate When Directed)

Phrases that dismiss or attack users. Split into "hard" (always hostile) and "soft" (context-dependent).

```json
"dismissive_hostile": {
  "hard": ["fuck off", "shut the fuck up", "eat shit", "go fuck yourself", "..."],
  "soft": ["cope", "seethe", "touch grass", "get a life", "..."]
}
```

**Hard vs Soft:**
- **Hard phrases** escalate if directed OR if it's a reply to someone
- **Soft phrases** only escalate if strongly directed (contains "you")

### 7. Contextual Sensitive Terms (Conditional Escalation)

Words that CAN be problematic but often appear in neutral contexts. Only escalate with additional signals.

```json
"contextual_sensitive_terms": {
  "racial_ambiguous": ["negro", "colored", "cracker", "gringo", "..."],
  "sexual_orientation": ["homo", "queer", "..."],
  "ideology_terms": ["nazi", "white power", "..."],
  "ambiguous_insults": ["thick", "tool", "dense", "freak", "..."]
}
```

**When these escalate:**
- Combined with high Detoxify identity_attack score, OR
- Strongly directed at a user

**Why contextual?**
- "negro" = Spanish for "black", can be neutral
- "queer" = reclaimed term, often positive
- "dense" = could mean "stupid" or "thick fog"

### 8. Benign Skip (Skip AI Entirely)

Phrases that indicate a comment is harmless. If matched AND not directed at a user, skip the AI completely.

```json
"benign_skip": {
  "frustration_exclamations": ["holy shit", "what the fuck", "no shit", "this shit", "..."],
  "profanity_as_emphasis": ["fucking ridiculous", "so fucking", "fucking amazing", "..."],
  "slang_expressions": ["full of shit", "fake and gay", "copium", "..."],
  "enthusiastic_agreement": ["hell yeah", "fuck yeah", "damn right", "..."],
  "genuine_questions": ["what exactly is a", "how is he a", "..."],
  "disbelief_at_situation": ["this is bullshit", "pure bullshit", "..."],
  "ufo_context_phrases": ["mind blowing", "incredible footage", "..."],
  "skeptic_phrases": ["looks fake", "obviously cgi", "chinese lantern", "..."]
}
```

**Customization tips:**
- Add common expressions from your community
- Only include PHRASES (2+ words), not single words
- These save significant API calls on obviously-benign comments

**Current count:** 240+ benign phrases

### 9. Public Figures

Names of people commonly discussed. Helps the AI understand criticism is about public figures, not users.

```json
"public_figures": {
  "ufo_personalities": ["Grusch", "Elizondo", "Corbell", "Knapp", "..."],
  "politicians": ["Rubio", "Schumer", "Gillibrand", "..."],
  "scientists": ["Avi Loeb", "Garry Nolan", "..."]
}
```

**Customization:** Replace entirely with figures relevant to your subreddit.

### 10. Obfuscation Map

Character substitutions used to evade filters. The bot uses this to detect "l33tspeak".

```json
"obfuscation_map": {
  "leet_speak": {
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "@": "a", "$": "s"
  },
  "unicode_lookalikes": {
    "а": "a", "е": "e", "о": "o"
  }
}
```

This lets the bot catch "n1gg3r", "f@g", "r3tard", etc.

### 11. Regex Patterns

Advanced patterns for complex matching.

```json
"regex_patterns": {
  "violence_illegal_advocacy": ["\\b(shoot|laser)\\s+(it|that|them|the\\s+ufo)\\b", "..."],
  "directedness_check": ["\\b(you|your|you're)\\b", "\\bop\\b", "..."],
  "generic_you_phrases": ["you don't need", "if you think", "you can see", "..."]
}
```

**generic_you_phrases:** These exclude "generic you" from directedness checks:
- "you don't need a scientist" → NOT directed (generic advice)
- "you're an idiot" → IS directed (personal attack)

---

## How Pattern Matching Works

### Word Boundaries

All patterns use word boundary matching by default:
- "cope" matches "cope" and "coping" but NOT "telescope"
- "ass" matches "ass" but NOT "class" or "assed"

### Normalization

Before matching, text is normalized:
1. Lowercased
2. Leet speak decoded (n1gg3r → nigger)
3. Unicode normalized (Cyrillic а → Latin a)
4. Excessive characters collapsed (fuuuuck → fuck)

### Directedness Detection

The bot checks if insults are aimed at users:

**Strongly directed (triggers escalation):**
- Contains: you, your, you're, ur
- Contains: OP, mods
- Contains: y'all, "you guys", "you people", "everyone here"
- Contains: "this sub", "this subreddit"

**Excludes generic "you":**
- "you don't need to" - generic advice
- "if you think about it" - hypothetical
- "you can see why" - rhetorical

---

## Customization Checklist

When adapting for your subreddit:

### Must Customize:
- [ ] **public_figures** - Replace with people relevant to your community
- [ ] **shill_accusations** - Add domain-specific accusations
- [ ] **benign_skip** - Add common expressions in your community

### Consider Customizing:
- [ ] **slurs** - Add any community-specific slurs
- [ ] **threats** - Add domain-specific threats (e.g., "laser the plane" for UFOs)
- [ ] **contextual_sensitive_terms** - Add ambiguous terms in your domain

### Usually Keep As-Is:
- [ ] **self_harm** - Universal patterns
- [ ] **obfuscation_map** - Universal character mappings
- [ ] **regex_patterns** - Core detection logic

---

## Testing Your Changes

After modifying the patterns file:

1. **Validate JSON:**
   ```bash
   python -c "import json; json.load(open('moderation_patterns.json'))"
   ```

2. **Test specific patterns:**
   ```bash
   python -c "
   import json, re
   with open('moderation_patterns.json') as f:
       patterns = json.load(f)
   
   # Test a phrase
   test = 'your test comment here'
   # Check against patterns...
   "
   ```

3. **Run in dry mode:**
   ```bash
   DRY_RUN=true python bot.py
   ```

4. **Monitor logs:**
   ```bash
   # Look for PREFILTER entries
   grep "PREFILTER" bot.log
   ```

---

## Performance Impact

| Pattern Type | API Calls | Speed |
|--------------|-----------|-------|
| Slur match | +1 (AI review) | Fast |
| Benign skip | 0 (skipped) | Very fast |
| No match | Depends on Detoxify | Medium |

**Optimization tips:**
- More benign_skip phrases = fewer API calls
- Specific patterns > broad patterns
- Test false positive rates after changes

---

## Common Mistakes

1. **Single-word benign phrases:** Only multi-word phrases work in benign_skip
   - ❌ `"shit"` - too broad
   - ✅ `"holy shit"` - specific phrase

2. **Overly broad patterns:** Can cause false positives
   - ❌ `"die"` in threats - matches "died", "diet"
   - ✅ `"i hope you die"` - specific threat

3. **Missing word boundaries:** Regex patterns need `\b`
   - ❌ `"ass"` - matches "class", "assed"
   - ✅ `"\bass\b"` - matches only "ass"

4. **Forgetting obfuscation:** Bad actors will try to evade
   - Add common misspellings to slurs
   - The bot handles leet speak automatically

---

## Getting Help

If you're unsure about a pattern:
1. Check the production `moderation_patterns.json` for examples
2. Test with real comments from your subreddit
3. Monitor `false_positives.json` and `benign_analyzed.json` after deployment
4. Adjust thresholds in `.env` if needed
