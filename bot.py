#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import time
import math
import argparse
import threading
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Iterable, Optional, Tuple, Set

import requests
from dotenv import load_dotenv

import praw
from praw.models import ModAction

import torch  # noqa: F401  (detoxify imports torch; keep to avoid lazy import hiccups)
from detoxify import Detoxify


# ------------------------
# Config
# ------------------------

class Config:
    def __init__(self) -> None:
        load_dotenv()
        # Reddit
        self.client_id = os.getenv("REDDIT_CLIENT_ID", "")
        self.client_secret = os.getenv("REDDIT_CLIENT_SECRET", "")
        self.username = os.getenv("REDDIT_USERNAME", "")
        self.password = os.getenv("REDDIT_PASSWORD", "")
        self.user_agent = os.getenv("REDDIT_USER_AGENT", "tox-report-bot/1.0")

        # Subs
        self.subreddits = [s.strip() for s in os.getenv("SUBREDDITS", "").split(",") if s.strip()]

        # Scoring
        self.detoxify_variant = os.getenv("DETOXIFY_VARIANT", "unbiased")
        self.threshold = float(os.getenv("THRESHOLD", "0.9"))
        self.conf_med = float(os.getenv("CONF_MEDIUM", "0.85"))
        self.conf_high = float(os.getenv("CONF_HIGH", "0.9"))
        self.conf_vhigh = float(os.getenv("CONF_VERY_HIGH", "0.95"))

        # Local model dir (optional)
        self.local_model_dir = os.getenv("DETOXIFY_LOCAL_DIR", "").strip() or None

        # Reporting
        self.report_as = os.getenv("REPORT_AS", "moderator")  # moderator | user-note | none
        self.report_style = os.getenv("REPORT_STYLE", "simple")
        self.report_reason_template = os.getenv("REPORT_REASON_TEMPLATE", "ToxicReportBot: {verdict} (confidence: {confidence}).")
        self.report_rule_bucket = os.getenv("REPORT_RULE_BUCKET", "").strip()
        self.enable_reddit_reports = os.getenv("ENABLE_REDDIT_REPORTS", "true").lower() == "true"
        self.dry_run = os.getenv("DRY_RUN", "false").lower() == "true"

        # Discord (per-item)
        self.enable_discord = os.getenv("ENABLE_DISCORD", "false").lower() == "true"
        self.discord_webhook = os.getenv("DISCORD_WEBHOOK", "").strip()

        # Weekly summary
        self.enable_weekly_summary = os.getenv("ENABLE_WEEKLY_SUMMARY", "true").lower() == "true"
        self.summary_webhook = os.getenv("SUMMARY_DISCORD_WEBHOOK", "").strip()
        self.summary_interval_days = int(os.getenv("SUMMARY_INTERVAL_DAYS", "7"))
        self.summary_state_path = os.getenv("SUMMARY_STATE_PATH", "summary_state.json")
        self.decision_lag_hours = int(os.getenv("DECISION_LAG_HOURS", "12"))

        # Outcome tracking
        self.decisions_path = os.getenv("DECISIONS_PATH", "report_outcomes.jsonl")
        self.enable_mod_reason_lookup = os.getenv("ENABLE_MOD_REASON_LOOKUP", "true").lower() == "true"
        self.modlog_lookback_days = int(os.getenv("MODLOG_LOOKBACK_DAYS", "14"))
        self.modlog_limit = int(os.getenv("MODLOG_LIMIT", "100000"))
        self.modlog_delay_ms = int(os.getenv("MODLOG_DELAY_MS", "150"))  # throttle between items

        # Runtime
        self.interval_sec = int(os.getenv("INTERVAL_SEC", "20"))
        self.limit = int(os.getenv("LIMIT", "120"))
        self.state_path = os.getenv("STATE_PATH", "reported_ids.jsonl")
        self.log_level = os.getenv("LOG_LEVEL", "INFO").upper()

        # Internal
        self.daily_refresh_hour_utc = int(os.getenv("DAILY_REFRESH_HOUR_UTC", "06"))  # daily modlog refresh
        self.force_summary_flag = False  # set via CLI


