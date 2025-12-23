#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RedditToxicReportBot v2 - LLM-Powered Comment Moderation

Uses Detoxify as a pre-filter to decide what needs analysis,
then Groq API (free tier) for intelligent context-aware toxicity detection.
"""

import os
import sys
import json
import time
import logging
import traceback
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from enum import Enum

# -------- env loading --------
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.getenv("ENV_FILE", ".env"))

# -------- reddit / http --------
import praw
import prawcore

# -------- LLM --------
from groq import Groq
from openai import OpenAI  # For x.ai Grok API (OpenAI-compatible)

# -------- discord optional --------
import urllib.request


# -------------------------------
# Enums
# -------------------------------

class Verdict(Enum):
    """Classification results from LLM analysis"""
    REPORT = "REPORT"           # Clearly toxic, should be reported
    BENIGN = "BENIGN"           # Not toxic, no action needed


# -------------------------------
# Reported Comments Tracking
# -------------------------------

TRACKING_FILE = "reported_comments.json"
BENIGN_TRACKING_FILE = "benign_analyzed.json"
BENIGN_TRACKING_MAX_AGE_HOURS = 48  # Auto-cleanup entries older than this

def load_tracked_comments() -> List[Dict]:
    """Load tracked comments from JSON file"""
    try:
        with open(TRACKING_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        logging.warning(f"Could not parse {TRACKING_FILE}, starting fresh")
        return []

def save_tracked_comments(comments: List[Dict]) -> None:
    """Save tracked comments to JSON file"""
    with open(TRACKING_FILE, "w", encoding="utf-8") as f:
        json.dump(comments, f, indent=2)

def load_benign_analyzed() -> List[Dict]:
    """Load benign analyzed comments from JSON file"""
    try:
        with open(BENIGN_TRACKING_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        logging.warning(f"Could not parse {BENIGN_TRACKING_FILE}, starting fresh")
        return []

def save_benign_analyzed(comments: List[Dict]) -> None:
    """Save benign analyzed comments to JSON file"""
    with open(BENIGN_TRACKING_FILE, "w", encoding="utf-8") as f:
        json.dump(comments, f, indent=2)

def track_benign_analyzed(comment_id: str, permalink: str, text: str,
                          llm_reason: str, detoxify_score: float,
                          detoxify_scores: Dict[str, float],
                          is_top_level: bool = False,
                          prefilter_trigger: str = "") -> None:
    """
    Track comments that were sent to LLM but came back BENIGN.
    Auto-cleans entries older than BENIGN_TRACKING_MAX_AGE_HOURS.
    """
    comments = load_benign_analyzed()
    now = time.time()
    cutoff = now - (BENIGN_TRACKING_MAX_AGE_HOURS * 3600)
    
    # Clean old entries
    comments = [c for c in comments if c.get("timestamp", 0) > cutoff]
    
    # Don't add duplicates
    if any(c.get("comment_id") == comment_id for c in comments):
        save_benign_analyzed(comments)  # Still save to persist cleanup
        return
    
    comments.append({
        "comment_id": comment_id,
        "permalink": permalink,
        "text": text[:500],
        "llm_reason": llm_reason,
        "detoxify_score": detoxify_score,
        "detoxify_scores": detoxify_scores,
        "is_top_level": is_top_level,
        "prefilter_trigger": prefilter_trigger,
        "analyzed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "timestamp": now
    })
    
    save_benign_analyzed(comments)
    logging.debug(f"Tracking benign analyzed comment: {comment_id}")

def track_reported_comment(comment_id: str, permalink: str, text: str, 
                           groq_reason: str, detoxify_score: float,
                           is_top_level: bool = False) -> None:
    """Add a newly reported comment to tracking"""
    comments = load_tracked_comments()
    
    # Don't add duplicates
    if any(c.get("comment_id") == comment_id for c in comments):
        return
    
    comments.append({
        "comment_id": comment_id,
        "permalink": permalink,
        "text": text[:500],  # Truncate long comments
        "groq_reason": groq_reason,
        "detoxify_score": detoxify_score,
        "is_top_level": is_top_level,
        "reported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "outcome": "pending",
        "checked_at": ""
    })
    
    save_tracked_comments(comments)
    logging.debug(f"Tracking reported comment: {comment_id}")

def check_reported_outcomes(reddit: praw.Reddit, min_age_hours: int = 24) -> Dict[str, int]:
    """
    Check outcomes of pending reported comments.
    Returns stats dict with counts.
    """
    comments = load_tracked_comments()
    now = time.time()
    stats = {"checked": 0, "removed": 0, "approved": 0, "still_pending": 0, "errors": 0}
    
    for entry in comments:
        if entry.get("outcome") != "pending":
            continue
        
        # Check if comment is old enough
        reported_at = entry.get("reported_at", "")
        if reported_at:
            try:
                reported_time = time.mktime(time.strptime(reported_at, "%Y-%m-%dT%H:%M:%SZ"))
                age_hours = (now - reported_time) / 3600
                if age_hours < min_age_hours:
                    stats["still_pending"] += 1
                    continue
            except ValueError:
                pass
        
        # Check comment status via Reddit API
        comment_id = entry.get("comment_id", "")
        if not comment_id:
            continue
            
        try:
            # Remove t1_ prefix if present for fetching
            clean_id = comment_id.replace("t1_", "")
            comment = reddit.comment(clean_id)
            
            # Force fetch the comment data
            _ = comment.body
            
            # Check if removed
            # removed_by_category is set if removed by mod/automod/admin
            # If comment.body is "[removed]" it was removed
            if comment.body == "[removed]" or getattr(comment, 'removed', False):
                entry["outcome"] = "removed"
                stats["removed"] += 1
            elif getattr(comment, 'removed_by_category', None):
                entry["outcome"] = "removed"
                stats["removed"] += 1
            else:
                # Comment still exists, was approved or not actioned
                entry["outcome"] = "approved"
                stats["approved"] += 1
            
            entry["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            stats["checked"] += 1
            
        except prawcore.exceptions.NotFound:
            # Comment was deleted (by user or mod)
            entry["outcome"] = "removed"
            entry["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            stats["removed"] += 1
            stats["checked"] += 1
        except Exception as e:
            logging.warning(f"Error checking comment {comment_id}: {e}")
            stats["errors"] += 1
    
    save_tracked_comments(comments)
    return stats

def cleanup_old_tracked(max_age_days: int = 30) -> int:
    """Remove entries older than max_age_days that have been resolved"""
    comments = load_tracked_comments()
    now = time.time()
    original_count = len(comments)
    
    filtered = []
    for entry in comments:
        # Keep pending entries regardless of age
        if entry.get("outcome") == "pending":
            filtered.append(entry)
            continue
        
        # Check age of resolved entries
        checked_at = entry.get("checked_at", "")
        if checked_at:
            try:
                checked_time = time.mktime(time.strptime(checked_at, "%Y-%m-%dT%H:%M:%SZ"))
                age_days = (now - checked_time) / 86400
                if age_days < max_age_days:
                    filtered.append(entry)
            except ValueError:
                filtered.append(entry)
        else:
            filtered.append(entry)
    
    save_tracked_comments(filtered)
    removed = original_count - len(filtered)
    if removed > 0:
        logging.info(f"Cleaned up {removed} old tracking entries")
    return removed

def get_accuracy_stats() -> Dict[str, any]:
    """Calculate accuracy statistics from tracked comments"""
    comments = load_tracked_comments()
    
    total = len(comments)
    pending = sum(1 for c in comments if c.get("outcome") == "pending")
    removed = sum(1 for c in comments if c.get("outcome") == "removed")
    approved = sum(1 for c in comments if c.get("outcome") == "approved")
    
    resolved = removed + approved
    accuracy = (removed / resolved * 100) if resolved > 0 else 0
    
    return {
        "total_tracked": total,
        "pending": pending,
        "removed": removed,
        "approved": approved,
        "resolved": resolved,
        "accuracy_pct": accuracy
    }


# -------------------------------
# Config
# -------------------------------

@dataclass
class Config:
    # Reddit
    client_id: str
    client_secret: str
    username: str
    password: str
    user_agent: str
    subreddits: List[str]

    # LLM
    groq_api_key: str
    xai_api_key: str  # Optional: for x.ai Grok models
    llm_model: str
    llm_fallback_chain: List[str]  # Fallback models in order of preference
    llm_daily_limit: int        # Switch to fallback after this many calls
    llm_requests_per_minute: int  # Max requests per minute to Groq
    
    # Detoxify pre-filter
    detoxify_model: str        # "original" or "unbiased"
    
    # Detoxify thresholds per label
    threshold_threat: float
    threshold_severe_toxicity: float
    threshold_identity_attack: float
    threshold_insult_directed: float
    threshold_insult_not_directed: float
    threshold_toxicity_directed: float
    threshold_toxicity_not_directed: float
    threshold_obscene: float
    threshold_borderline: float  # Score above this logs as borderline skip
    
    # Custom moderation guidelines (loaded from file or env)
    moderation_guidelines: str

    # Reporting behavior
    report_as: str              # "moderator" | "user"
    report_rule_bucket: str
    enable_reddit_reports: bool
    dry_run: bool

    # Discord
    enable_discord: bool
    discord_webhook: str

    # Runtime
    log_level: str


def load_config() -> Config:
    """Load and validate configuration from environment"""
    
    # Required Reddit vars
    for key in [
        "REDDIT_CLIENT_ID",
        "REDDIT_CLIENT_SECRET",
        "REDDIT_USERNAME",
        "REDDIT_PASSWORD",
        "REDDIT_USER_AGENT",
    ]:
        if not os.getenv(key):
            raise KeyError(f"Missing required env var: {key}")

    # Required Groq key
    if not os.getenv("GROQ_API_KEY"):
        raise KeyError("Missing required env var: GROQ_API_KEY (get free key at console.groq.com)")

    subs = os.getenv("SUBREDDITS", "").strip()
    if not subs:
        raise KeyError("SUBREDDITS is required, e.g. 'UFOs' or 'a,b,c'")

    # Load moderation guidelines from file or env
    guidelines_path = os.getenv("MODERATION_GUIDELINES_FILE", "moderation_guidelines.txt")
    guidelines = os.getenv("MODERATION_GUIDELINES", "").strip()
    
    if not guidelines and os.path.exists(guidelines_path):
        with open(guidelines_path, "r", encoding="utf-8") as f:
            guidelines = f.read().strip()
    
    if not guidelines:
        raise KeyError(f"No moderation guidelines found. Create '{guidelines_path}' or set MODERATION_GUIDELINES env var.")

    cfg = Config(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        username=os.environ["REDDIT_USERNAME"],
        password=os.environ["REDDIT_PASSWORD"],
        user_agent=os.environ["REDDIT_USER_AGENT"],
        subreddits=[s.strip() for s in subs.split(",") if s.strip()],
        
        groq_api_key=os.environ["GROQ_API_KEY"],
        xai_api_key=os.getenv("XAI_API_KEY", ""),  # Optional
        llm_model=os.getenv("LLM_MODEL", "groq/compound"),
        llm_fallback_chain=[s.strip() for s in os.getenv("LLM_FALLBACK_CHAIN", 
            "llama-3.3-70b-versatile,meta-llama/llama-4-scout-17b-16e-instruct,meta-llama/llama-4-maverick-17b-128e-instruct,llama-3.1-8b-instant"
        ).split(",") if s.strip()],
        llm_daily_limit=int(os.getenv("LLM_DAILY_LIMIT", "240")),
        llm_requests_per_minute=int(os.getenv("LLM_REQUESTS_PER_MINUTE", "2")),
        
        detoxify_model=os.getenv("DETOXIFY_MODEL", "original"),
        
        # Per-label thresholds (lower = more sensitive)
        threshold_threat=float(os.getenv("THRESHOLD_THREAT", "0.15")),
        threshold_severe_toxicity=float(os.getenv("THRESHOLD_SEVERE_TOXICITY", "0.20")),
        threshold_identity_attack=float(os.getenv("THRESHOLD_IDENTITY_ATTACK", "0.25")),
        threshold_insult_directed=float(os.getenv("THRESHOLD_INSULT_DIRECTED", "0.40")),
        threshold_insult_not_directed=float(os.getenv("THRESHOLD_INSULT_NOT_DIRECTED", "0.60")),
        threshold_toxicity_directed=float(os.getenv("THRESHOLD_TOXICITY_DIRECTED", "0.40")),
        threshold_toxicity_not_directed=float(os.getenv("THRESHOLD_TOXICITY_NOT_DIRECTED", "0.50")),
        threshold_obscene=float(os.getenv("THRESHOLD_OBSCENE", "0.90")),
        threshold_borderline=float(os.getenv("THRESHOLD_BORDERLINE", "0.35")),
        
        moderation_guidelines=guidelines,
        
        report_as=os.getenv("REPORT_AS", "moderator").lower(),
        report_rule_bucket=os.getenv("REPORT_RULE_BUCKET", "").strip(),
        enable_reddit_reports=os.getenv("ENABLE_REDDIT_REPORTS", "true").lower() == "true",
        dry_run=os.getenv("DRY_RUN", "false").lower() == "true",
        
        enable_discord=os.getenv("ENABLE_DISCORD", "false").lower() == "true",
        discord_webhook=os.getenv("DISCORD_WEBHOOK", "").strip(),
        
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
    return cfg


# -------------------------------
# Logging
# -------------------------------

def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


# -------------------------------
# Reddit
# -------------------------------

def praw_client(cfg: Config) -> praw.Reddit:
    reddit = praw.Reddit(
        client_id=cfg.client_id,
        client_secret=cfg.client_secret,
        username=cfg.username,
        password=cfg.password,
        user_agent=cfg.user_agent,
        ratelimit_seconds=60,
    )
    me = reddit.user.me()
    logging.info(f"Authenticated as u/{me.name}")
    return reddit


# -------------------------------
# Pre-filter using Detoxify
# -------------------------------

import re

# ============================================
# LOAD PATTERNS FROM JSON
# ============================================

PATTERNS_FILE = "moderation_patterns.json"

def load_moderation_patterns(path: str = PATTERNS_FILE) -> Dict:
    """Load moderation patterns from JSON file"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logging.warning(f"Patterns file not found at {path}, using defaults")
        return {}
    except json.JSONDecodeError as e:
        logging.warning(f"Could not parse {path}: {e}, using defaults")
        return {}

