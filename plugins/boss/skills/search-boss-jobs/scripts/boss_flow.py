#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import boss_bridge
import boss_ledger


import config_core as jobflow

SCRIPT_DIR = Path(__file__).resolve().parent
UIA_SCRIPT = SCRIPT_DIR / "uia-control.ps1"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def fail(reason: str, **details: object) -> dict:
    return {"ok": False, "error": reason, **details}


def delivery_text(data: dict) -> str:
    context = "\n".join(
        str(data.get(key) or "")
        for key in ("identity_context", "message_context")
    )
    for marker in ("已读", "送达", "已发送"):
        if marker in context:
            return marker
    return ""


def sent_message_count(data: dict, message: str) -> int:
    """Count exact messages in the selected conversation, excluding a draft.

    BOSS's in-detail conversation overlay can expose the sent message through
    identity/message context while reporting ``exact_message_count=0``.  Keep
    that context as a bounded fallback; it is scoped to the selected
    conversation and never uses the page/JD body.
    """
    count = int(data.get("exact_message_count") or 0)
    context_count = (
        sum(
            str(data.get(key) or "").count(message)
            for key in ("identity_context", "message_context")
        )
        if message
        else 0
    )
    if message and data.get("draft_text") == message:
        count = max(0, count - 1)
        context_count = max(0, context_count - 1)
    count = max(count, context_count)
    return count


def invoke_send() -> dict:
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(UIA_SCRIPT),
            "-Command",
            "invoke",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )
    output = completed.stdout.strip()
    try:
        result = json.loads(output) if output else {}
    except json.JSONDecodeError:
        result = {"ok": False, "error": output or completed.stderr.strip()}
    if completed.returncode != 0 or not result.get("ok"):
        return fail("uia_invoke_failed", uia=result, stderr=completed.stderr.strip())
    return result


def invoke_background_send(args: argparse.Namespace) -> dict:
    result = boss_bridge.command(
        "send_background",
        {
            "message": args.message,
            "expected_context": {
                "job_id": args.job_id,
                "company": args.company,
                "job_title": args.job_title,
                "city": args.city,
                "salary": args.salary,
            },
        },
        args.timeout,
    )
    if not result.get("ok"):
        return fail("background_send_failed", detail=result)
    data = result.get("data") or {}
    # The content bridge returns immediately after the single guarded DOM
    # dispatch.  BOSS can synchronously clear the textarea when that dispatch
    # is accepted, so an empty draft is valid trigger evidence; the following
    # selected-conversation post-check remains the authoritative proof.
    if not data.get("triggered") or data.get("draft_text") not in {args.message, ""}:
        return fail("background_send_trigger_unverified", detail=result)
    return {"ok": True, **data}


def inspect_send() -> dict:
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(UIA_SCRIPT),
            "-Command",
            "inspect",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )
    output = completed.stdout.strip()
    try:
        result = json.loads(output) if output else {}
    except json.JSONDecodeError:
        result = {"ok": False, "error": output or completed.stderr.strip()}
    if completed.returncode != 0 or not result.get("ok"):
        return fail("uia_inspect_failed", uia=result, stderr=completed.stderr.strip())
    return result


def wait_for_send_ready(timeout: float = 3.0, interval: float = 0.35) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while True:
        last = inspect_send()
        if not last.get("ok"):
            return last
        count = int(last.get("count") or 0)
        if count == 1:
            return {"ok": True, "count": 1, "matches": last.get("matches", [])}
        if count > 1:
            return fail("uia_readiness_ambiguous", count=count, uia=last)
        if time.monotonic() >= deadline:
            return fail("uia_readiness_timeout", count=0, uia=last)
        time.sleep(interval)


def require_context(state: dict, expected: list[tuple[str, str]]) -> dict | None:
    data = state.get("data") or {}
    haystack = (
        f"{data.get('page_context', '')}\n"
        f"{data.get('identity_context', '')}\n"
        f"{data.get('tail', '')}\n"
        f"{data.get('message_context', '')}"
    )
    missing = [label for label, value in expected if value and value not in haystack]
    if missing:
        return fail("conversation_context_mismatch", missing=missing)
    if data.get("signals", {}).get("security_check"):
        return fail("security_check_detected")
    if not data.get("signals", {}).get("conversation_selected"):
        return fail("no_conversation_selected")
    if not data.get("signals", {}).get("has_send_button"):
        return fail("send_button_missing")
    return None


