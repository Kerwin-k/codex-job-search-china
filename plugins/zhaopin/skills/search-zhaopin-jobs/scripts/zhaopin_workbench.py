#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import secrets
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


import config_core as jobflow

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = jobflow.platform_state("zhaopin") / "zhaopin-workbench.json"
CONFIRMED = jobflow.platform_state("zhaopin") / "confirmed-zhaopin.tsv"
SEEN = jobflow.platform_state("zhaopin") / "seen-decisions-zhaopin.tsv"
TECHNICAL = jobflow.platform_state("zhaopin") / "technical-failures-zhaopin.tsv"
TECHNICAL_EVIDENCE = jobflow.platform_state("zhaopin") / "technical-evidence-zhaopin.jsonl"
POOLS = tuple(jobflow.effective("zhaopin")["search"]["expectation_pools"])
KEYWORDS = tuple(jobflow.effective("zhaopin")["search"]["keywords"])
CITIES = tuple(jobflow.effective("zhaopin")["search"]["cities"])
CITY_TIERS = (CITIES,)
TERMINAL_DECISIONS = ("rejected", "applied", "duplicate", "deferred")
PAGE_BUDGET = {"expectation_pool":jobflow.effective("zhaopin")["search"]["page_budget"],"keyword":jobflow.effective("zhaopin")["search"]["page_budget"]}
SEEN_HEADER = ["date", "job_id", "company", "title", "reason", "expires_on"]
TECH_HEADER = ["date", "job_id", "lane", "error", "recorded_at"]
CONFIRMED_HEADER = ["date", "job_id", "company", "title", "city", "salary", "resume_code", "evidence"]
TECH_BREAKER_THRESHOLD = 3
ECONOMIC_JD_LIMIT = 10
RECEIPT_RESULTS = ("CONTINUE", "SUCCESS_HEARTBEAT", "PRE_STOP")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat(timespec="seconds")


def clean(value: object, limit: int = 300) -> str:
    return " ".join(str(value or "").replace("\t", " ").splitlines()).strip()[:limit]


def bump_progress(state: dict) -> int:
    value = int(state.get("progress_revision") or 0) + 1
    state["progress_revision"] = value
    return value


def fresh_control(state: dict, action: str = "unobserved") -> dict:
    return {
        "executor_epoch": int(state.get("executor_epoch") or 0),
        "last_progress_revision": int(state.get("progress_revision") or 0),
        "last_confirmed": int(state.get("confirmed") or 0),
        "unchanged_continue_count": 0,
        "last_result": "",
        "last_action": action,
        "updated_at": iso_now(),
    }


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def make_lane(source: str, primary: str, city: str, page: int) -> dict:
    return {
        "source": source,
        "pool": primary if source == "expectation_pool" else "",
        "keyword": primary if source == "keyword" else "",
        "city": city,
        "page": page,
    }


def lane_key(lane: dict) -> str:
    primary = lane.get("pool") or lane.get("keyword") or ""
    return "|".join((str(lane.get("source") or ""), str(primary), str(lane.get("city") or ""), str(int(lane.get("page") or 0))))


def daily_refresh_queue():
    if not CITIES:
        return []
    groups=[("expectation_pool",pool) for pool in POOLS]+[("keyword",keyword) for keyword in KEYWORDS]
    return [make_lane(source,primary,CITIES[0],1) for source,primary in groups]


def canonical_deep_queue():
    daily={lane_key(x) for x in daily_refresh_queue()}
    queue=[]
    for source,items in (("expectation_pool",POOLS),("keyword",KEYWORDS)):
        for city in CITIES:
            for primary in items:
                for page in range(1,PAGE_BUDGET[source]+1):
                    item=make_lane(source,primary,city,page)
                    if lane_key(item) not in daily: queue.append(item)
    return queue


def queue_from_lane(lane: dict) -> list[dict]:
    canonical = canonical_deep_queue()
    key = lane_key(lane)
    for index, item in enumerate(canonical):
        if lane_key(item) == key:
            return canonical[index:]
    return [dict(lane), *canonical]


def scheduler_from_previous(previous: dict) -> dict:
    old = previous.get("scheduler") or {}
    old_deep = [dict(item) for item in old.get("deep_queue") or [] if isinstance(item, dict)]
    if old_deep:
        return {"phase": "daily", "daily_queue": daily_refresh_queue(), "deep_queue": old_deep, "pending_date": "", "pending_target": 0}
    lane = dict(previous.get("lane") or {})
    if lane:
        return {"phase": "daily", "daily_queue": daily_refresh_queue(), "deep_queue": queue_from_lane(lane), "pending_date": "", "pending_target": 0}
    return {"phase": "daily", "daily_queue": daily_refresh_queue(), "deep_queue": canonical_deep_queue(), "pending_date": "", "pending_target": 0}


def scheduler_migrate(state: dict) -> bool:
    if int(state.get("schema") or 0) >= 3 and isinstance(state.get("scheduler"), dict):
        return False
    lane = dict(state.get("lane") or {})
    state["schema"] = 3
    state["scheduler"] = {
        "phase": "deep",
        "daily_queue": [],
        "deep_queue": queue_from_lane(lane) if lane else canonical_deep_queue(),
        "pending_date": "",
        "pending_target": 0,
    }
    state.pop("resume_lane", None)
    state["day_phase"] = "normal"
    return True


def base_state(target: int, run_date: str, previous: dict | None = None) -> dict:
    scheduler = scheduler_from_previous(previous or {})
    fresh = dict(scheduler["daily_queue"][0])
    return {
        "schema": 3,
        "platform": "zhaopin",
        "active": True,
        "date": run_date,
        "target": target,
        "confirmed": 0,
        "lane": fresh,
        "day_phase": "priority_pools",
        "scheduler": scheduler,
        "economy": {"jds_since_success": 0, "auto_rejected": 0, "model_reviewed": 0, "lane_rejects": {}, "city_rejects": {}, "pool_confirmed": {}},
        "circuit": {"tripped": False, "signature": "", "job_ids": []},
        "generation": 0,
        "progress_revision": int((previous or {}).get("progress_revision") or 0) + (1 if previous else 0),
        "executor_epoch": int((previous or {}).get("executor_epoch") or 0),
        "batch": {},
        "processed_job_ids": [],
        "technical_job_ids": [],
        "last_completed": {},
        "checkpoint": {"safe_state": "started", "updated_at": iso_now()},
        "lease": {},
        "started_at": iso_now(),
        "updated_at": iso_now(),
    }


