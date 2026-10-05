#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from typing import Callable

import boss_bridge
import boss_ledger


import config_core as jobflow

RECOVERABLE_ERRORS = {
    "jd_body_not_ready",
    "initial_listing_empty",
    "load_more_no_growth",
    "city_rotation_fuse_not_tripped",
    "current_shortlist_not_resolved",
}
CANDIDATE_SKIP_ERRORS = {
    "job_not_loaded",
    "job_not_in_current_batch_shortlist",
    "stale_shortlist_generation_or_job",
}
HARD_SAFETY_ERRORS = {
    "job_identity_mismatch",
    "search_city_route_mismatch",
    "recommend_city_route_mismatch",
    "communication_requires_detail_tab",
    "communication_requires_derived_detail_tab",
    "base_jobs_provenance_required_before_close",
    "refuse_to_close_base_jobs_tab",
    "execution_lease_mismatch",
    "execution_lease_expired",
    "execution_lease_held",
}


def error_code(value: object) -> str:
    text = str(value or "unknown_error")
    return text.split(":", 1)[0].strip()


def classify_error(value: object) -> str:
    text = str(value or "")
    if "jd_body_not_ready" in text:
        return "RECOVERABLE"
    code = error_code(value)
    if code in RECOVERABLE_ERRORS:
        return "RECOVERABLE"
    if code in CANDIDATE_SKIP_ERRORS:
        return "CANDIDATE_SKIP"
    if code in HARD_SAFETY_ERRORS or any(
        marker in code
        for marker in ("identity_mismatch", "security", "login", "draft", "receipt", "send")
    ):
        return "HARD_SAFETY"
    return "EXECUTION_BUG"


def result_error(result: dict) -> str:
    return str(result.get("error") or (result.get("data") or {}).get("error") or "unknown_error")


def _unresolved_shortlist_recovery(result: dict) -> dict:
    """Return a continuation hint without weakening the unresolved-job gate."""
    if error_code(result_error(result)) != "current_shortlist_not_resolved":
        return {**result, "error_class": classify_error(result_error(result))}
    return {
        **result,
        "error_class": "RECOVERABLE",
        "next_action": "resolve_current_shortlist",
        "instruction": "resolve every job in the current shortlist before retrying this command",
    }


def safe_read_timeout(result: dict) -> bool:
    return (
        error_code(result_error(result)) == "timeout"
        and str(result.get("current_path") or "") == "/web/geek/jobs"
        and str(result.get("safe_state") or "") == "lane_connected"
    )


def safe_selection_timeout(result: dict) -> bool:
    """Allow one expected-ID detail read after a safe click timeout.

    A raw bridge timeout does not prove whether the click reached the page.
    When the pinned jobs lane is still connected, one expected-ID detail read
    can prove success without issuing a second click. Any mismatch remains a
    hard stop.
    """
    return (
        error_code(result_error(result)) == "timeout"
        and str(result.get("current_path") or "") == "/web/geek/jobs"
        and str(result.get("safe_state") or "") == "lane_connected"
    )


def _record_error(token: str, phase: str, result: dict, path=None) -> dict:
    error = result_error(result)
    error_class = classify_error(error)
    boss_ledger.update_execution(token, phase, {
        "last_error_class": error_class,
        "last_error": error[:240],
    }, path or boss_ledger.BATCH_STATE)
    return {**result, "error_class": error_class}


def _lease_guard(token: str, path) -> dict:
    """Guard supervisor entry points and expose one safe recovery action.

    Lease mismatches/expiry are recoverable only while the persisted phase is
    still discovery or screening.  The executor must run the explicit
    same-owner ``lease-reacquire`` command, then retry the original command
    once.  Send-sensitive phases remain hard stops and never auto-reacquire.
    """
    state = boss_ledger.read_json(path)
    guard = boss_ledger.assert_execution_lease(state, token)
    if guard.get("ok"):
        return guard
    code = error_code(guard.get("error"))
    phase = str(guard.get("phase") or (state.get("execution") or {}).get("phase") or "PREFLIGHT").upper()
    if code in {"execution_lease_mismatch", "execution_lease_expired"} and phase in boss_ledger.SAFE_REACQUIRE_PHASES:
        owner = str(guard.get("owner") or (state.get("execution") or {}).get("owner") or "")
        return {
            **guard,
            "error_class": "RECOVERABLE",
            "next_action": "lease_reacquire",
            "owner": owner,
            "phase": phase,
            "instruction": "run lease-reacquire with the persisted owner and previous token, then retry this command once",
        }
    return {**guard, "error_class": classify_error(guard.get("error"))}