# Load patterns at module level
PATTERNS = load_moderation_patterns()

# ============================================
# 1. TEXT NORMALIZATION & DE-OBFUSCATION
# ============================================

# Get leet map from JSON or use defaults
LEET_MAP = PATTERNS.get("obfuscation_map", {}).get("leet_speak", {
    '0': 'o', '1': 'i', '3': 'e', '4': 'a', '5': 's',
    '7': 't', '8': 'b', '@': 'a', '$': 's', '!': 'i',
})

# Get common evasions from JSON
COMMON_EVASIONS = PATTERNS.get("obfuscation_map", {}).get("common_evasions", {
    "ph": "f", "ck": "k"
})

def normalize_text(text: str) -> str:
    """
    Normalize text for pattern matching.
    Returns lowercase with common obfuscations removed.
    """
    result = text.lower()
    
    # Replace leet speak
    for leet, normal in LEET_MAP.items():
        result = result.replace(leet, normal)
    
    # Apply common evasions
    for evasion, replacement in COMMON_EVASIONS.items():
        result = result.replace(evasion, replacement)
    
    return result

def squash_text(text: str) -> str:
    """
    Remove spaces and punctuation for catching spaced-out evasions.
    "k y s" -> "kys", "s.h" -> "sh"
    """
    result = normalize_text(text)
    result = re.sub(r'[^a-z0-9]', '', result)
    return result


# ============================================
# 2. BUILD PATTERN LISTS FROM JSON
# ============================================

def build_slur_sets() -> Tuple[set, set]:
    """
    Build separate sets for single-word slurs and multi-word slur phrases.
    Returns (slur_words, slur_phrases)
    """
    slur_words = set()
    slur_phrases = set()
    slurs_data = PATTERNS.get("slurs", {})
    for category, words in slurs_data.items():
        if category.startswith("_"):
            continue
        if isinstance(words, list):
            for w in words:
                w_lower = w.lower()
                if ' ' in w_lower:
                    slur_phrases.add(w_lower)
                else:
                    slur_words.add(w_lower)
    return slur_words, slur_phrases

def build_self_harm_set() -> set:
    """Build set of self-harm phrases from JSON"""
    phrases = set()
    self_harm = PATTERNS.get("self_harm", {}).get("phrases", [])
    phrases.update(p.lower() for p in self_harm)
    return phrases

def build_threat_set() -> set:
    """Build set of threat phrases from JSON"""
    phrases = set()
    threats = PATTERNS.get("threats", {})
    for category, words in threats.items():
        if category.startswith("_"):
            continue
        if isinstance(words, list):
            phrases.update(w.lower() for w in words)
    return phrases

def build_sexual_violence_set() -> set:
    """Build set of sexual violence phrases from JSON"""
    phrases = PATTERNS.get("sexual_violence", {}).get("phrases", [])
    return set(p.lower() for p in phrases)

def build_brigading_set() -> set:
    """Build set of brigading/harassment phrases from JSON"""
    phrases = PATTERNS.get("brigading_harassment", {}).get("phrases", [])
    return set(p.lower() for p in phrases)

def build_shill_set() -> set:
    """Build set of shill accusation phrases from JSON"""
    terms = PATTERNS.get("shill_accusations", {}).get("terms", [])
    return set(t.lower() for t in terms)

def build_dismissive_hostile_sets() -> Tuple[set, set]:
    """
    Build sets of dismissive/hostile phrases from JSON.
    Returns (hard_phrases, soft_phrases)
    - Hard: Always escalate on reply (fuck off, eat shit, etc.)
    - Soft: Only escalate when strongly directed (cope, touch grass, etc.)
    """
    dismissive = PATTERNS.get("dismissive_hostile", {})
    hard = set(p.lower() for p in dismissive.get("hard", []))
    soft = set(p.lower() for p in dismissive.get("soft", []))
    # Fallback for old format
    if not hard and not soft:
        phrases = dismissive.get("phrases", [])
        hard = set(p.lower() for p in phrases)
    return hard, soft

def build_insult_sets() -> Tuple[set, set]:
    """
    Build sets for direct insults from JSON.
    Returns (insult_words, insult_phrases)
    """
    insult_words = set()
    insult_phrases = set()
    insults_data = PATTERNS.get("insults_direct", {})
    for category, words in insults_data.items():
        if category.startswith("_"):
            continue
        if isinstance(words, list):
            for w in words:
                w_lower = w.lower()
                if ' ' in w_lower:
                    insult_phrases.add(w_lower)
                else:
                    insult_words.add(w_lower)
    return insult_words, insult_phrases

def build_benign_phrases_set() -> set:
    """Build set of benign skip phrases from JSON - PHRASES ONLY, not single words"""
    phrases = set()
    benign = PATTERNS.get("benign_skip", {})
    for category, words in benign.items():
        if category.startswith("_"):
            continue
        if isinstance(words, list):
            # Only add multi-word phrases
            for phrase in words:
                if ' ' in phrase:  # Must be a phrase, not a single word
                    phrases.add(phrase.lower())
    return phrases

def build_violence_illegal_set() -> set:
    """Build set of violence/illegal advocacy phrases from JSON"""
    phrases = PATTERNS.get("violence_illegal_advocacy", {}).get("phrases", [])
    return set(p.lower() for p in phrases)

def build_contextual_terms_sets() -> Tuple[set, set]:
    """
    Build sets of contextual sensitive terms from JSON.
    These are ambiguous terms that should only escalate with additional signals.
    Returns (context_words, context_phrases)
    """
    context_words = set()
    context_phrases = set()
    contextual = PATTERNS.get("contextual_sensitive_terms", {})
    for category, words in contextual.items():
        if category.startswith("_"):
            continue
        if isinstance(words, list):
            for w in words:
                w_lower = w.lower()
                if ' ' in w_lower:
                    context_phrases.add(w_lower)
                else:
                    context_words.add(w_lower)
    return context_words, context_phrases

# Build sets at module level
SLUR_WORDS, SLUR_PHRASES = build_slur_sets()
SELF_HARM_PHRASES = build_self_harm_set()
THREAT_PHRASES = build_threat_set()
SEXUAL_VIOLENCE_PHRASES = build_sexual_violence_set()
BRIGADING_PHRASES = build_brigading_set()
SHILL_PHRASES = build_shill_set()
DISMISSIVE_HARD_PHRASES, DISMISSIVE_SOFT_PHRASES = build_dismissive_hostile_sets()
INSULT_WORDS, INSULT_PHRASES = build_insult_sets()
VIOLENCE_ILLEGAL_PHRASES = build_violence_illegal_set()
CONTEXTUAL_WORDS, CONTEXTUAL_PHRASES = build_contextual_terms_sets()
BENIGN_PHRASES_SET = build_benign_phrases_set()

# Note: Pattern counts are logged when SmartPreFilter initializes (after logging is configured)

# ============================================
# 3. MUST-ESCALATE REGEX PATTERNS
# ============================================

def build_must_escalate_regex() -> List[re.Pattern]:
    """Build compiled regex patterns from JSON"""
    patterns = PATTERNS.get("regex_patterns", {}).get("must_escalate", [])
    compiled = []
    for p in patterns:
        try:
            compiled.append(re.compile(p, re.IGNORECASE))
        except re.error as e:
            logging.warning(f"Invalid regex pattern '{p}': {e}")
    return compiled