def activate_new_date(state: dict, target: int, run_date: str) -> None:
    scheduler_migrate(state)
    deep = [dict(item) for item in (state.get("scheduler") or {}).get("deep_queue") or []]
    if not deep:
        deep = canonical_deep_queue()
    daily = daily_refresh_queue()
    state.update({
        "schema": 3, "active": True, "date": run_date, "target": target, "confirmed": 0,
        "lane": dict(daily[0]), "day_phase": "priority_pools",
        "scheduler": {"phase": "daily", "daily_queue": daily, "deep_queue": deep, "pending_date": "", "pending_target": 0},
        "economy": {"jds_since_success": 0, "auto_rejected": 0, "model_reviewed": 0, "lane_rejects": {}, "city_rejects": {}, "pool_confirmed": {}},
        "circuit": {"tripped": False, "signature": "", "job_ids": []},
        "generation": 0, "batch": {}, "processed_job_ids": [], "technical_job_ids": [], "last_completed": {},
        "checkpoint": {"safe_state": "started", "updated_at": iso_now()}, "lease": {},
        "control": fresh_control(state, "new_date"),
        "started_at": iso_now(), "updated_at": iso_now(),
    })


def cursor_path(state_path: Path) -> Path:
    return state_path.with_name("search-state-zhaopin.json")


def sync_cursor(state_path: Path, state: dict) -> None:
    lane = state.get("lane") or {}
    primary = lane.get("pool") or lane.get("keyword") or ""
    if not primary or not lane.get("city"):
        if not state.get("active") and (state.get("checkpoint") or {}).get("action") == "exhausted":
            write_json(cursor_path(state_path), {
                "platform": "zhaopin", "source": "", "keyword_or_pool": "", "city": "",
                "nextPage": 0, "nextPrimary": "", "date": state.get("date", ""), "exhausted": True,
            })
        return
    write_json(cursor_path(state_path), {
        "platform": "zhaopin", "source": lane.get("source", ""),
        "keyword_or_pool": primary, "city": lane["city"],
        "nextPage": int(lane.get("page") or 1), "nextPrimary": primary,
        "date": state.get("date", ""),
    })


def pop_key(queue: list[dict], key: str) -> None:
    for index, item in enumerate(queue):
        if lane_key(item) == key:
            queue.pop(index)
            return


def next_planned_lane(state: dict, completed: dict) -> tuple[dict, str]:
    scheduler_migrate(state)
    scheduler = state["scheduler"]
    completed_key = lane_key(completed)
    phase = scheduler.get("phase", "deep")
    queue_name = "daily_queue" if phase == "daily" else "deep_queue"
    queue = scheduler.get(queue_name) or []
    pop_key(queue, completed_key)
    scheduler[queue_name] = queue
    if phase == "daily":
        pop_key(scheduler.get("deep_queue") or [], completed_key)
        if queue:
            return dict(queue[0]), "next_daily_pool"
        scheduler["phase"] = "deep"
        state["day_phase"] = "normal"
        deep = scheduler.get("deep_queue") or []
        return (dict(deep[0]), "resume_cursor") if deep else ({}, "exhausted")
    if queue:
        current_primary = completed.get("pool") or completed.get("keyword")
        next_primary = queue[0].get("pool") or queue[0].get("keyword")
        if completed.get("city") == queue[0].get("city") and current_primary == next_primary:
            action = "next_page"
        elif completed.get("city") != queue[0].get("city"):
            action = "next_city"
        elif completed.get("source") != queue[0].get("source"):
            action = "next_round"
        else:
            action = "next_primary"
        economy = state.get("economy") or {}
        if int(economy.get("jds_since_success") or 0) >= ECONOMIC_JD_LIMIT:
            economy["jds_since_success"] = 0
            state["economy"] = economy
            action = "economic_rotate"
        return dict(queue[0]), action
    return {}, "exhausted"


def lease_valid(lease: dict) -> bool:
    try:
        return bool(lease.get("token")) and datetime.fromisoformat(str(lease["expires_at"])) > now_utc()
    except (KeyError, TypeError, ValueError):
        return False


def require_lease(state: dict, token: str) -> None:
    lease = state.get("lease") or {}
    if not lease_valid(lease) or not token or token != lease.get("token"):
        raise ValueError("execution_lease_required")


def pending(batch: dict) -> list[dict]:
    return [item for item in batch.get("items") or [] if item.get("status") == "pending"]


def local_known_ids(today: date) -> set[str]:
    known = set()
    for path, expiry_field in ((CONFIRMED, ""), (SEEN, "expires_on")):
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle, delimiter="\t"):
                job_id = clean(row.get("job_id"), 100)
                if not job_id:
                    continue
                if expiry_field:
                    try:
                        if date.fromisoformat(clean(row.get(expiry_field), 20)) < today:
                            continue
                    except ValueError:
                        continue
                known.add(job_id)
    return known


def confirmed_path(state_path: Path) -> Path:
    return CONFIRMED if state_path == DEFAULT_STATE.resolve() else state_path.with_name("confirmed-zhaopin.tsv")


def read_confirmed_rows(state_path: Path) -> list[dict[str, str]]:
    path = confirmed_path(state_path)
    if not path.exists() or not path.stat().st_size:
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if reader.fieldnames != CONFIRMED_HEADER:
            raise ValueError("confirmed_header_mismatch")
        return [{key: clean(row.get(key), 600) for key in CONFIRMED_HEADER} for row in reader]


def seed_confirmed_from_ledger(state: dict, state_path: Path) -> int:
    run_date = clean(state.get("date"), 20)
    rows = [row for row in read_confirmed_rows(state_path) if row.get("date") == run_date]
    ids = sorted({clean(row.get("job_id"), 120) for row in rows if clean(row.get("job_id"), 120)})
    state["confirmed"] = len(ids)
    state["processed_job_ids"] = sorted(set(state.get("processed_job_ids") or []) | set(ids))
    return len(ids)


def ledger_audit(state: dict, state_path: Path) -> dict:
    row_date = clean(state.get("date"), 20)
    rows = [row for row in read_confirmed_rows(state_path) if row.get("date") == row_date]
    confirmed = int(state.get("confirmed") or 0)
    return {
        "confirmed": confirmed,
        "ledger_today_rows": len(rows),
        "delta": len(rows) - confirmed,
        "consistent": len(rows) == confirmed,
    }


def require_resume_ledger_consistency(state: dict, state_path: Path) -> None:
    """Fail closed before a new same-state executor can take over."""
    if not state.get("active"):
        return
    audit = ledger_audit(state, state_path)
    if not audit["consistent"]:
        raise ValueError("ledger_reconciliation_required")


