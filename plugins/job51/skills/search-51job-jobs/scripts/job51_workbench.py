#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import secrets
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ledger import append_if_new
from runtime_config import lanes


import config_core as jobflow

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = jobflow.platform_state("51job") / "51job-workbench.json"
CONFIRMED = jobflow.platform_state("51job") / "confirmed-51job.tsv"
SEEN = jobflow.platform_state("51job") / "seen-decisions-51job.tsv"
DEFERRED = jobflow.platform_state("51job") / "deferred-51job.tsv"
TECHNICAL = jobflow.platform_state("51job") / "technical-failures-51job.tsv"
SEEN_HEADER = ["date", "job_id", "company", "title", "reason", "expires_on"]
DEFERRED_HEADER = ["date", "job_id", "company", "title", "reason", "recorded_at"]
TECH_HEADER = ["date", "job_id", "lane", "error", "recorded_at"]
TECH_BREAKER_THRESHOLD = 2
DEFAULT_LEASE_TTL_SECONDS = 7200
LANE_REVIEW_BUDGET = 8
CITY_REVIEW_BUDGET = 20
CONFIRMED_HEADER = ["date", "job_id", "company", "title", "city", "salary", "resume_code", "evidence"]


def now() -> datetime:
    return datetime.now(timezone.utc)


def stamp() -> str:
    return now().isoformat(timespec="seconds")


def clean(value: object, limit: int = 300) -> str:
    return " ".join(str(value or "").replace("\t", " ").splitlines()).strip()[:limit]


def salary_band(value: object):
    return jobflow.salary_band(value)


def priority_score(card: list, lane: dict) -> int:
    return jobflow.priority({"title":card[1], "signals":card[6] if len(card)>6 else ""}, "51job")


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def archive_state(path: Path, state: dict) -> Path:
    archive_dir = path.parent / "archives"
    archive_dir.mkdir(parents=True, exist_ok=True)
    run_date = re.sub(r"[^0-9-]", "", clean(state.get("date"), 20)) or "unknown-date"
    generation = int(state.get("generation") or 0)
    destination = archive_dir / f"51job-workbench-{run_date}-g{generation}.json"
    suffix = 1
    while destination.exists():
        destination = archive_dir / f"51job-workbench-{run_date}-g{generation}-{suffix}.json"
        suffix += 1
    snapshot = json.loads(json.dumps(state, ensure_ascii=False))
    snapshot["lease"] = {}
    write_json(destination, snapshot)
    return destination


def lane_item(source: dict, page: int) -> dict:
    return {
        "id": source["id"], "source": "keyword", "round": source["round"], "tier": source["tier"],
        "keyword": source["keyword"], "city": source["city"], "cityCode": source["cityCode"],
        "page": page, "pageBudget": source["pageBudget"],
    }


def canonical_queue() -> list[dict]:
    result = []
    for source in lanes("51job"):
        for page in range(1, int(source["pageBudget"]) + 1):
            result.append(lane_item(source, page))
    return result


def pending(batch: dict) -> list[dict]:
    return [item for item in batch.get("items") or [] if item.get("status") == "pending"]


def quota_reached(state: dict) -> bool:
    target = int(state.get("target") or 0)
    return target > 0 and int(state.get("confirmed") or 0) >= target


def soft_quota_reached(state: dict) -> bool:
    return state.get("mode", "execution") == "execution" and quota_reached(state)


def quota_draining(state: dict) -> bool:
    batch = state.get("batch") or {}
    return soft_quota_reached(state) and bool(pending(batch) or batch.get("active_job_id"))


def review_budget_entry(state: dict, scope: str, key: str) -> dict:
    budget = state.setdefault("review_budget", {"lanes": {}, "cities": {}})
    bucket_name = "lanes" if scope == "lane" else "cities"
    bucket = budget.setdefault(bucket_name, {})
    return bucket.setdefault(key, {"jds": 0, "submits": 0, "jds_since_submit": 0})


def city_budget_key(lane: dict) -> str:
    city = clean(lane.get("city"), 80)
    return f"r{int(lane.get('round') or 0)}:t{int(lane.get('tier') or 0)}:{city}" if city else ""


def current_budget(state: dict) -> dict:
    lane = state.get("lane") or {}
    lane_id = clean(lane.get("id"), 160)
    city = clean(lane.get("city"), 80)
    city_key = city_budget_key(lane)
    lane_state = review_budget_entry(state, "lane", lane_id) if lane_id else {"jds": 0, "submits": 0, "jds_since_submit": 0}
    city_state = review_budget_entry(state, "city", city_key) if city_key else {"jds": 0, "submits": 0, "jds_since_submit": 0}
    return {
        "lane_id": lane_id,
        "city": city,
        "city_budget_key": city_key,
        "lane_jds_since_submit": int(lane_state.get("jds_since_submit") or 0),
        "city_jds_since_submit": int(city_state.get("jds_since_submit") or 0),
    }


def budget_guard(state: dict) -> dict:
    if soft_quota_reached(state):
        return {"action": "continue", "reason": "", "bypassed_for_quota_drain": True}
    budget = current_budget(state)
    if budget["city_jds_since_submit"] >= CITY_REVIEW_BUDGET:
        return {"action": "switch_city", "reason": "city_review_budget", **budget}
    if budget["lane_jds_since_submit"] >= LANE_REVIEW_BUDGET:
        return {"action": "switch_lane", "reason": "lane_review_budget", **budget}
    return {"action": "continue", "reason": "", **budget}