def _transition_failure(token: str, result: dict, path, *, page_action: bool = False) -> dict | None:
    """Normalize a failed ledger transition without hiding partial page work."""
    if result.get("ok"):
        return None
    code = error_code(result_error(result))
    if not page_action and code in {"execution_lease_mismatch", "execution_lease_expired"}:
        return _lease_guard(token, path)
    if page_action and code in {"execution_lease_mismatch", "execution_lease_expired"}:
        return {
            **result,
            "error_class": "HARD_SAFETY",
            "next_action": "reconcile_execution_phase",
            "instruction": "page action already occurred; reconcile the persisted candidate/page identity before any retry",
        }
    return {**result, "error_class": classify_error(result_error(result))}


def initial_listing(
    token: str,
    offset: int = 0,
    limit: int = 20,
    timeout: float = 15,
    wait_seconds: float = 8,
    list_fn: Callable[..., dict] | None = None,
    search_trigger_fn: Callable[[float], dict] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    state_path=None,
) -> dict:
    path = state_path or boss_ledger.BATCH_STATE
    state = boss_ledger.read_json(path)
    guard = _lease_guard(token, path)
    if not guard.get("ok"):
        return guard
    transitioned = boss_ledger.update_execution(token, "DISCOVER", path=path)
    failure = _transition_failure(token, transitioned, path)
    if failure:
        return failure
    real_list_call = list_fn is None
    call = list_fn or (
        lambda current_offset, current_limit, current_timeout: boss_bridge.command(
            "list_jobs",
            {"offset": current_offset, "limit": current_limit},
            current_timeout,
        )
    )
    if search_trigger_fn is not None:
        trigger = search_trigger_fn
    else:
        # A direct click on the page's generic “搜索” button is unsafe after
        # a slow/empty result: the visible route may say city=苏州 while the
        # site's internal city control still holds the previous default
        # (often 武汉).  Re-submitting the persisted keyword + city code
        # rebuilds both controls atomically and lets the bridge verify the
        # exact route before the next list read.  Keep the generic click only
        # for non-search batches (e.g. recommendation mode).
        mode = str(state.get("mode") or "")
        keyword = str(state.get("keyword") or "")
        city = str(state.get("current_city") or "")
        city_code = boss_bridge.CITY_CODES.get(city, "")
        if mode == "search-city" and keyword and city_code:
            def trigger(current_timeout: float) -> dict:
                refreshed = boss_bridge.command(
                    "search_jobs",
                    {"keyword": keyword, "city_code": city_code},
                    current_timeout,
                )
                if not refreshed.get("ok"):
                    return refreshed
                return boss_bridge.wait_for_search(
                    city_code,
                    keyword,
                    min(float(current_timeout), 8.0),
                )
        else:
            trigger = lambda current_timeout: boss_bridge.command(
                "click_text", {"text": "搜索", "exact": True}, current_timeout
            )
    attempts: list[dict] = []
    search_triggered = False
    for attempt in range(1, 4):
        result = call(offset, limit, timeout)
        if not result.get("ok"):
            if safe_read_timeout(result) and attempt < 3:
                attempts.append({"attempt": attempt, "count": 0, "outcome": "safe_read_timeout"})
                boss_ledger.record_telemetry("initial_retry", path, seconds=wait_seconds)
                sleep_fn(max(0.0, min(float(wait_seconds), 12.0)))
                continue
            return _record_error(token, "PAUSED", result) if state_path is None else {**result, "error_class": classify_error(result_error(result))}
        validation = boss_bridge.validate_batch_listing(result)
        if not validation.get("ok"):
            return _record_error(token, "PAUSED", validation) if state_path is None else {**validation, "error_class": classify_error(result_error(validation))}
        raw = result.get("data") or {}
        count = int(raw.get("count") or len(raw.get("jobs") or []))
        attempts.append({"attempt": attempt, "count": count})
        if count > 0:
            filtered = boss_ledger.filter_listing(raw)
            data = boss_ledger.filter_batch_listing(raw, filtered, path)
            generation = int(data.get("generation") or 0)
            transitioned = boss_ledger.update_execution(token, "SHORTLIST", {
                "shortlist_generation": generation,
                "last_error_class": "",
                "last_error": "",
            }, path)
            failure = _transition_failure(token, transitioned, path)
            if failure:
                return failure
            return {"ok": True, "data": data, "attempts": attempts}
        if attempt < 3:
            boss_ledger.record_telemetry("initial_retry", path, seconds=wait_seconds)
            if attempt == 2 and real_list_call and not search_triggered:
                trigger_result = trigger(min(float(timeout), 8.0))
                search_triggered = True
                attempts[-1]["search_trigger"] = bool(trigger_result.get("ok"))
            sleep_fn(max(0.0, min(float(wait_seconds), 12.0)))
    result = {"ok": False, "error": "initial_listing_empty", "attempts": attempts}
    if state_path is None:
        return _record_error(token, "PAUSED", result)
    transitioned = boss_ledger.update_execution(token, "PAUSED", {"last_error_class": "RECOVERABLE", "last_error": "initial_listing_empty"}, path)
    failure = _transition_failure(token, transitioned, path)
    if failure:
        return failure
    return {**result, "error_class": "RECOVERABLE"}