def _send_verified_impl(args: argparse.Namespace) -> dict:
    permit=jobflow.require_apply("boss")
    if boss_ledger.batch_summary().get("confirmed",0) >= permit["target"]: return fail("authorized_target_reached")
    if jobflow.salary_pass(args.salary,"boss") is not True: return fail("detail_salary_unverified_or_below_floor")
    duplicate = boss_ledger.find_duplicate(
        args.job_id,
        args.company,
        args.job_title,
        args.city,
    )
    if duplicate:
        return fail(
            "duplicate_receipt",
            job_id=args.job_id,
            company=args.company,
            job_title=args.job_title,
            city=args.city,
        )
    company_roles = boss_ledger.batch_company_roles(args.company)
    normalized_roles = {boss_ledger.canonical_role_title(title) for title in company_roles.get("roles") or []}
    if (
        company_roles.get("count", 0) >= 2
        and boss_ledger.canonical_role_title(args.job_title) not in normalized_roles
    ):
        return fail("batch_company_role_limit_reached", **company_roles)
    before = boss_bridge.command("chat_state", {"message": args.message}, args.timeout)
    if not before.get("ok"):
        return fail("precheck_failed", detail=before)
    mismatch = require_context(
        before,
        [
            ("company", args.company),
            ("job_title", args.job_title),
            ("city", args.city),
            ("salary", args.salary),
        ],
    )
    if mismatch:
        return mismatch
    before_data = before["data"]
    before_count = sent_message_count(before_data, args.message)
    if before_count > 0:
        return fail(
            "exact_message_already_present",
            exact_message_count=before_count,
        )

    filled = boss_bridge.command(
        "fill_message",
        {"text": args.message},
        args.timeout,
    )
    if not filled.get("ok"):
        return fail("fill_failed", detail=filled)
    if filled.get("data", {}).get("draft_text") != args.message:
        return fail(
            "draft_mismatch",
            expected=args.message,
            actual=filled.get("data", {}).get("draft_text"),
        )

    send_mode = str(getattr(args, "send_mode", "background") or "background").lower()
    if send_mode not in {"uia", "background"}:
        return fail("unsupported_send_mode", send_mode=send_mode)
    if send_mode == "uia":
        ready = wait_for_send_ready()
        if not ready.get("ok"):
            return ready

    final_precheck = boss_bridge.command(
        "chat_state",
        {"message": args.message},
        args.timeout,
    )
    if not final_precheck.get("ok"):
        return fail("final_precheck_failed", detail=final_precheck)
    final_mismatch = require_context(
        final_precheck,
        [
            ("company", args.company),
            ("job_title", args.job_title),
            ("city", args.city),
            ("salary", args.salary),
        ],
    )
    if final_mismatch:
        return final_mismatch
    final_draft = (final_precheck.get("data") or {}).get("draft_text")
    if final_draft != args.message:
        return fail(
            "draft_mismatch_before_invoke",
            expected=args.message,
            actual=final_draft,
        )

    jobflow.begin_attempt("boss", args.job_id)
    if send_mode == "uia": jobflow.dispatch_attempt("boss", args.job_id)
    invoked = (
        invoke_background_send(args)
        if send_mode == "background"
        else invoke_send()
    )
    if not invoked.get("ok"):
        return invoked

    deadline = time.monotonic() + args.timeout
    last_state: dict = {}
    while time.monotonic() < deadline:
        last_state = boss_bridge.command(
            "chat_state",
            {"message": args.message},
            min(5, args.timeout),
        )
        if last_state.get("ok"):
            data = last_state.get("data") or {}
            exact_message_present = sent_message_count(data, args.message) >= 1
            draft_cleared = not data.get("draft_text")
            send_state = delivery_text(data)
            post_mismatch = require_context(
                last_state,
                [
                    ("company", args.company),
                    ("job_title", args.job_title),
                    ("city", args.city),
                    ("salary", args.salary),
                ],
            )
            # BOSS's detail-page chat overlay commonly confirms with only
            # "已发送" and an empty draft; "送达/已读" may never appear.
            # Exact selected-conversation evidence is sufficient and avoids a
            # needless timeout while preserving the no-resend guard.
            if exact_message_present and draft_cleared and not post_mismatch:
                receipt = {
                    "confirmed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "platform": "boss",
                    "company": args.company,
                    "job_title": args.job_title,
                    "city": args.city,
                    "salary": args.salary,
                    "job_id": args.job_id,
                    "message": args.message,
                    "evidence": {
                        "exact_message_count_before": before_count,
                        "exact_message_count_after": data.get("exact_message_count"),
                        "draft_cleared": True,
                        "send_source": invoked.get("source", ""),
                        "uia_source": invoked.get("source", "") if send_mode == "uia" else "",
                        "delivery_text": send_state,
                        "send_state": send_state or "exact_message_and_draft_clear",
                        "context": data.get("message_context"),
                    },
                }
                ledger = boss_ledger.append_if_new(receipt)
                if not ledger.get("appended"):
                    return fail("receipt_append_conflict", ledger=ledger)
                if args.verbose:
                    return {"ok": True, "confirmed": True, "receipt": receipt}
                return {
                    "ok": True,
                    "confirmed": True,
                    "receipt": {
                        "confirmed_at": receipt["confirmed_at"],
                        "company": args.company,
                        "job_title": args.job_title,
                        "city": args.city,
                        "salary": args.salary,
                        "job_id": args.job_id,
                    },
                    "evidence": {
                        "exact_message_count_before": before_count,
                        "exact_message_count_after": data.get("exact_message_count"),
                        "draft_cleared": True,
                        "send_source": invoked.get("source", ""),
                        "uia_source": invoked.get("source", "") if send_mode == "uia" else "",
                        "delivery_text": send_state,
                        "send_state": send_state or "exact_message_and_draft_clear",
                    },
                }
        time.sleep(0.7)
    return fail(
        "verification_timeout",
        exact_message_count_before=before_count,
        last_state=last_state,
    )


