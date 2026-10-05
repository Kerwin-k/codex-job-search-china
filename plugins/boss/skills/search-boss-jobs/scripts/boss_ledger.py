#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


import config_core as jobflow

RECEIPTS = jobflow.platform_state("boss") / "receipts.jsonl"
SCREENING = jobflow.platform_state("boss") / "screening.jsonl"
BATCH_STATE = jobflow.platform_state("boss") / "batch-state.json"
BATCH_HISTORY = jobflow.platform_state("boss") / "batch-history.jsonl"
EXECUTION_RECEIPTS = jobflow.platform_state("boss") / "executor-receipts.jsonl"
SCREENING_TTL_DAYS = 30
SHORTLIST_LIMIT = 2
DEFAULT_BATCH_MODE = "search-city"
MIN_CITY_UNIQUE_READ = 40
MIN_CITY_LOAD_CALLS = 2
MIN_CITY_NO_GROWTH = 2
# A survivor resets the rolling fuse, but it must not let one city consume an
# unbounded share of a batch. These are absolute per-city caps and are folded
# into combo-fuse as safe rotation reasons.
MAX_CITY_UNIQUE_READ = 100
MAX_CITY_JD_REVIEWS = 16
CITY_ORDER = jobflow.effective("boss")["search"]["cities"]
# Keep the broad recall lane, but schedule higher-precision enterprise
# application terms first. A batch still records exactly one keyword; the
# controller starts the next batch with the next lane in this order.
SEARCH_KEYWORD_ORDER = jobflow.effective("boss")["search"]["keywords"]
PRECISION_KEYWORDS = set()
EXPANSION_KEYWORDS = set()
MIN_JD_REVIEW_LIMIT = 12
JD_REVIEWS_PER_TARGET = 4
DEFAULT_LEASE_TTL_SECONDS = 1200
LEASE_RENEW_SAFETY_SECONDS = 120
SAFE_REACQUIRE_PHASES = {
    "PREFLIGHT", "DISCOVER", "SHORTLIST", "SELECT", "READ_JD", "DECIDE",
    "ROTATE", "PAUSED",
}
SEND_SENSITIVE_PHASES = {
    "OPEN_DETAIL", "COMPANY_CHECK", "OPEN_CHAT", "DRAFT", "SEND_VERIFY", "CLOSE_RETURN",
}
TARGET_CITIES = set(CITY_ORDER)
OBVIOUS_TITLE_EXCLUSIONS = re.compile(r"(?!)")

CARD_PRIORITY_RULES = ()


def keyword_lane_policy(keyword):
    return {"tier":"configured","max_unique_without_survivor":40,"max_jd_without_survivor":8,"max_same_reason_streak":30}