def require_success_ledger(state: dict, item: dict, state_path: Path) -> str:
    raw = os.environ.get("CODEX_ZHAOPIN_SUCCESS_RECEIPT", "")
    if not raw.strip():
        raise ValueError("success_receipt_required")
    try:
        receipt = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("success_receipt_invalid_json") from exc
    if not isinstance(receipt, dict):
        raise ValueError("success_receipt_must_be_object")
    allowed = {"job_id", "evidence", "appended", "duplicate"}
    required = {"job_id", "evidence"}
    if set(receipt) - allowed or not required.issubset(receipt):
        raise ValueError("success_receipt_schema_invalid")
    if clean(receipt.get("job_id"), 100) != clean(item.get("job_id"), 100):
        raise ValueError("success_receipt_job_mismatch")
    if clean(receipt.get("evidence"), 80) != "job-applied":
        raise ValueError("success_receipt_evidence_invalid")
    appended = receipt.get("appended") is True
    duplicate = receipt.get("duplicate") is True
    if not appended and not duplicate:
        raise ValueError("success_receipt_ledger_not_appended")
    rows = [row for row in read_confirmed_rows(state_path) if row.get("job_id") == item.get("job_id")]
    if not rows:
        raise ValueError("success_ledger_missing")
    current = [row for row in rows if row.get("date") == clean(state.get("date"), 20)]
    if not current:
        raise ValueError("success_ledger_date_mismatch")
    if appended and duplicate:
        raise ValueError("success_receipt_status_ambiguous")
    return "duplicate_reconciled" if duplicate else "appended"


def append_seen(item: dict, reason: str, row_date: date, state_path: Path, ttl_days: int = 7) -> None:
    seen_path = SEEN if state_path == DEFAULT_STATE.resolve() else state_path.with_name("seen-decisions-zhaopin.tsv")
    seen_path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    if seen_path.exists() and seen_path.stat().st_size:
        with seen_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            if reader.fieldnames != SEEN_HEADER:
                raise ValueError("seen_header_mismatch")
            rows = list(reader)
    job_id = clean(item.get("job_id"), 100)
    rows = [row for row in rows if clean(row.get("job_id"), 100) != job_id]
    rows.append({
        "date": row_date.isoformat(), "job_id": job_id,
        "company": clean(item.get("company")), "title": clean(item.get("title")),
        "reason": clean(reason, 120), "expires_on": (row_date + timedelta(days=ttl_days)).isoformat(),
    })
    fd, name = tempfile.mkstemp(prefix=seen_path.name + ".", suffix=".tmp", dir=seen_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=SEEN_HEADER, delimiter="\t", lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, seen_path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def append_technical(item: dict, lane: dict, error: str, row_date: date, state_path: Path) -> None:
    path = TECHNICAL if state_path == DEFAULT_STATE.resolve() else state_path.with_name("technical-failures-zhaopin.tsv")
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    if path.exists() and path.stat().st_size:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            if reader.fieldnames != TECH_HEADER:
                raise ValueError("technical_header_mismatch")
            rows = list(reader)
    primary = lane.get("pool") or lane.get("keyword") or ""
    rows.append({"date": row_date.isoformat(), "job_id": clean(item.get("job_id"), 100),
                 "lane": clean(f"{primary}|{lane.get('city')}|p{lane.get('page')}", 160),
                 "error": clean(error, 160), "recorded_at": iso_now()})
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=TECH_HEADER, delimiter="\t", lineterminator="\n")
            writer.writeheader(); writer.writerows(rows); handle.flush(); os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def append_technical_evidence(item: dict, lane: dict, error: str, evidence: dict, row_date: date, state_path: Path) -> None:
    path = TECHNICAL_EVIDENCE if state_path == DEFAULT_STATE.resolve() else state_path.with_name("technical-evidence-zhaopin.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "date": row_date.isoformat(),
        "recorded_at": iso_now(),
        "job_id": clean(item.get("job_id"), 100),
        "lane": {"pool": clean(lane.get("pool"), 80), "keyword": clean(lane.get("keyword"), 80), "city": clean(lane.get("city"), 40), "page": int(lane.get("page") or 0)},
        "error": clean(error, 160),
        "evidence": evidence,
    }
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def rank_card(item: dict):
    return (-jobflow.priority(item, "zhaopin"), item.get("job_id", ""))


def compact_status(state: dict) -> dict:
    batch = state.get("batch") or {}
    lease = state.get("lease") or {}
    control = state.get("control") or fresh_control(state)
    return {
        "ok": True,
        "active": bool(state.get("active")),
        "date": state.get("date", ""),
        "target": int(state.get("target") or 0),
        "confirmed": int(state.get("confirmed") or 0),
        "lane": state.get("lane") or {},
        "day_phase": state.get("day_phase", "normal"),
        "scheduler": {
            "phase": (state.get("scheduler") or {}).get("phase", "legacy"),
            "daily_remaining": len((state.get("scheduler") or {}).get("daily_queue") or []),
            "deep_remaining": len((state.get("scheduler") or {}).get("deep_queue") or []),
            "pending_date": (state.get("scheduler") or {}).get("pending_date", ""),
        },
        "economy": state.get("economy") or {},
        "circuit": {"tripped": bool((state.get("circuit") or {}).get("tripped")),
                    "signature": (state.get("circuit") or {}).get("signature", ""),
                    "count": len((state.get("circuit") or {}).get("job_ids") or [])},
        "generation": int(state.get("generation") or 0),
        "progress_revision": int(state.get("progress_revision") or 0),
        "executor_epoch": int(state.get("executor_epoch") or 0),
        "control": {
            "executor_epoch": int(control.get("executor_epoch") or 0),
            "last_progress_revision": int(control.get("last_progress_revision") or 0),
            "last_confirmed": int(control.get("last_confirmed") or 0),
            "unchanged_continue_count": int(control.get("unchanged_continue_count") or 0),
            "last_result": control.get("last_result", ""),
            "last_action": control.get("last_action", "unobserved"),
        },
        "batch": {
            "batch_id": batch.get("batch_id", ""),
            "page": batch.get("page", 0),
            "total": len(batch.get("items") or []),
            "pending": len(pending(batch)),
            "active_job_id": batch.get("active_job_id", ""),
        },
        "technical_quarantine": len(set(state.get("technical_job_ids") or [])),
        "last_completed": state.get("last_completed") or {},
        "checkpoint": state.get("checkpoint") or {},
        "lease": {"owner": lease.get("owner", ""), "valid": lease_valid(lease)},
    }