MUST_ESCALATE_RE = build_must_escalate_regex()

# Benign phrase regex for exact matching short exclamations
BENIGN_PHRASES_RE = [
    re.compile(r'^(holy\s+)?(shit|fuck|crap|hell|cow)!*$', re.IGNORECASE),
    re.compile(r'^what\s+the\s+(fuck|hell|heck)\??!*$', re.IGNORECASE),
    re.compile(r'^(oh\s+)?(my\s+)?(god|gosh|lord)!*$', re.IGNORECASE),
    re.compile(r'^(damn|dang|darn)!*$', re.IGNORECASE),
    re.compile(r'^no\s+(fucking|freaking)?\s*way!*$', re.IGNORECASE),
    re.compile(r'^(wow|whoa|woah)!*$', re.IGNORECASE),
    re.compile(r'^(omg|wtf|lol|lmao)!*$', re.IGNORECASE),
]


# ============================================
# 4. DIRECTEDNESS CHECK
# ============================================

def is_strongly_directed(text: str) -> bool:
    """
    Check if comment is STRONGLY directed at another user.
    Use this for threshold lowering and shill accusation logic.
    
    Strong signals: explicit user reference, "you/your", "OP", "mods",
    collective addresses like "y'all", "you guys", "everyone here", "this sub"
    """
    text_lower = text.lower()
    
    # Explicit user mention
    if re.search(r'\bu/\w+', text_lower):
        return True
    # Direct address (you/your/you're/ur)
    if re.search(r'\b(you|your|you\'re|youre|ur)\b', text_lower):
        return True
    # OP reference
    if re.search(r'\bop\b', text_lower):
        return True
    # Mod reference (often targeted)
    if re.search(r'\bmods?\b', text_lower):
        return True
    # Y'all / yall
    if re.search(r'\by\'?all\b', text_lower):
        return True
    # Collective: "you all", "you guys", "you people"
    if re.search(r'\byou (all|guys|people)\b', text_lower):
        return True
    # "all of you"
    if re.search(r'\ball of you\b', text_lower):
        return True
    # "everyone here"
    if re.search(r'\beveryone here\b', text_lower):
        return True
    # "this sub" / "this subreddit" (attacking the community)
    if re.search(r'\bthis (sub|subreddit)\b', text_lower):
        return True
    
    return False

def is_weakly_directed(text: str) -> bool:
    """
    Check for weak directedness signals.
    "this guy", "this dude", etc. - often refers to public figures, not users.
    """
    text_lower = text.lower()
    if re.search(r'\b(this\s+)?(guy|dude|person)\b', text_lower):
        return True
    return False

# For backwards compatibility, keep is_directed_at_person as alias for strong
def is_directed_at_person(text: str) -> bool:
    """Alias for is_strongly_directed"""
    return is_strongly_directed(text)

def get_non_quoted_text(text: str) -> str:
    """
    Extract the non-quoted portion of a comment.
    Reddit quotes start with '>' at the beginning of a line.
    Returns the text without quoted lines.
    """
    lines = text.split('\n')
    non_quoted = []
    for line in lines:
        stripped = line.strip()
        # Skip lines that start with > (quotes)
        if not stripped.startswith('>'):
            non_quoted.append(line)
    return '\n'.join(non_quoted).strip()

def is_primarily_quote(text: str) -> bool:
    """
    Check if a comment is primarily quoting someone else.
    Returns True if more than 50% of the content is quoted.
    """
    lines = text.split('\n')
    quoted_chars = 0
    total_chars = 0
    
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        total_chars += len(stripped)
        if stripped.startswith('>'):
            quoted_chars += len(stripped)
    
    if total_chars == 0:
        return False
    
    return (quoted_chars / total_chars) > 0.5


# ============================================
# 5. PHRASE MATCHING HELPERS
# ============================================

def contains_slur(text: str) -> bool:
    """
    Check if text contains any slur words OR slur phrases.
    Handles both single-word slurs (token match) and multi-word slurs (substring match).
    """
    normalized = normalize_text(text)
    
    # Check single-word slurs via tokenization
    words = set(re.findall(r'\b\w+\b', normalized))
    if words & SLUR_WORDS:
        return True
    
    # Check multi-word slur phrases with word boundaries
    for phrase in SLUR_PHRASES:
        pattern = r'\b' + re.escape(phrase) + r'\b'
        if re.search(pattern, normalized):
            return True
    
    return False

def contains_self_harm(text: str) -> bool:
    """Check if text contains self-harm encouragement"""
    normalized = normalize_text(text)
    squashed = squash_text(text)
    
    # Check squashed for spaced evasions like "k y s" or "k.y" but NOT words that 
    # happen to contain these letters (e.g., "sticky slots" -> "stickys" contains "kys")
    # Only match if the squashed pattern appears near word boundaries in original
    
    # For "kys" - only match if original has k, y, s separated by non-letters
    # e.g., "k y s", "k.y.s", "k-y-s" but not "stickys"
    kys_pattern = r'\bk[\s\.\-\_\*]*y[\s\.\-\_\*]*s\b'
    if re.search(kys_pattern, normalized, re.IGNORECASE):
        return True
    
    # For "kill yourself" with spaces/punctuation
    if 'killyourself' in squashed:
        # Verify it's actually spaced out, not part of another word
        kill_yourself_pattern = r'\bkill[\s\.\-\_\*]*your[\s\.\-\_\*]*self\b'
        if re.search(kill_yourself_pattern, normalized, re.IGNORECASE):
            return True
    
    # "go die" with spaces  
    if 'godie' in squashed:
        go_die_pattern = r'\bgo[\s\.\-\_\*]*die\b'
        if re.search(go_die_pattern, normalized, re.IGNORECASE):
            return True
            
    # "drink bleach" with spaces
    if 'drinkbleach' in squashed:
        drink_bleach_pattern = r'\bdrink[\s\.\-\_\*]*bleach\b'
        if re.search(drink_bleach_pattern, normalized, re.IGNORECASE):
            return True
    
    # Check phrases with word boundaries to avoid false matches
    # e.g., "end it" should not match "recommend it"
    for phrase in SELF_HARM_PHRASES:
        # Use word boundaries for matching
        pattern = r'\b' + re.escape(phrase) + r'\b'
        if re.search(pattern, normalized):
            return True
    
    return False

def contains_threat(text: str) -> bool:
    """Check if text contains threats"""
    normalized = normalize_text(text)
    for phrase in THREAT_PHRASES:
        # Use word boundaries to avoid false matches
        pattern = r'\b' + re.escape(phrase) + r'\b'
        if re.search(pattern, normalized):
            return True
    return False

def contains_sexual_violence(text: str) -> bool:
    """Check if text contains sexual violence threats"""
    normalized = normalize_text(text)
    for phrase in SEXUAL_VIOLENCE_PHRASES:
        # Use word boundaries to avoid false matches
        pattern = r'\b' + re.escape(phrase) + r'\b'
        if re.search(pattern, normalized):
            return True
    return False

def contains_brigading(text: str) -> bool:
    """Check if text contains brigading/harassment calls"""
    normalized = normalize_text(text)
    for phrase in BRIGADING_PHRASES:
        pattern = r'\b' + re.escape(phrase) + r'\b'
        if re.search(pattern, normalized):
            return True
    return False

def contains_shill_accusation(text: str) -> bool:
    """Check if text contains shill/bot accusations"""
    normalized = normalize_text(text)
    for phrase in SHILL_PHRASES:
        pattern = r'\b' + re.escape(phrase) + r'\b'
        if re.search(pattern, normalized):
            return True
    return False

def contains_dismissive_hostile(text: str) -> Tuple[bool, str]:
    """
    Check if text contains dismissive/hostile phrases.
    Returns (matched, type) where type is 'hard', 'soft', or '' if no match.
    - Hard: Always escalate on reply (fuck off, stfu, etc.)
    - Soft: Only escalate when strongly directed (cope, touch grass, etc.)
    """
    normalized = normalize_text(text)
    
    # Check hard phrases first
    for phrase in DISMISSIVE_HARD_PHRASES:
        pattern = r'\b' + re.escape(phrase) + r'\b'
        if re.search(pattern, normalized):
            return True, "hard"
    
    # Check soft phrases
    for phrase in DISMISSIVE_SOFT_PHRASES:
        pattern = r'\b' + re.escape(phrase) + r'\b'
        if re.search(pattern, normalized):
            return True, "soft"
    
    return False, ""

def contains_violence_illegal(text: str) -> bool:
    """Check if text contains violence/illegal advocacy phrases"""
    normalized = normalize_text(text)
    
    for phrase in VIOLENCE_ILLEGAL_PHRASES:
        # Use word boundaries for all phrases to avoid false matches
        pattern = r'\b' + re.escape(phrase) + r'\b'
        if re.search(pattern, normalized):
            return True
    return False

def contains_direct_insult(text: str) -> bool:
    """
    Check if text contains direct insults (words or phrases).
    Note: This should be combined with directedness check.
    """
    normalized = normalize_text(text)
    
    # Check single-word insults
    words = set(re.findall(r'\b\w+\b', normalized))
    if words & INSULT_WORDS:
        return True
    
    # Check insult phrases with word boundaries
    for phrase in INSULT_PHRASES:
        pattern = r'\b' + re.escape(phrase) + r'\b'
        if re.search(pattern, normalized):
            return True
    
    return False

def contains_contextual_term(text: str) -> bool:
    """
    Check if text contains contextual sensitive terms (words or phrases).
    These are ambiguous terms that need additional signals to escalate.
    """
    normalized = normalize_text(text)
    
    # Check single-word contextual terms
    words = set(re.findall(r'\b\w+\b', normalized))
    if words & CONTEXTUAL_WORDS:
        return True
    
    # Check multi-word contextual phrases with word boundaries
    for phrase in CONTEXTUAL_PHRASES:
        pattern = r'\b' + re.escape(phrase) + r'\b'
        if re.search(pattern, normalized):
            return True
    
    return False

def is_benign_exclamation(text: str) -> bool:
    """
    Check if comment is just a benign exclamation.
    Only matches PHRASES (not single words) and only when NOT strongly directed at someone.
    """
    # If strongly directed at someone, don't skip
    if is_strongly_directed(text):
        return False
    
    # Strip and lowercase, then remove trailing punctuation for matching
    text_stripped = text.strip().lower()
    text_clean = text_stripped.rstrip('!?.,;:')
    
    # Check regex patterns for exact matches (short exclamations)
    for pattern in BENIGN_PHRASES_RE:
        if pattern.match(text_stripped):
            return True
    
    # Check if the entire comment (minus punctuation) is a benign phrase from our list
    for phrase in BENIGN_PHRASES_SET:
        if text_clean == phrase:
            return True
    
    return False


# ============================================
# 6. SMART PRE-FILTER CLASS
# ============================================