def claim_and_read(
    token: str,
    generation: int,
    job_id: str,
    timeout: float = 15,
    click_fn: Callable[[str, float], dict] | None = None,
    detail_fn: Callable[[str, float], dict] | None = None,
    state_path=None,
) -> dict:
    path = state_path or boss_ledger.BATCH_STATE
    guard = _lease_guard(token, path)
    if not guard.get("ok"):
        return guard
    claim = boss_ledger.claim_candidate(token, generation, job_id, path)
    if not claim.get("ok"):
        if error_code(result_error(claim)) in {"execution_lease_mismatch", "execution_lease_expired"}:
            return _lease_guard(token, path)
        return {**claim, "error_class": classify_error(claim.get("error"))}
    click = click_fn or (
        lambda expected_job_id, current_timeout: boss_bridge.command(
            "click_job_id", {"job_id": expected_job_id}, current_timeout
        )
    )
    detail = detail_fn or (
        lambda expected_job_id, current_timeout: boss_bridge.command(
            "job_detail",
            {"full": False, "expected_job_id": expected_job_id},
            current_timeout,
        )
    )
    clicked = click(job_id, timeout)
    recovered_after_late_jd = False
    if not clicked.get("ok"):
        click_error = result_error(clicked)
        # The list can be re-rendered after the first card in a shortlist is
        # opened/closed.  In that case the second card may briefly disappear
        # from the DOM even though its ID is still valid in this generation.
        # A single read-only current-list refresh is safe (the click produced
        # no page action) and avoids turning a transient DOM omission into an
        # execution-bug stop.  Never retry a send or a detail action here.
        if "job_not_loaded" in click_error:
            boss_ledger.record_telemetry("selection_retry", path)
            refreshed = boss_bridge.command(
                "list_jobs",
                {"offset": 0, "limit": 20},
                timeout,
            )
            if refreshed.get("ok"):
                validation = boss_bridge.validate_batch_listing(refreshed)
                jobs = (refreshed.get("data") or {}).get("jobs") or []
                visible_ids = {
                    boss_ledger.normalize(item.get("job_id"))
                    for item in jobs
                    if item.get("job_id")
                }
                if validation.get("ok") and boss_ledger.normalize(job_id) in visible_ids:
                    clicked = click(job_id, timeout)
                    click_error = result_error(clicked)
                else:
                    return _record_error(token, "PAUSED", {
                        "ok": False,
                        "error": "job_not_loaded_after_refresh",
                        "job_id": job_id,
                        "validation": validation,
                        "visible_job_count": len(visible_ids),
                    }, path)
            else:
                return _record_error(token, "PAUSED", {
                    "ok": False,
                    "error": "job_not_loaded_refresh_failed",
                    "job_id": job_id,
                    "detail": refreshed,
                }, path)
        if not clicked.get("ok") and "jd_body_not_ready" in click_error:
            recovered_after_late_jd = True
        elif not clicked.get("ok") and safe_selection_timeout(clicked):
            boss_ledger.record_telemetry("selection_retry", path)
            recovered_after_late_jd = True
        elif not clicked.get("ok"):
            return _record_error(token, "PAUSED", clicked, path)
    transitioned = boss_ledger.update_execution(token, "READ_JD", {"expected_job_id": job_id}, path)
    failure = _transition_failure(token, transitioned, path, page_action=True)
    if failure:
        return failure
    allowance = boss_ledger.can_review_jd(job_id, path)
    if not allowance.get("ok"):
        return _record_error(token, "PAUSED", allowance, path)
    read = detail(job_id, timeout)
    if not read.get("ok"):
        return _record_error(token, "PAUSED", read, path)
    data = read.get("data") or {}
    actual = str(data.get("job_id") or data.get("selected_job_id") or "")
    if boss_ledger.normalize(actual) != boss_ledger.normalize(job_id):
        return _record_error(token, "PAUSED", {
            "ok": False,
            "error": "job_identity_mismatch",
            "expected_job_id": job_id,
            "actual_job_id": actual,
        }, path)
    review = boss_ledger.record_jd_review(job_id, path)
    # Timing is best-effort telemetry only; a missing marker must never turn a
    # verified same-ID JD read into a screening or send blocker.
    stage = boss_ledger.record_candidate_jd_stage(token, job_id, path)
    if not stage.get("ok"):
        failure = _transition_failure(token, stage, path, page_action=True)
        if failure:
            return failure
    local_hint = boss_ledger.jd_hard_failure(data)
    transitioned = boss_ledger.update_execution(token, "DECIDE", {
        "expected_job_id": job_id,
        "last_error_class": "",
        "last_error": "",
    }, path)
    failure = _transition_failure(token, transitioned, path, page_action=True)
    if failure:
        return failure
    return {
        "ok": True,
        "recovered_after_late_jd": recovered_after_late_jd,
        "data": data,
        "review_budget": review,
        "local_screening_hint": {
            "decision": "HARD_FILTER" if local_hint else "MODEL_REVIEW",
            "reason": local_hint,
        },
    }