def send_verified(args: argparse.Namespace) -> dict:
    result=_send_verified_impl(args)
    if result.get("confirmed") is True:
        jobflow.resolve_attempt("boss",args.job_id,"sent","exact_conversation_message_and_cleared_draft_verified")
    return result


def reconcile_delivered(args: argparse.Namespace) -> dict:
    duplicate = boss_ledger.find_duplicate(
        args.job_id,
        args.company,
        args.job_title,
        args.city,
    )
    if duplicate:
        return {"ok": True, "confirmed": True, "already_recorded": True}

    state = boss_bridge.command("chat_state", {"message": args.message}, args.timeout)
    if not state.get("ok"):
        return fail("reconcile_precheck_failed", detail=state)
    mismatch = require_context(
        state,
        [
            ("company", args.company),
            ("job_title", args.job_title),
            ("city", args.city),
            ("salary", args.salary),
        ],
    )
    if mismatch:
        return mismatch
    data = state.get("data") or {}
    exact_count = sent_message_count(data, args.message)
    send_state = delivery_text(data)
    if exact_count < 1:
        return fail("reconcile_exact_message_missing")
    if data.get("draft_text"):
        return fail("reconcile_draft_not_empty")
    if not send_state:
        return fail("reconcile_send_state_missing")

    receipt = {
        "confirmed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "platform": "boss",
        "company": args.company,
        "job_title": args.job_title,
        "city": args.city,
        "salary": args.salary,
        "job_id": args.job_id,
        "message": args.message,
        "evidence": {
            "exact_message_count_after": exact_count,
            "draft_cleared": True,
            "delivery_text": send_state,
            "send_state": send_state,
            "reconciled_without_invoke": True,
            "context": data.get("message_context"),
        },
    }
    ledger = boss_ledger.append_if_new(receipt)
    if not ledger.get("appended"):
        return fail("receipt_append_conflict", ledger=ledger)
    return {
        "ok": True,
        "confirmed": True,
        "reconciled": True,
        "receipt": {
            "confirmed_at": receipt["confirmed_at"],
            "company": args.company,
            "job_title": args.job_title,
            "city": args.city,
            "salary": args.salary,
            "job_id": args.job_id,
        },
        "evidence": receipt["evidence"],
    }


def add_job_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--message", required=True)
    command.add_argument("--company", required=True)
    command.add_argument("--job-title", required=True)
    command.add_argument("--city", required=True)
    command.add_argument("--salary", required=True)
    command.add_argument("--job-id", default="")
    command.add_argument("--timeout", type=float, default=15)
    command.add_argument("--verbose", action="store_true")
    command.add_argument("--send-mode", choices=("uia", "background"), default="background")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verified one-at-a-time BOSS workflow")
    sub = parser.add_subparsers(dest="command", required=True)
    send = sub.add_parser("send-verified")
    add_job_arguments(send)
    reconcile = sub.add_parser("reconcile-delivered")
    add_job_arguments(reconcile)
    args = parser.parse_args()

    try:
        result = send_verified(args) if args.command == "send-verified" else reconcile_delivered(args)
    except jobflow.ConfigError as error:
        result = fail(str(error))
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