class PreFilterResult:
    """Result of pre-filtering"""
    MUST_ESCALATE = "MUST_ESCALATE"
    SEND_TO_LLM = "SEND_TO_LLM"
    SKIP = "SKIP"


class SmartPreFilter:
    """
    Multi-layered pre-filter that:
    1. Always escalates high-priority patterns (threats, slurs, accusations)
    2. Skips obviously benign enthusiasm
    3. Uses Detoxify with smart label-specific thresholds
    4. Considers directedness for borderline cases
    """
    
    def __init__(self, config: Config):
        self.config = config
        self.model = None
        self.available = False
        
        try:
            from detoxify import Detoxify
            logging.info(f"Loading Detoxify model '{config.detoxify_model}'...")
            self.model = Detoxify(config.detoxify_model)
            self.available = True
            logging.info(f"Detoxify model loaded successfully")
        except ImportError:
            logging.warning("Detoxify not installed. Using pattern matching only.")
        except Exception as e:
            logging.warning(f"Failed to load Detoxify: {e}. Using pattern matching only.")
        
        # Log pattern counts
        logging.info(f"SmartPreFilter patterns: {len(SLUR_WORDS)} slur words, {len(SLUR_PHRASES)} slur phrases, "
                     f"{len(CONTEXTUAL_WORDS)} contextual words, {len(CONTEXTUAL_PHRASES)} contextual phrases, "
                     f"{len(SELF_HARM_PHRASES)} self-harm, {len(THREAT_PHRASES)} threats, "
                     f"{len(INSULT_WORDS)} insult words, {len(INSULT_PHRASES)} insult phrases, "
                     f"{len(MUST_ESCALATE_RE)} regex patterns")
        
        # Log thresholds
        logging.info(f"Thresholds: threat={config.threshold_threat}, severe_toxicity={config.threshold_severe_toxicity}, "
                     f"identity_attack={config.threshold_identity_attack}, "
                     f"insult={config.threshold_insult_directed}/{config.threshold_insult_not_directed} (dir/not), "
                     f"toxicity={config.threshold_toxicity_directed}/{config.threshold_toxicity_not_directed} (dir/not), "
                     f"obscene={config.threshold_obscene}, borderline={config.threshold_borderline}")
        
        # Stats
        self.total = 0
        self.must_escalate = 0
        self.detoxify_triggered = 0
        self.benign_skipped = 0
        self.pattern_skipped = 0
    
    def should_analyze(self, text: str, is_top_level: bool = False) -> Tuple[bool, float, Dict[str, float]]:
        """
        Determine if comment should be sent to LLM.
        
        Args:
            text: The comment text to analyze
            is_top_level: Whether this is a top-level comment (not a reply)
        
        Returns:
            (should_send_to_llm, max_score, all_scores)
        """
        self.total += 1
        text_preview = text[:80].replace('\n', ' ')
        
        # -----------------------------------------
        # Layer 1: Must-escalate patterns
        # -----------------------------------------
        
        # Check regex patterns
        for pattern in MUST_ESCALATE_RE:
            if pattern.search(text):
                self.must_escalate += 1
                logging.info(f"PREFILTER | MUST_ESCALATE (regex) | '{text_preview}...'")
                return True, 1.0, {"pattern_match": 1.0}
        
        # Check slurs (now handles both words and phrases)
        if contains_slur(text):
            self.must_escalate += 1
            logging.info(f"PREFILTER | MUST_ESCALATE (slur) | '{text_preview}...'")
            return True, 1.0, {"slur": 1.0}
        
        # Check self-harm
        if contains_self_harm(text):
            self.must_escalate += 1
            logging.info(f"PREFILTER | MUST_ESCALATE (self-harm) | '{text_preview}...'")
            return True, 1.0, {"self_harm": 1.0}
        
        # Check threats
        if contains_threat(text):
            self.must_escalate += 1
            logging.info(f"PREFILTER | MUST_ESCALATE (threat) | '{text_preview}...'")
            return True, 1.0, {"threat": 1.0}
        
        # Check sexual violence
        if contains_sexual_violence(text):
            self.must_escalate += 1
            logging.info(f"PREFILTER | MUST_ESCALATE (sexual violence) | '{text_preview}...'")
            return True, 1.0, {"sexual_violence": 1.0}
        
        # Check brigading/harassment calls
        if contains_brigading(text):
            self.must_escalate += 1
            logging.info(f"PREFILTER | MUST_ESCALATE (brigading) | '{text_preview}...'")
            return True, 1.0, {"brigading": 1.0}
        
        # Check violence/illegal advocacy (e.g., "shoot it down", "shine a laser")
        if contains_violence_illegal(text):
            self.must_escalate += 1
            logging.info(f"PREFILTER | MUST_ESCALATE (violence/illegal) | '{text_preview}...'")
            return True, 1.0, {"violence_illegal": 1.0}
        
        # Check shill accusations (only if STRONGLY directed at someone)
        if is_strongly_directed(text) and contains_shill_accusation(text):
            self.must_escalate += 1
            logging.info(f"PREFILTER | MUST_ESCALATE (shill accusation) | '{text_preview}...'")
            return True, 1.0, {"shill_accusation": 1.0}
        
        # Check dismissive/hostile - now split into hard and soft
        # Hard phrases (fuck off, eat shit) escalate on any reply
        # Soft phrases (cope, touch grass) only escalate when strongly directed
        has_dismissive, dismissive_type = contains_dismissive_hostile(text)
        if has_dismissive:
            if dismissive_type == "hard":
                # Hard phrases: escalate if directed OR if it's a reply
                if is_strongly_directed(text) or not is_top_level:
                    self.must_escalate += 1
                    context = "directed" if is_strongly_directed(text) else "reply"
                    logging.info(f"PREFILTER | MUST_ESCALATE (dismissive_hard + {context}) | '{text_preview}...'")
                    return True, 1.0, {"dismissive_hostile": 1.0}
            else:
                # Soft phrases: only escalate if strongly directed (not just reply)
                if is_strongly_directed(text):
                    self.must_escalate += 1
                    logging.info(f"PREFILTER | MUST_ESCALATE (dismissive_soft + directed) | '{text_preview}...'")
                    return True, 1.0, {"dismissive_hostile": 1.0}
        
        # Check direct insults + strongly directed (or reply context)
        if contains_direct_insult(text):
            if is_strongly_directed(text) or not is_top_level:
                self.must_escalate += 1
                context = "directed" if is_strongly_directed(text) else "reply"
                logging.info(f"PREFILTER | MUST_ESCALATE (insult + {context}) | '{text_preview}...'")
                return True, 1.0, {"direct_insult": 1.0}
        
        # -----------------------------------------
        # Layer 2: Benign phrase skip
        # -----------------------------------------
        
        if is_benign_exclamation(text):
            self.benign_skipped += 1
            logging.info(f"PREFILTER | SKIP (benign phrase, not directed) | '{text_preview}...'")
            return False, 0.0, {"benign": 1.0}
        
        # -----------------------------------------
        # Layer 3: Detoxify with smart thresholds
        # -----------------------------------------
        
        if not self.available:
            # No Detoxify - check contextual terms with directedness as fallback
            if contains_contextual_term(text) and is_strongly_directed(text):
                self.must_escalate += 1
                logging.info(f"PREFILTER | SEND (contextual + directed, no detoxify) | '{text_preview}...'")
                return True, 0.7, {"contextual_directed": 1.0}
            self.detoxify_triggered += 1
            logging.info(f"PREFILTER | SEND (no detoxify) | '{text_preview}...'")
            return True, 0.5, {}
        
        try:
            results = self.model.predict(text)
            scores = {k: float(v) for k, v in results.items()}
            
            # Use STRONG directedness for threshold lowering
            # Weak directedness ("this guy") in top-level comments is often about public figures
            is_directed = is_strongly_directed(text)
            
            # For top-level comments, weak directedness doesn't count
            # For replies, weak directedness might matter more
            if not is_directed and not is_top_level and is_weakly_directed(text):
                # In a reply, "this guy" is more likely to mean the person being replied to
                is_directed = True
            
            # Check contextual terms - escalate if directed OR identity_attack is elevated
            has_contextual = contains_contextual_term(text)
            identity_attack_score = scores.get('identity_attack', 0)
            
            if has_contextual and (is_directed or identity_attack_score > 0.25):
                self.must_escalate += 1
                reason = "directed" if is_directed else f"identity_attack={identity_attack_score:.2f}"
                logging.info(f"PREFILTER | SEND (contextual term + {reason}) | '{text_preview}...'")
                return True, max(0.7, identity_attack_score), scores
            
            # Thresholds per label from config (lower = more sensitive)
            thresholds = {
                'threat': self.config.threshold_threat,
                'severe_toxicity': self.config.threshold_severe_toxicity,
                'identity_attack': self.config.threshold_identity_attack,
                'insult': self.config.threshold_insult_directed if is_directed else self.config.threshold_insult_not_directed,
                'toxicity': self.config.threshold_toxicity_directed if is_directed else self.config.threshold_toxicity_not_directed,
                'obscene': self.config.threshold_obscene,
            }
            
            triggered_labels = []
            for label, score in scores.items():
                threshold = thresholds.get(label, 0.7)
                if score >= threshold:
                    triggered_labels.append(f"{label}={score:.2f}")
            
            if triggered_labels:
                self.detoxify_triggered += 1
                max_score = max(scores.values())
                directed_str = "directed" if is_directed else "not directed"
                top_level_str = "top-level" if is_top_level else "reply"
                logging.info(f"PREFILTER | SEND (detoxify: {', '.join(triggered_labels)}) [{directed_str}, {top_level_str}] | '{text_preview}...'")
                return True, max_score, scores
            
            self.pattern_skipped += 1
            max_score = max(scores.values())
            top_label = max(scores, key=scores.get)
            logging.info(f"PREFILTER | SKIP (detoxify below threshold, top: {top_label}={max_score:.2f}) | '{text_preview}...'")
            return False, max_score, scores
            
        except Exception as e:
            logging.warning(f"Detoxify scoring failed: {e}. Sending to LLM.")
            self.detoxify_triggered += 1
            return True, 0.5, {}
    
    def get_stats(self) -> str:
        if self.total == 0:
            return "No comments processed yet"
        
        sent = self.must_escalate + self.detoxify_triggered
        skipped = self.benign_skipped + self.pattern_skipped
        pct_skipped = (skipped / self.total) * 100
        
        return (
            f"Total: {self.total} | "
            f"Sent to LLM: {sent} (must_escalate: {self.must_escalate}, detoxify: {self.detoxify_triggered}) | "
            f"Skipped: {skipped} ({pct_skipped:.1f}%)"
        )


# Alias for backward compatibility
DetoxifyFilter = SmartPreFilter


# -------------------------------
# LLM Analysis
# -------------------------------