def next_shortlist(token: str, state_path=None) -> dict:
    path = state_path or boss_ledger.BATCH_STATE
    state = boss_ledger.read_json(path)
    guard = _lease_guard(token, path)
    if not guard.get("ok"):
        return guard
    listing = boss_ledger.next_shortlist_listing(path)
    if not listing.get("ok", True):
        return _unresolved_shortlist_recovery(listing)
    generation = int(listing.get("generation") or 0)
    updated = boss_ledger.update_execution(
        token,
        "SHORTLIST",
        {"shortlist_generation": generation, "expected_job_id": "", "last_error_class": "", "last_error": ""},
        path,
    )
    if not updated.get("ok"):
        return _transition_failure(token, updated, path) or {**updated, "error_class": classify_error(updated.get("error"))}
    return {"ok": True, "data": listing, "generation": generation}


def load_and_list(
    token: str,
    offset: int = 0,
    limit: int = 20,
    timeout: float = 15,
    wait_seconds: float = 8,
    load_fn: Callable[[int, float], dict] | None = None,
    list_fn: Callable[..., dict] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    state_path=None,
) -> dict:
    path = state_path or boss_ledger.BATCH_STATE
    state = boss_ledger.read_json(path)
    guard = _lease_guard(token, path)
    if not guard.get("ok"):
        return guard
    allowance = boss_ledger.can_fetch_new_listing(path)
    if not allowance.get("ok"):
        return _unresolved_shortlist_recovery(allowance)
    fuse = boss_ledger.combo_fuse_status(path)
    if fuse.get("tripped"):
        transitioned = boss_ledger.update_execution(token, "ROTATE", {"last_error_class": "", "last_error": ""}, path)
        failure = _transition_failure(token, transitioned, path)
        if failure:
            return failure
        return {"ok": True, "rotate_required": True, "combo_fuse": fuse}
    before_unique = len(set(state.get("unique_job_ids") or []))
    prior_meta = state.get("pending_meta") or state.get("last_batch") or {}
    prior_total = int(prior_meta.get("total_loaded") or 0)
    call = load_fn or boss_bridge.load_more_bounded
    loaded = call(1, timeout)
    if not loaded.get("ok"):
        return _record_error(token, "PAUSED", loaded, path)
    transitioned = boss_ledger.update_execution(token, "DISCOVER", path=path)
    failure = _transition_failure(token, transitioned, path, page_action=True)
    if failure:
        return failure
    load_data = loaded.get("data") or {}
    effective_offset = int(load_data.get("before_count") or prior_total)
    listed = initial_listing(
        token,
        effective_offset,
        limit,
        timeout,
        wait_seconds,
        list_fn=list_fn,
        sleep_fn=sleep_fn,
        state_path=path,
    )
    if not listed.get("ok"):
        return listed
    after_state = boss_ledger.read_json(path)
    after_unique = len(set(after_state.get("unique_job_ids") or []))
    effective_growth = max(0, after_unique - before_unique)
    effective_result = {
        "ok": True,
        "data": {
            "outcome": "growth" if effective_growth > 0 else "no_growth_after_retry",
            "before_count": before_unique,
            "after_count": after_unique,
            "new_count": effective_growth,
        },
    }
    city_gate = boss_ledger.record_city_load(effective_result, path)
    listed["effective_offset"] = effective_offset
    listed["effective_growth"] = effective_growth
    listed["city_gate"] = city_gate
    listed["dom_load"] = {
        "before": int(load_data.get("before_count") or 0),
        "after": int(load_data.get("after_count") or 0),
        "new": int(load_data.get("new_count") or 0),
    }
    return listed