def record_detail_review(state: dict, lane: dict) -> dict:
    lane_id = clean(lane.get("id"), 160)
    city_key = city_budget_key(lane)
    lane_state = review_budget_entry(state, "lane", lane_id)
    city_state = review_budget_entry(state, "city", city_key)
    for entry in (lane_state, city_state):
        entry["jds"] = int(entry.get("jds") or 0) + 1
        entry["jds_since_submit"] = int(entry.get("jds_since_submit") or 0) + 1
    return current_budget(state)


def record_submit_budget(state: dict, lane: dict) -> dict:
    lane_id = clean(lane.get("id"), 160)
    city_key = city_budget_key(lane)
    lane_state = review_budget_entry(state, "lane", lane_id)
    city_state = review_budget_entry(state, "city", city_key)
    for entry in (lane_state, city_state):
        entry["submits"] = int(entry.get("submits") or 0) + 1
        entry["jds_since_submit"] = 0
    return current_budget(state)


def finalize_quota(state: dict) -> None:
    state["active"] = False
    state["queue"] = []
    state["batch"] = {}
    state["lease"] = {}
    state["checkpoint"] = {"safe_state": "quota_reached", "action": "quota_reached", "updated_at": stamp()}


def lease_valid(lease: dict) -> bool:
    try:
        return bool(lease.get("token")) and datetime.fromisoformat(str(lease["expires_at"])) > now()
    except (KeyError, TypeError, ValueError):
        return False


def require_lease(state: dict, token: str) -> None:
    lease = state.get("lease") or {}
    if not token or token != lease.get("token") or not lease_valid(lease):
        raise ValueError("execution_lease_required")


def touch_lease(state: dict, ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS) -> None:
    lease = state.get("lease") or {}
    if lease:
        lease["expires_at"] = (now() + timedelta(seconds=max(60, ttl_seconds))).isoformat(timespec="seconds")
        state["lease"] = lease


def base_state(target: int, run_date: str) -> dict:
    queue = canonical_queue()
    return {
        "schema": 1, "platform": "51job", "mode": "execution", "active": True, "date": run_date,
        "target": target, "confirmed": 0, "lane": dict(queue[0]), "queue": queue,
        "batch": {}, "generation": 0, "processed_job_ids": [], "page_fingerprints": [],
        "review_budget": {"lanes": {}, "cities": {}},
        "circuit": {"tripped": False, "signature": "", "job_ids": []},
        "checkpoint": {"safe_state": "started", "updated_at": stamp()},
        "lease": {}, "started_at": stamp(), "updated_at": stamp(),
    }


def compact(state: dict) -> dict:
    batch = state.get("batch") or {}
    circuit = state.get("circuit") or {}
    return {
        "ok": True, "mode": state.get("mode", "execution"), "active": bool(state.get("active")), "date": state.get("date", ""),
        "target": int(state.get("target") or 0), "confirmed": int(state.get("confirmed") or 0),
        "quota_reached": quota_reached(state), "quota_draining": quota_draining(state),
        "lane": state.get("lane") or {}, "queue_remaining": len(state.get("queue") or []),
        "batch": {"id": batch.get("batch_id", ""), "total": len(batch.get("items") or []),
                  "pending": len(pending(batch)), "active_job_id": batch.get("active_job_id", "")},
        "review_budget": current_budget(state) if state.get("lane") else {},
        "circuit": {"tripped": bool(circuit.get("tripped")), "signature": circuit.get("signature", ""),
                    "count": len(circuit.get("job_ids") or [])},
        "checkpoint": state.get("checkpoint") or {},
        "lease": {"owner": (state.get("lease") or {}).get("owner", ""), "valid": lease_valid(state.get("lease") or {})},
    }


def read_rows(path: Path, header: list[str]) -> list[dict]:
    if not path.exists() or not path.stat().st_size:
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if reader.fieldnames != header:
            raise ValueError(f"{path.stem}_header_mismatch")
        return list(reader)


def write_rows(path: Path, header: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=header, delimiter="\t", lineterminator="\n")
            writer.writeheader(); writer.writerows(rows); handle.flush(); os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def append_seen(item: dict, reason: str, run_date: str, state_path: Path) -> None:
    path = SEEN if state_path == DEFAULT_STATE.resolve() else state_path.with_name("seen-decisions-51job.tsv")
    rows = read_rows(path, SEEN_HEADER)
    job_id = clean(item.get("job_id"), 100)
    rows = [row for row in rows if clean(row.get("job_id"), 100) != job_id]
    day = date.fromisoformat(run_date)
    rows.append({"date": run_date, "job_id": job_id, "company": clean(item.get("company")),
                 "title": clean(item.get("title")), "reason": clean(reason, 120),
                 "expires_on": (day + timedelta(days=7)).isoformat()})
    write_rows(path, SEEN_HEADER, rows)


def is_budget_defer(reason: str) -> bool:
    text = clean(reason, 160).lower()
    return bool(re.search(
        r"lane_detail_budget_exhausted|lane_review_budget_reached_no_detail_read|"
        r"(?:lane|city)?[_ ]?review[_ ]?budget|budget[_ ]?review|复核预算|预算.*延期|延期.*预算",
        text,
        re.IGNORECASE,
    ))