@dataclass
class AnalysisResult:
    """Result from LLM comment analysis"""
    verdict: Verdict
    reason: str
    confidence: str  # "high", "medium", "low"
    raw_response: str
    detoxify_score: float = 0.0  # Pre-filter score that triggered analysis


class LLMAnalyzer:
    """Uses Groq (free tier) or x.ai Grok API for toxicity analysis with context understanding"""
    
    # x.ai model prefixes - models starting with these use x.ai API
    XAI_MODEL_PREFIXES = ("grok-", "grok/")
    
    def __init__(self, groq_api_key: str, model: str, guidelines: str, 
                 fallback_chain: List[str] = None, daily_limit: int = 240,
                 requests_per_minute: int = 2, xai_api_key: str = ""):
        # Groq client (always available)
        self.groq_client = Groq(api_key=groq_api_key)
        
        # x.ai client (optional, for Grok models)
        self.xai_client = None
        if xai_api_key:
            self.xai_client = OpenAI(api_key=xai_api_key, base_url="https://api.x.ai/v1")
            logging.info("x.ai Grok API configured")
        
        # Generate a fixed conversation ID for x.ai cache persistence
        # This increases likelihood of cache hits across requests
        self.xai_conv_id = str(uuid.uuid4())
        logging.debug(f"x.ai conversation ID for caching: {self.xai_conv_id}")
        
        self.primary_model = model
        self.fallback_chain = fallback_chain or []
        self.daily_limit = daily_limit
        self.guidelines = guidelines
        self.requests_per_minute = requests_per_minute
        
        # Track daily usage
        self.daily_calls = 0
        self.last_reset_date = time.strftime("%Y-%m-%d")
        
        # Rate limiting - track last request time
        self.last_request_time = 0
        self.min_request_interval = 60.0 / requests_per_minute  # seconds between requests
        
        # Model cooldowns - track when each model can be used again
        # Key: model name, Value: timestamp when cooldown expires
        self.model_cooldowns: Dict[str, float] = {}
        
        # Total stats
        self.api_calls = 0
    
    def _is_xai_model(self, model: str) -> bool:
        """Check if a model should use x.ai API"""
        return model.lower().startswith(self.XAI_MODEL_PREFIXES)
    
    def _get_client_for_model(self, model: str):
        """Get the appropriate client for a model"""
        if self._is_xai_model(model):
            if not self.xai_client:
                raise ValueError(f"Model {model} requires XAI_API_KEY to be set")
            return self.xai_client
        return self.groq_client
    
    def _wait_for_rate_limit(self) -> None:
        """Wait if needed to respect rate limit"""
        now = time.time()
        time_since_last = now - self.last_request_time
        
        if time_since_last < self.min_request_interval:
            wait_time = self.min_request_interval - time_since_last
            logging.debug(f"Rate limiting: waiting {wait_time:.1f}s before next Groq request")
            time.sleep(wait_time)
        
        self.last_request_time = time.time()
    
    def _get_current_model(self) -> str:
        """Get the model to use - always returns primary, fallback handled in analyze()"""
        today = time.strftime("%Y-%m-%d")
        
        # Reset counter if it's a new day
        if today != self.last_reset_date:
            logging.info(f"New day detected - resetting daily counter (was {self.daily_calls})")
            self.daily_calls = 0
            self.last_reset_date = today
            # Clear all cooldowns on new day
            self.model_cooldowns.clear()
        
        return self.primary_model
    
    def _parse_retry_time(self, time_str: str) -> float:
        """
        Parse retry wait time from Groq error messages or headers.
        Examples: "24h0m0s", "5m20.3712s", "try again in 30s", "2m5s", "220ms"
        Returns seconds as float, or None if not parseable.
        """
        if not time_str:
            return None
            
        import re
        
        time_str_lower = time_str.lower()
        
        # Try to find time pattern anywhere in string (handles "try again in X" format)
        # Pattern handles: 24h0m0s, 5m20s, 30s, 220ms
        match = re.search(r'(\d+h)?(\d+m(?!s))?(\d+(?:\.\d+)?s)?(\d+ms)?', time_str_lower)
        if not match:
            return None
        
        total_seconds = 0.0
        
        hours_part = match.group(1)
        minutes_part = match.group(2)
        seconds_part = match.group(3)
        ms_part = match.group(4)
        
        if hours_part:
            hours = float(hours_part.rstrip('h'))
            total_seconds += hours * 3600
        
        if minutes_part:
            minutes = float(minutes_part.rstrip('m'))
            total_seconds += minutes * 60
        
        if seconds_part:
            seconds = float(seconds_part.rstrip('s'))
            total_seconds += seconds
            
        if ms_part:
            ms = float(ms_part.rstrip('ms'))
            total_seconds += ms / 1000
        
        return total_seconds if total_seconds > 0 else None
    
    def _check_rate_limit_headers(self, model: str, headers) -> None:
        """
        Check rate limit headers from Groq response and preemptively set cooldowns.
        
        Headers available:
        - x-ratelimit-remaining-requests: RPD remaining
        - x-ratelimit-remaining-tokens: TPM remaining  
        - x-ratelimit-reset-requests: When RPD resets (could be rolling window)
        - x-ratelimit-reset-tokens: When TPM resets
        """
        try:
            if not headers:
                return
            
            remaining_requests = headers.get('x-ratelimit-remaining-requests')
            remaining_tokens = headers.get('x-ratelimit-remaining-tokens')
            reset_requests = headers.get('x-ratelimit-reset-requests')
            
            # Log remaining quota at debug level
            if remaining_requests:
                logging.debug(f"{model} - Remaining requests (RPD): {remaining_requests}, resets in: {reset_requests}")
            if remaining_tokens:
                logging.debug(f"{model} - Remaining tokens (TPM): {remaining_tokens}")
            
            # Preemptively set cooldown if running low on requests
            if remaining_requests:
                try:
                    remaining = int(remaining_requests)
                    if remaining <= 5:
                        # Running very low - set moderate cooldown (10 min)
                        # Don't use full reset time since limits may be rolling
                        cooldown_time = 600  # 10 minutes
                        self.model_cooldowns[model] = time.time() + cooldown_time
                        logging.warning(f"⚠️ {model} nearly exhausted ({remaining} requests left) - 10m cooldown, will retry")
                    elif remaining <= 20:
                        logging.info(f"📊 {model} has {remaining} requests remaining")
                except (ValueError, TypeError):
                    pass
                    
        except Exception as e:
            # Don't fail on header parsing errors
            logging.debug(f"Could not parse rate limit headers: {e}")
    
    def analyze(self, text: str, subreddit: str, parent_context: str = "", 
                detoxify_score: float = 0.0, is_top_level: bool = False, 
                post_title: str = "") -> AnalysisResult:
        """Send to Groq for nuanced analysis"""
        
        current_model = self._get_current_model()
        
        system_prompt = f"""{self.guidelines}"""

        # Add context about comment type for accurate reasoning
        if is_top_level:
            context_note = "[TOP-LEVEL COMMENT on a post - not replying to another user]"
        else:
            context_note = "[REPLY to another user's comment]"
        
        # Check if comment contains Reddit-style quotes
        has_quotes = '\n>' in text or text.startswith('>')
        if has_quotes:
            context_note += "\n[CONTAINS QUOTED TEXT - lines starting with '>' are quoting another user, not the commenter's own words]"
        
        # Build user prompt with post title and context
        user_prompt = f"{context_note}\n"
        
        if post_title:
            user_prompt += f"Post title: \"{post_title}\"\n"
        
        if parent_context:
            user_prompt += f"Parent context: {parent_context[:300]}\n"
        
        user_prompt += f"\nAnalyze this comment:\n\n{text}"

        # Debug: log what we're sending
        logging.debug(f"GROQ SYSTEM PROMPT LENGTH: {len(system_prompt)} chars")
        logging.debug(f"GROQ USER PROMPT: {user_prompt[:500]}")
        logging.debug(f"GROQ MODEL: {current_model} (daily calls: {self.daily_calls}/{self.daily_limit})")

        try:
            # Wait if needed to respect our own rate limit
            self._wait_for_rate_limit()
            
            self.api_calls += 1
            self.daily_calls += 1
            
            # Start with configured model first, then fall through to fallback chain
            models_to_try = [current_model]
            for m in self.fallback_chain:
                if m not in models_to_try:
                    models_to_try.append(m)
            
            last_error = None
            response = None
            success = False
            fallback_delay = 30  # seconds to wait between fallback models
            
            for model_idx, model_to_use in enumerate(models_to_try):
                if success:
                    break
                
                # Check if model is on cooldown
                cooldown_until = self.model_cooldowns.get(model_to_use, 0)
                if time.time() < cooldown_until:
                    remaining = int(cooldown_until - time.time())
                    logging.info(f"Skipping {model_to_use} - on cooldown for {remaining}s more")
                    continue
                
                # Wait before trying fallback models (not for the first model)
                if model_idx > 0:
                    logging.info(f"Waiting {fallback_delay}s before trying fallback model...")
                    time.sleep(fallback_delay)
                    
                # Retry logic for each model
                max_retries = 2 if model_idx > 0 else 3  # Fewer retries for fallbacks
                retry_delay = 3  # seconds
                
                logging.info(f"Trying model {model_idx + 1}/{len(models_to_try)}: {model_to_use}")
                
                # Check if this is an x.ai model and we have the client
                is_xai = self._is_xai_model(model_to_use)
                if is_xai and not self.xai_client:
                    logging.warning(f"Skipping {model_to_use} - XAI_API_KEY not configured")
                    continue
                
                for attempt in range(max_retries):
                    try:
                        if is_xai:
                            # x.ai API (OpenAI-compatible)
                            # Use conv_id header to improve prompt caching across requests
                            response = self.xai_client.chat.completions.create(
                                model=model_to_use,
                                messages=[
                                    {"role": "system", "content": system_prompt},
                                    {"role": "user", "content": user_prompt}
                                ],
                                max_tokens=100,
                                temperature=0.1,
                                extra_headers={"x-grok-conv-id": self.xai_conv_id}
                            )
                            raw_response = None  # No rate limit headers for x.ai
                        else:
                            # Groq API - use with_raw_response to get rate limit headers
                            raw_response = self.groq_client.chat.completions.with_raw_response.create(
                                model=model_to_use,
                                messages=[
                                    {"role": "system", "content": system_prompt},
                                    {"role": "user", "content": user_prompt}
                                ],
                                max_tokens=100,
                                temperature=0.1,  # Low temp for consistent classification
                            )
                            response = raw_response.parse()
                        
                        if model_to_use != models_to_try[0]:
                            logging.info(f"Successfully used fallback model: {model_to_use}")
                        # Clear any cooldown on success
                        if model_to_use in self.model_cooldowns:
                            del self.model_cooldowns[model_to_use]
                        
                        # Check rate limit headers from response (Groq only)
                        if raw_response and hasattr(raw_response, 'headers'):
                            self._check_rate_limit_headers(model_to_use, raw_response.headers)
                        
                        success = True
                        break  # Success - exit retry loop
                        
                    except Exception as e:
                        error_str = str(e)
                        last_error = e
                        if "429" in error_str or "rate_limit" in error_str.lower():
                            # Log full error for debugging
                            logging.debug(f"Full rate limit error: {error_str}")
                            
                            # Check if daily limit is fully exhausted (Used == Limit)
                            daily_exhausted = False
                            import re
                            rpd_match = re.search(r'Limit (\d+), Used (\d+)', error_str)
                            if rpd_match:
                                limit = int(rpd_match.group(1))
                                used = int(rpd_match.group(2))
                                if used >= limit:
                                    daily_exhausted = True
                                    logging.warning(f"⚠️ {model_to_use} daily limit EXHAUSTED ({used}/{limit} RPD)")
                            
                            # Try to get retry-after from exception response headers first
                            suggested_wait = None
                            if hasattr(e, 'response') and hasattr(e.response, 'headers'):
                                retry_after = e.response.headers.get('retry-after')
                                if retry_after:
                                    try:
                                        suggested_wait = float(retry_after)
                                        logging.debug(f"Got retry-after header: {suggested_wait}s")
                                    except (ValueError, TypeError):
                                        pass
                            
                            # Fall back to parsing error message
                            if not suggested_wait:
                                suggested_wait = self._parse_retry_time(error_str)
                                if suggested_wait:
                                    logging.debug(f"Parsed wait time from message: {suggested_wait:.0f}s")
                            
                            if not suggested_wait:
                                logging.debug(f"Could not parse wait time from error")
                            
                            # If daily limit exhausted, set 1 hour cooldown regardless of retry-after
                            if daily_exhausted:
                                cooldown_time = 3600  # 1 hour
                                self.model_cooldowns[model_to_use] = time.time() + cooldown_time
                                logging.warning(f"Rate limited on {model_to_use} - daily limit exhausted, 1h cooldown set, trying next model...")
                                break  # Exit retry loop, try next model
                            # If wait time is short (< 30s), wait and retry same model
                            elif suggested_wait and suggested_wait <= 30 and attempt < max_retries - 1:
                                logging.warning(f"Rate limited on {model_to_use}, waiting {suggested_wait:.0f}s (from API) before retry {attempt + 2}/{max_retries}")
                                time.sleep(suggested_wait)
                                continue
                            elif suggested_wait and suggested_wait > 30:
                                # Set cooldown - minimum 120s, plus 60s buffer on top of API time
                                # Cap at 1 hour - if longer, we'll just retry and get a fresh wait time
                                cooldown_time = min(max(suggested_wait + 60, 120), 3600)
                                self.model_cooldowns[model_to_use] = time.time() + cooldown_time
                                logging.warning(f"Rate limited on {model_to_use} for {suggested_wait:.0f}s - {cooldown_time:.0f}s cooldown set, trying next model...")
                                break  # Exit retry loop, try next model
                            elif attempt < max_retries - 1:
                                wait_time = retry_delay * (attempt + 1)
                                logging.warning(f"Rate limited on {model_to_use}, waiting {wait_time}s before retry {attempt + 2}/{max_retries}")
                                time.sleep(wait_time)
                                continue
                            else:
                                # Out of retries for this model, set longer cooldown (10 min)
                                # since we couldn't parse the wait time
                                self.model_cooldowns[model_to_use] = time.time() + 600
                                logging.warning(f"Rate limit exhausted for {model_to_use}, 10m cooldown set, trying next model...")
                                break  # Exit retry loop, try next model
                        else:
                            # Non-rate-limit error - log and try next model
                            logging.warning(f"Error on {model_to_use}: {e}, trying next model...")
                            break
            
            if not success:
                # All models exhausted
                raise last_error or Exception("All models rate limited")
            
            raw = response.choices[0].message.content.strip()
            
            # Debug: log raw response
            logging.debug(f"GROQ RAW RESPONSE: {raw}")
            
            # Parse the plain text response
            # Expected format:
            # VERDICT: REPORT | BENIGN
            # REASON: <short explanation>
            
            verdict = Verdict.BENIGN  # Default
            reason = ""
            
            # Normalize the response - handle variations in formatting
            raw_upper = raw.upper()
            
            # Look for verdict
            if 'VERDICT:' in raw_upper or 'VERDICT :' in raw_upper:
                for line in raw.split('\n'):
                    line_stripped = line.strip()
                    line_upper = line_stripped.upper()
                    if line_upper.startswith('VERDICT'):
                        # Extract the verdict value
                        parts = line_stripped.split(':', 1)
                        if len(parts) > 1:
                            verdict_str = parts[1].strip().upper()
                            if 'REPORT' in verdict_str:
                                verdict = Verdict.REPORT
                            else:
                                verdict = Verdict.BENIGN
                    elif line_upper.startswith('REASON'):
                        parts = line_stripped.split(':', 1)
                        if len(parts) > 1:
                            reason = parts[1].strip()
            else:
                # Fallback: look for REPORT or BENIGN anywhere in response
                if 'REPORT' in raw_upper and 'BENIGN' not in raw_upper:
                    verdict = Verdict.REPORT
                else:
                    verdict = Verdict.BENIGN
            
            # Safeguard: if reason is empty or invalid, use a default
            if not reason or reason.upper() in ['REPORT', 'BENIGN', 'N/A', 'NONE']:
                reason = "Flagged for moderator review" if verdict == Verdict.REPORT else "No issues detected"
            
            return AnalysisResult(
                verdict=verdict,
                reason=reason,
                confidence="high",  # Not used anymore but kept for compatibility
                raw_response=raw,
                detoxify_score=detoxify_score
            )
            
        except Exception as e:
            logging.error(f"LLM analysis failed after trying all models: {e}")
            
            # Default to BENIGN when LLM unavailable - don't auto-report without LLM verification
            # But log prominently so it can be manually reviewed
            if detoxify_score >= 0.7:
                logging.warning(f"⚠️ HIGH DETOXIFY SCORE ({detoxify_score:.2f}) but LLM unavailable - SKIPPING (not reporting)")
                logging.warning(f"⚠️ This comment may need manual review")
            
            return AnalysisResult(
                verdict=Verdict.BENIGN,
                reason=f"LLM unavailable - skipped (detox={detoxify_score:.2f})",
                confidence="low",
                raw_response="",
                detoxify_score=detoxify_score
            )
    
    def get_stats(self) -> str:
        cooldowns = [m for m, t in self.model_cooldowns.items() if time.time() < t]
        cooldown_str = f", {len(cooldowns)} models on cooldown" if cooldowns else ""
        return f"LLM API calls: {self.api_calls} (today: {self.daily_calls}, primary: {self.primary_model}{cooldown_str})"


