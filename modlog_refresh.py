# modlog_refresh.py
import os
import json
import time
import threading
import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Tuple, Set, Union, List

import praw
import prawcore


APPROVE_ACTIONS = {"approvecomment", "approvelink"}
REMOVE_ACTIONS = {
    "removecomment",
    "removelink",
    "spamcomment",
    "spamlink",
    "moderator_remove",  # rare alt labels
    "remove",            # some 3rd-party actions normalize to this
}

def _parse_subs(value: Union[str, Iterable[str]]) -> List[str]:
    if isinstance(value, str):
        return [s.strip() for s in value.split(",") if s.strip()]
    return [str(s).strip() for s in value if str(s).strip()]

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _cutoff(lookback_days: int) -> datetime:
    return _now_utc() - timedelta(days=lookback_days)

def _normalize_fullname(fullname: Optional[str]) -> Optional[str]:
    # Expect 't1_xxx' or 't3_xxx'. Anything else, we drop.
    if not fullname:
        return None
    fullname = fullname.strip()
    if fullname.startswith(("t1_", "t3_")):
        return fullname
    # If someone fed us just the id, we can’t know kind comment/link; skip.
    return None

def _read_seen_keys(path: str) -> Set[str]:
    seen: Set[str] = set()
    if not os.path.exists(path):
        return seen
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            fname = d.get("target_fullname")
            status = d.get("status") or d.get("outcome")  # legacy
            ts = int(d.get("created_utc", 0))
            if fname and status in ("approved", "removed") and ts:
                seen.add(f"{fname}|{status}|{ts}")
    return seen

def _save_outcome(path: str, record: dict, seen: Set[str]) -> bool:
    key = f"{record['target_fullname']}|{record['status']}|{int(record['created_utc'])}"
    if key in seen:
        return False
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    seen.add(key)
    return True

def _classify_status(action: str) -> Optional[str]:
    a = (action or "").lower()
    if a in APPROVE_ACTIONS:
        return "approved"
    if a in REMOVE_ACTIONS:
        return "removed"
    return None

def refresh_modlog(
    reddit: praw.Reddit,
    subreddits: Union[str, Iterable[str]],
    outcomes_path: str = "report_outcomes.jsonl",
    lookback_days: int = 14,
    limit: int = 100000,
    per_request_sleep: float = 0.0,
) -> Tuple[int, int]:
    """
    Returns (written, scanned). Writes JSONL lines:
      {target_fullname, status, raw_action, created_utc, subreddit, mod}
    """
    subs = _parse_subs(subreddits)
    cutoff_dt = _cutoff(lookback_days)
    written = 0
    scanned = 0

    seen = _read_seen_keys(outcomes_path)

    for name in subs:
        log = logging.getLogger("modlog_refresh")
        log.info(f"Modlog scan: /r/{name} (limit={limit}, lookback={lookback_days}d)")
        sub = reddit.subreddit(name)
        fetched = 0

        try:
            for action in sub.mod.log(limit=None):
                # Stop if we’ve scanned more than limit
                if fetched >= limit:
                    break
                fetched += 1
                scanned += 1

                # Stop if older than cutoff
                # PRAW gives created_utc as a float seconds UTC
                act_dt = datetime.fromtimestamp(getattr(action, "created_utc", 0.0), tz=timezone.utc)
                if act_dt < cutoff_dt:
                    break

                status = _classify_status(getattr(action, "action", ""))
                if not status:
                    continue

                fname = _normalize_fullname(getattr(action, "target_fullname", None))
                if not fname:
                    continue  # can’t map without a fullname

                record = {
                    "target_fullname": fname,
                    "status": status,                                # "approved" | "removed"
                    "raw_action": str(getattr(action, "action", "")),
                    "created_utc": float(getattr(action, "created_utc", 0.0)),
                    "subreddit": str(getattr(action, "subreddit", name)),
                    "mod": getattr(getattr(action, "mod", None), "name", None) or str(getattr(action, "mod", "")),
                }

                if _save_outcome(outcomes_path, record, seen):
                    written += 1

                if per_request_sleep:
                    time.sleep(per_request_sleep)

        except prawcore.exceptions.ServerError as e:
            log.warning(f"Retrying due to 500 status on /r/{name} modlog: {e}")
            time.sleep(2.5)
        except prawcore.exceptions.ResponseException as e:
            log.warning(f"HTTP error on /r/{name} modlog: {e}")
        except Exception as e:
            log.warning(f"Unexpected modlog error on /r/{name}: {e}")

    return written, scanned


# -------- optional: periodic scheduler you can call from bot.py --------

def start_periodic_refresh(
    reddit: praw.Reddit,
    subreddits: Union[str, Iterable[str]],
    outcomes_path: str,
    lookback_days: int,
    limit: int,
    interval_hours: float = 12.0,
    jitter_minutes: float = 5.0,
    per_request_sleep: float = 0.0,
) -> threading.Thread:
    """
    Spawns a daemon thread that refreshes outcomes every `interval_hours`
    with a small random jitter so you don’t sync-stampede yourself.
    """

    import random

    log = logging.getLogger("modlog_refresh")

    def runner():
        while True:
            try:
                w, s = refresh_modlog(
                    reddit=reddit,
                    subreddits=subreddits,
                    outcomes_path=outcomes_path,
                    lookback_days=lookback_days,
                    limit=limit,
                    per_request_sleep=per_request_sleep,
                )
                log.info(f"Periodic modlog refresh wrote {w} new outcome(s) (scanned ~{s}).")
            except Exception as e:
                log.warning(f"Periodic modlog refresh failed: {e}")
            # sleep with jitter
            base = max(1.0, float(interval_hours)) * 3600.0
            jitter = random.uniform(-jitter_minutes, jitter_minutes) * 60.0 if jitter_minutes else 0.0
            time.sleep(max(300.0, base + jitter))

    t = threading.Thread(target=runner, name="modlog-refresh", daemon=True)
    t.start()
    return t


# -------- CLI entrypoint -----------------------------------------------

def _build_reddit_from_env() -> praw.Reddit:
    return praw.Reddit(
        client_id=os.getenv("REDDIT_CLIENT_ID"),
        client_secret=os.getenv("REDDIT_CLIENT_SECRET"),
        username=os.getenv("REDDIT_USERNAME"),
        password=os.getenv("REDDIT_PASSWORD"),
        user_agent=os.getenv("REDDIT_USER_AGENT", "tox-report-bot/1.0"),
    )

if __name__ == "__main__":
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    reddit = _build_reddit_from_env()
    subs = os.getenv("SUBREDDITS", "ufos")
    outcomes = os.getenv("DECISIONS_PATH", "report_outcomes.jsonl")
    lookback = int(os.getenv("MODLOG_LOOKBACK_DAYS", "14"))
    limit = int(os.getenv("MODLOG_LIMIT", "100000"))
    per_req_sleep = float(os.getenv("MODLOG_PER_REQUEST_SLEEP", "0.0"))

    written, scanned = refresh_modlog(
        reddit=reddit,
        subreddits=subs,
        outcomes_path=outcomes,
        lookback_days=lookback,
        limit=limit,
        per_request_sleep=per_req_sleep,
    )
    logging.getLogger("modlog_refresh").info(f"Done. Wrote {written} new outcome(s).")
