#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path


import config_core as jobflow

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = jobflow.platform_state("51job")
SEEN_HEADER = ["date", "job_id", "company", "title", "reason", "expires_on"]


def clean(value: object) -> str:
    return " ".join(str(value or "").replace("\t", " ").splitlines()).strip()


def state_path(platform: str) -> Path:
    return STATE_DIR / f"search-state-{platform}.json"


def seen_path(platform: str) -> Path:
    return STATE_DIR / f"seen-decisions-{platform}.tsv"


def atomic_json(path: Path, value: dict) -> None:
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


def ensure_seen(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.stat().st_size == 0:
        path.write_text("\t".join(SEEN_HEADER) + "\n", encoding="utf-8")


def read_seen(path: Path) -> list[dict[str, str]]:
    ensure_seen(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if reader.fieldnames != SEEN_HEADER:
            raise ValueError("seen_header_mismatch")
        return [{key: clean(row.get(key)) for key in SEEN_HEADER} for row in reader]


def live_rows(rows: list[dict[str, str]], today: date) -> list[dict[str, str]]:
    result = []
    for row in rows:
        try:
            expiry = date.fromisoformat(row["expires_on"])
        except ValueError:
            continue
        if expiry >= today:
            result.append(row)
    return result


def rewrite_seen(path: Path, rows: list[dict[str, str]]) -> None:
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=SEEN_HEADER, delimiter="\t", lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main() -> int:
    parser = argparse.ArgumentParser(description="Cross-day search cursor and rejected-JD TTL ledger")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("get-search", "set-search", "seen-check", "seen-add", "seen-prune"):
        command = sub.add_parser(name)
        command.add_argument("--platform", required=True, choices=("51job",))
    set_search = sub.choices["set-search"]
    set_search.add_argument("--source", required=True, choices=("keyword", "expectation_pool"))
    set_search.add_argument("--keyword-or-pool", required=True)
    set_search.add_argument("--city", required=True)
    set_search.add_argument("--next-page", required=True, type=int, choices=range(1, 5))
    set_search.add_argument("--next-primary", required=True)
    set_search.add_argument("--date", default=date.today().isoformat())
    seen_check = sub.choices["seen-check"]
    seen_check.add_argument("--job-id", action="append", required=True)
    seen_add = sub.choices["seen-add"]
    seen_add.add_argument("--job-id", required=True)
    seen_add.add_argument("--company", required=True)
    seen_add.add_argument("--title", required=True)
    seen_add.add_argument("--reason", required=True)
    seen_add.add_argument("--ttl-days", type=int, default=7, choices=range(1, 31))
    seen_add.add_argument("--date", default=date.today().isoformat())
    args = parser.parse_args()

    if args.command == "get-search":
        path = state_path(args.platform)
        value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        result = {"ok": True, "platform": args.platform, "state": value}
    elif args.command == "set-search":
        if not clean(args.keyword_or_pool) or not clean(args.city) or not clean(args.next_primary):
            raise ValueError("empty_search_state_field")
        value = {
            "platform": args.platform, "source": args.source,
            "keyword_or_pool": clean(args.keyword_or_pool), "city": clean(args.city),
            "nextPage": args.next_page, "nextPrimary": clean(args.next_primary),
            "date": date.fromisoformat(args.date).isoformat(),
        }
        atomic_json(state_path(args.platform), value)
        result = {"ok": True, "state": value}
    else:
        path = seen_path(args.platform)
        rows = live_rows(read_seen(path), date.today())
        if args.command == "seen-check":
            known = {row["job_id"] for row in rows}
            requested = [clean(value) for value in args.job_id]
            result = {"ok": True, "platform": args.platform,
                      "seen": [value for value in requested if value in known],
                      "unseen": [value for value in requested if value not in known]}
        elif args.command == "seen-add":
            job_id = clean(args.job_id)
            rows = [row for row in rows if row["job_id"] != job_id]
            row_date = date.fromisoformat(args.date)
            row = {"date": row_date.isoformat(), "job_id": job_id,
                   "company": clean(args.company), "title": clean(args.title),
                   "reason": clean(args.reason),
                   "expires_on": (row_date + timedelta(days=args.ttl_days)).isoformat()}
            if any(not row[key] for key in SEEN_HEADER):
                raise ValueError("empty_seen_field")
            rows.append(row)
            rewrite_seen(path, rows)
            result = {"ok": True, "platform": args.platform, "record": row}
        else:
            before = len(read_seen(path))
            rewrite_seen(path, rows)
            result = {"ok": True, "platform": args.platform, "removed": before - len(rows), "remaining": len(rows)}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if hasattr(__import__("sys").stdout, "reconfigure"):
    __import__("sys").stdout.reconfigure(encoding="utf-8", errors="replace")

if __name__ == "__main__":
    raise SystemExit(main())
