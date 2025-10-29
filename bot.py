#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import json
import time
import logging
import traceback
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

# -------- env loading --------
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.getenv("ENV_FILE", ".env"))

# -------- reddit / http --------
import praw
import prawcore

# -------- scoring models --------
# Detoxify is primary; requires torch CPU build installed.
try:
    from detoxify import Detoxify
except Exception:
    Detoxify = None

# Hugging Face secondary, optional
try:
    from transformers import pipeline, AutoTokenizer, AutoModelForSequenceClassification
except Exception:
    pipeline = None
    AutoTokenizer = None
    AutoModelForSequenceClassification = None

# -------- discord optional --------
import urllib.request

# -------------------------------
# Config
# -------------------------------

@dataclass
class Config:
    client_id: str
    client_secret: str
    username: str
    password: str
    user_agent: str

    subreddits: List[str]

    # scoring
    detoxify_variant: str  # "original" | "unbiased"
    threshold: float
    enable_hf: bool
    hf_model_id: str
    hf_label: str
    hf_max_seq_len: int

    # confidence buckets
    conf_medium: float
    conf_high: float
    conf_very_high: float

    # reporting
    report_as: str  # "moderator" | "user"
    report_style: str  # "simple"
    report_reason_template: str
    report_rule_bucket: str

    enable_reddit_reports: bool
    dry_run: bool

    # discord
    enable_discord: bool
    discord_webhook: str

    # runtime
    interval_sec: int
    limit: int
    state_path: str
    log_level: str