# ------------------------
# Logging
# ------------------------

def setup_logging(level: str) -> None:
    class TZFormatter(logging.Formatter):
        def formatTime(self, record, datefmt=None):
            dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
            if datefmt:
                return dt.strftime(datefmt)
            return dt.strftime("%Y-%m-%d %H:%M:%S")

    fmt = "%(asctime)s | %(levelname)s | %(message)s"
    handler = logging.StreamHandler()
    handler.setFormatter(TZFormatter(fmt))
    logging.basicConfig(level=getattr(logging, level, logging.INFO), handlers=[handler])


# ------------------------
# Utilities
# ------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def to_epoch(dt: datetime) -> int:
    return int(dt.timestamp())

def week_window(now: datetime, days: int) -> Tuple[float, float]:
    end = now
    start = now - timedelta(days=days)
    return (start.timestamp(), end.timestamp())

def last_week_window(now: datetime, days: int) -> Tuple[float, float]:
    end = now
    start = end - timedelta(days=days)
    return (start.timestamp(), end.timestamp())

def safe_load_jsonl(path: str) -> Iterable[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue

def write_jsonl(path: str, obj: Dict[str, Any]) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

def ensure_dir_for(path: str) -> None:
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

def cap_pct(x: float) -> float:
    return max(0.0, min(100.0, x))

def sub_name_lower(sub_obj) -> str:
    """Safely derive a lowercase subreddit name from any PRAW-ish object."""
    name = getattr(sub_obj, "display_name", None) or getattr(sub_obj, "display_name_prefixed", None) or str(sub_obj)
    # display_name_prefixed like "r/UFOs" -> strip "r/" then lower
    if isinstance(name, str) and name.lower().startswith("r/"):
        name = name[2:]
    return (name or "").lower()

# JSON-safe mapping for PRAW objects etc.
def _primitive_json(v):
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    try:
        import praw
        if isinstance(v, praw.models.Redditor):
            return getattr(v, "name", str(v))
        if isinstance(v, praw.models.Comment):
            return getattr(v, "fullname", f"t1_{getattr(v, 'id', 'unknown')}")
        if isinstance(v, praw.models.Submission):
            return getattr(v, "fullname", f"t3_{getattr(v, 'id', 'unknown')}")
        if isinstance(v, praw.models.Subreddit):
            return sub_name_lower(v)
        if isinstance(v, praw.models.ModAction):
            return {
                "action": getattr(v, "action", None),
                "target_fullname": getattr(v, "target_fullname", None),
                "created_utc": int(getattr(v, "created_utc", 0) or 0),
                "mod": getattr(v, "mod", None) and getattr(v.mod, "name", None),
                "subreddit": sub_name_lower(getattr(v, "subreddit", None)),
                "details": getattr(v, "details", None) or "",
                "description": getattr(v, "description", None) or "",
            }
    except Exception:
        pass
    if isinstance(v, dict):
        return {k: _primitive_json(w) for k, w in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_primitive_json(w) for w in v]
    return str(v)

def json_safe(obj):
    return _primitive_json(obj)

def normalize_fullname(kind: str, id_or_full: str) -> str:
    if not id_or_full:
        return ""
    if id_or_full.startswith("t1_") or id_or_full.startswith("t3_"):
        return id_or_full
    if kind == "comment":
        return f"t1_{id_or_full}"
    return f"t3_{id_or_full}"


# ------------------------
# Reddit + Discord
# ------------------------

def praw_client(cfg: Config) -> praw.Reddit:
    reddit = praw.Reddit(
        client_id=cfg.client_id,
        client_secret=cfg.client_secret,
        username=cfg.username,
        password=cfg.password,
        user_agent=cfg.user_agent,
        ratelimit_seconds=5,
    )
    me = reddit.user.me()
    logging.info("Authenticated as u/%s", me.name)
    return reddit

def post_discord(webhook: str, content: str) -> bool:
    if not webhook:
        return False
    try:
        resp = requests.post(webhook, json={"content": content}, timeout=10)
        if 200 <= resp.status_code < 300:
            return True
        logging.warning("Discord post failed: HTTP %s %s", resp.status_code, resp.reason)
        return False
    except Exception as e:
        logging.warning("Discord post failed: %s", e)
        return False


# ------------------------
# Detoxify
# ------------------------

class DetoxWrapper:
    def __init__(self, variant: str, local_dir: Optional[str]):
        if local_dir and os.path.isdir(local_dir):
            self.model = Detoxify(variant, huggingface_config_path=local_dir)
        else:
            self.model = Detoxify(variant)
        self.variant = variant

    def score(self, text: str) -> float:
        out = self.model.predict(text or "")
        if "toxicity" in out:
            return float(out["toxicity"])
        return float(max(out.values()) if out else 0.0)


# ------------------------
# Streams and reporting
# ------------------------

def stream_comments(reddit: praw.Reddit, subs: Iterable[str]) -> Iterable[Any]:
    sub = reddit.subreddit("+".join(subs))
    for c in sub.stream.comments(skip_existing=True):
        yield c

def confidence_bucket(s: float, cfg: Config) -> str:
    if s >= cfg.conf_vhigh:
        return "VERY HIGH"
    if s >= cfg.conf_high:
        return "HIGH"
    if s >= cfg.conf_med:
        return "MEDIUM"
    return "LOW"

def load_seen_ids(path: str) -> Set[str]:
    seen: Set[str] = set()
    for row in safe_load_jsonl(path):
        tfn = row.get("target_fullname") or row.get("id")
        if tfn:
            tfn = normalize_fullname("comment", str(tfn))
            seen.add(tfn)
    return seen

def log_scan(comment, tox: float, cfg: Config):
    level = confidence_bucket(tox, cfg)
    sub_lc = sub_name_lower(comment.subreddit)
    logging.info("SCAN %s | %.4f | %s | %s", comment.fullname, tox, level, sub_lc)

def should_report(tox: float, cfg: Config) -> bool:
    return tox >= cfg.threshold

def do_report(comment, tox: float, cfg: Config) -> None:
    verdict = "TOXIC" if tox >= cfg.threshold else "NOT TOXIC"
    reason = cfg.report_reason_template.format(verdict=verdict, confidence=f"{tox:.2f}")
    if cfg.enable_reddit_reports and not cfg.dry_run:
        try:
            if cfg.report_rule_bucket:
                comment.report(reason, rule_text=cfg.report_rule_bucket)
            else:
                comment.report(reason)
        except Exception as e:
            logging.warning("Reddit report failed for %s: %s", comment.fullname, e)
    if cfg.enable_discord and cfg.discord_webhook:
        post_discord(cfg.discord_webhook, f"Reported {comment.fullname} @ {tox:.2f}: https://www.reddit.com{comment.permalink}")

def append_reported(state_path: str, comment, tox: float, reported: bool) -> None:
    row = {
        "id": comment.id,  # legacy
        "target_fullname": comment.fullname,
        "subreddit": sub_name_lower(comment.subreddit),
        "verdict": "TOXIC" if reported else "NOT TOXIC",
        "tox": round(float(tox), 6),
        "reported": bool(reported),
        "ts": time.time(),
    }
    ensure_dir_for(state_path)
    write_jsonl(state_path, row)


# ------------------------
# Modlog outcomes
# ------------------------

OUTCOME_ACTIONS = {
    "removecomment": "removed",
    "removelink": "removed",
    "approvelink": "approved",
    "approvecomment": "approved",
}

def load_outcome_keys(path: str) -> Set[Tuple[str, str, int]]:
    keys: Set[Tuple[str, str, int]] = set()
    for row in safe_load_jsonl(path):
        tfn = row.get("target_fullname") or row.get("id")
        action = row.get("action") or row.get("outcome")
        ts = int(row.get("ts") or row.get("created_utc") or 0)
        if not tfn or not action or not ts:
            continue
        keys.add((normalize_fullname("comment", str(tfn)), str(action), int(ts)))
    return keys

def refresh_modlog(reddit: praw.Reddit, cfg: Config) -> None:
    for sub in cfg.subreddits:
        try:
            logging.info("Modlog scan: /r/%s (limit=%d, lookback=%dd)", sub, cfg.modlog_limit, cfg.modlog_lookback_days)
            ensure_dir_for(cfg.decisions_path)
            existing = load_outcome_keys(cfg.decisions_path)

            cutoff = utcnow() - timedelta(days=cfg.modlog_lookback_days)
            cutoff_epoch = cutoff.timestamp()

            count_new = 0
            for ma in reddit.subreddit(sub).mod.log(limit=cfg.modlog_limit):
                created = float(getattr(ma, "created_utc", 0) or 0)
                if created < cutoff_epoch:
                    break
                action = str(getattr(ma, "action", "") or "").lower()
                if action not in OUTCOME_ACTIONS:
                    continue
                tfn = getattr(ma, "target_fullname", None) or ""
                if not tfn:
                    continue
                tfn = normalize_fullname("comment" if tfn.startswith("t1_") else "submission", tfn)
                key = (tfn, action, int(created))
                if key in existing:
                    continue

                row = {
                    "ts": int(created),
                    "subreddit": sub_name_lower(getattr(ma, "subreddit", None)),
                    "action": action,
                    "outcome": OUTCOME_ACTIONS[action],
                    "target_fullname": tfn,
                    "mod": getattr(ma, "mod", None) and getattr(ma.mod, "name", None),
                    "details": str(getattr(ma, "details", "") or ""),
                    "description": str(getattr(ma, "description", "") or ""),
                }
                with open(cfg.decisions_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(json_safe(row), ensure_ascii=False) + "\n")

                existing.add(key)
                count_new += 1
                time.sleep(cfg.modlog_delay_ms / 1000.0)

            logging.info("Modlog scan complete for /r/%s; %d new outcomes.", sub, count_new)
        except Exception as e:
            logging.warning("Modlog refresh failed for /r/%s: %s", sub, e)


def daily_modlog_refresher(reddit: praw.Reddit, cfg: Config, stop_evt: threading.Event) -> None:
    while not stop_evt.is_set():
        try:
            now = utcnow()
            if now.hour == cfg.daily_refresh_hour_utc:
                refresh_modlog(reddit, cfg)
                stop_evt.wait(timeout=3600)
            else:
                stop_evt.wait(timeout=300)
        except Exception:
            stop_evt.wait(timeout=600)


# ------------------------
# Weekly summary
# ------------------------

def load_summary_state(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.loads(f.read() or "{}")
    except Exception:
        return {}

def save_summary_state(path: str, state: Dict[str, Any]) -> None:
    ensure_dir_for(path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(state, ensure_ascii=False))

def compute_weekly_summary(cfg: Config) -> Tuple[str, Dict[str, Any]]:
    now = utcnow()
    start_ts, end_ts = week_window(now, cfg.summary_interval_days)
    pstart_ts, pend_ts = last_week_window(now - timedelta(days=cfg.summary_interval_days), cfg.summary_interval_days)

    reported_rows = list(safe_load_jsonl(cfg.state_path))
    outcomes_rows = list(safe_load_jsonl(cfg.decisions_path))

    def in_window(ts, a, b): return a <= float(ts) <= b

    def tfn_of(x):
        tfn = x.get("target_fullname") or x.get("id") or ""
        return normalize_fullname("comment", str(tfn))

    scanned_this = [r for r in reported_rows if in_window(r.get("ts", 0), start_ts, end_ts)]
    reported_this = [r for r in scanned_this if bool(r.get("reported"))]
    tox_all = [float(r.get("tox", 0.0) or 0.0) for r in scanned_this]
    tox_rep = [float(r.get("tox", 0.0) or 0.0) for r in reported_this]

    avg_all = sum(tox_all) / len(tox_all) if tox_all else 0.0
    avg_rep = sum(tox_rep) / len(tox_rep) if tox_rep else 0.0

    reported_tfns = {tfn_of(r) for r in reported_this}
    lag_cutoff_ts = end_ts - cfg.decision_lag_hours * 3600

    removed_tfns = set()
    approved_tfns = set()
    for o in outcomes_rows:
        ts = int(o.get("ts", 0) or o.get("created_utc", 0) or 0)
        if not in_window(ts, start_ts, end_ts):
            continue
        tfn = tfn_of(o)
        if tfn not in reported_tfns:
            continue
        outcome = str(o.get("outcome") or o.get("action") or "").lower()
        if "removed" in outcome or outcome in ("removecomment", "removelink"):
            removed_tfns.add(tfn)
        elif "approved" in outcome or outcome in ("approvecomment", "approvelink"):
            approved_tfns.add(tfn)

    decided_tfns = removed_tfns | approved_tfns
    pending_tfns = {tfn_of(r) for r in reported_this if r.get("ts", 0) >= lag_cutoff_ts and tfn_of(r) not in decided_tfns}
    left_up_tfns = reported_tfns - decided_tfns - pending_tfns

    reported_n = len(reported_this)
    removed_n = len(removed_tfns)
    approved_n = len(approved_tfns)
    left_up_n = len(left_up_tfns)
    pending_n = len(pending_tfns)

    # prior week
    scanned_prior = [r for r in reported_rows if in_window(r.get("ts", 0), pstart_ts, pend_ts)]
    reported_prior = [r for r in scanned_prior if bool(r.get("reported"))]
    tox_all_prior = [float(r.get("tox", 0.0) or 0.0) for r in scanned_prior]
    tox_rep_prior = [float(r.get("tox", 0.0) or 0.0) for r in reported_prior]
    avg_all_prior = sum(tox_all_prior) / len(tox_all_prior) if tox_all_prior else None
    avg_rep_prior = sum(tox_rep_prior) / len(tox_rep_prior) if tox_rep_prior else None
    reported_prior_n = len(reported_prior)

    removed_prior_map = set()
    rep_prior_tfns = {tfn_of(r) for r in reported_prior}
    for o in outcomes_rows:
        ts = int(o.get("ts", 0) or o.get("created_utc", 0) or 0)
        if not (pstart_ts <= ts <= pend_ts):
            continue
        tfn = tfn_of(o)
        if tfn not in rep_prior_tfns:
            continue
        outc = str(o.get("outcome") or o.get("action") or "").lower()
        if "removed" in outc or outc in ("removecomment", "removelink"):
            removed_prior_map.add(tfn)
    removed_prior_n = len(removed_prior_map)

    def delta_pct(curr: Optional[float], prev: Optional[float]) -> Optional[float]:
        if curr is None or prev is None:
            return None
        if prev == 0:
            return math.inf if curr > 0 else 0.0
        return (curr - prev) / prev * 100.0

    align_pct = cap_pct((removed_n / reported_n * 100.0) if reported_n else 0.0)

    def pct_s(v):
        if v is None:
            return "n/a"
        if math.isinf(v):
            return "+∞%"
        return f"{v:+.1f}%"

    def date_span():
        s = datetime.fromtimestamp(start_ts, tz=timezone.utc)
        e = datetime.fromtimestamp(end_ts, tz=timezone.utc)
        return f"{s:%b %d, %Y}–{e:%b %d, %Y}"

    content = []
    content.append(f":bar_chart: Weekly Toxicity Summary (UTC) {date_span()}")
    content.append(f"• Current threshold: {cfg.threshold:.2f}")
    d_all = delta_pct(avg_all, avg_all_prior)
    d_rep = delta_pct(avg_rep, avg_rep_prior)
    d_rep_count = delta_pct(float(reported_n), float(reported_prior_n))
    d_removed = delta_pct(float(removed_n), float(removed_prior_n) if removed_prior_n is not None else None)

    content.append(f"• Average toxicity this week (all scanned): {avg_all:.2f} ({pct_s(d_all)} vs prior)")
    content.append(f"• Average toxicity this week (reported): {avg_rep:.2f} ({pct_s(d_rep)} vs prior)")
    content.append(f"• Total reported comments: {reported_n} ({pct_s(d_rep_count)} vs prior)")
    content.append(f"• Reported comments removed by mods: {removed_n} ({pct_s(d_removed)} vs prior)")
    content.append(f"• Approved by mods: {approved_n}")
    content.append(f"• Left up (past lag): {left_up_n}")
    content.append(f"• Pending decisions (within {cfg.decision_lag_hours}h): {pending_n}")
    content.append(f"• % of reports aligned with mod removal: {align_pct:.1f}%")

    return "\n".join(content), {
        "start": start_ts,
        "end": end_ts,
        "reported_n": reported_n,
    }

def maybe_post_weekly_summary(cfg: Config) -> None:
    if not cfg.enable_weekly_summary:
        return
    try:
        state = load_summary_state(cfg.summary_state_path)
        now = utcnow()
        last_post_ts = state.get("last_post_ts", 0)
        last_post = datetime.fromtimestamp(last_post_ts, tz=timezone.utc) if last_post_ts else None
        due = cfg.force_summary_flag or (not last_post) or ((now - last_post) >= timedelta(days=cfg.summary_interval_days))

        logging.info("Building weekly summary (window=%dd)...", cfg.summary_interval_days)
        if not due and not cfg.force_summary_flag:
            return
        content, meta = compute_weekly_summary(cfg)
        if cfg.summary_webhook:
            posted = post_discord(cfg.summary_webhook, content)
            if posted:
                logging.info("Posted weekly summary to Discord.")
            else:
                logging.warning("Failed to post weekly summary to Discord.")
        state["last_post_ts"] = to_epoch(utcnow())
        state["last_meta"] = meta
        save_summary_state(cfg.summary_state_path, state)
    except Exception as e:
        logging.warning("Weekly summary build error: %s", e)


# ------------------------
# Main loop
# ------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force-summary", action="store_true", help="Post the weekly summary now regardless of schedule.")
    args = parser.parse_args()

    cfg = Config()
    setup_logging(cfg.log_level)
    cfg.force_summary_flag = bool(args.force_summary)

    reddit = praw_client(cfg)

    # one-time modlog refresh at startup
    refresh_modlog(reddit, cfg)

    # weekly summary (if due or forced)
    maybe_post_weekly_summary(cfg)

    # background daily modlog refresh
    stop_evt = threading.Event()
    t = threading.Thread(target=daily_modlog_refresher, args=(reddit, cfg, stop_evt), daemon=True)
    t.start()

    # load detox
    detox = DetoxWrapper(cfg.detoxify_variant, cfg.local_model_dir)

    # state of reported items to avoid duplicates
    seen_reported: Set[str] = load_seen_ids(cfg.state_path)
    logging.info("Starting ToxicReportBot; tracking %d previously-reported items", len(seen_reported))

    try:
        sub_stream = stream_comments(reddit, cfg.subreddits)
        for comment in sub_stream:
            try:
                body = comment.body or ""
                score = detox.score(body)
                log_scan(comment, score, cfg)

                fullname = comment.fullname
                if fullname not in seen_reported:
                    rep = score >= cfg.threshold
                    if rep:
                        do_report(comment, score, cfg)
                    append_reported(cfg.state_path, comment, score, rep)
                    if rep:
                        seen_reported.add(fullname)

                # polite throttle
                time.sleep(max(0.0, cfg.interval_sec / max(cfg.limit, 1)))
            except Exception as ie:
                logging.warning("Inner loop error on %s: %s", getattr(comment, "fullname", "unknown"), ie)
                time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logging.error("Loop error: %s", e)
    finally:
        stop_evt.set()


if __name__ == "__main__":
    main()