def normalize(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def lease_token_digest(token: object) -> str:
    """Return a compact non-reversible marker for the prior lease token."""
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


ROLE_NOISE = re.compile(r"急招|急聘|厂家直销|全职|兼职|远程|双休|可实习", re.IGNORECASE)


def canonical_role_title(title: object) -> str:
    """Collapse harmless title decorations for same-company de-duplication."""
    text = normalize(title)
    text = ROLE_NOISE.sub("", text)
    return re.sub(r"[\-_/·•:：,，。.!！()（）\[\]【】]+", "", text)


def read_entries(path: Path = RECEIPTS) -> list[dict]:
    if not path.exists():
        return []
    entries: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            entries.append(value)
    return entries


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


@contextmanager
def lease_state_lock(path: Path):
    """Serialize lease acquisition/recovery across executor processes."""
    lock_path = path.with_name(f"{path.name}.lease.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        locked = False
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
            yield
        finally:
            if locked:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _timing_bucket_defaults() -> dict:
    return {
        "count": 0,
        "sum_seconds": 0.0,
        "average_seconds": 0.0,
        "max_seconds": 0.0,
        "last_seconds": 0.0,
    }


def _candidate_timing_defaults() -> dict:
    return {
        "overall": _timing_bucket_defaults(),
        "by_outcome": {},
        "stages": {
            "claim_to_jd": _timing_bucket_defaults(),
            "jd_to_resolution": _timing_bucket_defaults(),
        },
    }


def _ensure_candidate_timing(telemetry: dict) -> dict:
    timing = telemetry.get("candidate_timing")
    if not isinstance(timing, dict):
        timing = {}
        telemetry["candidate_timing"] = timing
    for key in ("overall",):
        bucket = timing.get(key)
        if not isinstance(bucket, dict):
            bucket = {}
            timing[key] = bucket
        for name, default in _timing_bucket_defaults().items():
            bucket.setdefault(name, default)
    outcomes = timing.get("by_outcome")
    if not isinstance(outcomes, dict):
        timing["by_outcome"] = {}
    stages = timing.get("stages")
    if not isinstance(stages, dict):
        stages = {}
        timing["stages"] = stages
    for name in ("claim_to_jd", "jd_to_resolution"):
        bucket = stages.get(name)
        if not isinstance(bucket, dict):
            bucket = {}
            stages[name] = bucket
        for key, default in _timing_bucket_defaults().items():
            bucket.setdefault(key, default)
    return timing


def _timing_add(bucket: dict, seconds: float | None) -> None:
    if seconds is None:
        return
    bounded = round(max(0.0, min(float(seconds), 604800.0)), 2)
    bucket["count"] = int(bucket.get("count") or 0) + 1
    bucket["sum_seconds"] = round(float(bucket.get("sum_seconds") or 0.0) + bounded, 2)
    bucket["average_seconds"] = round(bucket["sum_seconds"] / bucket["count"], 2)
    bucket["max_seconds"] = round(max(float(bucket.get("max_seconds") or 0.0), bounded), 2)
    bucket["last_seconds"] = bounded


def _telemetry_defaults() -> dict:
    return {
        "events": 0,
        "phase_transitions": 0,
        "initial_retries": 0,
        "load_retries": 0,
        "selection_retries": 0,
        "technical_errors": 0,
        "lease_reacquires": 0,
        "candidate_skips": 0,
        "jd_reviews": 0,
        "sends": 0,
        "wait_seconds": 0.0,
        "started_at": utc_now().isoformat(),
        "last_event_at": "",
        "candidate_timing": _candidate_timing_defaults(),
    }


def _telemetry_apply(state: dict, event: str, **values: object) -> dict:
    telemetry = state.setdefault("telemetry", _telemetry_defaults())
    for key, default in _telemetry_defaults().items():
        telemetry.setdefault(key, default)
    _ensure_candidate_timing(telemetry)
    telemetry["events"] = int(telemetry.get("events") or 0) + 1
    telemetry["last_event_at"] = utc_now().isoformat()
    if values.get("seconds") is not None:
        telemetry["wait_seconds"] = round(
            float(telemetry.get("wait_seconds") or 0.0) + max(0.0, float(values.get("seconds") or 0.0)),
            2,
        )
    event = str(event or "").strip().lower()
    if event == "phase":
        telemetry["phase_transitions"] = int(telemetry.get("phase_transitions") or 0) + 1
    elif event == "initial_retry":
        telemetry["initial_retries"] = int(telemetry.get("initial_retries") or 0) + 1
    elif event == "load_retry":
        telemetry["load_retries"] = int(telemetry.get("load_retries") or 0) + 1
    elif event == "selection_retry":
        telemetry["selection_retries"] = int(telemetry.get("selection_retries") or 0) + 1
    elif event == "technical_error":
        telemetry["technical_errors"] = int(telemetry.get("technical_errors") or 0) + 1
    elif event == "lease_reacquire":
        telemetry["lease_reacquires"] = int(telemetry.get("lease_reacquires") or 0) + 1
    elif event == "candidate_skip":
        telemetry["candidate_skips"] = int(telemetry.get("candidate_skips") or 0) + 1
    elif event == "jd_review":
        telemetry["jd_reviews"] = int(telemetry.get("jd_reviews") or 0) + 1
    elif event == "send":
        telemetry["sends"] = int(telemetry.get("sends") or 0) + 1
    elif event == "wait":
        pass
    return telemetry


def record_telemetry(event: str, path: Path = BATCH_STATE, **values: object) -> dict:
    """Persist one compact efficiency event; never stores page content."""
    state = read_json(path)
    if not state:
        return {"ok": False, "error": "batch_state_missing"}
    telemetry = _telemetry_apply(state, event, **values)
    write_json(path, state)
    return {"ok": True, "telemetry": telemetry}


def _record_screening_reasons(state: dict, counts: dict[str, int] | None = None, reason: str = "", amount: int = 1) -> None:
    bucket = state.setdefault("screening_reason_counts", {})
    city_stats = None
    current_city = str(state.get("current_city") or "")
    if current_city:
        city_stats = _city_stats(state, current_city)
        city_reason_bucket = city_stats.setdefault("screening_reason_counts", {})
    else:
        city_reason_bucket = None
    for key, value in (counts or {}).items():
        normalized = str(key or "unknown")[:80]
        increment = max(0, int(value or 0))
        bucket[normalized] = int(bucket.get(normalized) or 0) + increment
        if city_reason_bucket is not None:
            city_reason_bucket[normalized] = int(city_reason_bucket.get(normalized) or 0) + increment
            city_stats["screening_count"] = int(city_stats.get("screening_count") or 0) + increment
    if reason:
        normalized = str(reason)[:80]
        increment = max(0, int(amount))
        bucket[normalized] = int(bucket.get(normalized) or 0) + increment
        if city_reason_bucket is not None:
            city_reason_bucket[normalized] = int(city_reason_bucket.get(normalized) or 0) + increment
            city_stats["screening_count"] = int(city_stats.get("screening_count") or 0) + increment


def receipt_token(entry: dict) -> str:
    return json.dumps(list(receipt_key(entry)), ensure_ascii=False, separators=(",", ":"))


def start_batch(
    target: int,
    path: Path = BATCH_STATE,
    mode: str = DEFAULT_BATCH_MODE,
    unique_read_limit: int = 0,
    no_send_jd_limit: int = 0,
    keyword: str = "",
) -> dict:
    target = max(1, int(target))
    existing = read_json(path)
    if existing.get("active"):
        execution = existing.get("execution") or {}
        return {
            "ok": False,
            "error": "active_batch_exists",
            "batch_id": existing.get("batch_id"),
            "phase": execution.get("phase", ""),
            "owner": execution.get("owner", ""),
        }
    review_limit = max(MIN_JD_REVIEW_LIMIT, target * JD_REVIEWS_PER_TARGET)
    state = {
        "version": 4,
        "active": True,
        "batch_id": utc_now().strftime("%Y%m%dT%H%M%SZ"),
        "started_at": utc_now().isoformat(),
        "target": target,
        "mode": str(mode or DEFAULT_BATCH_MODE),
        "keyword": str(keyword or "").strip()[:120],
        "keyword_order": SEARCH_KEYWORD_ORDER,
        "city_order": CITY_ORDER,
        "city_index": -1,
        "current_city": "",
        "city_history": [],
        "city_stats": {},
        "screening_reason_counts": {},
        "combo_policy": keyword_lane_policy(keyword),
        "combo_survivors": {},
        "jd_review_limit": review_limit,
        "jd_reviewed_job_ids": [],
        "jd_reviews_at_last_send": 0,
        "unique_read_limit": max(0, int(unique_read_limit)),
        "no_send_jd_limit": max(0, int(no_send_jd_limit)),
        "baseline_receipts": sorted(receipt_token(entry) for entry in read_entries()),
        "baseline_screened_ids": sorted(active_screening()),
        "raw_occurrences": 0,
        "list_calls": 0,
        "unique_job_ids": [],
        "seen_output_job_ids": [],
        "presented_job_ids": [],
        "pending_jobs": [],
        "pending_meta": {},
        "batch_duplicates_filtered": 0,
        "last_batch": {},
        "checkpoint": {},
        "telemetry": _telemetry_defaults(),
        "shortlist_generation": 0,
        "execution": {
            "phase": "PREFLIGHT",
            "owner": "",
            "lease_token": "",
            "previous_lease_token_hash": "",
            "lease_expires_at": "",
            "executor_epoch": 0,
            "progress_revision": 0,
            "last_receipt_type": "",
            "last_receipt_at": "",
            "last_receipt_error": "",
            "browser_cleanup_complete": False,
            "expected_job_id": "",
            "shortlist_generation": 0,
            "base_client_id": "",
            "derived_client_id": "",
            "message_hash": "",
            "send_state": "NOT_STARTED",
            "candidate_job_id": "",
            "candidate_started_at": "",
            "candidate_jd_at": "",
            "last_error_class": "",
            "last_error": "",
            "updated_at": utc_now().isoformat(),
        },
    }
    write_json(path, state)
    return {
        "ok": True,
        "batch_id": state["batch_id"],
        "target": state["target"],
        "mode": state["mode"],
        "next_city": CITY_ORDER[0],
        "jd_review_limit": review_limit,
        "unique_read_limit": state["unique_read_limit"],
        "no_send_jd_limit": state["no_send_jd_limit"],
        "keyword": state["keyword"],
    }


def batch_policy(path: Path = BATCH_STATE) -> dict:
    state = read_json(path)
    order = list(state.get("city_order") or CITY_ORDER)
    index = int(state.get("city_index") if state.get("city_index") is not None else -1)
    next_index = 0 if index < 0 else (index + 1) % len(order)
    return {
        "active": bool(state.get("active")),
        "mode": str(state.get("mode") or DEFAULT_BATCH_MODE),
        "keyword": str(state.get("keyword") or ""),
        "current_city": str(state.get("current_city") or ""),
        "next_city": order[next_index] if order else "",
        "jd_review_limit": int(state.get("jd_review_limit") or MIN_JD_REVIEW_LIMIT),
        "jd_reviewed": len(set(state.get("jd_reviewed_job_ids") or [])),
        "city_progress": city_progress(state, str(state.get("current_city") or "")),
    }


def _city_stats(state: dict, city: str) -> dict:
    all_stats = state.setdefault("city_stats", {})
    stats = all_stats.setdefault(city, {})
    stats.setdefault("unique_job_ids", [])
    stats.setdefault("list_calls", 0)
    stats.setdefault("load_calls", 0)
    stats.setdefault("growth_calls", 0)
    stats.setdefault("consecutive_no_growth", 0)
    stats.setdefault("jd_reviewed_count", 0)
    stats.setdefault("screening_count", 0)
    stats.setdefault("screening_reason_counts", {})
    stats.setdefault("last_survivor_unique_read", 0)
    stats.setdefault("last_survivor_jd_reviewed", 0)
    stats.setdefault("last_survivor_screening_count", 0)
    stats.setdefault("last_survivor_reason_counts", {})
    return stats


def city_progress(state: dict, city: str) -> dict:
    if not city:
        return {}
    stats = _city_stats(state, city)
    unique_read = len(set(stats.get("unique_job_ids") or []))
    pending_count = len(state.get("pending_jobs") or [])
    load_calls = int(stats.get("load_calls") or 0)
    no_growth = int(stats.get("consecutive_no_growth") or 0)
    deep_dive_complete = (
        pending_count == 0
        and load_calls >= MIN_CITY_LOAD_CALLS
        and (unique_read >= MIN_CITY_UNIQUE_READ or no_growth >= MIN_CITY_NO_GROWTH)
    )
    return {
        "city": city,
        "unique_read": unique_read,
        "list_calls": int(stats.get("list_calls") or 0),
        "load_calls": load_calls,
        "growth_calls": int(stats.get("growth_calls") or 0),
        "consecutive_no_growth": no_growth,
        "jd_reviewed_count": int(stats.get("jd_reviewed_count") or 0),
        "pending_count": pending_count,
        "required_unique_read": MIN_CITY_UNIQUE_READ,
        "required_load_calls": MIN_CITY_LOAD_CALLS,
        "absolute_unique_cap": MAX_CITY_UNIQUE_READ,
        "absolute_jd_cap": MAX_CITY_JD_REVIEWS,
        "deep_dive_complete": deep_dive_complete,
    }


def can_select_batch_city(city: str, path: Path = BATCH_STATE) -> dict:
    state = read_json(path)
    if not state.get("active"):
        return {"ok": False, "error": "batch_not_active"}
    order = list(state.get("city_order") or CITY_ORDER)
    if city not in order:
        return {"ok": False, "error": "city_not_allowed", "city": city}
    current = str(state.get("current_city") or "")
    index = int(state.get("city_index") if state.get("city_index") is not None else -1)
    expected = order[0] if index < 0 else order[(index + 1) % len(order)]
    if not current:
        return {"ok": city == expected, "error": "city_order_violation" if city != expected else "", "expected_city": expected}
    if city not in {current, expected}:
        return {"ok": False, "error": "city_order_violation", "expected_city": expected, "requested_city": city}
    progress = city_progress(state, current)
    fuse = combo_fuse_status(path)
    if not progress.get("deep_dive_complete") and not fuse.get("tripped"):
        return {"ok": False, "error": "city_deep_dive_incomplete", "requested_city": city, "progress": progress}
    return {"ok": True, "expected_city": expected, "progress": progress, "combo_fuse": fuse}


def can_fetch_new_listing(path: Path = BATCH_STATE) -> dict:
    state = read_json(path)
    unresolved = unresolved_shortlist(state)
    if unresolved:
        return {
            "ok": False,
            "error": "current_shortlist_not_resolved",
            "generation": int((state.get("last_batch") or {}).get("generation") or 0),
            "unresolved_count": len(unresolved),
        }
    pending = len(state.get("pending_jobs") or [])
    if pending:
        return {"ok": False, "error": "pending_shortlist_not_drained", "pending_count": pending}
    unique_limit = int(state.get("unique_read_limit") or 0)
    unique_read = len(set(state.get("unique_job_ids") or []))
    if unique_limit and unique_read >= unique_limit:
        return {"ok": False, "error": "batch_unique_read_limit_reached", "unique_read": unique_read, "limit": unique_limit}
    return {"ok": True}


def record_city_load(result: dict, path: Path = BATCH_STATE) -> dict:
    state = read_json(path)
    city = str(state.get("current_city") or "")
    if not state.get("active") or not city:
        return {"ok": False, "error": "recommend_city_not_selected"}
    stats = _city_stats(state, city)
    outcome = str((result.get("data") or {}).get("outcome") or "")
    stats["load_calls"] = int(stats.get("load_calls") or 0) + 1
    if outcome == "growth":
        stats["growth_calls"] = int(stats.get("growth_calls") or 0) + 1
        stats["consecutive_no_growth"] = 0
    elif outcome == "no_growth_after_retry":
        stats["consecutive_no_growth"] = int(stats.get("consecutive_no_growth") or 0) + 1
        _telemetry_apply(state, "load_retry")
    write_json(path, state)
    return {"ok": True, "progress": city_progress(state, city)}


def select_batch_city(city: str, path: Path = BATCH_STATE, allow_current_recovery: bool = False) -> dict:
    state = read_json(path)
    current = str(state.get("current_city") or "")
    guard = can_select_batch_city(city, path)
    if not guard.get("ok"):
        safe_same_city = (
            allow_current_recovery
            and current
            and city == current
            and not state.get("pending_jobs")
            and not unresolved_shortlist(state)
        )
        if not safe_same_city:
            return guard
    order = list(state.get("city_order") or CITY_ORDER)
    if city not in order:
        return {"ok": False, "error": "city_not_allowed", "city": city}
    index = int(state.get("city_index") if state.get("city_index") is not None else -1)
    expected = order[0] if index < 0 else order[(index + 1) % len(order)]
    if current and city == current:
        state["pending_jobs"] = []
        state["pending_meta"] = {}
        state["last_batch"] = {}
        write_json(path, state)
        return {"ok": True, "city": city, "refreshed": True, "next_city": expected}
    if city != expected:
        return {
            "ok": False,
            "error": "city_order_violation",
            "expected_city": expected,
            "requested_city": city,
        }
    state["city_index"] = order.index(city)
    state["current_city"] = city
    _city_stats(state, city)
    state["pending_jobs"] = []
    state["pending_meta"] = {}
    state["last_batch"] = {}
    history = list(state.get("city_history") or [])
    history.append({"city": city, "selected_at": utc_now().isoformat()})
    state["city_history"] = history[-40:]
    write_json(path, state)
    next_city = order[(state["city_index"] + 1) % len(order)]
    return {"ok": True, "city": city, "refreshed": False, "next_city": next_city}


def job_in_last_shortlist(job_id: str, path: Path = BATCH_STATE, generation: int | None = None) -> bool:
    state = read_json(path)
    last = state.get("last_batch") or {}
    if generation is not None and int(last.get("generation") or 0) != int(generation):
        return False
    expected = normalize(job_id)
    return bool(expected) and any(
        normalize(job.get("job_id")) == expected
        for job in last.get("jobs") or []
    )


def can_review_jd(job_id: str, path: Path = BATCH_STATE) -> dict:
    state = read_json(path)
    reviewed = set(state.get("jd_reviewed_job_ids") or [])
    normalized = normalize(job_id)
    limit = int(state.get("jd_review_limit") or MIN_JD_REVIEW_LIMIT)
    if normalized and normalized in reviewed:
        return {"ok": True, "already_counted": True, "reviewed": len(reviewed), "limit": limit}
    current_city = str(state.get("current_city") or "")
    if current_city:
        city_reviewed = int(_city_stats(state, current_city).get("jd_reviewed_count") or 0)
        if city_reviewed >= MAX_CITY_JD_REVIEWS:
            return {
                "ok": False,
                "error": "city_absolute_jd_cap",
                "city": current_city,
                "reviewed": city_reviewed,
                "limit": MAX_CITY_JD_REVIEWS,
            }
    if len(reviewed) >= limit:
        return {"ok": False, "error": "jd_review_limit_reached", "reviewed": len(reviewed), "limit": limit}
    no_send_limit = int(state.get("no_send_jd_limit") or 0)
    since_send = len(reviewed) - int(state.get("jd_reviews_at_last_send") or 0)
    if no_send_limit and since_send >= no_send_limit:
        return {"ok": False, "error": "no_send_jd_fuse_reached", "reviewed_since_send": since_send, "limit": no_send_limit}
    return {"ok": True, "already_counted": False, "reviewed": len(reviewed), "limit": limit}


def record_jd_review(job_id: str, path: Path = BATCH_STATE) -> dict:
    state = read_json(path)
    normalized = normalize(job_id)
    reviewed = set(state.get("jd_reviewed_job_ids") or [])
    was_new = bool(normalized and normalized not in reviewed)
    if normalized:
        reviewed.add(normalized)
    state["jd_reviewed_job_ids"] = sorted(reviewed)
    if was_new:
        _telemetry_apply(state, "jd_review")
        current_city = str(state.get("current_city") or "")
        if current_city:
            stats = _city_stats(state, current_city)
            stats["jd_reviewed_count"] = int(stats.get("jd_reviewed_count") or 0) + 1
    write_json(path, state)
    return {"ok": True, "reviewed": len(reviewed), "limit": int(state.get("jd_review_limit") or MIN_JD_REVIEW_LIMIT)}


def record_combo_survivor(city: str = "", path: Path = BATCH_STATE) -> dict:
    state = read_json(path)
    city = str(city or state.get("current_city") or "")
    if not state.get("active") or not city:
        return {"ok": False, "error": "batch_or_city_not_active"}
    survivors = state.setdefault("combo_survivors", {})
    survivors[city] = int(survivors.get(city) or 0) + 1
    stats = _city_stats(state, city)
    # The fuse is rolling: a previous survivor must not keep a low-yield
    # keyword/city combination alive forever.  Snapshot the current city
    # progress so the next fuse check measures only new work after this point.
    stats["last_survivor_unique_read"] = len(set(stats.get("unique_job_ids") or []))
    stats["last_survivor_jd_reviewed"] = _city_jd_reviewed_count(state, city)
    stored_screening_count = int(stats.get("screening_count") or 0)
    stats["last_survivor_screening_count"] = (
        stored_screening_count
        if stored_screening_count
        else _city_screening_count(state, city)
    )
    stats["last_survivor_reason_counts"] = dict(
        stats.get("screening_reason_counts") or {}
    )
    write_json(path, state)
    return {
        "ok": True,
        "city": city,
        "survivors": survivors[city],
        "last_survivor_unique_read": stats["last_survivor_unique_read"],
        "last_survivor_jd_reviewed": stats["last_survivor_jd_reviewed"],
    }


def _city_screening_rows(state: dict, city: str, screening_path: Path = SCREENING) -> list[dict]:
    baseline_ids = {normalize(item) for item in state.get("baseline_screened_ids") or []}
    return [
        entry
        for entry in read_entries(screening_path)
        if normalize(entry.get("city")) == normalize(city)
        and str(entry.get("feed") or "").endswith(f"|{state.get('keyword')}")
        and normalize(entry.get("job_id")) not in baseline_ids
    ]


def _city_screening_count(state: dict, city: str, screening_path: Path = SCREENING) -> int:
    return len(_city_screening_rows(state, city, screening_path))


def _city_jd_reviewed_count(state: dict, city: str, screening_path: Path = SCREENING) -> int:
    reviewed_ids = {normalize(item) for item in state.get("jd_reviewed_job_ids") or []}
    derived = len({
        normalize(entry.get("job_id"))
        for entry in _city_screening_rows(state, city, screening_path)
        if normalize(entry.get("job_id")) in reviewed_ids
    })
    stored = int(_city_stats(state, city).get("jd_reviewed_count") or 0)
    return max(derived, stored)


def combo_fuse_status(path: Path = BATCH_STATE, screening_path: Path = SCREENING) -> dict:
    state = read_json(path)
    city = str(state.get("current_city") or "")
    if not state.get("active") or not city:
        return {"ok": False, "error": "batch_or_city_not_active"}
    policy = state.get("combo_policy") or {}
    stats = _city_stats(state, city)
    unique_read = len(set(stats.get("unique_job_ids") or []))
    current_rows = _city_screening_rows(state, city, screening_path)
    reviewed_ids = {normalize(item) for item in state.get("jd_reviewed_job_ids") or []}
    derived_jd_in_city = len({
        normalize(entry.get("job_id")) for entry in current_rows
        if normalize(entry.get("job_id")) in reviewed_ids
    })
    jd_in_city = max(
        derived_jd_in_city,
        int(stats.get("jd_reviewed_count") or 0),
    )
    survivors = int((state.get("combo_survivors") or {}).get(city) or 0)
    last_unique = int(stats.get("last_survivor_unique_read") or 0)
    last_jd = int(stats.get("last_survivor_jd_reviewed") or 0)
    last_screening_count = int(stats.get("last_survivor_screening_count") or 0)
    unique_since_survivor = max(0, unique_read - last_unique)
    jd_since_survivor = max(0, jd_in_city - last_jd)
    reason_counts: dict[str, int] = {}
    stored_screening_count = int(stats.get("screening_count") or 0)
    if stored_screening_count:
        # New batches keep only compact per-city reason counters. Subtract
        # the snapshot taken at the last survivor to preserve the rolling
        # same-reason fuse without requiring one file row per candidate.
        previous = stats.get("last_survivor_reason_counts") or {}
        for reason, count in (stats.get("screening_reason_counts") or {}).items():
            delta = max(0, int(count or 0) - int(previous.get(reason) or 0))
            if delta:
                reason_counts[str(reason)] = delta
    else:
        # Legacy batches may not have the compact counters yet; retain the
        # existing screening-file fallback for backward compatibility.
        rows_since_survivor = current_rows[last_screening_count:] if last_screening_count <= len(current_rows) else []
        for entry in rows_since_survivor:
            reason = str(entry.get("reason") or "unknown")
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    dominant_reason = max(reason_counts, key=reason_counts.get, default="")
    dominant_count = int(reason_counts.get(dominant_reason) or 0)
    reasons: list[str] = []
    if unique_since_survivor >= int(policy.get("max_unique_without_survivor") or 60):
        reasons.append("unique_without_survivor")
    if jd_since_survivor >= int(policy.get("max_jd_without_survivor") or 8):
        reasons.append("jd_without_survivor")
    if dominant_count >= int(policy.get("max_same_reason_streak") or 40):
        reasons.append(f"dominant_reason:{dominant_reason}")
    if unique_read >= MAX_CITY_UNIQUE_READ:
        reasons.append("city_absolute_unique_cap")
    if jd_in_city >= MAX_CITY_JD_REVIEWS:
        reasons.append("city_absolute_jd_cap")
    return {
        "ok": True,
        "tripped": bool(reasons),
        "city": city,
        "unique_read": unique_read,
        "jd_reviewed": jd_in_city,
        "unique_since_survivor": unique_since_survivor,
        "jd_since_survivor": jd_since_survivor,
        "survivors": survivors,
        "dominant_reason": dominant_reason,
        "dominant_count": dominant_count,
        "reasons": reasons,
    }


def card_priority(job: dict) -> int:
    return jobflow.priority(job,"boss")


def _take_shortlist(state: dict, limit: int = SHORTLIST_LIMIT) -> list[dict]:
    pending = list(state.get("pending_jobs") or [])
    pending.sort(key=card_priority, reverse=True)
    selected = pending[: max(1, int(limit))]
    state["pending_jobs"] = pending[len(selected):]

    seen_ids = set(state.get("seen_output_job_ids") or [])
    presented_ids = set(state.get("presented_job_ids") or [])
    for job in selected:
        job_id = normalize(job.get("job_id"))
        if job_id:
            seen_ids.add(job_id)
            presented_ids.add(job_id)
    state["seen_output_job_ids"] = sorted(seen_ids)
    state["presented_job_ids"] = sorted(presented_ids)
    return selected


def _save_last_shortlist(state: dict, jobs: list[dict], meta: dict) -> None:
    generation = int(state.get("shortlist_generation") or 0) + 1
    state["shortlist_generation"] = generation
    state["last_batch"] = {
        "generation": generation,
        "feed": str(meta.get("feed") or "")[:240],
        "offset": int(meta.get("offset") or 0),
        "limit": int(meta.get("limit") or 20),
        "total_loaded": int(meta.get("total_loaded") or 0),
        "jobs": jobs,
        "resolutions": {},
    }


def unresolved_shortlist(state: dict) -> list[dict]:
    last = state.get("last_batch") or {}
    resolutions = last.get("resolutions") or {}
    return [
        job for job in last.get("jobs") or []
        if normalize(job.get("job_id")) not in resolutions
    ]


def resolve_candidate(
    token: str,
    generation: int,
    job_id: str,
    outcome: str,
    path: Path = BATCH_STATE,
    reason: str = "",
) -> dict:
    state = read_json(path)
    guard = assert_execution_lease(state, token)
    if not guard.get("ok"):
        return guard
    last = state.get("last_batch") or {}
    current_generation = int(last.get("generation") or 0)
    if current_generation != int(generation) or not job_in_last_shortlist(job_id, path, generation):
        return {
            "ok": False,
            "error": "stale_shortlist_generation_or_job",
            "requested_generation": int(generation),
            "current_generation": current_generation,
            "job_id": job_id,
        }
    outcome = str(outcome or "").upper()
    if outcome not in {"HARD_FILTER", "JD_DECIDED", "SENT", "DUPLICATE"}:
        return {"ok": False, "error": "invalid_candidate_resolution", "outcome": outcome}
    normalized = normalize(job_id)
    resolutions = last.setdefault("resolutions", {})
    if normalized in resolutions:
        return {"ok": True, "already_resolved": True, "resolution": resolutions[normalized]}
    if outcome == "JD_DECIDED" and normalized not in set(state.get("jd_reviewed_job_ids") or []):
        return {"ok": False, "error": "jd_decision_requires_recorded_review", "job_id": job_id}
    if outcome == "SENT" and not find_duplicate(job_id=job_id):
        return {"ok": False, "error": "sent_resolution_requires_receipt", "job_id": job_id}
    resolutions[normalized] = {"outcome": outcome, "resolved_at": utc_now().isoformat()}
    timing = _record_candidate_timing(state, outcome)
    execution = state.setdefault("execution", {})
    if normalize(execution.get("candidate_job_id")) == normalized:
        execution["candidate_job_id"] = ""
        execution["candidate_started_at"] = ""
        execution["candidate_jd_at"] = ""
    if outcome in {"HARD_FILTER", "DUPLICATE"}:
        _telemetry_apply(state, "candidate_skip")
        _record_screening_reasons(state, reason=reason or outcome.lower())
    state["last_batch"] = last
    write_json(path, state)
    return {
        "ok": True,
        "generation": current_generation,
        "job_id": job_id,
        "outcome": outcome,
        "unresolved_count": len(unresolved_shortlist(state)),
        "candidate_timing": timing,
    }


def filter_batch_listing(
    raw_data: dict,
    filtered_data: dict,
    path: Path = BATCH_STATE,
) -> dict:
    state = read_json(path)
    if not state.get("active"):
        return filtered_data

    raw_ids = {
        normalize(job.get("job_id"))
        for job in raw_data.get("jobs") or []
        if normalize(job.get("job_id"))
    }
    unique_ids = set(state.get("unique_job_ids") or [])
    unique_ids.update(raw_ids)
    seen_ids = set(state.get("seen_output_job_ids") or [])
    pending = list(state.get("pending_jobs") or [])
    pending_ids = {
        normalize(job.get("job_id")) for job in pending if normalize(job.get("job_id"))
    }
    batch_duplicates = 0
    for job in filtered_data.get("jobs") or []:
        job_id = normalize(job.get("job_id"))
        if job_id and (job_id in seen_ids or job_id in pending_ids):
            batch_duplicates += 1
            continue
        pending.append(job)
        if job_id:
            pending_ids.add(job_id)

    state["raw_occurrences"] = int(state.get("raw_occurrences") or 0) + len(
        raw_data.get("jobs") or []
    )
    state["list_calls"] = int(state.get("list_calls") or 0) + 1
    state["unique_job_ids"] = sorted(unique_ids)
    current_city = str(state.get("current_city") or "")
    if current_city:
        stats = _city_stats(state, current_city)
        city_ids = set(stats.get("unique_job_ids") or [])
        city_ids.update(raw_ids)
        stats["unique_job_ids"] = sorted(city_ids)
        stats["list_calls"] = int(stats.get("list_calls") or 0) + 1
    state["pending_jobs"] = pending
    state["batch_duplicates_filtered"] = int(
        state.get("batch_duplicates_filtered") or 0
    ) + batch_duplicates
    _record_screening_reasons(state, filtered_data.get("hard_filter_counts") or {})
    meta = {
        "feed": str(filtered_data.get("feed") or "")[:240],
        "offset": int(filtered_data.get("offset") or 0),
        "limit": int(filtered_data.get("limit") or 20),
        "total_loaded": int(filtered_data.get("total_loaded") or 0),
    }
    state["pending_meta"] = meta
    kept = _take_shortlist(state)
    _save_last_shortlist(state, kept, meta)
    write_json(path, state)
    return {
        **filtered_data,
        "generation": int((state.get("last_batch") or {}).get("generation") or 0),
        "jobs": kept,
        "count": len(kept),
        "shortlist_limit": SHORTLIST_LIMIT,
        "pending_count": len(state.get("pending_jobs") or []),
        "batch_duplicates_filtered": batch_duplicates,
        "batch_unique_read": len(unique_ids),
        "city_progress": city_progress(state, current_city),
    }


def next_shortlist_listing(path: Path = BATCH_STATE) -> dict:
    state = read_json(path)
    if not state.get("active"):
        return {"ok": False, "error": "batch_not_active", "jobs": [], "count": 0}
    unresolved = unresolved_shortlist(state)
    if unresolved:
        return {
            "ok": False,
            "error": "current_shortlist_not_resolved",
            "generation": int((state.get("last_batch") or {}).get("generation") or 0),
            "unresolved_job_ids": [str(job.get("job_id") or "") for job in unresolved],
            "unresolved_count": len(unresolved),
        }
    # Once an absolute city cap is reached, pending cards have not been
    # selected or JD-reviewed. Discard them as bounded deferrals so the
    # executor can rotate without spending another shortlist/JD read. The
    # rolling low-yield fuse still drains pending cards normally.
    fuse = combo_fuse_status(path)
    absolute_reasons = {
        "city_absolute_unique_cap",
        "city_absolute_jd_cap",
    }
    if fuse.get("tripped") and absolute_reasons.intersection(fuse.get("reasons") or []):
        dropped = len(state.get("pending_jobs") or [])
        state["pending_jobs"] = []
        state["pending_meta"] = {}
        write_json(path, state)
        return {
            "generation": int((state.get("last_batch") or {}).get("generation") or 0),
            "feed": "",
            "offset": 0,
            "limit": 20,
            "total_loaded": 0,
            "count": 0,
            "shortlist_limit": SHORTLIST_LIMIT,
            "pending_count": 0,
            "shortlist_exhausted": True,
            "city_cap_drained": True,
            "dropped_pending_count": dropped,
            "combo_fuse": fuse,
            "jobs": [],
        }
    meta = state.get("pending_meta") or {}
    jobs = _take_shortlist(state)
    _save_last_shortlist(state, jobs, meta)
    write_json(path, state)
    return {
        "generation": int((state.get("last_batch") or {}).get("generation") or 0),
        "feed": meta.get("feed", ""),
        "offset": int(meta.get("offset") or 0),
        "limit": int(meta.get("limit") or 20),
        "total_loaded": int(meta.get("total_loaded") or 0),
        "count": len(jobs),
        "shortlist_limit": SHORTLIST_LIMIT,
        "pending_count": len(state.get("pending_jobs") or []),
        "shortlist_exhausted": not jobs,
        "jobs": jobs,
    }


def requeue_selected_job(
    token: str,
    job: dict,
    path: Path = BATCH_STATE,
) -> dict:
    """Put one user-selected current-city job into a fresh shortlist."""
    state = read_json(path)
    guard = assert_execution_lease(state, token)
    if not guard.get("ok"):
        return guard
    current_city = str(state.get("current_city") or "")
    city = str(job.get("city") or "")
    if not current_city or city != current_city:
        return {
            "ok": False,
            "error": "selected_job_city_mismatch",
            "current_city": current_city,
            "job_city": city,
        }
    keyword = str(state.get("keyword") or "")
    feed = str(job.get("feed") or "")
    if keyword and keyword.casefold() not in feed.casefold():
        return {
            "ok": False,
            "error": "selected_job_keyword_mismatch",
            "keyword": keyword,
            "feed": feed,
        }
    unresolved = unresolved_shortlist(state)
    if unresolved:
        return {
            "ok": False,
            "error": "current_shortlist_not_resolved",
            "unresolved_count": len(unresolved),
        }
    job_id = normalize(job.get("job_id"))
    title = str(job.get("title") or "").strip()
    company = str(job.get("company") or "").strip()
    if not job_id or not title or not company:
        return {"ok": False, "error": "selected_job_identity_incomplete"}
    if find_duplicate(job_id=job.get("job_id"), company=company, job_title=title):
        return {"ok": False, "error": "selected_job_already_receipted", "job_id": job.get("job_id")}
    existing_ids = {
        normalize(item.get("job_id"))
        for item in (state.get("pending_jobs") or []) + (state.get("last_batch") or {}).get("jobs", [])
    }
    if job_id in existing_ids:
        return {"ok": False, "error": "selected_job_already_queued", "job_id": job.get("job_id")}
    candidate = {
        "job_id": str(job.get("job_id") or "").strip(),
        "title": title[:120],
        "summary": str(job.get("summary") or "").strip()[:500],
        "href": str(job.get("href") or "").strip()[:500],
        "company": company[:120],
        "city": city[:40],
        "salary": str(job.get("salary") or "").strip()[:80],
    }
    state["presented_job_ids"] = sorted(set(state.get("presented_job_ids") or []) | {job_id})
    state["seen_output_job_ids"] = sorted(set(state.get("seen_output_job_ids") or []) | {job_id})
    prior_meta = state.get("pending_meta") or {}
    meta = {
        "feed": str(prior_meta.get("feed") or feed)[:240],
        "offset": int(prior_meta.get("offset") or 0),
        "limit": int(prior_meta.get("limit") or 20),
        "total_loaded": int(prior_meta.get("total_loaded") or 0),
    }
    _save_last_shortlist(state, [candidate], meta)
    state.setdefault("pending_meta", meta)
    write_json(path, state)
    updated = update_execution(
        token,
        "SHORTLIST",
        {
            "shortlist_generation": int((state.get("last_batch") or {}).get("generation") or 0),
            "expected_job_id": "",
            "last_error_class": "",
            "last_error": "",
        },
        path,
    )
    if not updated.get("ok"):
        return updated
    return {
        "ok": True,
        "job_id": candidate["job_id"],
        "generation": int((state.get("last_batch") or {}).get("generation") or 0),
        "city": city,
        "keyword": keyword,
    }


def last_batch_listing(path: Path = BATCH_STATE) -> dict:
    state = read_json(path)
    last = state.get("last_batch") or {}
    jobs = last.get("jobs") or []
    return {
        "generation": int(last.get("generation") or 0),
        "feed": last.get("feed", ""),
        "offset": int(last.get("offset") or 0),
        "limit": int(last.get("limit") or 20),
        "total_loaded": int(last.get("total_loaded") or 0),
        "count": len(jobs),
        "has_more": False,
        "replayed": True,
        "jobs": jobs,
    }


def batch_summary(path: Path = BATCH_STATE) -> dict:
    state = read_json(path)
    if not state.get("active"):
        return {"ok": True, "active": False}
    baseline_receipts = set(state.get("baseline_receipts") or [])
    new_receipts = [
        entry for entry in read_entries() if receipt_token(entry) not in baseline_receipts
    ]
    tracked_reviews = set(state.get("jd_reviewed_job_ids") or [])
    unique_ids = set(state.get("unique_job_ids") or [])
    presented_ids = set(state.get("presented_job_ids") or [])
    pending_ids = {
        normalize(job.get("job_id"))
        for job in state.get("pending_jobs") or []
        if normalize(job.get("job_id"))
    }
    checkpoint = state.get("checkpoint") or {}
    return {
        "ok": True,
        "active": True,
        "batch_id": state.get("batch_id"),
        "target": int(state.get("target") or 0),
        "confirmed": len(new_receipts),
        "unique_read": len(unique_ids),
        "raw_occurrences": int(state.get("raw_occurrences") or 0),
        "presented_unique": len(presented_ids),
        "deferred_unique": len(pending_ids),
        "base_filtered_unique": max(
            0, len(unique_ids) - len(presented_ids) - len(pending_ids)
        ),
        "jd_reviewed_unique": len(tracked_reviews),
        "jd_review_limit": int(state.get("jd_review_limit") or MIN_JD_REVIEW_LIMIT),
        "unique_read_limit": int(state.get("unique_read_limit") or 0),
        "no_send_jd_limit": int(state.get("no_send_jd_limit") or 0),
        "jd_reviews_since_send": len(tracked_reviews) - int(state.get("jd_reviews_at_last_send") or 0),
        "mode": str(state.get("mode") or DEFAULT_BATCH_MODE),
        "keyword": str(state.get("keyword") or ""),
        "current_city": str(state.get("current_city") or ""),
        "city_progress": city_progress(state, str(state.get("current_city") or "")),
        "cross_call_duplicates_filtered": int(
            state.get("batch_duplicates_filtered") or 0
        ),
        "list_calls": int(state.get("list_calls") or 0),
        "telemetry": state.get("telemetry") or _telemetry_defaults(),
        "screening_reason_counts": state.get("screening_reason_counts") or {},
        "checkpoint": checkpoint,
        "execution": state.get("execution") or {},
    }


def _parse_iso(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def acquire_execution(owner: str, ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS, path: Path = BATCH_STATE) -> dict:
    with lease_state_lock(path):
        return _acquire_execution_unlocked(owner, ttl_seconds, path)


def _acquire_execution_unlocked(owner: str, ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS, path: Path = BATCH_STATE) -> dict:
    state = read_json(path)
    if not state.get("active"):
        return {"ok": False, "error": "batch_not_active"}
    now = utc_now()
    execution = state.setdefault("execution", {})
    current_owner = str(execution.get("owner") or "")
    expires = _parse_iso(execution.get("lease_expires_at"))
    # Acquisition is idempotent for the current owner while its lease is
    # valid.  Rotating the token on a harmless re-acquire invalidates the
    # executor's still-live token and was the source of avoidable
    # execution_lease_mismatch stops after long continuations.
    if current_owner == owner and execution.get("lease_token") and expires and expires > now:
        renewed = False
        if expires - now <= timedelta(seconds=LEASE_RENEW_SAFETY_SECONDS):
            execution["lease_expires_at"] = (
                now + timedelta(seconds=max(60, int(ttl_seconds)))
            ).isoformat()
            execution["updated_at"] = now.isoformat()
            write_json(path, state)
            expires = _parse_iso(execution["lease_expires_at"])
            renewed = True
        return {
            "ok": True,
            "batch_id": state.get("batch_id"),
            "owner": current_owner,
            "lease_token": str(execution.get("lease_token") or ""),
            "lease_expires_at": expires.isoformat(),
            "phase": execution.get("phase"),
            "executor_epoch": int(execution.get("executor_epoch") or 0),
            "reused": True,
            "renewed": renewed,
        }
    if current_owner and current_owner != owner and expires and expires > now:
        return {
            "ok": False,
            "error": "execution_lease_held",
            "owner": current_owner,
            "lease_expires_at": expires.isoformat(),
        }
    phase = str(execution.get("phase") or "PREFLIGHT").upper()
    if current_owner == owner and expires and expires <= now and phase in SEND_SENSITIVE_PHASES:
        return {
            "ok": False,
            "error": "send_sensitive_lease_reacquire_requires_reconcile",
            "phase": phase,
            "send_state": str(execution.get("send_state") or ""),
        }
    if current_owner and current_owner != owner and expires and expires <= now and phase not in SAFE_REACQUIRE_PHASES:
        return {"ok": False, "error": "unsafe_phase_for_lease_takeover", "phase": phase}
    token = uuid.uuid4().hex
    next_epoch = int(execution.get("executor_epoch") or 0) + 1
    prior_token = str(execution.get("lease_token") or "")
    execution.update({
        "owner": str(owner or "executor")[:120],
        "lease_token": token,
        "previous_lease_token_hash": lease_token_digest(prior_token) if prior_token else "",
        "lease_expires_at": (now + timedelta(seconds=max(60, int(ttl_seconds)))).isoformat(),
        "executor_epoch": next_epoch,
        "last_receipt_error": "",
        "updated_at": now.isoformat(),
    })
    execution.setdefault("phase", "PREFLIGHT")
    execution.setdefault("send_state", "NOT_STARTED")
    _telemetry_apply(state, "lease_acquire")
    write_json(path, state)
    return {
        "ok": True,
        "batch_id": state.get("batch_id"),
        "owner": execution["owner"],
        "lease_token": token,
        "lease_expires_at": execution["lease_expires_at"],
        "phase": execution.get("phase"),
        "executor_epoch": next_epoch,
    }


def reacquire_execution(
    owner: str,
    previous_token: str,
    ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    path: Path = BATCH_STATE,
) -> dict:
    with lease_state_lock(path):
        return _reacquire_execution_unlocked(owner, previous_token, ttl_seconds, path)


def _reacquire_execution_unlocked(
    owner: str,
    previous_token: str,
    ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    path: Path = BATCH_STATE,
) -> dict:
    """Recover one stale safe-phase token without touching the browser.

    This is deliberately narrower than normal acquisition: the same logical
    owner and a caller-held previous token are required, and send-sensitive
    phases never permit takeover.  If another same-owner acquisition already
    rotated the token, the current valid token is adopted instead of rotating
    it again.  A new token is minted only after a safe-phase expiry.
    """
    state = read_json(path)
    if not state.get("active"):
        return {"ok": False, "error": "batch_not_active"}
    owner = str(owner or "").strip()
    previous_token = str(previous_token or "").strip()
    execution = state.setdefault("execution", {})
    current_owner = str(execution.get("owner") or "")
    current_token = str(execution.get("lease_token") or "")
    phase = str(execution.get("phase") or "PREFLIGHT").upper()
    expires = _parse_iso(execution.get("lease_expires_at"))
    now = utc_now()
    if not owner or not previous_token:
        return {"ok": False, "error": "lease_recovery_requires_owner_and_previous_token"}
    if current_owner != owner:
        if current_owner and expires and expires > now:
            return {
                "ok": False,
                "error": "execution_lease_held",
                "owner": current_owner,
                "lease_expires_at": expires.isoformat(),
            }
        return {
            "ok": False,
            "error": "execution_lease_owner_mismatch",
            "owner": current_owner,
            "requested_owner": owner,
        }
    if current_token == previous_token and expires and expires > now:
        # The caller still owns the live token; renew it without changing the
        # token or executor epoch.
        execution["lease_expires_at"] = (
            now + timedelta(seconds=max(60, int(ttl_seconds)))
        ).isoformat()
        execution["updated_at"] = now.isoformat()
        write_json(path, state)
        return {
            "ok": True,
            "batch_id": state.get("batch_id"),
            "owner": current_owner,
            "lease_token": current_token,
            "lease_expires_at": execution["lease_expires_at"],
            "phase": phase,
            "executor_epoch": int(execution.get("executor_epoch") or 0),
            "reused": True,
            "renewed": True,
        }
    if phase in SEND_SENSITIVE_PHASES:
        return {
            "ok": False,
            "error": "send_sensitive_lease_reacquire_requires_reconcile",
            "phase": phase,
            "send_state": str(execution.get("send_state") or ""),
        }
    if not current_token:
        return {"ok": False, "error": "execution_lease_not_held", "phase": phase}

    # A valid current token means another same-owner acquisition already
    # repaired the session.  Adopt it without a second rotation.
    if expires and expires > now and current_token != previous_token:
        prior_hash = str(execution.get("previous_lease_token_hash") or "")
        if prior_hash and prior_hash != lease_token_digest(previous_token):
            return {
                "ok": False,
                "error": "lease_recovery_token_unknown",
                "phase": phase,
                "executor_epoch": int(execution.get("executor_epoch") or 0),
            }
        return {
            "ok": True,
            "batch_id": state.get("batch_id"),
            "owner": current_owner,
            "lease_token": current_token,
            "lease_expires_at": expires.isoformat(),
            "phase": phase,
            "executor_epoch": int(execution.get("executor_epoch") or 0),
            "reused": True,
            "recovered": True,
        }

    if phase not in SAFE_REACQUIRE_PHASES:
        return {"ok": False, "error": "unsafe_phase_for_lease_reacquire", "phase": phase}
    next_epoch = int(execution.get("executor_epoch") or 0) + 1
    token = uuid.uuid4().hex
    prior_token = current_token
    execution.update({
        "lease_token": token,
        "previous_lease_token_hash": lease_token_digest(prior_token) if prior_token else "",
        "lease_expires_at": (now + timedelta(seconds=max(60, int(ttl_seconds)))).isoformat(),
        "executor_epoch": next_epoch,
        "last_receipt_error": "",
        "updated_at": now.isoformat(),
    })
    _telemetry_apply(state, "lease_reacquire")
    write_json(path, state)
    return {
        "ok": True,
        "batch_id": state.get("batch_id"),
        "owner": current_owner,
        "lease_token": token,
        "lease_expires_at": execution["lease_expires_at"],
        "phase": phase,
        "executor_epoch": next_epoch,
        "recovered": True,
    }


def assert_execution_lease(state: dict, token: str) -> dict:
    execution = state.get("execution") or {}
    expires = _parse_iso(execution.get("lease_expires_at"))
    details = {
        "phase": str(execution.get("phase") or "PREFLIGHT").upper(),
        "owner": str(execution.get("owner") or ""),
        "executor_epoch": int(execution.get("executor_epoch") or 0),
        "lease_expires_at": str(execution.get("lease_expires_at") or ""),
    }
    if not token or token != execution.get("lease_token"):
        return {"ok": False, "error": "execution_lease_mismatch", **details}
    if not expires or expires <= utc_now():
        return {"ok": False, "error": "execution_lease_expired", **details}
    return {"ok": True, "execution": execution}


def update_execution(
    token: str,
    phase: str,
    values: dict | None = None,
    path: Path = BATCH_STATE,
    expected_phase: str | None = None,
    expected_generation: int | None = None,
    renew_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
) -> dict:
    state = read_json(path)
    guard = assert_execution_lease(state, token)
    if not guard.get("ok"):
        return guard
    execution = state.setdefault("execution", {})
    current_phase = str(execution.get("phase") or "PREFLIGHT").upper()
    current_generation = int(execution.get("shortlist_generation") or 0)
    if expected_phase is not None and current_phase != str(expected_phase).upper():
        return {
            "ok": False,
            "error": "execution_phase_conflict",
            "expected_phase": str(expected_phase).upper(),
            "actual_phase": current_phase,
        }
    if expected_generation is not None and current_generation != int(expected_generation):
        return {
            "ok": False,
            "error": "execution_generation_conflict",
            "expected_generation": int(expected_generation),
            "actual_generation": current_generation,
        }
    allowed = {
        "PREFLIGHT", "DISCOVER", "SHORTLIST", "SELECT", "READ_JD", "DECIDE",
        "OPEN_DETAIL", "COMPANY_CHECK", "OPEN_CHAT", "DRAFT", "SEND_VERIFY",
        "CLOSE_RETURN", "ROTATE", "PAUSED", "DONE",
    }
    phase = str(phase or "").upper()
    if phase not in allowed:
        return {"ok": False, "error": "invalid_execution_phase", "phase": phase}
    safe_keys = {
        "expected_job_id", "shortlist_generation", "base_client_id", "derived_client_id",
        "message_hash", "send_state", "last_error_class", "last_error",
    }
    execution["phase"] = phase
    for key, value in (values or {}).items():
        if key in safe_keys:
            execution[key] = value
    if phase != current_phase:
        _telemetry_apply(state, "phase")
    error_class = str((values or {}).get("last_error_class") or "").upper()
    if error_class in {"RECOVERABLE", "HARD_SAFETY", "EXECUTION_BUG"}:
        _telemetry_apply(state, "technical_error")
    elif error_class == "CANDIDATE_SKIP":
        _telemetry_apply(state, "candidate_skip")
    execution["updated_at"] = utc_now().isoformat()
    execution["lease_expires_at"] = (
        utc_now() + timedelta(seconds=max(60, int(renew_seconds)))
    ).isoformat()
    write_json(path, state)
    return {"ok": True, "batch_id": state.get("batch_id"), "execution": execution}


def release_execution(
    token: str,
    reason: str = "terminal",
    phase: str = "PAUSED",
    browser_cleanup_complete: bool = False,
    path: Path = BATCH_STATE,
) -> dict:
    """Release the single execution lease after a compact terminal receipt."""
    state = read_json(path)
    guard = assert_execution_lease(state, token)
    if not guard.get("ok"):
        return guard
    execution = state.setdefault("execution", {})
    phase = str(phase or "PAUSED").upper()
    if phase not in {"PAUSED", "DONE"}:
        return {"ok": False, "error": "invalid_release_phase", "phase": phase}
    execution.update({
        "phase": phase,
        "owner": "",
        "lease_token": "",
        "lease_expires_at": "",
        "last_receipt_type": "PRE_STOP",
        "last_receipt_at": utc_now().isoformat(),
        "last_receipt_error": "",
        "last_error": str(reason or "terminal")[:240],
        "updated_at": utc_now().isoformat(),
    })
    execution["browser_cleanup_complete"] = bool(browser_cleanup_complete)
    _telemetry_apply(state, "release")
    write_json(path, state)
    return {
        "ok": True,
        "batch_id": state.get("batch_id"),
        "phase": phase,
        "lease": "released",
        "browser_cleanup_complete": bool(browser_cleanup_complete),
        "reason": str(reason or "terminal")[:120],
    }


def _append_compact_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def observe_execution_receipt(
    receipt: dict | None = None,
    path: Path = BATCH_STATE,
    receipt_path: Path = EXECUTION_RECEIPTS,
) -> dict:
    """Validate and persist one fixed executor receipt for controller handoff."""
    if receipt is None:
        raw = os.environ.get("CODEX_BOSS_EXECUTOR_RECEIPT", "")
        if not raw.strip():
            return {"ok": False, "error": "executor_receipt_missing", "action": "reject"}
        try:
            receipt = json.loads(raw)
        except json.JSONDecodeError:
            return {"ok": False, "error": "executor_receipt_invalid_json", "action": "reject"}
    if not isinstance(receipt, dict):
        return {"ok": False, "error": "executor_receipt_not_object", "action": "reject"}

    state = read_json(path)
    if not state.get("active"):
        return {"ok": False, "error": "batch_not_active", "action": "reject"}
    execution = state.setdefault("execution", {})
    result = str(receipt.get("Result") or receipt.get("result") or "").upper()
    if result not in {"CONTINUE", "SUCCESS_HEARTBEAT", "PRE_STOP"}:
        return {"ok": False, "error": "executor_receipt_result_invalid", "action": "reject"}

    batch_id = str(receipt.get("batch_id") or receipt.get("batchId") or "")
    if batch_id != str(state.get("batch_id") or ""):
        return {"ok": False, "error": "executor_receipt_batch_mismatch", "action": "reject"}
    try:
        confirmed = int(receipt.get("confirmed"))
        revision = int(receipt.get("progress_revision"))
        epoch = int(receipt.get("executor_epoch"))
        pending_count = int(receipt.get("pending_count"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "executor_receipt_counter_invalid", "action": "reject"}
    if revision <= int(execution.get("progress_revision") or 0):
        return {"ok": False, "error": "stale_executor_receipt", "action": "reject", "progress_revision": revision}
    expected_epoch = int(execution.get("executor_epoch") or 0)
    if epoch <= 0 or epoch != expected_epoch:
        return {
            "ok": False,
            "error": "executor_receipt_epoch_mismatch",
            "action": "reject",
            "expected_epoch": expected_epoch,
            "actual_epoch": epoch,
        }
    current = batch_summary(path)
    current_confirmed = int(current.get("confirmed") or 0)
    current_pending = len(state.get("pending_jobs") or [])
    if confirmed != current_confirmed:
        return {
            "ok": False,
            "error": "executor_receipt_confirmed_mismatch",
            "action": "reject",
            "expected_confirmed": current_confirmed,
            "actual_confirmed": confirmed,
        }
    if pending_count != current_pending:
        return {
            "ok": False,
            "error": "executor_receipt_pending_mismatch",
            "action": "reject",
            "expected_pending": current_pending,
            "actual_pending": pending_count,
        }

    expected_lease = "released" if result == "PRE_STOP" else "held"
    lease = str(receipt.get("lease") or "").lower()
    if lease != expected_lease:
        return {
            "ok": False,
            "error": "executor_receipt_lease_mismatch",
            "action": "reject",
            "expected_lease": expected_lease,
            "actual_lease": lease,
        }
    cleanup = bool(receipt.get("browser_cleanup_complete", False))
    if result == "PRE_STOP":
        if not cleanup:
            return {"ok": False, "error": "executor_receipt_cleanup_missing", "action": "reject"}
        if execution.get("owner") or execution.get("lease_token"):
            return {"ok": False, "error": "executor_receipt_lease_still_held", "action": "reject"}
    else:
        expires = _parse_iso(execution.get("lease_expires_at"))
        if not execution.get("owner") or not execution.get("lease_token") or not expires or expires <= utc_now():
            return {"ok": False, "error": "executor_receipt_execution_not_held", "action": "reject"}

    if receipt.get("current_city") is not None and str(receipt.get("current_city") or "") != str(state.get("current_city") or ""):
        return {"ok": False, "error": "executor_receipt_city_mismatch", "action": "reject"}
    if receipt.get("keyword") is not None and str(receipt.get("keyword") or "") != str(state.get("keyword") or ""):
        return {"ok": False, "error": "executor_receipt_keyword_mismatch", "action": "reject"}

    compact = {
        "Result": result,
        "batch_id": batch_id,
        "confirmed": confirmed,
        "progress_revision": revision,
        "executor_epoch": epoch,
        "current_city": str(state.get("current_city") or ""),
        "keyword": str(state.get("keyword") or ""),
        "pending_count": pending_count,
        "lease": lease,
        "browser_cleanup_complete": cleanup,
        "safe_state": str(receipt.get("safe_state") or "")[:80],
        "observed_at": utc_now().isoformat(),
    }
    _append_compact_json(receipt_path, compact)
    execution.update({
        "progress_revision": revision,
        "last_receipt_type": result,
        "last_receipt_at": compact["observed_at"],
        "last_receipt_error": "",
        "browser_cleanup_complete": cleanup,
    })
    _telemetry_apply(state, "receipt")
    write_json(path, state)
    action = {
        "CONTINUE": "continue_same_executor",
        "SUCCESS_HEARTBEAT": "ack_success_heartbeat",
        "PRE_STOP": "terminal_cleanup_recorded",
    }[result]
    return {"ok": True, "action": action, "receipt": compact}


def _elapsed_seconds(start: object, end: object = None) -> float | None:
    started = _parse_iso(start)
    finished = _parse_iso(end) if end is not None else utc_now()
    if not started or not finished:
        return None
    return max(0.0, (finished - started).total_seconds())


def _record_candidate_timing(state: dict, outcome: str, finished_at: object = None) -> dict:
    """Aggregate one candidate's timing without retaining page text or a job list."""
    execution = state.setdefault("execution", {})
    started = execution.get("candidate_started_at")
    total = _elapsed_seconds(started, finished_at)
    if total is None:
        return {"tracked": False, "reason": "candidate_start_missing"}
    jd_at = execution.get("candidate_jd_at")
    claim_to_jd = _elapsed_seconds(started, jd_at) if jd_at else None
    if claim_to_jd is not None:
        total_to_jd = min(total, claim_to_jd)
        jd_to_resolution = max(0.0, total - total_to_jd)
    else:
        jd_to_resolution = None
    telemetry = _telemetry_apply(state, "candidate_timing")
    timing = _ensure_candidate_timing(telemetry)
    _timing_add(timing["overall"], total)
    normalized_outcome = str(outcome or "UNKNOWN").upper()[:40]
    bucket = timing["by_outcome"].get(normalized_outcome)
    if not isinstance(bucket, dict):
        bucket = _timing_bucket_defaults()
        timing["by_outcome"][normalized_outcome] = bucket
    _timing_add(bucket, total)
    _timing_add(timing["stages"]["claim_to_jd"], claim_to_jd)
    _timing_add(timing["stages"]["jd_to_resolution"], jd_to_resolution)
    return {
        "tracked": True,
        "total_seconds": round(max(0.0, min(total, 604800.0)), 2),
        "claim_to_jd_seconds": None if claim_to_jd is None else round(max(0.0, min(claim_to_jd, 604800.0)), 2),
        "jd_to_resolution_seconds": None if jd_to_resolution is None else round(max(0.0, min(jd_to_resolution, 604800.0)), 2),
    }


def record_candidate_jd_stage(
    token: str,
    job_id: str,
    path: Path = BATCH_STATE,
) -> dict:
    """Mark the first readable JD for the currently claimed candidate."""
    state = read_json(path)
    guard = assert_execution_lease(state, token)
    if not guard.get("ok"):
        return guard
    execution = state.setdefault("execution", {})
    expected = normalize(execution.get("candidate_job_id") or execution.get("expected_job_id"))
    if expected != normalize(job_id):
        return {
            "ok": False,
            "error": "candidate_timing_identity_mismatch",
            "expected_job_id": execution.get("candidate_job_id") or execution.get("expected_job_id"),
            "actual_job_id": job_id,
        }
    if not execution.get("candidate_started_at"):
        return {"ok": False, "error": "candidate_timing_not_started"}
    if not execution.get("candidate_jd_at"):
        execution["candidate_jd_at"] = utc_now().isoformat()
        write_json(path, state)
    return {
        "ok": True,
        "job_id": str(job_id),
        "candidate_started_at": execution.get("candidate_started_at"),
        "candidate_jd_at": execution.get("candidate_jd_at"),
    }


def claim_candidate(token: str, generation: int, job_id: str, path: Path = BATCH_STATE) -> dict:
    state = read_json(path)
    guard = assert_execution_lease(state, token)
    if not guard.get("ok"):
        return guard
    if not job_in_last_shortlist(job_id, path, generation):
        return {
            "ok": False,
            "error": "stale_shortlist_generation_or_job",
            "requested_generation": int(generation),
            "current_generation": int((state.get("last_batch") or {}).get("generation") or 0),
            "job_id": job_id,
        }
    execution = state.get("execution") or {}
    updated = update_execution(token, "SELECT", {
        "expected_job_id": str(job_id),
        "shortlist_generation": int(generation),
        "last_error_class": "",
        "last_error": "",
    }, path, expected_phase=str(execution.get("phase") or "SHORTLIST"), expected_generation=int(execution.get("shortlist_generation") or 0))
    if not updated.get("ok"):
        return updated
    current = read_json(path)
    current_execution = current.setdefault("execution", {})
    same_candidate = (
        normalize(current_execution.get("candidate_job_id")) == normalize(job_id)
        and bool(current_execution.get("candidate_started_at"))
    )
    if not same_candidate:
        current_execution["candidate_job_id"] = str(job_id)
        current_execution["candidate_started_at"] = utc_now().isoformat()
        current_execution["candidate_jd_at"] = ""
        write_json(path, current)
    updated["candidate_timing"] = {
        "job_id": str(job_id),
        "started_at": current_execution.get("candidate_started_at"),
    }
    return updated


def close_batch(
    reason: str = "superseded",
    path: Path = BATCH_STATE,
    history_path: Path = BATCH_HISTORY,
) -> dict:
    state = read_json(path)
    if not state.get("active"):
        return {"ok": False, "error": "batch_not_active", "batch_id": state.get("batch_id")}
    execution = state.get("execution") or {}
    owner = str(execution.get("owner") or "")
    lease_token = str(execution.get("lease_token") or "")
    expires = parse_timestamp(execution.get("lease_expires_at"))
    if owner and lease_token and (expires is None or expires > utc_now()):
        return {
            "ok": False,
            "error": "batch_execution_lease_held",
            "batch_id": state.get("batch_id"),
            "owner": owner,
            "phase": execution.get("phase", ""),
            "lease_expires_at": execution.get("lease_expires_at", ""),
        }
    summary = batch_summary(path)
    closed_at = utc_now().isoformat()
    archive = {
        "version": 1,
        "batch_id": state.get("batch_id"),
        "started_at": state.get("started_at"),
        "closed_at": closed_at,
        "close_reason": str(reason or "superseded")[:120],
        "summary": summary,
        "state": state,
    }
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(archive, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    state["active"] = False
    state["closed_at"] = closed_at
    state["close_reason"] = archive["close_reason"]
    state["archived_to"] = str(history_path)
    write_json(path, state)
    return {
        "ok": True,
        "active": False,
        "batch_id": state.get("batch_id"),
        "close_reason": state["close_reason"],
        "history_path": str(history_path),
        "summary": summary,
    }


def set_checkpoint(values: dict, path: Path = BATCH_STATE) -> dict:
    state = read_json(path)
    if not state.get("active"):
        return {"ok": False, "error": "batch_not_active"}
    checkpoint = {
        "count": str(values.get("count") or "")[:20],
        "pool": str(values.get("pool") or "")[:120],
        "offset": int(values.get("offset") or 0),
        "base": str(values.get("base") or "")[:120],
        "job_id": str(values.get("job_id") or "")[:160],
        "message_hash": str(values.get("message_hash") or "")[:128],
        "receipt_status": str(values.get("receipt_status") or "")[:40],
        "safe_state": str(values.get("safe_state") or "")[:80],
        "updated_at": utc_now().isoformat(),
    }
    state["checkpoint"] = checkpoint
    write_json(path, state)
    return {"ok": True, "checkpoint": checkpoint}


def active_screening(
    path: Path = SCREENING,
    ttl_days: int = SCREENING_TTL_DAYS,
    now: datetime | None = None,
) -> dict[str, dict]:
    cutoff = (now or utc_now()) - timedelta(days=max(1, ttl_days))
    latest: dict[str, dict] = {}
    for entry in read_entries(path):
        job_id = normalize(entry.get("job_id"))
        screened_at = parse_timestamp(entry.get("screened_at"))
        if not job_id or not screened_at or screened_at < cutoff:
            continue
        previous = latest.get(job_id)
        if not previous or screened_at >= (parse_timestamp(previous.get("screened_at")) or cutoff):
            latest[job_id] = entry
    return latest


HEADHUNTER_CARD_MARKER = re.compile(r"猎头", re.IGNORECASE)
PUBLIC_EMPLOYER_MARKER = re.compile(
    r"国企|国有企业|国有控股|国有独资|国有资本|央企|中央企业|地方国企|省属国企|市属国企|区属国企|"
    r"国资委|事业单位|机关单位|政府机关|行政单位|公务员|公立医院|公办高校|政府机构",
    re.IGNORECASE,
)
CARD_STRONG_APPLICATION_ANCHOR = re.compile(r"(?!)")
CARD_VENDOR_MARKER = re.compile(r"(?!)")
CARD_UNRELATED_SPECIALIZATION = re.compile(r"(?!)")
# BOSS sometimes obfuscates salary digits in card payloads with a private-use
# glyph range (U+E030..U+E039). Decode only for the local salary gate; visible
# titles, company names, and message text must remain untouched.
CARD_PUA_DIGIT_TRANSLATION = str.maketrans(
    {chr(0xE030 + digit): str(digit) for digit in range(10)}
)
CARD_SALARY_RANGE = re.compile(
    r"(?<!\d)(\d+(?:\.\d+)?)\s*[-~—到]\s*(\d+(?:\.\d+)?)\s*(K|千|万)", re.IGNORECASE
)


def _card_salary_upper(job: dict) -> float:
    text = f"{job.get('salary', '')} {job.get('salary_raw', '')} {job.get('summary', '')}"
    # Decode the platform's PUA digit obfuscation before applying the strict
    # monthly-K range parser. Unknown formats (for example daily pay) remain
    # unparsed and therefore reviewable rather than being guessed.
    text = text.translate(CARD_PUA_DIGIT_TRANSLATION)
    values = []
    for match in CARD_SALARY_RANGE.finditer(text):
        try:
            upper = float(match.group(2))
            if match.group(3) == "万":
                upper *= 10
            values.append(upper)
        except (TypeError, ValueError):
            continue
    return max(values, default=0.0)


def _card_has_target_city(job: dict) -> bool:
    explicit = str(job.get("city") or "").strip()
    if explicit:
        # BOSS may expose a district-qualified value such as ``成都·高新区``.
        # Compare the city component instead of rejecting a valid target card.
        city_part = re.split(r"[·\s,/，、]", explicit, maxsplit=1)[0]
        return city_part in TARGET_CITIES
    text = f"{job.get('summary', '')}"
    return any(city in text for city in TARGET_CITIES)


def _headhunter_card_is_reviewable(job: dict) -> bool:
    return not jobflow.card_reason(job,"boss")


def card_hard_failure(job: dict) -> str:
    return jobflow.card_reason(job,"boss")


JD_APPLICATION_ANCHOR = re.compile(r"(?!)")
JD_VENDOR_MARKERS = ()
JD_SPECIALIZATION_MARKERS = re.compile(r"(?!)")


def jd_hard_failure(detail: dict) -> str:
    normalized={**detail,"summary":str(detail.get("header_text", ""))+" "+str(detail.get("jd_text", ""))}
    return jobflow.card_reason(normalized,"boss")


def receipt_key(entry: dict) -> tuple[str, ...]:
    job_id = normalize(entry.get("job_id"))
    if job_id:
        return ("id", job_id)
    return (
        "fallback",
        normalize(entry.get("company")),
        normalize(entry.get("job_title")),
        normalize(entry.get("city")),
    )


def find_duplicate(
    job_id: str = "",
    company: str = "",
    job_title: str = "",
    city: str = "",
    path: Path = RECEIPTS,
) -> dict | None:
    candidate = receipt_key({
        "job_id": job_id,
        "company": company,
        "job_title": job_title,
        "city": city,
    })
    normalized_company = normalize(company)
    normalized_title = canonical_role_title(job_title)
    for entry in read_entries(path):
        same_role = (
            normalized_company
            and normalized_title
            and normalize(entry.get("company")) == normalized_company
            and canonical_role_title(entry.get("job_title")) == normalized_title
        )
        if receipt_key(entry) == candidate or same_role:
            return entry
    return None


def batch_company_roles(company: str, path: Path = RECEIPTS) -> dict:
    state = read_json(BATCH_STATE)
    baseline = set(state.get("baseline_receipts") or [])
    wanted = normalize(company)
    roles = {
        canonical_role_title(entry.get("job_title")): str(entry.get("job_title") or "").strip()
        for entry in read_entries(path)
        if receipt_token(entry) not in baseline
        and wanted
        and normalize(entry.get("company")) == wanted
        and normalize(entry.get("job_title"))
    }
    return {"company": company, "count": len(roles), "roles": sorted(roles.values())}


def filter_listing(
    data: dict,
    path: Path = RECEIPTS,
    screening_path: Path = SCREENING,
) -> dict:
    entries = read_entries(path)
    screened = active_screening(screening_path)
    skipped_ids = {
        job_id
        for job_id, entry in screened.items()
        if str(entry.get("decision") or "S").upper() == "S"
    }
    ids = {
        normalize(entry.get("job_id"))
        for entry in entries
        if normalize(entry.get("job_id"))
    }
    sent_roles = [
        (
            normalize(entry.get("company")),
            canonical_role_title(entry.get("job_title")),
        )
        for entry in entries
        if normalize(entry.get("company")) and normalize(entry.get("job_title"))
    ]
    kept: list[dict] = []
    receipt_filtered = 0
    screened_filtered = 0
    hard_filtered = 0
    hard_filter_counts: dict[str, int] = {}
    for job in data.get("jobs") or []:
        job_id = normalize(job.get("job_id"))
        title = normalize(job.get("title"))
        summary = normalize(job.get("summary"))
        duplicate = bool(job_id and job_id in ids)
        if not duplicate:
            duplicate = any(
                company and canonical_role_title(title) == old_title and company in summary
                for company, old_title in sent_roles
            )
        if duplicate:
            receipt_filtered += 1
        elif job_id and job_id in skipped_ids:
            screened_filtered += 1
        else:
            hard_failure = card_hard_failure(job)
            if hard_failure:
                hard_filtered += 1
                hard_filter_counts[hard_failure] = hard_filter_counts.get(hard_failure, 0) + 1
            else:
                kept.append(job)
    return {
        **data,
        "jobs": kept,
        "count": len(kept),
        "duplicates_filtered": receipt_filtered,
        "screened_filtered": screened_filtered,
        "hard_filtered": hard_filtered,
        "hard_filter_counts": hard_filter_counts,
        "known_receipts": len(entries),
        "known_screened": len(screened),
    }


def append_screening(entry: dict, path: Path = SCREENING) -> dict:
    job_id = normalize(entry.get("job_id"))
    if not job_id:
        return {"appended": False, "error": "missing_job_id"}
    decision = str(entry.get("decision") or "S").upper()
    if decision not in {"S", "P"}:
        return {"appended": False, "error": "invalid_decision"}
    row = {
        "screened_at": str(entry.get("screened_at") or utc_now().isoformat()),
        "job_id": str(entry.get("job_id") or "").strip(),
        "decision": decision,
        "reason": str(entry.get("reason") or "").strip()[:160],
        "job_title": str(entry.get("job_title") or "").strip()[:120],
        "company": str(entry.get("company") or "").strip()[:120],
        "city": str(entry.get("city") or "").strip()[:40],
        "feed": str(entry.get("feed") or "").strip()[:160],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
    with path.open("a+", encoding="utf-8", newline="") as handle:
        locked = False
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                locked = True
            handle.seek(0, os.SEEK_END)
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            if locked:
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    return {"appended": True, "job_id": row["job_id"], "decision": decision}


def append_if_new(receipt: dict, path: Path = RECEIPTS) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
    with path.open("a+", encoding="utf-8", newline="") as handle:
        locked = False
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                locked = True
            handle.seek(0)
            entries = []
            for line in handle.read().splitlines():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    entries.append(value)
            key = receipt_key(receipt)
            if any(receipt_key(entry) == key for entry in entries):
                return {"appended": False, "duplicate": True, "key": list(key)}
            handle.seek(0, os.SEEK_END)
            handle.write(json.dumps(receipt, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            if path == RECEIPTS:
                state = read_json(BATCH_STATE)
                if state.get("active"):
                    state["jd_reviews_at_last_send"] = len(set(state.get("jd_reviewed_job_ids") or []))
                    _telemetry_apply(state, "send")
                    write_json(BATCH_STATE, state)
            return {"appended": True, "duplicate": False, "key": list(key)}
        finally:
            if locked:
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def main() -> int:
    parser = argparse.ArgumentParser(description="Compact BOSS receipt ledger")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("summary")
    sub.add_parser("screening-summary")
    batch_start = sub.add_parser("batch-start")
    batch_start.add_argument("--target", type=int, default=5)
    batch_start.add_argument("--mode", choices=("recommend-city", "search-city"), default=DEFAULT_BATCH_MODE)
    batch_start.add_argument("--keyword", default="")
    batch_start.add_argument("--unique-read-limit", type=int, default=0)
    batch_start.add_argument("--no-send-jd-limit", type=int, default=0)
    sub.add_parser("batch-summary")
    batch_close = sub.add_parser("batch-close")
    batch_close.add_argument("--reason", default="superseded")
    lease_acquire = sub.add_parser("lease-acquire")
    lease_acquire.add_argument("--owner", required=True)
    lease_acquire.add_argument("--ttl", type=int, default=1200)
    lease_reacquire = sub.add_parser("lease-reacquire")
    lease_reacquire.add_argument("--owner", required=True)
    lease_reacquire.add_argument("--previous-token", required=True)
    lease_reacquire.add_argument("--ttl", type=int, default=1200)
    release = sub.add_parser("lease-release")
    release.add_argument("--lease-token", required=True)
    release.add_argument("--reason", default="terminal")
    release.add_argument("--phase", choices=("PAUSED", "DONE"), default="PAUSED")
    release.add_argument("--browser-cleanup-complete", action="store_true")
    sub.add_parser("receipt-observe")
    sub.add_parser("telemetry-summary")
    phase_set = sub.add_parser("phase-set")
    phase_set.add_argument("--lease-token", required=True)
    phase_set.add_argument("--phase", required=True)
    phase_set.add_argument("--expected-job-id", default=None)
    phase_set.add_argument("--shortlist-generation", type=int, default=None)
    phase_set.add_argument("--base-client-id", default=None)
    phase_set.add_argument("--derived-client-id", default=None)
    phase_set.add_argument("--message-hash", default=None)
    phase_set.add_argument("--send-state", default=None)
    phase_set.add_argument("--error-class", default=None)
    phase_set.add_argument("--error", default=None)
    phase_set.add_argument("--expected-phase", default=None)
    phase_set.add_argument("--expected-generation", type=int, default=None)
    phase_set.add_argument("--renew-seconds", type=int, default=DEFAULT_LEASE_TTL_SECONDS)
    claim = sub.add_parser("claim-candidate")
    claim.add_argument("--lease-token", required=True)
    claim.add_argument("--generation", type=int, required=True)
    claim.add_argument("--job-id", required=True)
    requeue = sub.add_parser("requeue-job")
    requeue.add_argument("--lease-token", required=True)
    requeue.add_argument("--job-id", required=True)
    requeue.add_argument("--title", required=True)
    requeue.add_argument("--company", required=True)
    requeue.add_argument("--city", required=True)
    requeue.add_argument("--feed", required=True)
    requeue.add_argument("--summary", default="")
    requeue.add_argument("--href", default="")
    requeue.add_argument("--salary", default="")
    resolve = sub.add_parser("resolve-candidate")
    resolve.add_argument("--lease-token", required=True)
    resolve.add_argument("--generation", type=int, required=True)
    resolve.add_argument("--job-id", required=True)
    resolve.add_argument("--outcome", choices=("HARD_FILTER", "JD_DECIDED", "SENT", "DUPLICATE"), required=True)
    resolve.add_argument("--reason", default="")
    sub.add_parser("combo-fuse")
    survivor = sub.add_parser("combo-survivor")
    survivor.add_argument("--city", default="")
    sub.add_parser("checkpoint-show")
    checkpoint = sub.add_parser("checkpoint-set")
    checkpoint.add_argument("--count", default="")
    checkpoint.add_argument("--pool", default="")
    checkpoint.add_argument("--offset", type=int, default=0)
    checkpoint.add_argument("--base", default="")
    checkpoint.add_argument("--job-id", default="")
    checkpoint.add_argument("--message-hash", default="")
    checkpoint.add_argument("--receipt-status", default="")
    checkpoint.add_argument("--safe-state", default="")
    check = sub.add_parser("check")
    check.add_argument("--job-id", default="")
    check.add_argument("--company", default="")
    check.add_argument("--job-title", default="")
    check.add_argument("--city", default="")
    mark = sub.add_parser("mark")
    mark.add_argument("--job-id", required=True)
    mark.add_argument("--decision", choices=("S", "P"), default="S")
    mark.add_argument("--reason", default="")
    mark.add_argument("--job-title", default="")
    mark.add_argument("--company", default="")
    mark.add_argument("--city", default="")
    mark.add_argument("--feed", default="")
    args = parser.parse_args()
    if args.command == "summary":
        entries = read_entries()
        keys = [receipt_key(entry) for entry in entries]
        result = {
            "ok": True,
            "rows": len(entries),
            "unique_keys": len(set(keys)),
            "duplicate_keys": len(keys) - len(set(keys)),
        }
    elif args.command == "screening-summary":
        active = active_screening()
        result = {
            "ok": True,
            "active_rows": len(active),
            "ttl_days": SCREENING_TTL_DAYS,
        }
    elif args.command == "batch-start":
        result = start_batch(
            args.target,
            mode=args.mode,
            unique_read_limit=args.unique_read_limit,
            no_send_jd_limit=args.no_send_jd_limit,
            keyword=args.keyword,
        )
    elif args.command == "batch-summary":
        result = batch_summary()
    elif args.command == "batch-close":
        result = close_batch(args.reason)
    elif args.command == "lease-acquire":
        result = acquire_execution(args.owner, args.ttl)
    elif args.command == "lease-reacquire":
        result = reacquire_execution(args.owner, args.previous_token, args.ttl)
    elif args.command == "lease-release":
        result = release_execution(
            args.lease_token,
            reason=args.reason,
            phase=args.phase,
            browser_cleanup_complete=args.browser_cleanup_complete,
        )
    elif args.command == "receipt-observe":
        result = observe_execution_receipt()
    elif args.command == "telemetry-summary":
        state = read_json(BATCH_STATE)
        result = {
            "ok": True,
            "active": bool(state.get("active")),
            "batch_id": state.get("batch_id"),
            "telemetry": state.get("telemetry") or _telemetry_defaults(),
        }
    elif args.command == "phase-set":
        values = {
            "expected_job_id": args.expected_job_id,
            "shortlist_generation": args.shortlist_generation,
            "base_client_id": args.base_client_id,
            "derived_client_id": args.derived_client_id,
            "message_hash": args.message_hash,
            "send_state": args.send_state,
            "last_error_class": args.error_class,
            "last_error": args.error,
        }
        result = update_execution(
            args.lease_token,
            args.phase,
            {key: value for key, value in values.items() if value is not None},
            expected_phase=args.expected_phase,
            expected_generation=args.expected_generation,
            renew_seconds=args.renew_seconds,
        )
    elif args.command == "claim-candidate":
        result = claim_candidate(args.lease_token, args.generation, args.job_id)
    elif args.command == "requeue-job":
        result = requeue_selected_job(
            args.lease_token,
            {
                "job_id": args.job_id,
                "title": args.title,
                "company": args.company,
                "city": args.city,
                "feed": args.feed,
                "summary": args.summary,
                "href": args.href,
                "salary": args.salary,
            },
        )
    elif args.command == "resolve-candidate":
        result = resolve_candidate(
            args.lease_token,
            args.generation,
            args.job_id,
            args.outcome,
            reason=args.reason,
        )
    elif args.command == "combo-fuse":
        result = combo_fuse_status()
    elif args.command == "combo-survivor":
        result = record_combo_survivor(args.city)
    elif args.command == "checkpoint-show":
        state = read_json(BATCH_STATE)
        result = {
            "ok": True,
            "active": bool(state.get("active")),
            "batch_id": state.get("batch_id"),
            "checkpoint": state.get("checkpoint") or {},
            "last_batch": {
                "feed": (state.get("last_batch") or {}).get("feed", ""),
                "offset": int((state.get("last_batch") or {}).get("offset") or 0),
                "job_ids": [
                    job.get("job_id", "")
                    for job in (state.get("last_batch") or {}).get("jobs") or []
                ],
            },
        }
    elif args.command == "checkpoint-set":
        result = set_checkpoint(vars(args))
    elif args.command == "mark":
        appended = append_screening(vars(args))
        result = {"ok": bool(appended.get("appended")), **appended}
    else:
        duplicate = find_duplicate(args.job_id, args.company, args.job_title, args.city)
        result = {
            "ok": True,
            "duplicate": bool(duplicate),
            "key": list(receipt_key(vars(args))),
        }
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    raise SystemExit(main())