def append_deferred(item: dict, reason: str, run_date: str, state_path: Path) -> None:
    append_deferred_many([item], reason, run_date, state_path)


def append_deferred_many(items: list[dict], reason: str, run_date: str, state_path: Path) -> int:
    path = DEFERRED if state_path == DEFAULT_STATE.resolve() else state_path.with_name("deferred-51job.tsv")
    rows = read_rows(path, DEFERRED_HEADER)
    job_ids = {clean(item.get("job_id"), 100) for item in items if clean(item.get("job_id"), 100)}
    if not job_ids:
        return 0
    rows = [row for row in rows if clean(row.get("job_id"), 100) not in job_ids]
    recorded_at = stamp()
    for item in items:
        job_id = clean(item.get("job_id"), 100)
        if not job_id:
            continue
        rows.append({"date": run_date, "job_id": job_id, "company": clean(item.get("company")),
                     "title": clean(item.get("title")), "reason": clean(reason, 120), "recorded_at": recorded_at})
    write_rows(path, DEFERRED_HEADER, rows)
    return len(job_ids)


def append_technical(item: dict, lane: dict, error: str, run_date: str, state_path: Path) -> None:
    path = TECHNICAL if state_path == DEFAULT_STATE.resolve() else state_path.with_name("technical-failures-51job.tsv")
    rows = read_rows(path, TECH_HEADER)
    rows.append({"date": run_date, "job_id": clean(item.get("job_id"), 100),
                 "lane": clean(f"{lane.get('keyword')}|{lane.get('city')}|p{lane.get('page')}", 160),
                 "error": clean(error, 160), "recorded_at": stamp()})
    write_rows(path, TECH_HEADER, rows)


def known_ids(run_date: str, state_path: Path) -> set[str]:
    confirmed = CONFIRMED if state_path == DEFAULT_STATE.resolve() else state_path.with_name("confirmed-51job.tsv")
    seen = SEEN if state_path == DEFAULT_STATE.resolve() else state_path.with_name("seen-decisions-51job.tsv")
    result = {clean(row.get("job_id"), 100) for row in read_rows(confirmed, ["date","job_id","company","title","city","salary","resume_code","evidence"]) if row.get("job_id")}
    today = date.fromisoformat(run_date)
    for row in read_rows(seen, SEEN_HEADER):
        try:
            if date.fromisoformat(row.get("expires_on", "")) >= today:
                result.add(clean(row.get("job_id"), 100))
        except ValueError:
            pass
    return result