def parse_executor_receipt(raw: str) -> dict:
    try:
        receipt = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("executor_receipt_invalid_json") from exc
    if not isinstance(receipt, dict):
        raise ValueError("executor_receipt_must_be_object")
    result = receipt.get("Result")
    if result not in RECEIPT_RESULTS:
        raise ValueError("executor_receipt_result_invalid")
    common = {"Result", "confirmed", "progress_revision", "executor_epoch", "lane", "batch_pending", "lease"}
    allowed = common | ({"reason", "browser_cleanup_complete"} if result == "PRE_STOP" else set())
    if set(receipt) != allowed:
        raise ValueError("executor_receipt_schema_invalid")
    if not isinstance(receipt.get("lane"), dict):
        raise ValueError("executor_receipt_lane_invalid")
    for key in ("confirmed", "progress_revision", "executor_epoch", "batch_pending"):
        if not isinstance(receipt.get(key), int) or isinstance(receipt.get(key), bool) or int(receipt[key]) < 0:
            raise ValueError(f"executor_receipt_{key}_invalid")
    if result == "PRE_STOP":
        if not clean(receipt.get("reason"), 160) or receipt.get("browser_cleanup_complete") is not True:
            raise ValueError("executor_receipt_cleanup_invalid")
    return receipt


def observe_executor_receipt(state: dict, receipt: dict) -> dict:
    result = receipt["Result"]
    epoch = int(receipt["executor_epoch"])
    revision = int(receipt["progress_revision"])
    confirmed = int(receipt["confirmed"])
    if epoch != int(state.get("executor_epoch") or 0):
        raise ValueError("stale_executor_receipt")
    if revision != int(state.get("progress_revision") or 0):
        raise ValueError("executor_receipt_revision_mismatch")
    if confirmed != int(state.get("confirmed") or 0):
        raise ValueError("executor_receipt_confirmed_mismatch")
    expected_lane = state.get("lane") or {}
    receipt_lane = dict(receipt.get("lane") or {})
    # Empty opposite-side lane fields are optional in compact receipts. The
    # identity-bearing source/pool-or-keyword/city/page fields remain strict.
    if receipt_lane.get("source") == "expectation_pool" and "keyword" not in receipt_lane:
        receipt_lane["keyword"] = ""
    if receipt_lane.get("source") == "keyword" and "pool" not in receipt_lane:
        receipt_lane["pool"] = ""
    if receipt_lane != expected_lane:
        raise ValueError("executor_receipt_lane_mismatch")
    if int(receipt.get("batch_pending") or 0) != len(pending(state.get("batch") or {})):
        raise ValueError("executor_receipt_batch_mismatch")
    control = dict(state.get("control") or fresh_control(state))
    if int(control.get("executor_epoch") or 0) != epoch:
        raise ValueError("stale_executor_receipt")
    previous_revision = int(control.get("last_progress_revision") or 0)
    if revision < previous_revision:
        raise ValueError("stale_executor_receipt")
    if result in ("CONTINUE", "SUCCESS_HEARTBEAT"):
        if receipt.get("lease") != "held" or not lease_valid(state.get("lease") or {}):
            raise ValueError("executor_receipt_live_lease_required")
        if result == "CONTINUE":
            unchanged = int(control.get("unchanged_continue_count") or 0) + 1 if revision == previous_revision else 0
            action = "pause_same_executor" if unchanged >= 2 else "continue_same_executor"
        else:
            unchanged = 0
            action = "continue_same_executor"
    else:
        if receipt.get("lease") != "released" or lease_valid(state.get("lease") or {}):
            raise ValueError("executor_receipt_released_lease_required")
        unchanged = 0
        action = "terminal_recorded"
    state["control"] = {
        "executor_epoch": epoch,
        "last_progress_revision": revision,
        "last_confirmed": confirmed,
        "unchanged_continue_count": unchanged,
        "last_result": result,
        "last_action": action,
        "updated_at": iso_now(),
    }
    return {
        "ok": True,
        "action": action,
        "executor_epoch": epoch,
        "progress_revision": revision,
        "unchanged_continue_count": unchanged,
    }