def load_config() -> Config:
    # Required
    for key in [
        "REDDIT_CLIENT_ID",
        "REDDIT_CLIENT_SECRET",
        "REDDIT_USERNAME",
        "REDDIT_PASSWORD",
        "REDDIT_USER_AGENT",
    ]:
        if not os.getenv(key):
            raise KeyError(f"Missing required env var: {key}")

    subs = os.getenv("SUBREDDITS", "").strip()
    if not subs:
        raise KeyError("SUBREDDITS is required, e.g. 'ToxicReportBotTest' or 'a,b,c'")

    def _getfloat(name: str, default: float) -> float:
        try:
            return float(os.getenv(name, str(default)))
        except Exception:
            return default

    cfg = Config(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        username=os.environ["REDDIT_USERNAME"],
        password=os.environ["REDDIT_PASSWORD"],
        user_agent=os.environ["REDDIT_USER_AGENT"],
        subreddits=[s.strip() for s in subs.split(",") if s.strip()],
        detoxify_variant=os.getenv("DETOXIFY_VARIANT", "original"),
        threshold=_getfloat("THRESHOLD", 0.71),
        enable_hf=os.getenv("ENABLE_HF", "true").lower() == "true",
        hf_model_id=os.getenv("HF_MODEL_ID", "unitary/unbiased-toxic-roberta"),
        hf_label=os.getenv("HF_LABEL", "OFFENSIVE"),
        hf_max_seq_len=int(os.getenv("HF_MAX_SEQ_LEN", "256")),
        conf_medium=_getfloat("CONF_MEDIUM", 0.80),
        conf_high=_getfloat("CONF_HIGH", 0.90),
        conf_very_high=_getfloat("CONF_VERY_HIGH", 0.95),
        report_as=os.getenv("REPORT_AS", "moderator").lower(),
        report_style=os.getenv("REPORT_STYLE", "simple"),
        report_reason_template=os.getenv(
            "REPORT_REASON_TEMPLATE", "ToxicReportBot: {verdict} (confidence: {confidence})."
        ),
        report_rule_bucket=os.getenv("REPORT_RULE_BUCKET", "").strip(),
        enable_reddit_reports=os.getenv("ENABLE_REDDIT_REPORTS", "true").lower() == "true",
        dry_run=os.getenv("DRY_RUN", "false").lower() == "true",
        enable_discord=os.getenv("ENABLE_DISCORD", "false").lower() == "true",
        discord_webhook=os.getenv("DISCORD_WEBHOOK", "").strip(),
        interval_sec=int(os.getenv("INTERVAL_SEC", "20")),
        limit=int(os.getenv("LIMIT", "120")),
        state_path=os.getenv("STATE_PATH", "reported_ids.jsonl"),
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
    # smoke test creds
    me = reddit.user.me()
    logging.info(f"Authenticated as u/{me.name}")
    return reddit


# -------------------------------
# Models
# -------------------------------

class DetoxWrapper:
    def __init__(self, variant: str):
        if Detoxify is None:
            raise RuntimeError("Detoxify not available. Ensure detoxify and torch are installed.")
        variant = (variant or "original").strip().lower()
        if variant not in ("original", "unbiased"):
            logging.warning(f"Unknown DETOXIFY_VARIANT={variant}, defaulting to 'original'")
            variant = "original"
        self.model = Detoxify(variant)

    def score(self, text: str) -> Dict[str, float]:
        """
        Returns standardized keys:
          - tox: primary toxicity score
          - insult: insult/obscene proxy if available
          - hate: hate proxy if available
        """
        out = self.model.predict(text)
        # Detoxify returns a dict; keys depend on variant. Normalize.
        tox = float(out.get("toxicity", out.get("toxic", 0.0)) or 0.0)
        insult = float(
            out.get("insult", out.get("obscene", out.get("severe_toxicity", 0.0))) or 0.0
        )
        hate = float(
            out.get("identity_attack", out.get("threat", out.get("sexual_explicit", 0.0))) or 0.0
        )
        return {"tox": tox, "insult": insult, "hate": hate}


class HFWrapper:
    def __init__(self, model_id: str, max_len: int):
        if pipeline is None:
            raise RuntimeError("transformers not available. Install transformers to enable HF scoring.")
        self.model_id = model_id
        # Load tokenizer/model explicitly for deterministic labels
        self.tokenizer = AutoTokenizer.from_pretrained(model_id, use_fast=True)
        self.model = AutoModelForSequenceClassification.from_pretrained(model_id)
        self.pipe = pipeline(
            "text-classification",
            model=self.model,
            tokenizer=self.tokenizer,
            truncation=True,
            max_length=max_len,
            top_k=None,  # return all scores if available
        )

    def _extract_toxic(self, results: List[Dict]) -> float:
        """
        Try to find the 'toxic/offensive' probability across label spellings.
        Fallback to max score if we can't find a known label.
        """
        if not results:
            return 0.0
        # results can be [{'label': 'toxic', 'score': 0.8}, {'label':'neutral',...}] OR a single dict.
        if isinstance(results, dict):
            results = [results]
        # Some pipelines return a list with a single dict when top_k=None isn't supported.
        if results and isinstance(results[0], list):
            # When return_all_scores=True style
            results = results[0]
        candidates = []
        for r in results:
            label = str(r.get("label", "")).lower()
            score = float(r.get("score", 0.0))
            candidates.append((label, score))
        # prioritize any label containing 'toxic' or 'offensive'
        for needle in ("toxic", "offensive"):
            for label, score in candidates:
                if needle in label:
                    return score
        # fallback to highest
        return max((s for _, s in candidates), default=0.0)

    def score(self, text: str) -> float:
        res = self.pipe(text, truncation=True)
        return float(self._extract_toxic(res))


# -------------------------------
# Verdicts and formatting
# -------------------------------

def confidence_bucket(p: float, cfg: Config) -> str:
    if p >= cfg.conf_very_high:
        return "very high"
    if p >= cfg.conf_high:
        return "high"
    if p >= cfg.conf_medium:
        return "medium"
    return "low"


def verdict_label(p: float, threshold: float) -> str:
    return "TOXIC" if p >= threshold else "NOT TOXIC"


def build_reason_simple(primary_p: float, cfg: Config) -> str:
    v = verdict_label(primary_p, cfg.threshold)
    c = confidence_bucket(primary_p, cfg)
    return cfg.report_reason_template.format(verdict=v, confidence=c)


# -------------------------------
# State (dedupe)
# -------------------------------

class State:
    def __init__(self, path: str):
        self.path = path
        self.seen = set()  # just cache ids

        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        _id = obj.get("id") or obj.get("thing_id")
                        if _id:
                            self.seen.add(_id)
                    except Exception:
                        continue
        except FileNotFoundError:
            pass

    def add(self, thing_id: str, meta: Dict) -> None:
        self.seen.add(thing_id)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            obj = {"id": thing_id, **meta}
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def has(self, thing_id: str) -> bool:
        return thing_id in self.seen


# -------------------------------
# Discord helper (optional)
# -------------------------------

def post_discord(webhook: str, content: str) -> None:
    if not webhook:
        return
    data = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        webhook,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            _ = resp.read()
    except Exception as e:
        logging.warning(f"Discord post failed: {e}")


# -------------------------------
# Target iteration
# -------------------------------

def iter_targets(sr, limit: int) -> Iterable[Tuple[str, object, str]]:
    # Comments first
    for c in sr.comments(limit=limit):
        body = getattr(c, "body", None)
        if not body:
            continue
        yield (c.fullname, c, body)

    # Then submissions
    for s in sr.new(limit=limit):
        title = getattr(s, "title", "") or ""
        selftext = getattr(s, "selftext", "") or ""
        text = f"{title.strip()}  {selftext.strip()}".strip()
        if not text:
            continue
        yield (s.fullname, s, text)


# -------------------------------
# Report
# -------------------------------

def file_report(thing, reason: str, cfg: Config) -> None:
    # Prefer moderator report if requested and available
    try:
        if cfg.report_as == "moderator":
            # PRAW: thing.mod.report(reason=..., rule_name=optional)
            if cfg.report_rule_bucket:
                thing.mod.report(reason=reason, rule_name=cfg.report_rule_bucket)
            else:
                thing.mod.report(reason=reason)
        else:
            # user report
            thing.report(reason)
    except AttributeError:
        # older PRAW fallback
        thing.report(reason)


# -------------------------------
# Main loop
# -------------------------------

def main() -> None:
    cfg = load_config()
    setup_logging(cfg.log_level)
    logging.info("Starting ToxicReportBot")

    # Reddit
    reddit = praw_client(cfg)

    # Models
    if Detoxify is None:
        raise RuntimeError("Detoxify not installed. Install detoxify and torch (CPU-only).")
    detox = DetoxWrapper(cfg.detoxify_variant)

    hf = None
    if cfg.enable_hf:
        try:
            hf = HFWrapper(cfg.hf_model_id, cfg.hf_max_seq_len)
            logging.info(f"Loaded HF model: {cfg.hf_model_id}")
        except Exception as e:
            logging.warning(f"HF model load failed: {e}; continuing without HF.")
            hf = None

    state = State(cfg.state_path)
    logging.info(f"Monitoring subreddits: {', '.join(cfg.subreddits)}")

    while True:
        try:
            for sub in cfg.subreddits:
                sr = reddit.subreddit(sub)
                logging.debug(f"Scanning r/{sub}")
                for thing_id, thing, text in iter_targets(sr, cfg.limit):
                    if state.has(thing_id):
                        continue

                    # Score primary
                    scores = detox.score(text)
                    tox_p = scores["tox"]
                    # Secondary (optional)
                    hf_p = None
                    if hf is not None:
                        try:
                            hf_p = hf.score(text)
                        except Exception as e:
                            logging.debug(f"HF scoring failed: {e}")
                            hf_p = None

                    # Decide
                    should_report = tox_p >= cfg.threshold
                    verdict = verdict_label(tox_p, cfg.threshold)
                    reason = build_reason_simple(tox_p, cfg)

                    logging.info(
                        f"{thing_id} | {verdict} | tox={tox_p:.2f}"
                        + (f" | hf={hf_p:.2f}" if hf_p is not None else "")
                        + f" | '{text[:120].replace(chr(10), ' ')}{'...' if len(text)>120 else ''}'"
                    )

                    # Post to Discord for visibility if enabled
                    if cfg.enable_discord and cfg.discord_webhook:
                        msg = f"[r/{sub}] {verdict} tox={tox_p:.2f}" + (f" hf={hf_p:.2f}" if hf_p is not None else "")
                        try:
                            post_discord(cfg.discord_webhook, msg)
                        except Exception:
                            pass

                    # Report
                    if cfg.enable_reddit_reports and should_report:
                        if cfg.dry_run:
                            logging.info(f"DRY_RUN: would file {'mod' if cfg.report_as=='moderator' else 'user'} report: {reason}")
                        else:
                            try:
                                file_report(thing, reason, cfg)
                                logging.info(f"Reported {thing_id} with reason: {reason}")
                            except prawcore.exceptions.Forbidden as e:
                                logging.error(f"Forbidden when reporting {thing_id}: {e}")
                            except prawcore.exceptions.NotFound as e:
                                logging.error(f"Item not found {thing_id}: {e}")
                            except Exception as e:
                                logging.error(f"Report failed for {thing_id}: {e}")

                    # Record processed to avoid reprocessing forever
                    state.add(
                        thing_id,
                        {
                            "subreddit": sub,
                            "verdict": verdict,
                            "tox": round(tox_p, 4),
                            "hf": round(hf_p, 4) if hf_p is not None else None,
                            "reported": bool(should_report and cfg.enable_reddit_reports and not cfg.dry_run),
                            "ts": int(time.time()),
                        },
                    )

            time.sleep(cfg.interval_sec)

        except prawcore.exceptions.ResponseException as e:
            # 401s and friends. Sleep and retry.
            logging.error("ResponseException: %s", e)
            time.sleep(10)
            try:
                reddit = praw_client(cfg)  # refresh session
            except Exception:
                time.sleep(20)
        except prawcore.exceptions.RequestException as e:
            logging.error("RequestException: %s", e)
            time.sleep(10)
        except KeyboardInterrupt:
            logging.info("Shutting down by user request.")
            break
        except Exception as e:
            logging.error("Loop error\n%s", traceback.format_exc())
            time.sleep(5)


if __name__ == "__main__":
    main()