def parse_cards(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        raise ValueError("cards_must_be_list")
    result, unique = [], set()
    for row in raw:
        if not isinstance(row, list) or len(row) < 5:
            raise ValueError("card_must_be_positional_array")
        job_id = clean(row[0], 100)
        if not job_id or job_id in unique: continue
        unique.add(job_id)
        result.append({"job_id": job_id, "title": clean(row[1]), "company": clean(row[2]),
                       "city": clean(row[3], 60), "salary": clean(row[4], 80),
                       "card": row, "status": "pending", "reason": ""})
    return result


def compact_candidate(item: dict | None) -> dict | None:
    """Return only the fields needed to generate the bound detail wrapper."""
    if not item:
        return None
    return {
        "job_id": clean(item.get("job_id"), 100),
        "card": item.get("card") or [],
        "priority": int(item.get("priority") or 0),
        "review_reason": clean(item.get("review_reason"), 80),
    }


def pop_current_queue(state: dict) -> None:
    current=state.get("lane") or {}
    queue=state.get("queue") or []
    for index,item in enumerate(queue):
        if all(item.get(key)==current.get(key) for key in ("id","page")):
            queue.pop(index); break
    state["queue"]=queue


def advance_budget_scope(state: dict, action: str) -> int:
    current = state.get("lane") or {}
    queue = state.get("queue") or []
    before = len(queue)
    if action == "switch_lane":
        queue = [item for item in queue if item.get("id") != current.get("id")]
    elif action == "switch_city":
        queue = [item for item in queue if not (
            item.get("city") == current.get("city")
            and int(item.get("round") or 0) == int(current.get("round") or 0)
            and int(item.get("tier") or 0) == int(current.get("tier") or 0)
        )]
    else:
        raise ValueError("unsupported_budget_action")
    state["queue"] = queue
    state["lane"] = dict(queue[0]) if queue else {}
    return before - len(queue)


def main() -> int:
    parser = argparse.ArgumentParser(description="Durable 51job execution workbench")
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start"); start.add_argument("--target", type=int, required=True); start.add_argument("--date", default=date.today().isoformat())
    resume = sub.add_parser("resume", help="resume a paused execution without resetting its receipts or batch")
    resume.add_argument("--date", default=date.today().isoformat()); resume.add_argument("--target", type=int, default=0)
    bootstrap = sub.add_parser("bootstrap-resume", help=argparse.SUPPRESS)
    bootstrap.add_argument("--target", type=int, required=True); bootstrap.add_argument("--date", required=True)
    bootstrap.add_argument("--confirmed", type=int, required=True); bootstrap.add_argument("--keyword", required=True)
    bootstrap.add_argument("--city", required=True); bootstrap.add_argument("--page", type=int, required=True)
    tuning = sub.add_parser("tuning-only", help="mark the durable state as observation-only without a target")
    tuning.add_argument("--date", default=date.today().isoformat())
    validation = sub.add_parser("validation-start", help="start one isolated real-submit validation without changing the default workbench")
    validation.add_argument("--date", default=date.today().isoformat()); validation.add_argument("--city", required=True)
    validation.add_argument("--keyword", required=True); validation.add_argument("--page", type=int, default=1)
    validation_sync = sub.add_parser("validation-sync-confirmed", help="mirror an applied isolated-validation receipt into the default confirmed ledger")
    validation_sync.add_argument("--job-id", required=True)
    sub.add_parser("status")
    acquire = sub.add_parser("lease-acquire"); acquire.add_argument("--owner", required=True); acquire.add_argument("--ttl-seconds", type=int, default=DEFAULT_LEASE_TTL_SECONDS)
    release = sub.add_parser("lease-release"); release.add_argument("--lease-token", required=True)
    renew = sub.add_parser("lease-renew"); renew.add_argument("--lease-token", required=True); renew.add_argument("--ttl-seconds", type=int, default=DEFAULT_LEASE_TTL_SECONDS)
    ingest = sub.add_parser("batch-ingest"); ingest.add_argument("--lease-token", required=True); ingest.add_argument("--cards-result-env", action="store_true", required=True)
    next_cmd = sub.add_parser("next"); next_cmd.add_argument("--lease-token", required=True)
    reviewed = sub.add_parser("detail-reviewed", help="durably record one successfully bound JD read")
    reviewed.add_argument("--lease-token", required=True); reviewed.add_argument("--job-id", required=True); reviewed.add_argument("--effective-salary", default="")
    budget_advance = sub.add_parser("budget-advance", help="bulk-defer the current batch and advance the exhausted review-budget scope")
    budget_advance.add_argument("--lease-token", required=True)
    decision = sub.add_parser("decision"); decision.add_argument("--lease-token", required=True); decision.add_argument("--job-id", required=True); decision.add_argument("--decision", choices=("rejected","duplicate","deferred"), required=True); decision.add_argument("--reason", required=True)
    commit = sub.add_parser("commit-applied", help="atomically append a success receipt and mark the active candidate applied")
    commit.add_argument("--lease-token", required=True); commit.add_argument("--job-id", required=True); commit.add_argument("--resume-code", required=True, choices=("CONFIGURED", "PLATFORM_DEFAULT")); commit.add_argument("--evidence", required=True); commit.add_argument("--date", default="")
    technical = sub.add_parser("technical-failure"); technical.add_argument("--lease-token", required=True); technical.add_argument("--job-id", required=True); technical.add_argument("--error", required=True)
    reset = sub.add_parser("circuit-reset"); reset.add_argument("--lease-token", required=True)
    complete = sub.add_parser("page-complete"); complete.add_argument("--lease-token", required=True)
    pause = sub.add_parser("pause"); pause.add_argument("--lease-token", required=True); pause.add_argument("--reason", required=True)
    args = parser.parse_args(); path = args.state_path.resolve(); state = read_json(path)
    try:
        if args.command == "tuning-only":
            archived_previous_state = ""
            if state and state.get("date") != args.date and not state.get("active") and state.get("mode") == "execution" and (
                pending(state.get("batch") or {})
                or (state.get("batch") or {}).get("active_job_id")
                or bool(state.get("queue"))
                or (state.get("checkpoint") or {}).get("safe_state") == "paused"
            ):
                archived_previous_state = str(archive_state(path, state))
                state = {}
            current_batch = state.get("batch") or {}
            if pending(current_batch) or current_batch.get("active_job_id") or lease_valid(state.get("lease") or {}):
                raise ValueError("cannot_enter_tuning_with_active_execution")
            if not state:
                state = base_state(0, args.date)
            state["mode"] = "tuning"
            state["active"] = False
            state["target"] = 0
            state["confirmed"] = 0
            state["batch"] = {}
            state["lease"] = {}
            state["checkpoint"] = {"safe_state": "tuning_only", "reason": "daily_target_not_set", "updated_at": stamp()}
            state["updated_at"] = stamp(); write_json(path, state)
            result = {"ok": True, "mode": "tuning", "active": False, "target": 0, "checkpoint": state["checkpoint"]}
            if archived_previous_state:
                result["archived_previous_state"] = archived_previous_state
        elif args.command == "validation-start":
            if path == DEFAULT_STATE.resolve(): raise ValueError("validation_requires_isolated_state")
            current_batch = state.get("batch") or {}
            if state and (pending(current_batch) or current_batch.get("active_job_id") or lease_valid(state.get("lease") or {})):
                raise ValueError("validation_state_busy")
            state = base_state(1, args.date)
            index = next((i for i,item in enumerate(state["queue"]) if item.get("keyword")==args.keyword and item.get("city")==args.city and int(item.get("page") or 0)==args.page), -1)
            if index < 0: raise ValueError("validation_lane_not_found")
            state["queue"] = state["queue"][index:]; state["lane"] = dict(state["queue"][0]); state["mode"] = "validation"
            state["checkpoint"] = {"safe_state":"validation_started","updated_at":stamp()}; state["updated_at"] = stamp(); write_json(path,state)
            result = {**compact(state), "validation": True}
        elif args.command == "bootstrap-resume":
            if state:
                raise ValueError("bootstrap_requires_missing_state")
            state = base_state(args.target, args.date)
            index = next((i for i,item in enumerate(state["queue"]) if item.get("keyword")==args.keyword and item.get("city")==args.city and int(item.get("page") or 0)==args.page), -1)
            if index < 0:
                raise ValueError("bootstrap_lane_not_found")
            state["queue"] = state["queue"][index:]; state["lane"] = dict(state["queue"][0]); state["confirmed"] = args.confirmed
            state["checkpoint"] = {"safe_state":"legacy_resume","updated_at":stamp()}; state["updated_at"] = stamp(); write_json(path,state)
            result = {"ok":True,"bootstrapped":True,**compact(state)}
        elif args.command == "start":
            if args.target < 1: raise ValueError("target_must_be_positive")
            if state.get("date") == args.date and soft_quota_reached(state):
                state["target"] = args.target
                if quota_draining(state):
                    state["active"] = True
                    state["checkpoint"] = {"safe_state":"quota_draining","updated_at":stamp()}
                    state["updated_at"] = stamp(); write_json(path,state)
                    result = {**compact(state), "resumed": True, "quota_draining": True}
                else:
                    result = {**compact(state), "already_quota_reached": True}
            elif state.get("date") == args.date and not state.get("active") and (state.get("checkpoint") or {}).get("action") == "exhausted":
                result = {**compact(state), "already_exhausted": True}
            elif state.get("active") and state.get("date") == args.date:
                result = {**compact(state), "resumed": True}
            elif state.get("active") and (pending(state.get("batch") or {}) or (state.get("batch") or {}).get("active_job_id")):
                result = {**compact(state), "date_transition_blocked": True}
            elif (not state.get("active")) and state.get("date") != args.date and state.get("mode") == "execution" and (
                pending(state.get("batch") or {})
                or (state.get("batch") or {}).get("active_job_id")
                or bool(state.get("queue"))
                or (state.get("checkpoint") or {}).get("safe_state") == "paused"
            ):
                archived = archive_state(path, state)
                state = base_state(args.target, args.date); write_json(path, state)
                result = {**compact(state), "resumed": False, "archived_previous_state": str(archived)}
            else:
                state = base_state(args.target, args.date); write_json(path, state); result = {**compact(state), "resumed": False}
        elif args.command == "resume":
            if not state: raise ValueError("state_missing")
            if state.get("date") != args.date: raise ValueError("date_mismatch")
            target = int(args.target or state.get("target") or 0)
            if target < 1: raise ValueError("target_must_be_positive")
            if state.get("active"):
                result = {**compact(state), "resumed": True, "already_active": True}
            elif (state.get("checkpoint") or {}).get("safe_state") != "paused":
                raise ValueError("resume_requires_paused_state")
            elif int(state.get("confirmed") or 0) >= target:
                state["target"] = target
                if pending(state.get("batch") or {}):
                    state["mode"] = "execution"; state["active"] = True
                    state["checkpoint"] = {"safe_state":"quota_draining","updated_at":stamp()}
                    state["updated_at"] = stamp(); write_json(path,state)
                    result = {**compact(state), "resumed": True, "quota_draining": True}
                else:
                    state["checkpoint"] = {"safe_state":"quota_reached","action":"quota_reached","updated_at":stamp()}
                    state["updated_at"] = stamp(); write_json(path,state)
                    result = {**compact(state), "resumed": False, "quota_reached": True}
            else:
                state["mode"] = "execution"; state["target"] = target; state["active"] = bool(state.get("queue") or pending(state.get("batch") or {})); state["checkpoint"] = {"safe_state":"resumed","updated_at":stamp()}; state["updated_at"] = stamp(); write_json(path,state)
                result = {**compact(state), "resumed": True}
        elif args.command == "status":
            result = compact(state) if state else {"ok": True, "active": False}
        elif args.command == "lease-acquire":
            old = state.get("lease") or {}
            if lease_valid(old) and old.get("owner") != args.owner: raise ValueError("execution_already_claimed")
            token = old.get("token") if lease_valid(old) else "lease_" + secrets.token_urlsafe(18)
            state["lease"] = {"owner": clean(args.owner, 100), "token": token, "expires_at": (now() + timedelta(seconds=max(60,args.ttl_seconds))).isoformat(timespec="seconds")}
            state["updated_at"] = stamp(); write_json(path, state); result = {"ok": True, "lease_token": token, "expires_at": state["lease"]["expires_at"]}
        elif args.command == "lease-release":
            require_lease(state, args.lease_token); state["lease"] = {}; state["updated_at"] = stamp(); write_json(path, state); result = {"ok": True, "released": True}
        elif args.command == "lease-renew":
            require_lease(state, args.lease_token); touch_lease(state, args.ttl_seconds); state["updated_at"] = stamp(); write_json(path, state); result = {"ok": True, "renewed": True, "expires_at": state["lease"]["expires_at"]}
        elif args.command == "batch-ingest":
            require_lease(state, args.lease_token)
            touch_lease(state)
            if (state.get("batch") or {}).get("batch_id"): raise ValueError("pending_batch_must_be_drained")
            raw = os.environ.get("CODEX_ENTERPRISE_CARDS_RESULT_JSON", "")
            if not raw.strip(): raise ValueError("CODEX_ENTERPRISE_CARDS_RESULT_JSON_missing")
            snapshot = json.loads(raw)
            supplied_guard = isinstance(snapshot, dict) and snapshot.get("ok") is False and snapshot.get("terminal") is False and snapshot.get("action") == "switch_lane" and clean(snapshot.get("reason"),80) in {"pager_exhausted","page_budget"}
            fingerprint = clean(snapshot.get("pageFingerprint"), 160) if isinstance(snapshot, dict) else ""
            seen_fingerprints = set(state.get("page_fingerprints") or [])
            duplicate_page = bool(fingerprint and fingerprint in seen_fingerprints)
            guard = supplied_guard or duplicate_page
            if guard: cards_raw = []
            elif isinstance(snapshot, dict) and snapshot.get("ok") and isinstance(snapshot.get("cards"), list): cards_raw = snapshot["cards"]
            else: raise ValueError("cards_result_invalid")
            if fingerprint and not duplicate_page:
                state["page_fingerprints"] = [*list(state.get("page_fingerprints") or []), fingerprint][-64:]
            parsed = parse_cards(cards_raw); known = known_ids(state["date"], path) | set(state.get("processed_job_ids") or []); cards = [item for item in parsed if item["job_id"] not in known]
            review_by_id = {
                clean(row.get("jobId"), 100): clean(row.get("reason"), 80)
                for row in (snapshot.get("reviewCards") or [])
                if isinstance(row, dict) and clean(row.get("jobId"), 100)
            }
            lane = state.get("lane") or {}
            for item in cards:
                item["priority"] = priority_score(item["card"], lane)
                item["review_reason"] = review_by_id.get(item["job_id"], "")
            state["generation"] = int(state.get("generation") or 0) + 1
            state["batch"] = {"batch_id": f"{state['date']}:{state['generation']}:{lane.get('keyword')}:{lane.get('city')}:p{lane.get('page')}", "lane": dict(lane), "items": cards, "active_job_id": "", "guard": "duplicate_page" if duplicate_page else (clean(snapshot.get("reason"),80) if supplied_guard else ""), "page_fingerprint": fingerprint, "created_at": stamp()}
            state["checkpoint"] = {"safe_state":"batch_ready","updated_at":stamp()}; state["updated_at"] = stamp(); write_json(path,state)
            result = {"ok":True,"count":len(cards),"raw_count":len(parsed),"known_filtered":len(parsed)-len(cards),"guard":state["batch"]["guard"],"page_fingerprint":fingerprint}
        elif args.command == "next":
            require_lease(state,args.lease_token)
            touch_lease(state)
            if (state.get("circuit") or {}).get("tripped"): raise ValueError("technical_circuit_breaker")
            batch=state.get("batch") or {}
            active=batch.get("active_job_id")
            item=next((x for x in batch.get("items") or [] if x.get("job_id")==active and x.get("status")=="pending"),None)
            budget_result=None
            if not item and pending(batch):
                guard=budget_guard(state)
                if guard["action"] != "continue":
                    state["checkpoint"]={"safe_state":"review_budget","action":guard["action"],"reason":guard["reason"],"updated_at":stamp()}
                    state["updated_at"]=stamp(); write_json(path,state)
                    budget_result={"ok":True,"candidate":None,"drained":False,"budget_action":guard["action"],"budget_reason":guard["reason"],"review_budget":guard}
                else:
                    item=min((x for x in batch.get("items") or [] if x.get("status")=="pending"), key=lambda x: (-int(x.get("priority") or 0), bool(x.get("review_reason")), batch.get("items").index(x)), default=None)
            if budget_result is not None:
                result=budget_result
            else:
                batch["active_job_id"]=item.get("job_id") if item else ""
                state["batch"]=batch
                state["checkpoint"]={"safe_state":"candidate_selected" if item else "batch_drained","updated_at":stamp()}
                state["updated_at"]=stamp(); write_json(path,state)
                result={"ok":True,"candidate":compact_candidate(item),"drained":item is None,"quota_draining":quota_draining(state)}
        elif args.command == "detail-reviewed":
            require_lease(state,args.lease_token); touch_lease(state)
            batch=state.get("batch") or {}
            if batch.get("active_job_id") != args.job_id: raise ValueError("job_not_active_in_current_batch")
            item=next((x for x in batch.get("items") or [] if x.get("job_id")==args.job_id and x.get("status")=="pending"),None)
            if not item: raise ValueError("stale_batch_job")
            effective_salary=clean(args.effective_salary,80)
            if item.get("detail_reviewed"):
                if effective_salary and not item.get("detail_salary"):
                    item["detail_salary"]=effective_salary; state["batch"]=batch; state["updated_at"]=stamp(); write_json(path,state)
                result={"ok":True,"job_id":args.job_id,"already_recorded":True,"review_budget":current_budget(state),"effective_salary":item.get("detail_salary","")}
            else:
                item["detail_reviewed"]=True; item["detail_reviewed_at"]=stamp(); item["detail_salary"]=effective_salary
                budget=record_detail_review(state,batch.get("lane") or state.get("lane") or {})
                state["batch"]=batch; state["checkpoint"]={"safe_state":"detail_reviewed","updated_at":stamp()}; state["updated_at"]=stamp(); write_json(path,state)
                result={"ok":True,"job_id":args.job_id,"already_recorded":False,"review_budget":budget,"effective_salary":effective_salary}
        elif args.command == "budget-advance":
            require_lease(state,args.lease_token); touch_lease(state)
            batch=state.get("batch") or {}
            if batch.get("active_job_id"): raise ValueError("active_candidate_must_be_decided_before_budget_advance")
            guard=budget_guard(state)
            if guard["action"]=="continue": raise ValueError("review_budget_not_exhausted")
            items=pending(batch)
            recorded=append_deferred_many(items,guard["reason"],state["date"],path)
            processed=set(state.get("processed_job_ids") or [])
            for item in items:
                item["status"]="deferred"; item["reason"]=guard["reason"]; item["decided_at"]=stamp(); processed.add(item["job_id"])
            state["processed_job_ids"]=sorted(processed)
            state["batch"]={}
            skipped=advance_budget_scope(state,guard["action"])
            queue=state.get("queue") or []
            state["active"]=bool(queue)
            state["checkpoint"]={"safe_state":"page_boundary","action":guard["action"],"reason":guard["reason"],"updated_at":stamp()}
            if not queue: state["lease"]={}
            state["updated_at"]=stamp(); write_json(path,state)
            result={"ok":True,"action":guard["action"],"reason":guard["reason"],"deferred":recorded,"queue_skipped":skipped,"next_lane":state.get("lane") or {},"exhausted":not queue}
        elif args.command == "commit-applied":
            require_lease(state, args.lease_token)
            touch_lease(state)
            batch = state.get("batch") or {}
            item = next((x for x in batch.get("items") or [] if x.get("job_id") == args.job_id), None)
            if not item:
                raise ValueError("job_not_found_in_current_batch")
            if item.get("status") == "applied":
                result = {"ok": True, "job_id": args.job_id, "already_committed": True, "confirmed": state.get("confirmed", 0)}
            else:
                if batch.get("active_job_id") != args.job_id: raise ValueError("job_not_active_in_current_batch")
                if item.get("status") != "pending": raise ValueError("stale_batch_job")
                if not item.get("detail_reviewed"): raise ValueError("detail_review_required_before_commit")
                detail_salary = clean(item.get("detail_salary"), 80)
                band = salary_band(detail_salary)
                floor = int((batch.get("lane") or state.get("lane") or {}).get("salaryFloor") or 0)
                if band is None: raise ValueError("detail_salary_unverified")
                if jobflow.salary_pass(detail_salary, "51job") is not True: raise ValueError("detail_salary_below_floor")
                confirmed_path = CONFIRMED if path == DEFAULT_STATE.resolve() else path.with_name("confirmed-51job.tsv")
                row = {"date": clean(args.date or state["date"]), "job_id": clean(item.get("job_id"), 100), "company": clean(item.get("company")), "title": clean(item.get("title")), "city": clean(item.get("city"), 60), "salary": detail_salary, "resume_code": clean(args.resume_code), "evidence": clean(args.evidence, 160)}
                if any(not row[key] for key in CONFIRMED_HEADER): raise ValueError("empty_required_field")
                try:
                    receipt = append_if_new(confirmed_path, row)
                except (OSError, ValueError) as exc:
                    raise ValueError(f"confirmed_receipt_failed:{exc}") from exc
                already_counted = args.job_id in set(state.get("processed_job_ids") or [])
                item["status"] = "applied"; item["reason"] = clean(args.evidence, 120); item["decided_at"] = stamp(); batch["active_job_id"] = ""
                state["processed_job_ids"] = sorted(set(state.get("processed_job_ids") or []) | {args.job_id})
                if not already_counted: state["confirmed"] = int(state.get("confirmed") or 0) + 1
                budget = record_submit_budget(state, batch.get("lane") or state.get("lane") or {})
                remaining = len(pending(batch))
                state["batch"] = batch
                if soft_quota_reached(state) and remaining:
                    state["checkpoint"] = {"safe_state":"quota_draining","updated_at":stamp()}
                else:
                    state["checkpoint"] = {"safe_state":"candidate_committed","updated_at":stamp()}
                state["updated_at"] = stamp(); write_json(path, state)
                result = {"ok": True, "job_id": args.job_id, "receipt": receipt, "confirmed": state.get("confirmed", 0), "remaining": remaining, "quota_reached": soft_quota_reached(state), "quota_draining": soft_quota_reached(state) and remaining > 0, "review_budget": budget}
        elif args.command == "validation-sync-confirmed":
            if path == DEFAULT_STATE.resolve() or state.get("mode") != "validation": raise ValueError("validation_sync_requires_isolated_validation_state")
            batch = state.get("batch") or {}
            item = next((x for x in batch.get("items") or [] if x.get("job_id") == args.job_id and x.get("status") == "applied"), None)
            if not item: raise ValueError("validation_applied_job_required")
            local_confirmed = path.with_name("confirmed-51job.tsv")
            row = next((x for x in read_rows(local_confirmed, CONFIRMED_HEADER) if clean(x.get("job_id"), 100) == args.job_id), None)
            if not row: raise ValueError("validation_confirmed_receipt_missing")
            receipt = append_if_new(CONFIRMED, row)
            state["active"] = False
            state["lease"] = {}
            state["checkpoint"] = {"safe_state":"validation_synced","updated_at":stamp()}
            state["updated_at"] = stamp()
            write_json(path, state)
            result = {"ok": True, "job_id": args.job_id, "synced": True, "completed": True, "receipt": receipt}
        elif args.command == "decision":
            require_lease(state,args.lease_token); touch_lease(state); batch=state.get("batch") or {}
            if batch.get("active_job_id")!=args.job_id: raise ValueError("job_not_active_in_current_batch")
            item=next((x for x in batch.get("items") or [] if x.get("job_id")==args.job_id and x.get("status")=="pending"),None)
            if not item: raise ValueError("stale_batch_job")
            item["status"]=args.decision; item["reason"]=clean(args.reason,120); item["decided_at"]=stamp(); batch["active_job_id"]=""
            state["processed_job_ids"]=sorted(set(state.get("processed_job_ids") or [])|{args.job_id})
            seen_recorded = False
            deferred_recorded = False
            if args.decision=="rejected": append_seen(item,item["reason"],state["date"],path); seen_recorded = True
            elif args.decision=="deferred":
                if is_budget_defer(item["reason"]): append_deferred(item,item["reason"],state["date"],path); deferred_recorded = True
                else: append_seen(item,item["reason"],state["date"],path); seen_recorded = True
            state["batch"]=batch; state["checkpoint"]={"safe_state":"candidate_decided","updated_at":stamp()}; state["updated_at"]=stamp(); write_json(path,state)
            result={"ok":True,"decision":args.decision,"remaining":len(pending(batch)),"confirmed":state.get("confirmed",0),"seen_recorded":seen_recorded,"deferred_recorded":deferred_recorded}
        elif args.command == "technical-failure":
            require_lease(state,args.lease_token); touch_lease(state); batch=state.get("batch") or {}
            if batch.get("active_job_id")!=args.job_id: raise ValueError("job_not_active_in_current_batch")
            item=next((x for x in batch.get("items") or [] if x.get("job_id")==args.job_id and x.get("status")=="pending"),None)
            if not item: raise ValueError("stale_batch_job")
            signature=clean(args.error,160); item["status"]="technical"; item["reason"]=signature; item["decided_at"]=stamp(); batch["active_job_id"]=""; append_technical(item,state.get("lane") or {},signature,state["date"],path)
            circuit=state.get("circuit") or {}; ids=list(circuit.get("job_ids") or []) if circuit.get("signature")==signature else []
            if args.job_id not in ids: ids.append(args.job_id)
            circuit={"tripped":len(ids)>=TECH_BREAKER_THRESHOLD,"signature":signature,"job_ids":ids[-TECH_BREAKER_THRESHOLD:]}; state["circuit"]=circuit; state["batch"]=batch
            if circuit["tripped"]: state["checkpoint"]={"safe_state":"technical_circuit","error":signature,"updated_at":stamp()}; state["lease"]={}
            else: state["checkpoint"]={"safe_state":"technical_recorded","updated_at":stamp()}
            state["updated_at"]=stamp(); write_json(path,state); result={"ok":True,"technical":True,"circuit_breaker":circuit["tripped"],"signature":signature,"count":len(ids),"remaining":len(pending(batch))}
        elif args.command == "circuit-reset":
            require_lease(state,args.lease_token); touch_lease(state); state["circuit"]={"tripped":False,"signature":"","job_ids":[]}; state["checkpoint"]={"safe_state":"circuit_reset","updated_at":stamp()}; state["updated_at"]=stamp(); write_json(path,state); result={"ok":True,"reset":True}
        elif args.command == "page-complete":
            require_lease(state,args.lease_token); touch_lease(state); batch=state.get("batch") or {}
            if not batch.get("batch_id"): raise ValueError("page_batch_required")
            if pending(batch) or batch.get("active_job_id"): raise ValueError("pending_batch_must_be_drained")
            if (state.get("circuit") or {}).get("tripped"): raise ValueError("technical_circuit_breaker")
            if soft_quota_reached(state):
                final_confirmed=int(state.get("confirmed") or 0)
                finalize_quota(state); state["updated_at"]=stamp(); write_json(path,state)
                result={"ok":True,"action":"quota_reached","confirmed":final_confirmed,"next_lane":{},"exhausted":False,"quota_reached":True}
            else:
                pop_current_queue(state); queue=state.get("queue") or []; state["lane"]=dict(queue[0]) if queue else {}; state["batch"]={}; state["active"]=bool(queue); action="next_lane" if queue else "exhausted"
                state["checkpoint"]={"safe_state":"page_boundary","action":action,"updated_at":stamp()}
                if not queue: state["lease"]={}
                state["updated_at"]=stamp(); write_json(path,state); result={"ok":True,"action":action,"next_lane":state["lane"],"exhausted":not queue}
        else:
            require_lease(state,args.lease_token); touch_lease(state); state["active"]=False; state["checkpoint"]={"safe_state":"paused","reason":clean(args.reason,160),"updated_at":stamp()}; state["lease"]={}; state["updated_at"]=stamp(); write_json(path,state); result={"ok":True,"paused":True}
    except ValueError as exc:
        result={"ok":False,"error":str(exc)}
    print(json.dumps(result,ensure_ascii=False,separators=(",",":"))); return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
