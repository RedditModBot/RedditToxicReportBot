#!/usr/bin/env python3
import os, sys, json, time, math, threading, random, logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, Iterable, List, Tuple, Optional

import requests
from dotenv import load_dotenv
import praw

# === Optional models (loaded lazily when needed) ===
_DETOX = None
_OFF_PIPE = None
_HATE_PIPE = None

try:
    from detoxify import Detoxify
except Exception:
    Detoxify = None

try:
    from transformers import pipeline
except Exception:
    pipeline = None


# ---------- Utilities ----------
def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def ts() -> float:
    return utcnow().timestamp()

def to_fullname(thing_id: str, kind: Optional[str] = None) -> str:
    """Return t1_xxx or t3_xxx; accept raw 'xxx', 't1_xxx', PRAW objects, etc."""
    if hasattr(thing_id, "fullname"):
        return thing_id.fullname
    if isinstance(thing_id, str):
        s = thing_id.strip()
        if s.startswith("t1_") or s.startswith("t3_"):
            return s
        # guess kind when missing
        if kind in ("t1", "comment", "c"):
            return f"t1_{s}"
        if kind in ("t3", "link", "post", "submission", "s"):
            return f"t3_{s}"
        # default to comment
        return f"t1_{s}"
    return str(thing_id)

def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out

def append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

def save_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

def jitter_seconds(min_s: float, max_s: float) -> float:
    return random.uniform(min_s, max_s)


# ---------- Config ----------
@dataclass
class Config:
    # Reddit
    client_id: str
    client_secret: str
    username: str
    password: str
    user_agent: str

    # subs
    subreddits: List[str]

    # scoring
    detoxify_variant: str
    composite_enable: bool
    composite_weights: Tuple[float, float, float]
    composite_threshold: float
    text_normalize: bool
    log_show_components: bool
    log_show_comment: bool

    # optional models
    offensive_model: str
    hate_model: str

    # confidence bands
    conf_medium: float
    conf_high: float
    conf_very_high: float

    # lexicon
    lexicon_path: Optional[Path]

    # reporting
    report_as: str
    report_style: str
    report_reason_template: str
    report_rule_bucket: str
    enable_reddit_reports: bool
    dry_run: bool

    # discord per-item
    enable_discord: bool
    discord_webhook: Optional[str]

    # weekly summary
    enable_weekly_summary: bool
    summary_webhook: Optional[str]
    summary_interval_days: int
    summary_state_path: Path
    summary_include_top_reasons: bool  # kept for compatibility, not used

    # outcomes + lag
    decisions_path: Path
    decision_lag_hours: int

    # modlog refresh
    modlog_lookback_days: int
    modlog_limit: int
    modlog_refresh_interval_hours: int
    modlog_refresh_jitter_min: int
    modlog_per_request_sleep: float

    # runtime
    interval_sec: float
    limit: int
    state_path: Path
    log_level: str
    log_scan: int

