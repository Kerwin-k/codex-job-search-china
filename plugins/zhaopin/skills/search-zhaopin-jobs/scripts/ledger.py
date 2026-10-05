#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import unicodedata
from datetime import date, timedelta
from pathlib import Path


import config_core as jobflow

ROOT = Path(__file__).resolve().parents[1]
HEADER = ["date", "job_id", "company", "title", "city", "salary", "resume_code", "evidence"]
DEFAULTS = {
    "zhaopin": jobflow.platform_state("zhaopin") / "confirmed-zhaopin.tsv",
}


def clean(value: object) -> str:
    return " ".join(str(value or "").replace("\t", " ").splitlines()).strip()


JOB_CODE_SUFFIX = re.compile(r"\s*[\(\[]\s*(?:[a-z]{1,5})?\d{3,}\s*[\)\]]\s*$", re.IGNORECASE)


def normalize_text(value: object, *, title: bool = False) -> str:
    text = unicodedata.normalize("NFKC", clean(value)).casefold()
    if title:
        text = JOB_CODE_SUFFIX.sub("", text)
    return "".join(char for char in text if not char.isspace() and not unicodedata.category(char).startswith("P"))


def parse_date(value: object) -> date | None:
    try:
        return date.fromisoformat(clean(value))
    except ValueError:
        return None


def parse_candidates(raw: str) -> list[dict[str, str]]:
    value = json.loads(raw)
    if not isinstance(value, list):
        raise ValueError("candidates_json_must_be_list")
    candidates = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("candidate_must_be_object")
        candidate = {key: clean(item.get(key)) for key in ("job_id", "company", "title")}
        if any(not candidate[key] for key in candidate):
            raise ValueError("candidate_missing_required_field")
        candidates.append(candidate)
    return candidates


def batch_check(rows: list[dict[str, str]], candidates: list[dict[str, str]]) -> dict:
    by_id = {row["job_id"]: row for row in rows if row["job_id"]}
    cutoff = date.today() - timedelta(days=30)
    recent = []
    for row in rows:
        row_date = parse_date(row["date"])
        company_key = normalize_text(row["company"])
        title_key = normalize_text(row["title"], title=True)
        if row_date and row_date >= cutoff and company_key and title_key:
            recent.append((row_date, company_key, title_key, row))
    recent.sort(key=lambda item: item[0], reverse=True)
    duplicates = []
    unseen = []
    for candidate in candidates:
        matched = by_id.get(candidate["job_id"])
        duplicate_type = "id" if matched else ""
        if not matched:
            company_key = normalize_text(candidate["company"])
            title_key = normalize_text(candidate["title"], title=True)
            for _, old_company, old_title, row in recent:
                if company_key == old_company and title_key == old_title:
                    matched = row
                    duplicate_type = "semantic"
                    break
        if matched:
            duplicates.append({
                "job_id": candidate["job_id"],
                "type": duplicate_type,
                "matched_job_id": matched["job_id"],
                "date": matched["date"],
            })
        else:
            unseen.append(candidate["job_id"])
    return {"duplicates": duplicates, "unseen": unseen}


def ensure_ledger(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.stat().st_size == 0:
        path.write_text("\t".join(HEADER) + "\n", encoding="utf-8")


def read_rows(path: Path) -> list[dict[str, str]]:
    ensure_ledger(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if reader.fieldnames != HEADER:
            raise ValueError("ledger_header_mismatch")
        return [{key: clean(row.get(key)) for key in HEADER} for row in reader]


def append_if_new(path: Path, row: dict[str, str]) -> dict:
    ensure_ledger(path)
    with path.open("a+", encoding="utf-8", newline="") as handle:
        locked = False
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                locked = True
            handle.seek(0)
            reader = csv.DictReader(handle, delimiter="\t")
            if reader.fieldnames != HEADER:
                raise ValueError("ledger_header_mismatch")
            existing = {clean(item.get("job_id")) for item in reader if clean(item.get("job_id"))}
            if row["job_id"] in existing:
                return {"appended": False, "duplicate": True, "job_id": row["job_id"]}
            handle.seek(0, os.SEEK_END)
            writer = csv.DictWriter(handle, fieldnames=HEADER, delimiter="\t", lineterminator="\n")
            writer.writerow(row)
            handle.flush()
            os.fsync(handle.fileno())
            return {"appended": True, "duplicate": False, "job_id": row["job_id"]}
        finally:
            if locked:
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def ledger_path(platform: str, override: str = "") -> Path:
    return Path(override).resolve() if override else DEFAULTS[platform]


def main() -> int:
    parser = argparse.ArgumentParser(description="Idempotent Zhaopin/51job receipt ledger")
    sub = parser.add_subparsers(dest="command", required=True)
    summary = sub.add_parser("summary")
    check = sub.add_parser("check")
    append = sub.add_parser("append")
    for command in (summary, check, append):
        command.add_argument("--platform", required=True, choices=tuple(DEFAULTS))
        command.add_argument("--ledger-path", default="", help=argparse.SUPPRESS)
    check_input = check.add_mutually_exclusive_group(required=True)
    check_input.add_argument("--job-id", action="append")
    check_input.add_argument("--candidates-json")
    check_input.add_argument("--candidates-env", action="store_true")
    append.add_argument("--job-id", required=True)
    append.add_argument("--company", required=True)
    append.add_argument("--title", required=True)
    append.add_argument("--city", required=True)
    append.add_argument("--salary", required=True)
    append.add_argument("--resume-code", required=True, choices=("A", "B", "C"))
    append.add_argument("--evidence", required=True)
    append.add_argument("--date", default=date.today().isoformat())
    args = parser.parse_args()
    path = ledger_path(args.platform, args.ledger_path)
    rows = read_rows(path)
    if args.command == "summary":
        ids = [row["job_id"] for row in rows if row["job_id"]]
        result = {
            "ok": True,
            "platform": args.platform,
            "rows": len(rows),
            "unique_ids": len(set(ids)),
            "duplicate_ids": len(ids) - len(set(ids)),
        }
    elif args.command == "check":
        if args.candidates_env:
            candidates_json = os.environ.get("CODEX_ENTERPRISE_CANDIDATES_JSON", "")
            if not candidates_json.strip():
                raise ValueError("CODEX_ENTERPRISE_CANDIDATES_JSON_missing_or_empty")
            result = {
                "ok": True,
                "platform": args.platform,
                **batch_check(rows, parse_candidates(candidates_json)),
            }
        elif args.candidates_json:
            result = {
                "ok": True,
                "platform": args.platform,
                **batch_check(rows, parse_candidates(args.candidates_json)),
            }
        else:
            known = {row["job_id"] for row in rows if row["job_id"]}
            requested = [clean(value) for value in args.job_id]
            result = {
                "ok": True,
                "platform": args.platform,
                "known": [value for value in requested if value in known],
                "unseen": [value for value in requested if value not in known],
            }
    else:
        row = {
            "date": clean(args.date),
            "job_id": clean(args.job_id),
            "company": clean(args.company),
            "title": clean(args.title),
            "city": clean(args.city),
            "salary": clean(args.salary),
            "resume_code": clean(args.resume_code),
            "evidence": clean(args.evidence),
        }
        if any(not row[key] for key in HEADER):
            raise ValueError("empty_required_field")
        result = {"ok": True, "platform": args.platform, **append_if_new(path, row)}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    raise SystemExit(main())