# -------------------------------
# Discord helper (enhanced)
# -------------------------------

FALSE_POSITIVES_FILE = "false_positives.json"

def post_discord(webhook: str, content: str) -> None:
    """Post a simple text message to Discord"""
    if not webhook:
        return
    data = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        webhook,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "ToxicReportBot/2.0"
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            _ = resp.read()
    except Exception as e:
        logging.warning(f"Discord post failed: {e}")


def post_discord_embed(webhook: str, title: str, description: str, 
                       color: int = 0xFF0000, fields: List[Dict] = None,
                       url: str = None) -> bool:
    """Post a rich embed message to Discord. Returns True on success."""
    if not webhook:
        return False
    
    embed = {
        "title": title[:256],  # Discord limit
        "description": description[:4096],  # Discord limit
        "color": color,
    }
    
    if url:
        embed["url"] = url
    
    if fields:
        # Ensure fields have required structure and respect limits
        valid_fields = []
        for f in fields[:25]:  # Discord limit
            if isinstance(f, dict) and "name" in f and "value" in f:
                valid_fields.append({
                    "name": str(f["name"])[:256],  # Discord limit
                    "value": str(f["value"])[:1024],  # Discord limit
                    "inline": bool(f.get("inline", False))
                })
        if valid_fields:
            embed["fields"] = valid_fields
    
    embed["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    
    payload = {"embeds": [embed]}
    data = json.dumps(payload).encode("utf-8")
    
    req = urllib.request.Request(
        webhook,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "ToxicReportBot/2.0"
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            _ = resp.read()
        return True
    except urllib.error.HTTPError as e:
        # Read error response for debugging
        error_body = ""
        try:
            error_body = e.read().decode('utf-8')
        except:
            pass
        logging.warning(f"Discord embed post failed: {e} - {error_body}")
        return False
    except Exception as e:
        logging.warning(f"Discord embed post failed: {e}")
        return False


def notify_discord_report(webhook: str, comment_text: str, permalink: str, 
                          reason: str, detoxify_score: float) -> None:
    """Send a Discord notification when a comment is reported"""
    if not webhook:
        return
    
    # Truncate comment for Discord (keep under embed limit)
    truncated = comment_text[:1500] + "..." if len(comment_text) > 1500 else comment_text
    
    fields = [
        {"name": "Reason", "value": reason[:1024], "inline": True},
        {"name": "Detoxify Score", "value": f"{detoxify_score:.2f}", "inline": True},
    ]
    
    post_discord_embed(
        webhook=webhook,
        title="🚨 Comment Reported",
        description=f"```{truncated}```",
        color=0xFF4444,  # Red
        fields=fields,
        url=permalink
    )


def notify_discord_llm_analysis(webhook: str, comment_text: str, permalink: str,
                                 detoxify_score: float, subreddit: str) -> None:
    """Send a Discord notification when a comment is sent to LLM for analysis"""
    if not webhook:
        return
    
    # Truncate comment for Discord
    truncated = comment_text[:800] + "..." if len(comment_text) > 800 else comment_text
    
    fields = [
        {"name": "Subreddit", "value": f"r/{subreddit}", "inline": True},
        {"name": "Detoxify Score", "value": f"{detoxify_score:.2f}", "inline": True},
    ]
    
    post_discord_embed(
        webhook=webhook,
        title="🔍 Analyzing Comment",
        description=f"```{truncated}```",
        color=0x3498DB,  # Blue
        fields=fields,
        url=permalink
    )


def notify_discord_verdict(webhook: str, verdict: str, reason: str, 
                           permalink: str) -> None:
    """Send a Discord notification with the LLM verdict"""
    if not webhook:
        return
    
    if verdict == "REPORT":
        color = 0xFF4444  # Red
        emoji = "🚨"
    else:
        color = 0x44FF44  # Green
        emoji = "✅"
    
    post_discord_embed(
        webhook=webhook,
        title=f"{emoji} Verdict: {verdict}",
        description=f"**Reason:** {reason}",
        color=color,
        url=permalink
    )


def notify_discord_borderline_skip(webhook: str, comment_text: str, permalink: str,
                                    detoxify_score: float, subreddit: str) -> None:
    """Send a Discord notification for borderline skipped comments"""
    if not webhook:
        return
    
    # Truncate comment for Discord
    truncated = comment_text[:800] + "..." if len(comment_text) > 800 else comment_text
    
    fields = [
        {"name": "Subreddit", "value": f"r/{subreddit}", "inline": True},
        {"name": "Detoxify Score", "value": f"{detoxify_score:.2f}", "inline": True},
        {"name": "Status", "value": "Skipped (below threshold)", "inline": True},
    ]
    
    post_discord_embed(
        webhook=webhook,
        title="⚪ Borderline Skip",
        description=f"```{truncated}```",
        color=0x808080,  # Gray
        fields=fields,
        url=permalink
    )


def notify_discord_daily_stats(webhook: str, stats: Dict) -> None:
    """Send daily statistics to Discord"""
    if not webhook:
        return
    
    total_processed = stats.get("total_processed", 0)
    sent_to_llm = stats.get("sent_to_llm", 0)
    reported = stats.get("reported", 0)
    benign = stats.get("benign", 0)
    
    # Accuracy from outcome tracking
    accuracy_stats = stats.get("accuracy", {})
    removed = accuracy_stats.get("removed", 0)
    approved = accuracy_stats.get("approved", 0)
    pending = accuracy_stats.get("pending", 0)
    
    resolved = removed + approved
    accuracy_pct = (removed / resolved * 100) if resolved > 0 else 0
    
    description = f"""
**Processed:** {total_processed:,} comments
**Sent to LLM:** {sent_to_llm:,} ({(sent_to_llm/total_processed*100):.1f}% of total)
**Reported:** {reported:,}
**Benign:** {benign:,}
"""
    
    fields = [
        {"name": "📊 Outcome Tracking", "value": f"Removed: {removed}\nApproved: {approved}\nPending: {pending}", "inline": True},
        {"name": "🎯 Accuracy", "value": f"{accuracy_pct:.1f}%\n({removed}/{resolved} confirmed)", "inline": True},
    ]
    
    # Color based on accuracy
    if accuracy_pct >= 80:
        color = 0x44FF44  # Green
    elif accuracy_pct >= 60:
        color = 0xFFAA00  # Orange
    else:
        color = 0xFF4444  # Red
    
    post_discord_embed(
        webhook=webhook,
        title="📈 Daily Moderation Stats",
        description=description,
        color=color,
        fields=fields
    )


def load_false_positives() -> List[Dict]:
    """Load false positives from JSON file"""
    try:
        with open(FALSE_POSITIVES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        logging.warning(f"Could not parse {FALSE_POSITIVES_FILE}, starting fresh")
        return []


def save_false_positives(entries: List[Dict]) -> None:
    """Save false positives to JSON file"""
    with open(FALSE_POSITIVES_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)


def track_false_positive(comment_id: str, permalink: str, text: str,
                         groq_reason: str, detoxify_score: float,
                         reported_at: str, is_top_level: bool = False) -> None:
    """Track a false positive (reported comment that was approved)"""
    entries = load_false_positives()
    
    # Don't add duplicates
    if any(e.get("comment_id") == comment_id for e in entries):
        return
    
    entries.append({
        "comment_id": comment_id,
        "permalink": permalink,
        "text": text[:1000],
        "groq_reason": groq_reason,
        "detoxify_score": detoxify_score,
        "is_top_level": is_top_level,
        "reported_at": reported_at,
        "discovered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    
    save_false_positives(entries)
    logging.info(f"Tracked false positive: {comment_id}")


def check_and_track_false_positives(reddit: praw.Reddit, webhook: str = None) -> Dict[str, int]:
    """
    Check reported comments and track false positives.
    Returns stats and optionally notifies Discord.
    """
    comments = load_tracked_comments()
    now = time.time()
    stats = {"checked": 0, "removed": 0, "approved": 0, "still_pending": 0, "errors": 0}
    new_false_positives = []
    
    for entry in comments:
        if entry.get("outcome") != "pending":
            continue
        
        # Check if comment is old enough (24 hours)
        reported_at = entry.get("reported_at", "")
        if reported_at:
            try:
                reported_time = time.mktime(time.strptime(reported_at, "%Y-%m-%dT%H:%M:%SZ"))
                age_hours = (now - reported_time) / 3600
                if age_hours < 24:
                    stats["still_pending"] += 1
                    continue
            except ValueError:
                pass
        
        comment_id = entry.get("comment_id", "")
        if not comment_id:
            continue
            
        try:
            clean_id = comment_id.replace("t1_", "")
            comment = reddit.comment(clean_id)
            _ = comment.body  # Force fetch
            
            if comment.body == "[removed]" or getattr(comment, 'removed', False):
                entry["outcome"] = "removed"
                stats["removed"] += 1
            elif getattr(comment, 'removed_by_category', None):
                entry["outcome"] = "removed"
                stats["removed"] += 1
            else:
                # Comment still exists = approved/not actioned = false positive
                entry["outcome"] = "approved"
                stats["approved"] += 1
                
                # Track as false positive
                track_false_positive(
                    comment_id=comment_id,
                    permalink=entry.get("permalink", ""),
                    text=entry.get("text", ""),
                    groq_reason=entry.get("groq_reason", ""),
                    detoxify_score=entry.get("detoxify_score", 0),
                    reported_at=reported_at,
                    is_top_level=entry.get("is_top_level", False)
                )
                new_false_positives.append(entry)
            
            entry["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            stats["checked"] += 1
            
        except prawcore.exceptions.NotFound:
            entry["outcome"] = "removed"
            entry["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            stats["removed"] += 1
            stats["checked"] += 1
        except Exception as e:
            logging.warning(f"Error checking comment {comment_id}: {e}")
            stats["errors"] += 1
    
    save_tracked_comments(comments)
    
    # Notify Discord about new false positives
    if webhook and new_false_positives:
        for fp in new_false_positives[:5]:  # Limit to 5 notifications
            post_discord_embed(
                webhook=webhook,
                title="⚠️ False Positive Detected",
                description=f"```{fp.get('text', '')[:500]}```",
                color=0xFFAA00,  # Orange
                fields=[
                    {"name": "Reason", "value": fp.get("groq_reason", "Unknown"), "inline": True},
                    {"name": "Detoxify", "value": f"{fp.get('detoxify_score', 0):.2f}", "inline": True},
                ],
                url=fp.get("permalink", "")
            )
    
    return stats


# -------------------------------
# Helper functions
# -------------------------------

def get_parent_context(thing) -> Tuple[str, str]:
    """
    Get parent context and post title for better analysis.
    Returns (parent_context, post_title)
    """
    parent_context = ""
    post_title = ""
    
    try:
        # Get the submission (post) this comment is on
        if hasattr(thing, 'submission'):
            submission = thing.submission
            post_title = getattr(submission, 'title', '') or ''
        
        # Get immediate parent context
        if hasattr(thing, 'parent'):
            parent = thing.parent()
            if hasattr(parent, 'body'):
                # Parent is a comment
                parent_context = parent.body[:500]
            elif hasattr(parent, 'title'):
                # Parent is the submission itself
                selftext = getattr(parent, 'selftext', '') or ""
                if selftext:
                    parent_context = selftext[:500]
    except Exception:
        pass
    
    return parent_context, post_title


def get_text_from_thing(thing) -> Optional[str]:
    """Extract text content from a comment or submission"""
    # Comment
    if hasattr(thing, 'body'):
        body = thing.body
        if body and body != '[deleted]' and body != '[removed]':
            return body
    # Submission
    elif hasattr(thing, 'title'):
        title = getattr(thing, 'title', '') or ''
        selftext = getattr(thing, 'selftext', '') or ''
        text = f"{title.strip()}  {selftext.strip()}".strip()
        if text:
            return text
    return None


# -------------------------------
# Report
# -------------------------------

def file_report(thing, reason: str, cfg: Config) -> None:
    try:
        if cfg.report_as == "moderator":
            if cfg.report_rule_bucket:
                thing.mod.report(reason=reason, rule_name=cfg.report_rule_bucket)
            else:
                thing.mod.report(reason=reason)
        else:
            thing.report(reason)
    except AttributeError:
        thing.report(reason)


def build_report_reason(result: AnalysisResult) -> str:
    # Reddit report reasons have a ~100 char limit
    # Truncate cleanly without cutting mid-word
    reason = result.reason
    if len(reason) <= 100:
        return reason
    
    # Find the last space before the 97 char mark (leave room for "...")
    truncated = reason[:97]
    last_space = truncated.rfind(' ')
    if last_space > 50:  # Only use space if it's not too far back
        truncated = truncated[:last_space]
    
    return truncated + "..."


# -------------------------------
# Main loop
# -------------------------------

def process_thing(thing, detox_filter: DetoxifyFilter, analyzer: LLMAnalyzer, cfg: Config, subreddit_name: str) -> None:
    """Process a single comment or submission"""
    
    text = get_text_from_thing(thing)
    if not text:
        return
    
    thing_id = thing.fullname
    permalink = f"https://reddit.com{getattr(thing, 'permalink', '')}"
    
    # Get parent context and post title
    parent_ctx, post_title = "", ""
    if hasattr(thing, 'body'):  # It's a comment
        parent_ctx, post_title = get_parent_context(thing)
    
    # Check if this is a top-level comment (parent is the submission, not another comment)
    is_top_level = False
    if hasattr(thing, 'parent_id'):
        # parent_id starts with t3_ for submissions, t1_ for comments
        is_top_level = thing.parent_id.startswith('t3_')
    
    # Pre-filter with Detoxify
    should_analyze, detox_score, detox_scores = detox_filter.should_analyze(text, is_top_level=is_top_level)
    
    if not should_analyze:
        # Below threshold - skip LLM analysis
        # Log at INFO level if score was borderline so we can review skips
        if detox_score > cfg.threshold_borderline:
            logging.info(f"SKIP (borderline) | score={detox_score:.2f} | {permalink}")
            logging.info(f"  Text: {text[:200].replace(chr(10), ' ')}{'...' if len(text) > 200 else ''}")
            # Discord notification for borderline skips
            if cfg.discord_webhook:
                notify_discord_borderline_skip(
                    webhook=cfg.discord_webhook,
                    comment_text=text,
                    permalink=permalink,
                    detoxify_score=detox_score,
                    subreddit=subreddit_name
                )
        else:
            logging.debug(f"SKIP | detox={detox_score:.3f} | '{text[:80].replace(chr(10), ' ')}...'")
        return
    
    # Above threshold - send to Groq
    log_text_short = text[:100].replace('\n', ' ')
    logging.info(f"")
    logging.info(f"={'='*60}")
    logging.info(f"SENDING TO GROQ (detox score: {detox_score:.3f})")
    logging.info(f"Comment: \"{log_text_short}{'...' if len(text) > 100 else ''}\"")
    logging.info(f"Link: {permalink}")
    
    # Discord notification for sending to LLM
    if cfg.discord_webhook:
        notify_discord_llm_analysis(
            webhook=cfg.discord_webhook,
            comment_text=text,
            permalink=permalink,
            detoxify_score=detox_score,
            subreddit=subreddit_name
        )
    
    result = analyzer.analyze(text, subreddit_name, parent_ctx, detox_score, is_top_level, post_title)
    
    # Show Groq's verdict and reasoning
    logging.info(f"")
    logging.info(f"GROQ VERDICT: {result.verdict.value}")
    logging.info(f"GROQ REASONING: {result.reason}")
    if result.raw_response:
        logging.debug(f"GROQ RAW RESPONSE: {result.raw_response}")
    
    # Update Discord with verdict
    if cfg.discord_webhook:
        notify_discord_verdict(
            webhook=cfg.discord_webhook,
            verdict=result.verdict.value,
            reason=result.reason,
            permalink=permalink
        )
    
    should_report = result.verdict == Verdict.REPORT
    
    if should_report:
        if cfg.dry_run:
            logging.info(f"ACTION: >>> WOULD REPORT <<< (dry run enabled)")
        else:
            logging.info(f"ACTION: >>> REPORTING <<<")
    else:
        logging.info(f"ACTION: No action needed (benign)")
        # Track benign analyzed comments for prefilter optimization
        # Extract trigger reason from detox_scores dict (e.g., {"slur": 1.0} -> "slur")
        prefilter_trigger = ""
        for key in detox_scores:
            if key not in ('toxicity', 'severe_toxicity', 'obscene', 'threat', 'insult', 'identity_attack'):
                prefilter_trigger = key  # Pattern match like "slur", "direct_insult", etc.
                break
        if not prefilter_trigger and detox_scores:
            # Detoxify triggered - find highest score
            top_label = max(detox_scores, key=detox_scores.get)
            prefilter_trigger = f"detoxify:{top_label}={detox_scores[top_label]:.2f}"
        
        track_benign_analyzed(
            comment_id=thing_id,
            permalink=permalink,
            text=text,
            llm_reason=result.reason,
            detoxify_score=detox_score,
            detoxify_scores=detox_scores,
            is_top_level=is_top_level,
            prefilter_trigger=prefilter_trigger
        )
    
    logging.info(f"={'='*60}")

    # File report (only if not dry run)
    if cfg.enable_reddit_reports and should_report:
        reason = build_report_reason(result)
        if cfg.dry_run:
            # Already logged above
            pass
        else:
            try:
                file_report(thing, reason, cfg)
                # Track the reported comment for accuracy measurement
                track_reported_comment(
                    comment_id=thing_id,
                    permalink=permalink,
                    text=text,
                    groq_reason=result.reason,
                    detoxify_score=detox_score,
                    is_top_level=is_top_level
                )
                # Send Discord notification for report
                notify_discord_report(
                    webhook=cfg.discord_webhook,
                    comment_text=text,
                    permalink=permalink,
                    reason=result.reason,
                    detoxify_score=detox_score
                )
            except prawcore.exceptions.Forbidden as e:
                logging.error(f"Forbidden when reporting {thing_id}: {e}")
            except prawcore.exceptions.NotFound as e:
                logging.error(f"Item not found {thing_id}: {e}")
            except Exception as e:
                logging.error(f"Report failed for {thing_id}: {e}")


def stream_subreddit(reddit: praw.Reddit, subreddit_name: str, detox_filter: DetoxifyFilter, analyzer: LLMAnalyzer, cfg: Config) -> None:
    """Stream comments and submissions from a subreddit"""
    
    sr = reddit.subreddit(subreddit_name)
    logging.info(f"Starting stream for r/{subreddit_name}")
    
    # Stream comments (this blocks and yields comments as they arrive)
    for comment in sr.stream.comments(skip_existing=True):
        try:
            process_thing(comment, detox_filter, analyzer, cfg, subreddit_name)
        except Exception as e:
            logging.error(f"Error processing {comment.fullname}: {e}")
            continue


# -------------------------------
# Main
# -------------------------------

import threading

def accuracy_check_loop(reddit: praw.Reddit, discord_webhook: str = None, 
                        check_interval_hours: int = 12):
    """Background thread that periodically checks reported comment outcomes"""
    check_interval_sec = check_interval_hours * 3600
    
    while True:
        time.sleep(check_interval_sec)
        try:
            logging.info("Running accuracy check on reported comments...")
            
            # Check outcomes and track false positives
            stats = check_and_track_false_positives(reddit, webhook=discord_webhook)
            
            if stats["checked"] > 0:
                logging.info(
                    f"Accuracy check complete: {stats['checked']} comments checked - "
                    f"{stats['removed']} removed, {stats['approved']} approved (false positives)"
                )
            
            # Get overall stats
            overall = get_accuracy_stats()
            if overall["resolved"] > 0:
                logging.info(
                    f"Overall accuracy: {overall['accuracy_pct']:.1f}% "
                    f"({overall['removed']}/{overall['resolved']} removed) | "
                    f"{overall['pending']} still pending"
                )
            
            # Cleanup old entries
            cleanup_old_tracked(max_age_days=7)
            
        except Exception as e:
            logging.error(f"Accuracy check failed: {e}")


def main() -> None:
    cfg = load_config()
    setup_logging(cfg.log_level)
    logging.info("Starting ToxicReportBot v2 (Detoxify + Groq LLM)")

    # Initialize smart pre-filter
    detox_filter = SmartPreFilter(config=cfg)

    # Reddit
    reddit = praw_client(cfg)

    # LLM Analyzer
    analyzer = LLMAnalyzer(
        groq_api_key=cfg.groq_api_key,
        xai_api_key=cfg.xai_api_key,
        model=cfg.llm_model,
        guidelines=cfg.moderation_guidelines,
        fallback_chain=cfg.llm_fallback_chain,
        daily_limit=cfg.llm_daily_limit,
        requests_per_minute=cfg.llm_requests_per_minute
    )
    logging.info(f"Using LLM model: {cfg.llm_model} (max {cfg.llm_requests_per_minute} requests/min)")
    if cfg.xai_api_key:
        logging.info(f"x.ai Grok API available for grok-* models")
    logging.info(f"Fallback chain: {' -> '.join(cfg.llm_fallback_chain)}")
    
    # Discord setup
    if cfg.discord_webhook:
        logging.info(f"Discord notifications enabled (webhook configured)")
        # Send startup notification
        try:
            success = post_discord_embed(
                webhook=cfg.discord_webhook,
                title="🤖 ToxicReportBot Started",
                description=f"Monitoring r/{cfg.subreddits[0]}\nDry run: {cfg.dry_run}",
                color=0x00FF00,  # Green
                fields=[
                    {"name": "LLM Model", "value": cfg.llm_model, "inline": True},
                    {"name": "Detoxify Model", "value": cfg.detoxify_model, "inline": True},
                ]
            )
            if success:
                logging.info("Discord startup notification sent successfully")
            else:
                logging.warning("Discord startup notification failed - check webhook URL")
        except Exception as e:
            logging.error(f"Discord startup notification failed: {e}")
    else:
        logging.info("Discord notifications disabled (no webhook configured)")
    
    # Start accuracy check background thread
    accuracy_thread = threading.Thread(
        target=accuracy_check_loop, 
        args=(reddit, cfg.discord_webhook, 12),  # Check every 12 hours
        daemon=True
    )
    accuracy_thread.start()
    logging.info("Started accuracy tracking (checks every 12 hours)")
    
    # Start daily stats thread
    def daily_stats_loop(discord_webhook: str, detox_filter: DetoxifyFilter, 
                         analyzer: LLMAnalyzer):
        """Post daily stats to Discord"""
        if not discord_webhook:
            return
        
        # Wait until next day at 00:00 UTC, then post daily
        while True:
            # Calculate seconds until midnight UTC
            now = time.gmtime()
            seconds_until_midnight = (24 - now.tm_hour) * 3600 - now.tm_min * 60 - now.tm_sec
            if seconds_until_midnight <= 0:
                seconds_until_midnight += 86400
            
            time.sleep(seconds_until_midnight + 60)  # Wait until just after midnight
            
            try:
                # Gather stats
                accuracy = get_accuracy_stats()
                
                # Get filter stats (parse from string - not ideal but works)
                filter_stats = detox_filter.get_stats()
                
                stats = {
                    "total_processed": detox_filter.total,
                    "sent_to_llm": detox_filter.must_escalate + detox_filter.detoxify_triggered,
                    "reported": accuracy.get("removed", 0) + accuracy.get("pending", 0),
                    "benign": detox_filter.benign_skipped + detox_filter.pattern_skipped,
                    "accuracy": accuracy
                }
                
                notify_discord_daily_stats(discord_webhook, stats)
                logging.info("Posted daily stats to Discord")
                
            except Exception as e:
                logging.error(f"Daily stats post failed: {e}")
    
    if cfg.discord_webhook:
        stats_thread = threading.Thread(
            target=daily_stats_loop,
            args=(cfg.discord_webhook, detox_filter, analyzer),
            daemon=True
        )
        stats_thread.start()
        logging.info("Started daily Discord stats (posts at midnight UTC)")
    
    # For now, we only support a single subreddit with streaming
    # Multiple subreddits would require threading or asyncio
    if len(cfg.subreddits) > 1:
        logging.warning("Streaming mode only supports one subreddit. Using first one.")
    
    subreddit_name = cfg.subreddits[0]
    logging.info(f"Monitoring r/{subreddit_name} via comment stream")

    while True:
        try:
            stream_subreddit(reddit, subreddit_name, detox_filter, analyzer, cfg)
            
        except prawcore.exceptions.ResponseException as e:
            logging.error("ResponseException: %s", e)
            logging.info(f"Stats - {detox_filter.get_stats()} | {analyzer.get_stats()}")
            time.sleep(10)
            try:
                reddit = praw_client(cfg)
            except Exception:
                time.sleep(20)
        except prawcore.exceptions.RequestException as e:
            logging.error("RequestException: %s", e)
            time.sleep(10)
        except KeyboardInterrupt:
            logging.info("Shutting down by user request.")
            logging.info(f"Final stats - {detox_filter.get_stats()} | {analyzer.get_stats()}")
            # Print final accuracy stats
            overall = get_accuracy_stats()
            if overall["resolved"] > 0:
                logging.info(
                    f"Final accuracy: {overall['accuracy_pct']:.1f}% "
                    f"({overall['removed']}/{overall['resolved']} removed)"
                )
            break
        except Exception as e:
            logging.error("Stream error: %s\n%s", e, traceback.format_exc())
            logging.info(f"Stats - {detox_filter.get_stats()} | {analyzer.get_stats()}")
            time.sleep(5)
            # Reconnect and continue
            try:
                reddit = praw_client(cfg)
            except Exception:
                time.sleep(20)


if __name__ == "__main__":
    main()