def load_config() -> Config:
    load_dotenv(Path.cwd() / ".env")

    def getenvb(k, d=False):
        v = os.getenv(k, str(d)).strip().lower()
        return v in ("1", "true", "yes", "y", "on")

    def getfloat(k, d):
        try:
            return float(os.getenv(k, str(d)))
        except Exception:
            return d

    def getint(k, d):
        try:
            return int(os.getenv(k, str(d)))
        except Exception:
            return d

    def getcsv(k, default=""):
        raw = os.getenv(k, default) or ""
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        return parts

    # composite weights
    w_raw = os.getenv("COMPOSITE_WEIGHTS", "0.45,0.35,0.20")
    try:
        w = tuple(float(x.strip()) for x in w_raw.split(","))
        if len(w) != 3:
            raise ValueError
    except Exception:
        w = (0.45, 0.35, 0.20)

    lex_path = os.getenv("LEXICON_PATH", "").strip()
    lex_path = Path(lex_path) if lex_path else None

    return Config(
        client_id=os.getenv("REDDIT_CLIENT_ID", ""),
        client_secret=os.getenv("REDDIT_CLIENT_SECRET", ""),
        username=os.getenv("REDDIT_USERNAME", ""),
        password=os.getenv("REDDIT_PASSWORD", ""),
        user_agent=os.getenv("REDDIT_USER_AGENT", "tox-report-bot/1.0"),
        subreddits=[s.strip() for s in (os.getenv("SUBREDDITS", "ufos")).split(",") if s.strip()],
        detoxify_variant=os.getenv("DETOXIFY_VARIANT", "unbiased"),
        composite_enable=getenvb("COMPOSITE_ENABLE", True),
        composite_weights=w,
        composite_threshold=getfloat("COMPOSITE_THRESHOLD", 0.85),
        text_normalize=getenvb("TEXT_NORMALIZE", False),
        log_show_components=getenvb("LOG_SHOW_COMPONENTS", True),
        log_show_comment=getenvb("LOG_SHOW_COMMENT", False),
        offensive_model=os.getenv("OFFENSIVE_MODEL", "unitary/toxic-bert"),
        hate_model=os.getenv("HATE_MODEL", "Hate-speech-CNERG/dehatebert-mono-english"),
        conf_medium=getfloat("CONF_MEDIUM", 0.85),
        conf_high=getfloat("CONF_HIGH", 0.95),
        conf_very_high=getfloat("CONF_VERY_HIGH", 0.98),
        lexicon_path=lex_path,
        report_as=os.getenv("REPORT_AS", "moderator"),
        report_style=os.getenv("REPORT_STYLE", "simple"),
        report_reason_template=os.getenv("REPORT_REASON_TEMPLATE", "{verdict} (confidence: {confidence})."),
        report_rule_bucket=os.getenv("REPORT_RULE_BUCKET", ""),
        enable_reddit_reports=getenvb("ENABLE_REDDIT_REPORTS", True),
        dry_run=getenvb("DRY_RUN", False),
        enable_discord=getenvb("ENABLE_DISCORD", False),
        discord_webhook=(os.getenv("DISCORD_WEBHOOK") or "").strip() or None,
        enable_weekly_summary=getenvb("ENABLE_WEEKLY_SUMMARY", True),
        summary_webhook=(os.getenv("SUMMARY_DISCORD_WEBHOOK") or "").strip() or None,
        summary_interval_days=getint("SUMMARY_INTERVAL_DAYS", 7),
        summary_state_path=Path(os.getenv("SUMMARY_STATE_PATH", "summary_state.json")),
        summary_include_top_reasons=os.getenv("SUMMARY_INCLUDE_TOP_REASONS", "false").lower() == "true",
        decisions_path=Path(os.getenv("DECISIONS_PATH", "report_outcomes.jsonl")),
        decision_lag_hours=getint("DECISION_LAG_HOURS", 12),
        modlog_lookback_days=getint("MODLOG_LOOKBACK_DAYS", 2),
        modlog_limit=getint("MODLOG_LIMIT", 100000),
        modlog_refresh_interval_hours=getint("MODLOG_REFRESH_INTERVAL_HOURS", 24),
        modlog_refresh_jitter_min=getint("MODLOG_REFRESH_JITTER_MIN", 10),
        modlog_per_request_sleep=float(os.getenv("MODLOG_PER_REQUEST_SLEEP", "0.05")),
        interval_sec=float(os.getenv("INTERVAL_SEC", "20")),
        limit=getint("LIMIT", 120),
        state_path=Path(os.getenv("STATE_PATH", "reported_ids.jsonl")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        log_scan=getint("LOG_SCAN", 1)
    )


# ---------- Models / scoring ----------
def load_detoxify(variant: str):
    global _DETOX
    if _DETOX is None:
        if Detoxify is None:
            raise RuntimeError("Detoxify not available. Install package to enable.")
        _DETOX = Detoxify(variant)
    return _DETOX

def load_offensive(model_name: str):
    global _OFF_PIPE
    if _OFF_PIPE is None:
        if pipeline is None:
            raise RuntimeError("transformers not available.")
        _OFF_PIPE = pipeline("text-classification", model=model_name)
    return _OFF_PIPE

def load_hate(model_name: str):
    global _HATE_PIPE
    if _HATE_PIPE is None:
        if pipeline is None:
            raise RuntimeError("transformers not available.")
        _HATE_PIPE = pipeline("text-classification", model=model_name)
    return _HATE_PIPE

def normalize_text(s: str, enable: bool) -> str:
    if not enable:
        return s
    # keep normalization mild to avoid changing meaning
    return " ".join(s.split())

def score_text(text: str, cfg: Config) -> Dict[str, float]:
    t = normalize_text(text or "", cfg.text_normalize)

    # Detoxify
    dmodel = load_detoxify(cfg.detoxify_variant)
    d = dmodel.predict(t)
    tox = float(d.get("toxicity", 0.0))

    # Offensiveness
    opipe = load_offensive(cfg.offensive_model)
    o = opipe(t)[0]
    # map to "offensive probability"
    off_score = o["score"] if o["label"].lower() in ("toxic", "offensive") else 1.0 - o["score"]

    # Hate
    hpipe = load_hate(cfg.hate_model)
    h = hpipe(t)[0]
    hate_score = h["score"] if h["label"].lower() not in ("normal", "non-hate", "non_hate") else 0.0

    # composite
    if cfg.composite_enable:
        w1, w2, w3 = cfg.composite_weights
        composite = w1 * tox + w2 * off_score + w3 * hate_score
    else:
        composite = tox

    return {
        "tox": tox,
        "off": off_score,
        "hate": hate_score,
        "score": composite
    }


# ---------- Discord ----------
def post_discord(webhook: Optional[str], content: str) -> bool:
    if not webhook:
        return False
    try:
        r = requests.post(
            webhook,
            json={"content": content},
            timeout=15
        )
        if r.status_code // 100 == 2:
            return True
        logging.warning("Discord post failed: %s %s", r.status_code, r.text[:200])
        return False
    except Exception as e:
        logging.warning("Discord post exception: %s", e)
        return False


# ---------- Modlog refresh ----------
def refresh_modlog_async(reddit, cfg: Config, stop_flag: threading.Event):
    """Quiet periodic updater. De-dupes report_outcomes by action + fullname."""
    def once():
        try:
            sub = reddit.subreddit(cfg.subreddits[0])
            lookback = utcnow() - timedelta(days=cfg.modlog_lookback_days)
            cutoff = lookback.replace(tzinfo=timezone.utc).timestamp()

            existing = read_jsonl(cfg.decisions_path)
            seen = set((e.get("action_id"), e.get("target_fullname")) for e in existing if e.get("action_id"))
            wrote = 0

            for action in sub.mod.log(limit=cfg.modlog_limit):
                # action.created_utc is float epoch
                if float(action.created_utc) < cutoff:
                    break
                tfn = None
                if getattr(action, "target_fullname", None):
                    tfn = action.target_fullname
                else:
                    # reconstruct if possible
                    tid = getattr(action, "target_fullname", None) or getattr(action, "target_fullname", None)
                    if tid:
                        tfn = to_fullname(tid)
                    elif getattr(action, "target_body", None) and getattr(action, "target_perm", None):
                        # last-ditch, skip
                        continue

                if not tfn:
                    continue

                key = (getattr(action, "id", None), tfn)
                if key in seen:
                    continue
                act = str(getattr(action, "action", "")).lower()
                # Only care about approve/remove
                if act not in ("removecomment", "approvelink", "approvecomment", "removelink"):
                    continue

                rec = {
                    "action_id": getattr(action, "id", None),
                    "target_fullname": tfn,
                    "raw_action": act,
                    "mod": str(getattr(action, "mod", "")),
                    "created_utc": float(getattr(action, "created_utc", ts()))
                }
                append_jsonl(cfg.decisions_path, rec)
                wrote += 1
                time.sleep(cfg.modlog_per_request_sleep)
            if wrote:
                logging.info("Modlog refresh wrote %d new actions.", wrote)
        except Exception as e:
            logging.warning("Modlog refresh failed: %s", e)

    # initial jittered delay so we don't stampede at boot
    delay = jitter_seconds(1, 5)
    time.sleep(delay)
    while not stop_flag.is_set():
        once()
        # interval with jitter minutes
        # Sleep in small chunks so we can exit fast
        total = cfg.modlog_refresh_interval_hours * 3600 + cfg.modlog_refresh_jitter_min * 60 * random.random()
        slept = 0
        while slept < total and not stop_flag.is_set():
            time.sleep(min(5, total - slept))
            slept += 5


# ---------- Summary ----------
def safe_pct(curr: int, prev: int) -> str:
    if prev <= 0:
        return "+∞% vs prior"
    delta = (curr - prev) / prev * 100.0
    sign = "+" if delta >= 0 else ""
    return f"{sign}{delta:.1f}% vs prior"

def fmt_ratio(n: float) -> str:
    if math.isnan(n) or math.isinf(n):
        return "n/a"
    return f"{n:.1f}%"

def read_summary_state(path: Path) -> Optional[float]:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if "last_posted_ts" in raw:
            return float(raw["last_posted_ts"])
        # backward compat: ISO timestamp
        if "last_posted_at" in raw:
            try:
                dt = datetime.fromisoformat(raw["last_posted_at"].replace("Z", "+00:00"))
                return dt.timestamp()
            except Exception:
                return None
    except Exception:
        return None
    return None

def write_summary_state(path: Path, now_ts: float) -> None:
    save_text(path, json.dumps({"last_posted_ts": now_ts}, ensure_ascii=False))

def build_weekly_summary(cfg: Config) -> str:
    now = utcnow()
    start = now - timedelta(days=7)
    prev_start = start - timedelta(days=7)
    prev_end = start

    # load scans/reports
    scans = read_jsonl(cfg.state_path)
    decisions = read_jsonl(cfg.decisions_path)

    def in_window(rec, lo: float, hi: float) -> bool:
        t = float(rec.get("ts", 0.0))
        return lo <= t < hi

    # Normalize all fullnames
    for r in scans:
        if "target_fullname" in r:
            r["target_fullname"] = to_fullname(r["target_fullname"])
        elif "id" in r:
            # old schema: reported/non-reported lines with comment ids
            # treat as comments by default
            r["target_fullname"] = to_fullname(r["id"], "comment")

    for d in decisions:
        d["target_fullname"] = to_fullname(d.get("target_fullname", ""))

    # Windows (epoch)
    lo = start.timestamp()
    hi = now.timestamp()
    plo = prev_start.timestamp()
    phi = prev_end.timestamp()

    scans_w = [r for r in scans if in_window(r, lo, hi)]
    scans_prev = [r for r in scans if in_window(r, plo, phi)]

    # toxicity averages
    def avg_tox(rows):
        vals = [float(r.get("tox", 0.0)) for r in rows if "tox" in r]
        return (sum(vals) / len(vals)) if vals else 0.0

    avg_all = avg_tox(scans_w)
    rep_w = [r for r in scans_w if r.get("reported") is True]
    avg_rep = avg_tox(rep_w)

    # reported counts (current / prior)
    reported_ids = {r["target_fullname"] for r in rep_w}
    reported_prev_ids = {to_fullname(r.get("target_fullname", r.get("id", "")))
                         for r in scans_prev if r.get("reported") is True}

    reported_count = len(reported_ids)
    reported_prev_count = len(reported_prev_ids)

    # map decisions by target
    by_target = {}
    for d in decisions:
        key = d.get("target_fullname")
        if not key:
            continue
        L = by_target.setdefault(key, [])
        L.append(d)

    # decision windows for lag
    lag_sec = cfg.decision_lag_hours * 3600.0
    cutoff_pending = now.timestamp() - lag_sec

    removed = 0
    approved = 0
    pending = 0
    for r in rep_w:
        tfn = r["target_fullname"]
        r_ts = float(r.get("ts", 0.0))
        acts = [a for a in by_target.get(tfn, [])]
        # see if any remove/approve occurred after report time
        decided = False
        for a in acts:
            act = a.get("raw_action", "").lower()
            at = float(a.get("created_utc", 0.0))
            if at < r_ts:
                continue
            if act in ("removecomment", "removelink"):
                removed += 1
                decided = True
                break
            if act in ("approvecomment", "approvelink"):
                approved += 1
                decided = True
                break
        if not decided:
            if r_ts >= cutoff_pending:
                pending += 1

    left_up = max(0, reported_count - removed - approved - pending)

    # Alignment = removed / reported, capped at 100
    align = 0.0 if reported_count == 0 else min(100.0, removed * 100.0 / reported_count)

    # prior metrics for deltas
    removed_prev = 0
    approved_prev = 0
    for pid in reported_prev_ids:
        acts = by_target.get(pid, [])
        # use prev week boundaries
        # A removal is counted if it occurred in [prev_start, prev_end)
        for a in acts:
            act = a.get("raw_action", "").lower()
            at = float(a.get("created_utc", 0.0))
            if not (plo <= at < phi):
                continue
            if act in ("removecomment", "removelink"):
                removed_prev += 1
                break
            if act in ("approvecomment", "approvelink"):
                approved_prev += 1
                break

    # deltas
    avg_all_prev = avg_tox([r for r in scans_prev])
    avg_rep_prev = avg_tox([r for r in scans_prev if r.get("reported") is True])

    lines = []
    date_str = f"{start.strftime('%b %d, %Y')}–{now.strftime('%b %d, %Y')}"
    thresh = cfg.composite_threshold if cfg.composite_enable else float(os.getenv("THRESHOLD", "0.85"))
    lines.append(f":bar_chart: Weekly Toxicity Summary (UTC) {date_str}")
    lines.append(f"• Current threshold: {thresh:.2f}")
    lines.append(f"• Average toxicity this week (all scanned): {avg_all:.2f} ({safe_pct(round(avg_all*1000), round(avg_all_prev*1000))})")
    lines.append(f"• Average toxicity this week (reported): {avg_rep:.2f} ({safe_pct(round(avg_rep*1000), round(avg_rep_prev*1000))})")
    lines.append(f"• Total reported comments: {reported_count} ({safe_pct(reported_count, reported_prev_count)})")
    lines.append(f"• Reported comments removed by mods: {removed} ({safe_pct(removed, removed_prev)})")
    lines.append(f"• Approved by mods: {approved} ({safe_pct(approved, approved_prev)})")
    lines.append(f"• Left up (past lag): {left_up}")
    lines.append(f"• Pending decisions (within {cfg.decision_lag_hours}h): {pending}")
    lines.append(f"• % of reports aligned with mod removal: {fmt_ratio(align)}")

    return "\n".join(lines)


def maybe_post_weekly_summary(cfg: Config) -> None:
    if not cfg.enable_weekly_summary or not cfg.summary_webhook:
        return
    last_ts = read_summary_state(cfg.summary_state_path)
    now_ts = ts()
    interval = cfg.summary_interval_days * 86400
    # only post if last post is missing or older than interval
    if last_ts is not None and (now_ts - last_ts) < interval:
        logging.info("Weekly summary not due yet.")
        return

    try:
        logging.info("Building weekly summary (window=%dd)...", cfg.summary_interval_days)
        content = build_weekly_summary(cfg)
        if post_discord(cfg.summary_webhook, content):
            logging.info("Posted weekly summary to Discord.")
            write_summary_state(cfg.summary_state_path, now_ts)
        else:
            logging.warning("Failed to post weekly summary to Discord.")
    except Exception as e:
        logging.warning("Weekly summary build error: %s", e)


# ---------- Main bot ----------
def setup_logging(cfg: Config):
    lvl = getattr(logging, cfg.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=lvl,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

def reddit_client(cfg: Config):
    r = praw.Reddit(
        client_id=cfg.client_id,
        client_secret=cfg.client_secret,
        username=cfg.username,
        password=cfg.password,
        user_agent=cfg.user_agent,
        ratelimit_seconds=5
    )
    # sanity check
    me = None
    try:
        me = str(r.user.me())
    except Exception:
        pass
    logging.info("Authenticated as u/%s", me or cfg.username or "unknown")
    return r

def permal(c) -> str:
    try:
        return f"https://www.reddit.com{c.permalink}"
    except Exception:
        fid = to_fullname(getattr(c, "id", ""), "comment")
        return f"(no permalink for {fid})"

def load_seen_ids(cfg: Config) -> set:
    rows = read_jsonl(cfg.state_path)
    ids = set()
    for r in rows:
        ids.add(to_fullname(r.get("target_fullname", r.get("id", ""))))
    return ids

def record_scan(cfg: Config, c, verdict: str, tox: float, reported: bool):
    rec = {
        "id": getattr(c, "id", None) or "",
        "target_fullname": to_fullname(getattr(c, "id", ""),"comment"),
        "subreddit": str(getattr(c, "subreddit", "unknown")),
        "verdict": verdict,
        "tox": float(tox),
        "reported": bool(reported),
        "ts": ts()
    }
    append_jsonl(cfg.state_path, rec)

def report_comment(cfg: Config, c, verdict: str, confidence: float):
    if cfg.dry_run or not cfg.enable_reddit_reports:
        return True
    try:
        reason = cfg.report_reason_template.format(verdict=verdict, confidence=f"{confidence:.2f}")
        # actual report call
        c.report(reason)
        return True
    except Exception as e:
        logging.warning("Reddit report failed on %s: %s", getattr(c, "id", "?"), e)
        return False

def scan_loop(reddit, cfg: Config, stop_flag: threading.Event):
    sub = reddit.subreddit("+".join(cfg.subreddits))
    seen = load_seen_ids(cfg)
    logging.info("Starting ToxicReportBot; tracking %d previously-reported items", len(seen))

    # PRAW stream; use pause_after so we can check stop flag
    stream = sub.stream.comments(skip_existing=True, pause_after=5)
    for c in stream:
        if stop_flag.is_set():
            break
        if c is None:
            continue

        try:
            text = str(getattr(c, "body", "") or "")
            s = score_text(text, cfg)
            score = s["score"]
            tox = s["tox"]
            off = s["off"]
            hate = s["hate"]

            band = "LOW"
            if score >= cfg.conf_very_high:
                band = "VERY HIGH"
            elif score >= cfg.conf_high:
                band = "HIGH"
            elif score >= cfg.conf_medium:
                band = "MEDIUM"

            subname = str(getattr(c, "subreddit", "")).lower()
            components = f" | tox={tox:.4f} off={off:.4f} hate={hate:.4f}" if cfg.log_show_components else ""
            body_echo = f" | {text[:280].replace(chr(10),' ')}" if cfg.log_show_comment else ""
            logging.info("SCAN %s | %.4f | %s | %s%s%s",
                         to_fullname(getattr(c, "id", ""),"comment"),
                         score, band, subname, components, body_echo)

            should = score >= cfg.composite_threshold if cfg.composite_enable else tox >= float(os.getenv("THRESHOLD","0.85"))
            reported = False
            if should:
                ok = report_comment(cfg, c, "TOXIC", score)
                reported = ok
                logging.info("Reported comment %s @ %.2f: %s",
                             to_fullname(getattr(c, "id",""),"comment"),
                             score, permal(c))

            record_scan(cfg, c, "TOXIC" if should else "NOT TOXIC", tox, reported)

        except Exception as e:
            logging.warning("Inner loop error on %s: %s", getattr(c, "id", "?"), e)
            continue

        # modest pacing so we don't melt
        if cfg.interval_sec:
            time.sleep(cfg.interval_sec / max(1, cfg.limit))


def main():
    cfg = load_config()
    setup_logging(cfg)
    reddit = reddit_client(cfg)

    # periodic modlog refresher in the background
    stop_flag = threading.Event()
    t = threading.Thread(target=refresh_modlog_async, args=(reddit, cfg, stop_flag), daemon=True)
    t.start()

    # post weekly summary if due (respects last_posted state)
    try:
        maybe_post_weekly_summary(cfg)
    except Exception as e:
        logging.warning("Summary step failed: %s", e)

    # main scan loop
    try:
        scan_loop(reddit, cfg, stop_flag)
    except KeyboardInterrupt:
        pass
    finally:
        stop_flag.set()

if __name__ == "__main__":
    main()