def rotate_city(
    token: str,
    timeout: float = 15,
    configure_fn: Callable[[str, str, float], dict] | None = None,
    state_path=None,
) -> dict:
    path = state_path or boss_ledger.BATCH_STATE
    state = boss_ledger.read_json(path)
    guard = _lease_guard(token, path)
    if not guard.get("ok"):
        return guard
    unresolved = boss_ledger.unresolved_shortlist(state)
    if unresolved or state.get("pending_jobs"):
        return {
            "ok": False,
            "error": "city_rotation_requires_drained_resolved_queue",
            "unresolved_count": len(unresolved),
            "pending_count": len(state.get("pending_jobs") or []),
            "error_class": "EXECUTION_BUG",
        }
    fuse = boss_ledger.combo_fuse_status(path)
    if not fuse.get("tripped"):
        return {
            "ok": False,
            "error": "city_rotation_fuse_not_tripped",
            "combo_fuse": fuse,
            "error_class": "RECOVERABLE",
            "next_action": "continue_current_city",
        }
    order = list(state.get("city_order") or boss_ledger.CITY_ORDER)
    index = int(state.get("city_index") if state.get("city_index") is not None else -1)
    next_city = order[0] if index < 0 else order[(index + 1) % len(order)]
    keyword = str(state.get("keyword") or "")
    transitioned = boss_ledger.update_execution(token, "ROTATE", {"expected_job_id": "", "last_error_class": "", "last_error": ""}, path)
    failure = _transition_failure(token, transitioned, path)
    if failure:
        return failure
    call = configure_fn or boss_bridge.configure_search_city
    result = call(keyword, next_city, timeout)
    if not result.get("ok"):
        return _record_error(token, "PAUSED", result, path)
    transitioned = boss_ledger.update_execution(token, "DISCOVER", {
        "expected_job_id": "",
        "shortlist_generation": int((boss_ledger.read_json(path).get("last_batch") or {}).get("generation") or 0),
        "last_error_class": "",
        "last_error": "",
    }, path)
    failure = _transition_failure(token, transitioned, path, page_action=True)
    if failure:
        return failure
    return {"ok": True, "from_city": fuse.get("city"), "city": next_city, "keyword": keyword, "combo_fuse": fuse, "navigation": result}


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic BOSS execution supervisor")
    sub = parser.add_subparsers(dest="command", required=True)
    classify = sub.add_parser("classify")
    classify.add_argument("error")
    listing = sub.add_parser("initial-list")
    listing.add_argument("--lease-token", required=True)
    listing.add_argument("--offset", type=int, default=0)
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--timeout", type=float, default=15)
    listing.add_argument("--wait", type=float, default=8)
    claim = sub.add_parser("claim-read")
    claim.add_argument("--lease-token", required=True)
    claim.add_argument("--generation", type=int, required=True)
    claim.add_argument("--job-id", required=True)
    claim.add_argument("--timeout", type=float, default=15)
    next_parser = sub.add_parser("next-shortlist")
    next_parser.add_argument("--lease-token", required=True)
    load_parser = sub.add_parser("load-list")
    load_parser.add_argument("--lease-token", required=True)
    load_parser.add_argument("--offset", type=int, default=-1, help="ignored; supervisor derives the next unread offset")
    load_parser.add_argument("--limit", type=int, default=20)
    load_parser.add_argument("--timeout", type=float, default=15)
    load_parser.add_argument("--wait", type=float, default=8)
    sub.add_parser("fuse-status")
    rotate = sub.add_parser("rotate-city")
    rotate.add_argument("--lease-token", required=True)
    rotate.add_argument("--timeout", type=float, default=15)
    args = parser.parse_args()
    if args.command == "classify":
        result = {"ok": True, "error": args.error, "error_class": classify_error(args.error)}
    elif args.command == "initial-list":
        result = initial_listing(args.lease_token, args.offset, args.limit, args.timeout, args.wait)
    elif args.command == "claim-read":
        result = claim_and_read(args.lease_token, args.generation, args.job_id, args.timeout)
    elif args.command == "next-shortlist":
        result = next_shortlist(args.lease_token)
    elif args.command == "load-list":
        result = load_and_list(args.lease_token, args.offset, args.limit, args.timeout, args.wait)
    elif args.command == "fuse-status":
        result = boss_ledger.combo_fuse_status()
    else:
        result = rotate_city(args.lease_token, args.timeout)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