def parse_cards(raw: str) -> list[dict]:
    value = json.loads(raw)
    if not isinstance(value, list):
        raise ValueError("cards_must_be_list")
    result = []
    seen = set()
    for row in value:
        if not isinstance(row, list) or len(row) < 5:
            raise ValueError("card_must_be_positional_array")
        job_id = clean(row[0], 100)
        if not job_id or job_id in seen:
            continue
        seen.add(job_id)
        result.append({
            "job_id": job_id,
            "title": clean(row[1]),
            "company": clean(row[2]),
            "city": clean(row[3], 40),
            "salary": clean(row[4], 80),
            "state": clean(row[5], 40) if len(row) > 5 else "",
            "signals": clean(row[6]) if len(row) > 6 else "",
            "href": clean(row[7], 600) if len(row) > 7 else "",
            "status": "pending",
            "reason": "",
        })
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Local Zhaopin batch workbench")
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start")
    start.add_argument("--target", type=int, required=True)
    start.add_argument("--date", default=date.today().isoformat())
    sub.add_parser("status")
    sub.add_parser("ledger-audit")

    acquire = sub.add_parser("lease-acquire")
    acquire.add_argument("--owner", required=True)
    acquire.add_argument("--ttl-seconds", type=int, default=1800)
    release = sub.add_parser("lease-release")
    release.add_argument("--lease-token", required=True)

    lane = sub.add_parser("lane-set")
    lane.add_argument("--lease-token", required=True)
    lane_source = lane.add_mutually_exclusive_group(required=True)
    lane_source.add_argument("--pool", choices=POOLS)
    lane_source.add_argument("--keyword", choices=KEYWORDS)
    lane.add_argument("--city", required=True, choices=CITIES)
    lane.add_argument("--page", type=int, default=1, choices=range(1, 5))

    repair = sub.add_parser("repair-pristine-daily")
    repair.add_argument("--lease-token", required=True)
    repair_source = repair.add_mutually_exclusive_group(required=True)
    repair_source.add_argument("--resume-pool", choices=POOLS)
    repair_source.add_argument("--resume-keyword", choices=KEYWORDS)
    repair.add_argument("--resume-city", required=True, choices=CITIES)
    repair.add_argument("--resume-page", type=int, required=True, choices=range(1, 5))

    ingest = sub.add_parser("batch-ingest")
    ingest.add_argument("--lease-token", required=True)
    ingest_source = ingest.add_mutually_exclusive_group(required=True)
    ingest_source.add_argument("--cards-env", action="store_true")
    ingest_source.add_argument("--cards-result-env", action="store_true")
    rebase = sub.add_parser("batch-rebase")
    rebase.add_argument("--lease-token", required=True)
    rebase.add_argument("--cards-result-env", action="store_true", required=True)
    next_item = sub.add_parser("next")
    next_item.add_argument("--lease-token", required=True)
    decision = sub.add_parser("decision")
    decision.add_argument("--lease-token", required=True)
    decision.add_argument("--job-id", required=True)
    decision.add_argument("--decision", choices=TERMINAL_DECISIONS, required=True)
    decision.add_argument("--reason", required=True)
    decision.add_argument("--review-mode", choices=("auto", "model"), default="model")
    detail_resolve = sub.add_parser("detail-resolve")
    detail_resolve.add_argument("--lease-token", required=True)
    detail_resolve.add_argument("--detail-env", action="store_true", required=True)
    technical = sub.add_parser("technical-failure")
    technical.add_argument("--lease-token", required=True)
    technical.add_argument("--job-id", required=True)
    technical.add_argument("--error", required=True)
    technical.add_argument("--evidence-env", action="store_true")
    circuit_reset = sub.add_parser("circuit-reset")
    circuit_reset.add_argument("--lease-token", required=True)
    migrate = sub.add_parser("migrate-processed-seen")
    migrate.add_argument("--lease-token", required=True)
    scheduler_migration = sub.add_parser("scheduler-migrate")
    scheduler_migration.add_argument("--lease-token", required=True)
    complete = sub.add_parser("page-complete")
    complete.add_argument("--lease-token", required=True)
    pause = sub.add_parser("pause")
    pause.add_argument("--lease-token", required=True)
    pause.add_argument("--reason", required=True)
    receipt = sub.add_parser("receipt-observe")
    receipt.add_argument("--receipt-env", action="store_true", required=True)
    sub.add_parser("finalize-exhausted", help=argparse.SUPPRESS)

    args = parser.parse_args()
    path = args.state_path.resolve()
    state = read_json(path)

    try:
        if args.command == "start":
            if args.target < 1:
                raise ValueError("target_must_be_positive")
            if (not state.get("active") and state.get("date") == args.date
                    and (state.get("checkpoint") or {}).get("action") == "exhausted"):
                result = {**compact_status(state), "resumed": False, "already_exhausted": True}
            elif state.get("active") and state.get("date") == args.date:
                current_target = int(state.get("target") or 0)
                target_extended = args.target > current_target
                if target_extended:
                    state["target"] = args.target
                    state["checkpoint"] = {
                        "safe_state": "target_extended",
                        "reason": "user_requested_same_day_quota",
                        "updated_at": iso_now(),
                    }
                changed = scheduler_migrate(state)
                if changed or target_extended:
                    bump_progress(state)
                    state["updated_at"] = iso_now()
                    write_json(path, state)
                    sync_cursor(path, state)
                result = {**compact_status(state), "resumed": True}
            elif (state.get("active") and (pending(state.get("batch") or {}) or (state.get("batch") or {}).get("active_job_id"))
                  and not lease_valid(state.get("lease") or {})):
                retired_batch_id = (state.get("batch") or {}).get("batch_id", "")
                state = base_state(args.target, args.date, state)
                seeded_confirmed = seed_confirmed_from_ledger(state, path)
                state["checkpoint"] = {
                    "safe_state": "started",
                    "reason": "retired_stale_batch_on_new_date",
                    "retired_batch_id": retired_batch_id,
                    "seeded_confirmed": seeded_confirmed,
                    "updated_at": iso_now(),
                }
                write_json(path, state)
                sync_cursor(path, state)
                result = {**compact_status(state), "resumed": False, "retired_pending_batch": True}
            elif state.get("active") and (pending(state.get("batch") or {}) or (state.get("batch") or {}).get("active_job_id")):
                scheduler_migrate(state)
                state["scheduler"]["pending_date"] = args.date
                state["scheduler"]["pending_target"] = args.target
                bump_progress(state)
                state["updated_at"] = iso_now()
                write_json(path, state)
                result = {**compact_status(state), "resumed": True, "date_transition_pending": True}
            else:
                state = base_state(args.target, args.date, state)
                seeded_confirmed = seed_confirmed_from_ledger(state, path)
                if seeded_confirmed:
                    state["checkpoint"] = {
                        "safe_state": "started",
                        "reason": "seeded_confirmed_ledger",
                        "seeded_confirmed": seeded_confirmed,
                        "updated_at": iso_now(),
                    }
                write_json(path, state)
                sync_cursor(path, state)
                result = {**compact_status(state), "resumed": False, "seeded_confirmed": seeded_confirmed}
        elif args.command == "status":
            result = compact_status(state) if state else {"ok": True, "active": False}
        elif args.command == "ledger-audit":
            result = {"ok": True, **ledger_audit(state, path)}
        elif args.command == "lease-acquire":
            existing = state.get("lease") or {}
            if lease_valid(existing) and existing.get("owner") != args.owner:
                raise ValueError("execution_already_claimed")
            new_execution = not lease_valid(existing)
            if new_execution:
                require_resume_ledger_consistency(state, path)
            if new_execution:
                state["executor_epoch"] = int(state.get("executor_epoch") or 0) + 1
            token = existing.get("token") if lease_valid(existing) else "lease_" + secrets.token_urlsafe(18)
            state["lease"] = {
                "owner": clean(args.owner, 100),
                "token": token,
                "expires_at": (now_utc() + timedelta(seconds=max(60, args.ttl_seconds))).isoformat(timespec="seconds"),
            }
            if new_execution:
                state["control"] = fresh_control(state, "executor_started")
            state["updated_at"] = iso_now()
            write_json(path, state)
            result = {"ok": True, "owner": args.owner, "lease_token": token,
                      "executor_epoch": int(state.get("executor_epoch") or 0),
                      "progress_revision": int(state.get("progress_revision") or 0),
                      "expires_at": state["lease"]["expires_at"]}
        elif args.command == "lease-release":
            require_lease(state, args.lease_token)
            state["lease"] = {}
            state["updated_at"] = iso_now()
            write_json(path, state)
            result = {"ok": True, "released": True}
        elif args.command == "receipt-observe":
            receipt_value = parse_executor_receipt(os.environ.get("CODEX_ZHAOPIN_EXECUTOR_RECEIPT", ""))
            result = observe_executor_receipt(state, receipt_value)
            state["updated_at"] = iso_now()
            write_json(path, state)
        elif args.command == "finalize-exhausted":
            scheduler = state.get("scheduler") or {}
            batch = state.get("batch") or {}
            if (state.get("active") or pending(batch) or batch.get("active_job_id")
                    or scheduler.get("daily_queue") or scheduler.get("deep_queue")
                    or (state.get("checkpoint") or {}).get("action") != "exhausted"):
                raise ValueError("exhausted_state_required")
            state["lease"] = {}
            state["updated_at"] = iso_now()
            write_json(path, state)
            sync_cursor(path, state)
            result = {"ok": True, "finalized": True, **compact_status(state)}
        elif args.command == "repair-pristine-daily":
            require_lease(state, args.lease_token)
            if int(state.get("confirmed") or 0) != 0 or int(state.get("generation") or 0) != 0 or state.get("last_completed") or (state.get("batch") or {}).get("batch_id"):
                raise ValueError("pristine_daily_repair_not_allowed")
            resume_lane = {
                "source": "keyword" if args.resume_keyword else "expectation_pool",
                "pool": args.resume_pool or "",
                "keyword": args.resume_keyword or "",
                "city": args.resume_city,
                "page": args.resume_page,
            }
            daily = daily_refresh_queue()
            state["lane"] = dict(daily[0])
            state["day_phase"] = "priority_pools"
            state["scheduler"] = {"phase": "daily", "daily_queue": daily, "deep_queue": queue_from_lane(resume_lane), "pending_date": "", "pending_target": 0}
            state["batch"] = {}
            state["circuit"] = {"tripped": False, "signature": "", "scope": "", "job_ids": []}
            state["checkpoint"] = {"safe_state": "daily_repaired", "updated_at": iso_now()}
            bump_progress(state)
            state["updated_at"] = iso_now()
            write_json(path, state)
            sync_cursor(path, state)
            result = {"ok": True, "repaired": True, **compact_status(state)}
        elif args.command == "lane-set":
            require_lease(state, args.lease_token)
            if pending(state.get("batch") or {}):
                raise ValueError("pending_batch_must_be_drained")
            if args.keyword and args.page > 2:
                raise ValueError("keyword_page_out_of_range")
            requested_lane = {
                "source": "keyword" if args.keyword else "expectation_pool",
                "pool": args.pool or "",
                "keyword": args.keyword or "",
                "city": args.city,
                "page": args.page,
            }
            same_priority_lane = state.get("day_phase") == "priority_pools" and lane_key(requested_lane) == lane_key(state.get("lane") or {})
            state["lane"] = requested_lane
            if not same_priority_lane:
                state["day_phase"] = "normal"
                state["schema"] = 3
                state["scheduler"] = {"phase": "deep", "daily_queue": [], "deep_queue": queue_from_lane(state["lane"]), "pending_date": "", "pending_target": 0}
            state["batch"] = {}
            state["checkpoint"] = {"safe_state": "lane_ready", "updated_at": iso_now()}
            bump_progress(state)
            state["updated_at"] = iso_now()
            write_json(path, state)
            sync_cursor(path, state)
            result = {"ok": True, "lane": state["lane"]}
        elif args.command == "batch-ingest":
            require_lease(state, args.lease_token)
            old = state.get("batch") or {}
            if old.get("batch_id"):
                raise ValueError("pending_batch_must_be_drained")
            env_name = "CODEX_ENTERPRISE_CARDS_RESULT_JSON" if args.cards_result_env else "CODEX_ENTERPRISE_CARDS_JSON"
            raw = os.environ.get(env_name, "")
            if not raw.strip():
                raise ValueError(f"{env_name}_missing")
            snapshot = {}
            if args.cards_result_env:
                snapshot = json.loads(raw)
                guard_reason = clean(snapshot.get("reason"), 80) if isinstance(snapshot, dict) else ""
                exhausted_guard = (
                    isinstance(snapshot, dict)
                    and snapshot.get("ok") is False
                    and snapshot.get("terminal") is False
                    and snapshot.get("action") == "switch_lane"
                    and guard_reason in {"pager_exhausted", "page_budget"}
                )
                if exhausted_guard:
                    snapshot = {**snapshot, "cards": [], "rawCount": 0, "pageFingerprint": ""}
                if not isinstance(snapshot, dict) or (not snapshot.get("ok") and not exhausted_guard) or not isinstance(snapshot.get("cards"), list):
                    raise ValueError("cards_result_invalid")
                lane_page = int((state.get("lane") or {}).get("page") or 0)
                if snapshot.get("pageCall") is not None and int(snapshot["pageCall"]) != lane_page:
                    raise ValueError("cards_result_page_mismatch")
                raw = json.dumps(snapshot["cards"], ensure_ascii=False)
            processed = set(state.get("processed_job_ids") or [])
            technical = set(state.get("technical_job_ids") or [])
            known = processed | technical | local_known_ids(date.today())
            parsed = parse_cards(raw)
            cards = sorted((item for item in parsed if item["job_id"] not in known), key=rank_card)
            state["generation"] = int(state.get("generation") or 0) + 1
            lane_value = state.get("lane") or {}
            primary = lane_value.get("pool") or lane_value.get("keyword") or ""
            batch_id = f"{state.get('date')}:{state['generation']}:{primary}:{lane_value.get('city','')}:{lane_value.get('page',0)}"
            state["batch"] = {
                "batch_id": batch_id,
                "generation": state["generation"],
                "lane": dict(lane_value),
                "page": int(lane_value.get("page") or 0),
                "items": cards,
                "active_job_id": "",
                "created_at": iso_now(),
                "source_snapshot": {
                    "fingerprint": clean(snapshot.get("pageFingerprint"), 1200),
                    "raw_count": int(snapshot.get("rawCount") or len(parsed)),
                    "skips": snapshot.get("skips") if isinstance(snapshot.get("skips"), dict) else {},
                    "guard": clean(snapshot.get("reason"), 80) if exhausted_guard else "",
                } if snapshot else {},
            }
            state["checkpoint"] = {"safe_state": "batch_ready", "updated_at": iso_now()}
            bump_progress(state)
            state["updated_at"] = iso_now()
            write_json(path, state)
            result = {"ok": True, "batch_id": batch_id, "generation": state["generation"], "raw_count": len(parsed), "known_filtered": len(parsed) - len(cards), "count": len(cards)}
        elif args.command == "batch-rebase":
            require_lease(state, args.lease_token)
            circuit = state.get("circuit") or {}
            batch = state.get("batch") or {}
            if not circuit.get("tripped") or circuit.get("signature") != "stale_card":
                raise ValueError("stale_page_rebase_required")
            if not batch.get("batch_id") or batch.get("active_job_id"):
                raise ValueError("stale_page_rebase_requires_idle_batch")
            raw = os.environ.get("CODEX_ENTERPRISE_CARDS_RESULT_JSON", "")
            if not raw.strip():
                raise ValueError("CODEX_ENTERPRISE_CARDS_RESULT_JSON_missing")
            snapshot = json.loads(raw)
            if not isinstance(snapshot, dict) or not snapshot.get("ok") or not isinstance(snapshot.get("cards"), list):
                raise ValueError("cards_result_invalid")
            lane_page = int((state.get("lane") or {}).get("page") or 0)
            if snapshot.get("pageCall") is not None and int(snapshot["pageCall"]) != lane_page:
                raise ValueError("cards_result_page_mismatch")
            new_fingerprint = clean(snapshot.get("pageFingerprint"), 1200)
            old_fingerprint = clean((batch.get("source_snapshot") or {}).get("fingerprint"), 1200)
            parsed = parse_cards(json.dumps(snapshot["cards"], ensure_ascii=False))
            processed = set(state.get("processed_job_ids") or [])
            technical = set(state.get("technical_job_ids") or [])
            known = processed | technical | local_known_ids(date.today())
            cards = sorted((item for item in parsed if item["job_id"] not in known), key=rank_card)
            current_ids = {item["job_id"] for item in parsed}
            old_ids = {item.get("job_id") for item in batch.get("items") or [] if item.get("job_id")}
            superseded = sorted(old_ids - current_ids)
            if not new_fingerprint or (old_fingerprint and new_fingerprint == old_fingerprint) or (not old_fingerprint and not superseded):
                raise ValueError("stale_page_fingerprint_unchanged")
            old_batch_id = batch.get("batch_id", "")
            state["generation"] = int(state.get("generation") or 0) + 1
            lane_value = state.get("lane") or {}
            primary = lane_value.get("pool") or lane_value.get("keyword") or ""
            batch_id = f"{state.get('date')}:{state['generation']}:{primary}:{lane_value.get('city','')}:{lane_value.get('page',0)}"
            state["batch"] = {
                "batch_id": batch_id,
                "generation": state["generation"],
                "lane": dict(lane_value),
                "page": int(lane_value.get("page") or 0),
                "items": cards,
                "active_job_id": "",
                "created_at": iso_now(),
                "source_snapshot": {
                    "fingerprint": new_fingerprint,
                    "raw_count": int(snapshot.get("rawCount") or len(parsed)),
                    "skips": snapshot.get("skips") if isinstance(snapshot.get("skips"), dict) else {},
                    "reconciled_from": old_batch_id,
                    "superseded_job_ids": superseded,
                    "legacy_snapshot": not bool(old_fingerprint),
                },
            }
            state["circuit"] = {"tripped": False, "signature": "", "scope": "", "job_ids": []}
            state["checkpoint"] = {"safe_state": "batch_rebased", "reason": "stale_page_reconciled", "updated_at": iso_now()}
            bump_progress(state)
            state["updated_at"] = iso_now()
            write_json(path, state)
            result = {"ok": True, "reconciled": True, "from_batch_id": old_batch_id, "batch_id": batch_id,
                      "generation": state["generation"], "superseded": len(superseded), "count": len(cards), "page_fingerprint": new_fingerprint}
        elif args.command == "next":
            require_lease(state, args.lease_token)
            if (state.get("circuit") or {}).get("tripped"):
                raise ValueError("technical_circuit_breaker")
            batch = state.get("batch") or {}
            active_id = batch.get("active_job_id")
            items = batch.get("items") or []
            item = next((x for x in items if x.get("job_id") == active_id and x.get("status") == "pending"), None)
            if item is None:
                item = next((x for x in items if x.get("status") == "pending"), None)
                batch["active_job_id"] = item.get("job_id") if item else ""
                state["batch"] = batch
                state["checkpoint"] = {"safe_state": "candidate_selected" if item else "batch_drained", "updated_at": iso_now()}
                bump_progress(state)
                state["updated_at"] = iso_now()
                write_json(path, state)
            result = {"ok": True, "batch_id": batch.get("batch_id", ""), "generation": batch.get("generation", 0), "candidate": item, "drained": item is None}
        elif args.command == "technical-failure":
            require_lease(state, args.lease_token)
            batch = state.get("batch") or {}
            if batch.get("active_job_id") != args.job_id:
                raise ValueError("job_not_active_in_current_batch")
            item = next((x for x in batch.get("items") or [] if x.get("job_id") == args.job_id and x.get("status") == "pending"), None)
            if not item:
                raise ValueError("stale_batch_job")
            signature = clean(args.error, 160)
            evidence = {}
            if args.evidence_env:
                raw_evidence = os.environ.get("CODEX_ENTERPRISE_TECHNICAL_EVIDENCE_JSON", "")
                if not raw_evidence.strip():
                    raise ValueError("CODEX_ENTERPRISE_TECHNICAL_EVIDENCE_JSON_missing")
                evidence = json.loads(raw_evidence)
                if not isinstance(evidence, dict):
                    raise ValueError("technical_evidence_invalid")
            item["status"] = "technical"; item["reason"] = signature; item["decided_at"] = iso_now(); batch["active_job_id"] = ""
            append_technical(item, state.get("lane") or {}, signature, date.fromisoformat(state["date"]), path)
            if evidence:
                append_technical_evidence(item, state.get("lane") or {}, signature, evidence, date.fromisoformat(state["date"]), path)
            old = state.get("circuit") or {}
            lane_value = state.get("lane") or {}
            scope = clean(f"{lane_value.get('pool') or lane_value.get('keyword')}|{lane_value.get('city')}|p{lane_value.get('page')}", 160)
            ids = list(old.get("job_ids") or []) if old.get("signature") == signature and old.get("scope") == scope else []
            if args.job_id not in ids: ids.append(args.job_id)
            circuit = {"tripped": len(ids) >= TECH_BREAKER_THRESHOLD, "signature": signature, "scope": scope, "job_ids": ids[-TECH_BREAKER_THRESHOLD:]}
            technical_ids = set(state.get("technical_job_ids") or [])
            technical_ids.add(args.job_id)
            state["technical_job_ids"] = sorted(technical_ids)
            state["circuit"] = circuit; state["batch"] = batch
            if circuit["tripped"]:
                state["checkpoint"] = {"safe_state": "technical_circuit", "error": signature, "updated_at": iso_now()}; state["lease"] = {}
            else:
                state["checkpoint"] = {"safe_state": "technical_recorded", "updated_at": iso_now()}
            bump_progress(state)
            state["updated_at"] = iso_now(); write_json(path, state)
            result = {"ok": True, "technical": True, "evidence_recorded": bool(evidence), "circuit_breaker": circuit["tripped"], "signature": signature, "scope": scope, "count": len(ids), "remaining": len(pending(batch))}
        elif args.command == "circuit-reset":
            require_lease(state, args.lease_token)
            state["circuit"] = {"tripped": False, "signature": "", "job_ids": []}
            state["checkpoint"] = {"safe_state": "circuit_reset", "updated_at": iso_now()}
            bump_progress(state); state["updated_at"] = iso_now(); write_json(path, state)
            result = {"ok": True, "reset": True}
        elif args.command == "detail-resolve":
            require_lease(state, args.lease_token)
            raw = os.environ.get("CODEX_ENTERPRISE_DETAIL_JSON", "")
            if not raw.strip():
                raise ValueError("CODEX_ENTERPRISE_DETAIL_JSON_missing")
            detail = json.loads(raw)
            batch = state.get("batch") or {}
            active_id = batch.get("active_job_id")
            item = next((x for x in batch.get("items") or [] if x.get("job_id") == active_id and x.get("status") == "pending"), None)
            if not item or clean(detail.get("jobId"), 100) != active_id:
                raise ValueError("detail_not_bound_to_active_job")
            reason = clean(detail.get("autoReject"), 120)
            if not reason:
                result = {"ok": True, "action": "model_review_required", "job_id": active_id, "detail": detail}
            else:
                item["status"] = "rejected"
                item["reason"] = reason
                item["review_mode"] = "auto"
                item["decided_at"] = iso_now()
                state["processed_job_ids"] = sorted(set(state.get("processed_job_ids") or []) | {active_id})
                append_seen(item, reason, date.fromisoformat(state["date"]), path)
                economy = state.get("economy") or {"jds_since_success": 0, "auto_rejected": 0, "model_reviewed": 0}
                economy["jds_since_success"] = int(economy.get("jds_since_success") or 0) + 1
                economy["auto_rejected"] = int(economy.get("auto_rejected") or 0) + 1
                state["economy"] = economy
                next_item = next((x for x in batch.get("items") or [] if x.get("status") == "pending"), None)
                batch["active_job_id"] = next_item.get("job_id") if next_item else ""
                state["batch"] = batch
                state["checkpoint"] = {"safe_state": "candidate_selected" if next_item else "batch_drained", "updated_at": iso_now()}
                bump_progress(state)
                state["updated_at"] = iso_now()
                write_json(path, state)
                result = {"ok": True, "action": "auto_rejected", "job_id": active_id, "reason": reason,
                          "next_candidate": next_item, "drained": next_item is None, "economy": economy}
        elif args.command == "migrate-processed-seen":
            require_lease(state, args.lease_token)
            row_date = date.fromisoformat(state["date"])
            known = local_known_ids(row_date)
            migrated = 0
            for job_id in state.get("processed_job_ids") or []:
                if job_id in known:
                    continue
                append_seen({"job_id": job_id, "company": "未保留", "title": "未保留"}, "当日已检查", row_date, path)
                known.add(job_id)
                migrated += 1
            result = {"ok": True, "migrated": migrated, "processed": len(state.get("processed_job_ids") or [])}
        elif args.command == "scheduler-migrate":
            require_lease(state, args.lease_token)
            changed = scheduler_migrate(state)
            state["updated_at"] = iso_now()
            write_json(path, state)
            sync_cursor(path, state)
            result = {"ok": True, "changed": changed, **compact_status(state)}
        elif args.command == "decision":
            require_lease(state, args.lease_token)
            batch = state.get("batch") or {}
            if batch.get("active_job_id") != args.job_id:
                raise ValueError("job_not_active_in_current_batch")
            item = next((x for x in batch.get("items") or [] if x.get("job_id") == args.job_id), None)
            if not item or item.get("status") != "pending":
                raise ValueError("stale_batch_job")
            success_ledger_status = ""
            if args.decision == "applied":
                success_ledger_status = require_success_ledger(state, item, path)
            item["status"] = args.decision
            item["reason"] = clean(args.reason, 120)
            item["decided_at"] = iso_now()
            item["review_mode"] = args.review_mode
            batch["active_job_id"] = ""
            state["processed_job_ids"] = sorted(set(state.get("processed_job_ids") or []) | {args.job_id})
            economy = state.get("economy") or {"jds_since_success": 0, "auto_rejected": 0, "model_reviewed": 0}
            if args.decision == "applied":
                state["confirmed"] = int(state.get("confirmed") or 0) + 1
                economy["jds_since_success"] = 0
            else:
                if args.decision in {"rejected", "error", "deferred"}:
                    append_seen(item, item["reason"], date.fromisoformat(state["date"]), path)
                economy["jds_since_success"] = int(economy.get("jds_since_success") or 0) + 1
                key = "auto_rejected" if args.review_mode == "auto" else "model_reviewed"
                economy[key] = int(economy.get(key) or 0) + 1
            state["economy"] = economy
            state["circuit"] = {"tripped": False, "signature": "", "scope": "", "job_ids": []}
            state["batch"] = batch
            state["checkpoint"] = {"safe_state": "candidate_decided", "updated_at": iso_now()}
            bump_progress(state)
            state["updated_at"] = iso_now()
            write_json(path, state)
            result = {"ok": True, "job_id": args.job_id, "decision": args.decision, "remaining": len(pending(batch)), "confirmed": state.get("confirmed", 0), "economy": economy}
            if success_ledger_status:
                result["success_ledger"] = success_ledger_status
        elif args.command == "page-complete":
            require_lease(state, args.lease_token)
            if (state.get("circuit") or {}).get("tripped"):
                raise ValueError("technical_circuit_breaker")
            batch = state.get("batch") or {}
            if not batch.get("batch_id"):
                raise ValueError("page_batch_required")
            if pending(batch) or batch.get("active_job_id"):
                raise ValueError("pending_batch_must_be_drained")
            lane_value = state.get("lane") or {}
            completed = {**lane_value, "completedPage": int(lane_value.get("page") or 0), "completed_at": iso_now()}
            state["last_completed"] = completed
            state["circuit"] = {"tripped": False, "signature": "", "scope": "", "job_ids": []}
            next_lane, action = next_planned_lane(state, lane_value)
            state["batch"] = {}
            pending_date = (state.get("scheduler") or {}).get("pending_date", "")
            if pending_date:
                pending_target = int((state.get("scheduler") or {}).get("pending_target") or state.get("target") or 1)
                activate_new_date(state, pending_target, pending_date)
                next_lane, action = dict(state["lane"]), "start_daily_refresh_after_pending_batch"
            else:
                state["lane"] = next_lane
                state["active"] = action != "exhausted"
                state["checkpoint"] = {"safe_state": "page_boundary", "action": action, "updated_at": iso_now()}
                if action == "exhausted":
                    state["lease"] = {}
            bump_progress(state)
            state["updated_at"] = iso_now()
            write_json(path, state)
            sync_cursor(path, state)
            result = {"ok": True, "last_completed": completed, "action": action, "next_lane": next_lane, "exhausted": action == "exhausted"}
        else:
            require_lease(state, args.lease_token)
            state["checkpoint"] = {"safe_state": "paused", "reason": clean(args.reason, 160), "updated_at": iso_now()}
            state["lease"] = {}
            bump_progress(state)
            state["updated_at"] = iso_now()
            write_json(path, state)
            result = {"ok": True, "paused": True, "checkpoint": state["checkpoint"]}
    except ValueError as exc:
        result = {"ok": False, "error": str(exc)}

    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
