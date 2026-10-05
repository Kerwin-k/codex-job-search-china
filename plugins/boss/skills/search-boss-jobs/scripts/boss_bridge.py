#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

import boss_ledger


import config_core as jobflow

HOST = "127.0.0.1"
_session_file = jobflow.platform_state("boss") / "bridge-session.json"
_session = json.loads(_session_file.read_text(encoding="utf-8")) if _session_file.exists() else {}
PORT = int(_session.get("port", 17897))
TOKEN = str(_session.get("token", ""))
BASE_URL = f"http://{HOST}:{PORT}"
CLIENT_STALE_SECONDS = 75
RUNTIME_DIR = jobflow.platform_state("boss")
EVENT_LOG = RUNTIME_DIR / "events.jsonl"
CITY_CODES = {**jobflow.CITY_CODES["boss"], **jobflow.effective("boss")["search"]["city_codes"]}

# A stale jobs-page pin is recoverable for these read/navigation operations:
# the bridge can rebind to one verified current jobs route before retrying the
# queue request. Sending, filling, and tab-closing are deliberately excluded;
# identity-sensitive actions must still stop rather than retarget themselves.
SAFE_REBIND_ACTIONS = {
    "page_summary",
    "list_jobs",
    "load_more_jobs",
    "search_jobs",
    "search_state",
    "click_job_id",
    "job_detail",
    "company_check",
    "click_text",
    "click_item",
    "filter_options",
    "set_filter",
    "reset_filters",
}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


class State:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.commands: deque[dict] = deque()
        self.results: dict[str, dict] = {}
        self.last_seen = 0.0
        self.clients: dict[str, dict] = {}
        self.closed_clients: dict[str, float] = {}
        self.pinned_client_id = ""
        self.base_client_id = ""

    def active_client(self) -> dict:
        self._prune()
        if self.pinned_client_id:
            return self.clients.get(self.pinned_client_id, {})
        candidates = list(self.clients.values())
        visible = [item for item in candidates if item.get("visible")]
        newest_visible = max(
            visible,
            key=lambda item: item.get("last_seen", 0),
            default={},
        )
        newest_seen = newest_visible.get("last_seen", 0)
        focused = [
            item
            for item in visible
            if item.get("focused")
            and newest_seen - item.get("last_seen", 0) <= 2
        ]
        pool = focused or visible or candidates
        return max(pool, key=lambda item: item.get("last_seen", 0), default={})

    def clients_snapshot(self) -> list[dict]:
        self._prune()
        return sorted(
            (
                {
                    "id": item.get("id"),
                    "url": item.get("url"),
                    "title": item.get("title"),
                    "visible": bool(item.get("visible")),
                    "focused": bool(item.get("focused")),
                    "last_seen": item.get("last_seen"),
                }
                for item in self.clients.values()
            ),
            key=lambda item: item.get("last_seen") or 0,
            reverse=True,
        )

    def pin(self, mode: str, client_id: str = "", require_path: str = "") -> dict:
        with self.lock:
            self._prune()
            if mode == "clear":
                self.pinned_client_id = ""
                return {"ok": True, "pinned_client_id": "", "base_client_id": self.base_client_id}
            rebound_from = ""
            if mode == "base":
                client_id = self.base_client_id
                # Closing a derived detail tab or reloading the extension can
                # cause the jobs page to re-register under a new client ID.
                # The old pin is then gone even though exactly one verified
                # jobs-route client is still available. Rebind only to that
                # unique, batch-matching route; never pick an arbitrary tab.
                if not self.clients.get(client_id):
                    policy = boss_ledger.batch_policy()
                    candidates = [
                        item for item in self.clients.values()
                        if urlparse(str(item.get("url") or "")).path == "/web/geek/jobs"
                    ]
                    keyword = str(policy.get("keyword") or "")
                    city = str(policy.get("current_city") or "")
                    city_code = CITY_CODES.get(city, "")
                    if policy.get("active") and keyword:
                        matched = []
                        for item in candidates:
                            query = parse_qs(urlparse(str(item.get("url") or "")).query)
                            if str((query.get("query") or [""])[0]) != keyword:
                                continue
                            if city_code and str((query.get("city") or [""])[0]) != city_code:
                                continue
                            matched.append(item)
                        candidates = matched
                    if len(candidates) == 1:
                        rebound_from = client_id
                        client_id = str(candidates[0].get("id") or "")
                    elif len(candidates) > 1:
                        return {
                            "ok": False,
                            "error": "base_rebind_ambiguous",
                            "old_base_client_id": self.base_client_id,
                            "candidates": [item.get("id") for item in candidates],
                        }
            elif mode == "active":
                candidates = list(self.clients.values())
                focused = [item for item in candidates if item.get("visible") and item.get("focused")]
                visible = [item for item in candidates if item.get("visible")]
                selected = max(
                    focused or visible or candidates,
                    key=lambda item: item.get("last_seen", 0),
                    default={},
                )
                client_id = str(selected.get("id") or "")
            client = self.clients.get(client_id)
            if not client:
                return {"ok": False, "error": "pin_client_not_found", "client_id": client_id}
            if require_path and require_path not in str(client.get("url") or ""):
                return {
                    "ok": False,
                    "error": "pin_route_mismatch",
                    "expected_path": require_path,
                    "client": client,
                }
            self.pinned_client_id = client_id
            if mode in {"active", "client-base"} or not self.base_client_id or rebound_from:
                self.base_client_id = client_id
            return {
                "ok": True,
                "pinned_client_id": self.pinned_client_id,
                "base_client_id": self.base_client_id,
                "client": client,
                **({"rebound_from": rebound_from} if rebound_from else {}),
            }

    def _prune(self) -> None:
        now = time.time()
        stale_clients = {
            key
            for key, value in self.clients.items()
            if now - value.get("last_seen", 0) > CLIENT_STALE_SECONDS
        }
        for key in stale_clients:
            self.clients.pop(key, None)
        self.closed_clients = {
            key: closed_at
            for key, closed_at in self.closed_clients.items()
            if now - closed_at <= 30
        }
        self.commands = deque(
            command
            for command in self.commands
            if now - command.get("created_at", 0) <= 30
            and (
                not command.get("target_client_id")
                or command.get("target_client_id") in self.clients
            )
        )

    def queue(self, action: str, payload: dict, target_client_id: str = "") -> dict:
        with self.lock:
            target = self.clients.get(target_client_id) if target_client_id else self.active_client()
            if not target:
                reason = (
                    "target_client_unavailable"
                    if target_client_id
                    else "pinned_client_unavailable"
                    if self.pinned_client_id
                    else "no_client_connected"
                )
                raise RuntimeError(reason)
            command = {
                "id": uuid.uuid4().hex,
                "action": action,
                "payload": payload,
                "created_at": time.time(),
                "target_client_id": target.get("id"),
            }
            self.commands.append(command)
        return command

    def next_command(self, client_id: str, wait_seconds: float = 0) -> dict | None:
        deadline = time.monotonic() + max(0.0, min(wait_seconds, 25.0))
        while True:
            with self.lock:
                self._prune()
                for index, command in enumerate(self.commands):
                    target = command.get("target_client_id")
                    requester = self.clients.get(client_id) or {}
                    target_client = self.clients.get(target) or {}
                    same_page_replacement = (
                        not self.pinned_client_id
                        and
                        target
                        and target != client_id
                        and requester.get("visible")
                        and requester.get("url")
                        and requester.get("url") == target_client.get("url")
                        and requester.get("last_seen", 0) >= target_client.get("last_seen", 0)
                    )
                    if not target or target == client_id or same_page_replacement:
                        selected = command
                        del self.commands[index]
                        return selected
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.15)

    def record(self, event: dict) -> None:
        now = time.time()
        event["received_at"] = now
        with self.lock:
            self.last_seen = now
            client = event.get("client") or {}
            client_id = client.get("id")
            if client_id:
                closing = bool((event.get("data") or {}).get("closing"))
                if closing:
                    self.clients.pop(client_id, None)
                    self.closed_clients[client_id] = now
                elif client_id not in self.closed_clients:
                    self.clients[client_id] = {**client, "last_seen": now}
                self._prune()
            command_id = event.get("command_id")
            if command_id:
                self.results[command_id] = event
                if len(self.results) > 200:
                    oldest = next(iter(self.results))
                    self.results.pop(oldest, None)
        if event.get("type") != "heartbeat":
            RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            with EVENT_LOG.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
                )

    def result(self, command_id: str) -> dict | None:
        with self.lock:
            return self.results.get(command_id)

    def status(self) -> dict:
        with self.lock:
            client = self.active_client()
            return {
                "ok": True,
                # Chrome may throttle timers in a background or minimized tab.
                # A 75-second grace period keeps that normal throttling from
                # looking like a disconnect while still detecting a dead tab.
                "connected": time.time() - self.last_seen < 75,
                "last_seen": self.last_seen or None,
                "client": client,
                "clients": self.clients_snapshot(),
                "client_count": len(self.clients),
                "pinned_client_id": self.pinned_client_id or None,
                "base_client_id": self.base_client_id or None,
                "pin_connected": bool(
                    self.pinned_client_id and self.pinned_client_id in self.clients
                ),
                "queued_commands": len(self.commands),
            }


STATE = State()


def cors(handler: BaseHTTPRequestHandler) -> None:
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, X-Boss-Bridge-Token")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Cache-Control", "no-store")


def respond(handler: BaseHTTPRequestHandler, status: int, data: dict) -> None:
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    cors(handler)
    handler.end_headers()
    handler.wfile.write(body)


def authorized(handler: BaseHTTPRequestHandler, query: dict | None = None) -> bool:
    query = query or {}
    supplied = handler.headers.get("X-Boss-Bridge-Token") or query.get("token", [""])[0]
    return bool(TOKEN) and __import__("secrets").compare_digest(str(supplied), TOKEN)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        cors(self)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/health":
            respond(self, 200, {"ok": True, "service": "jobflow-boss-bridge"})
            return
        if not authorized(self, query):
            respond(self, 403, {"ok": False, "error": "forbidden"})
            return
        if parsed.path == "/api/status":
            respond(self, 200, STATE.status())
            return
        if parsed.path == "/api/command/next":
            client_id = query.get("client_id", [""])[0]
            try:
                wait_seconds = float(query.get("wait", ["0"])[0])
            except ValueError:
                wait_seconds = 0
            respond(
                self,
                200,
                {"ok": True, "command": STATE.next_command(client_id, wait_seconds)},
            )
            return
        if parsed.path == "/api/result":
            command_id = query.get("id", [""])[0]
            respond(self, 200, {"ok": True, "event": STATE.result(command_id)})
            return
        respond(self, 404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not authorized(self):
            respond(self, 403, {"ok": False, "error": "forbidden"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            respond(self, 400, {"ok": False, "error": "invalid_json"})
            return
        if parsed.path == "/api/command":
            try:
                command = STATE.queue(
                    str(data.get("action", "")),
                    data.get("payload") or {},
                    str(data.get("target_client_id") or ""),
                )
            except RuntimeError as exc:
                respond(self, 409, {"ok": False, "error": str(exc)})
                return
            respond(self, 200, {"ok": True, "command": command})
            return
        if parsed.path == "/api/pin":
            result = STATE.pin(
                str(data.get("mode") or "client"),
                str(data.get("client_id") or ""),
                str(data.get("require_path") or ""),
            )
            respond(self, 200 if result.get("ok") else 409, result)
            return
        if parsed.path == "/api/event":
            STATE.record(data)
            respond(self, 200, {"ok": True})
            return
        respond(self, 404, {"ok": False, "error": "not_found"})


def request_json(method: str, path: str, data: dict | None = None, timeout: float = 5) -> dict:
    body = None if data is None else json.dumps(data, ensure_ascii=False).encode("utf-8")
    request = Request(
        BASE_URL + path,
        data=body,
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-Boss-Bridge-Token": TOKEN,
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def command(
    action: str,
    payload: dict | None = None,
    timeout: float = 15,
    target_client_id: str = "",
    _allow_job_loaded_retry: bool = True,
) -> dict:
    if action == "send_background":
        try:
            permit=jobflow.require_apply("boss")
            jobflow.dispatch_attempt("boss",str((payload or {}).get("expected_context",{}).get("job_id") or ""))
            if boss_ledger.batch_summary().get("confirmed",0) >= permit["target"]: return {"ok":False,"error":"authorized_target_reached"}
        except jobflow.ConfigError as error:
            return {"ok":False,"error":str(error)}
    command_payload = {
        "action": action,
        "payload": payload or {},
        "target_client_id": target_client_id,
    }
    try:
        queued = request_json("POST", "/api/command", command_payload)
    except HTTPError as exc:
        # The local bridge uses HTTP 409 when a pinned target has vanished.
        # Rebind once only for non-mutating/read navigation actions, and only
        # through State.pin's unique batch-matching jobs-route guard.
        if exc.code != 409 or target_client_id or action not in SAFE_REBIND_ACTIONS:
            raise
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = {}
        if str(body.get("error") or "") not in {
            "pinned_client_unavailable",
            "no_client_connected",
        }:
            raise
        rebound = pin_client("base", require_path="/web/geek/jobs")
        if not rebound.get("ok"):
            # Preserve the bridge's structured rebind reason. Re-raising the
            # consumed HTTPError hid whether the route was absent or
            # ambiguous and made every failure look like a generic 409.
            return {
                "ok": False,
                "error": "stale_pin_rebind_failed",
                "rebind_error": rebound.get("error") or "unknown",
                "rebind_candidates": rebound.get("candidates") or [],
            }
        queued = request_json("POST", "/api/command", command_payload)
    command_id = queued["command"]["id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        latest = request_json("GET", f"/api/result?id={command_id}")
        event = latest.get("event")
        if event:
            if not event.get("ok"):
                # A shortlist card can vanish during a list re-render after a
                # previous detail tab closes.  For the selection action only,
                # repopulate the current list once and retry the same ID when
                # the exact route and ID are still proven.  This keeps direct
                # bridge callers aligned with the supervisor safety rule.
                event_error = str(event.get("error") or "")
                expected_job_id = str((payload or {}).get("job_id") or "")
                if (
                    action == "click_job_id"
                    and _allow_job_loaded_retry
                    and expected_job_id
                    and "job_not_loaded" in event_error
                ):
                    boss_ledger.record_telemetry("selection_retry")
                    refreshed = command(
                        "list_jobs",
                        {"offset": 0, "limit": 20},
                        timeout,
                        target_client_id,
                        _allow_job_loaded_retry=False,
                    )
                    validation = validate_batch_listing(refreshed) if refreshed.get("ok") else refreshed
                    jobs = (refreshed.get("data") or {}).get("jobs") or []
                    visible_ids = {
                        boss_ledger.normalize(item.get("job_id"))
                        for item in jobs
                        if item.get("job_id")
                    }
                    if validation.get("ok") and boss_ledger.normalize(expected_job_id) in visible_ids:
                        retried = command(
                            action,
                            payload,
                            timeout,
                            target_client_id,
                            _allow_job_loaded_retry=False,
                        )
                        if retried.get("ok"):
                            return retried
                    return {
                        "ok": False,
                        "error": "job_not_loaded_after_refresh",
                        "job_id": expected_job_id,
                        "original_error": event_error,
                        "validation": validation,
                        "visible_job_count": len(visible_ids),
                    }
                return {
                    "ok": False,
                    "error": event_error,
                    "command_id": command_id,
                }
            return {
                "ok": True,
                "command_id": command_id,
                "data": event.get("data") or {},
            }
        time.sleep(0.2)
    status = request_json("GET", "/api/status")
    current = status.get("client") or {}
    return {
        "ok": False,
        "error": "timeout",
        "command_id": command_id,
        "current_path": urlparse(str(current.get("url") or "")).path,
        "current_client_id": current.get("id"),
        "safe_state": "lane_connected" if status.get("pin_connected") else "lane_unpinned",
    }


def load_more_bounded(rounds: int, timeout: float) -> dict:
    attempts: list[dict] = []
    attempt_timeout = min(max(float(timeout), 1.0), 12.0)
    last_data: dict = {}
    # Allow the initial load plus at most two bounded retries.  The retry
    # budget is intentionally small: it covers BOSS's delayed waterfall
    # render without turning a blocked lane into an unbounded loop.
    for attempt in range(1, 4):
        result = command(
            "load_more_jobs",
            {"rounds": min(max(int(rounds), 1), 2)},
            attempt_timeout,
        )
        if not result.get("ok"):
            error = str(result.get("error") or "load_more_failed")
            return {
                "ok": False,
                "error": "load_more_timeout_unknown" if error == "timeout" else error,
                "command_id": result.get("command_id"),
                "attempt": attempt,
                "current_path": result.get("current_path", ""),
                "safe_state": result.get("safe_state", ""),
            }
        data = result.get("data") or {}
        last_data = data
        new_count = int(data.get("new_count") or 0)
        attempts.append({
            "attempt": attempt,
            "before": int(data.get("before_count") or 0),
            "after": int(data.get("after_count") or 0),
            "new": new_count,
        })
        if new_count > 0:
            return {
                "ok": True,
                "command_id": result.get("command_id"),
                "data": {
                    **data,
                    "outcome": "growth",
                    "attempts": attempts,
                },
            }
        if attempt < 3:
            # BOSS often updates the waterfall asynchronously after the
            # scroll command returns.  Wait one bounded 8-second render
            # window before each of the two permitted retries.
            time.sleep(8.0)
    return {
        "ok": True,
        "data": {
            **last_data,
            "outcome": "no_growth_after_retry",
            "attempts": attempts,
        },
    }


def pin_client(mode: str, client_id: str = "", require_path: str = "") -> dict:
    try:
        return request_json(
            "POST",
            "/api/pin",
            {"mode": mode, "client_id": client_id, "require_path": require_path},
        )
    except HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {"ok": False, "error": f"pin_failed: {exc}"}


def matching_client(status: dict, predicate) -> dict:
    clients = status.get("clients") or []
    matches = [client for client in clients if predicate(client)]
    visible = [client for client in matches if client.get("visible")]
    return max(
        visible or matches,
        key=lambda item: item.get("last_seen") or 0,
        default={},
    )


def wait_for_route(path_fragment: str, timeout: float = 8) -> dict:
    """Wait for any connected client on a route and pin commands to it."""
    deadline = time.monotonic() + timeout
    latest: dict = {}
    while time.monotonic() < deadline:
        status = request_json("GET", "/api/status")
        latest = matching_client(
            status,
            lambda item: path_fragment in str(item.get("url", "")),
        )
        if status.get("connected") and latest:
            pinned = pin_client("client", str(latest.get("id") or ""))
            return {"ok": True, "client": latest, "pin": pinned}
        time.sleep(0.2)
    return {
        "ok": False,
        "error": "route_timeout",
        "expected_path": path_fragment,
        "current_client": latest,
    }


def job_id_from_url(url: object) -> str:
    match = re.search(
        r"/job_detail/([^/?#]+?)(?:\.html)?(?:[?#]|$)",
        str(url or ""),
    )
    return match.group(1) if match else ""


def wait_for_job_route(
    expected_job_id: str,
    before_clients: dict[str, str],
    clicked_after: float,
    timeout: float = 12,
) -> dict:
    """Pin only a newly created or newly navigated exact-ID detail client."""
    deadline = time.monotonic() + timeout
    latest_exact: dict = {}
    observed_job_ids: set[str] = set()
    while time.monotonic() < deadline:
        status = request_json("GET", "/api/status")
        candidates = []
        for client in status.get("clients") or []:
            actual_job_id = job_id_from_url(client.get("url"))
            if actual_job_id:
                observed_job_ids.add(actual_job_id)
            if actual_job_id != expected_job_id:
                continue
            latest_exact = client
            client_id = str(client.get("id") or "")
            old_url = before_clients.get(client_id)
            newly_created = client_id not in before_clients
            newly_navigated = old_url is not None and old_url != str(client.get("url") or "")
            fresh_seen = float(client.get("last_seen") or 0) >= clicked_after - 0.5
            if fresh_seen and (newly_created or newly_navigated):
                candidates.append(client)
        if candidates:
            selected = max(
                candidates,
                key=lambda item: item.get("last_seen") or 0,
            )
            pinned = pin_client("client", str(selected.get("id") or ""))
            if pinned.get("ok"):
                return {
                    "ok": True,
                    "expected_job_id": expected_job_id,
                    "client": selected,
                    "pin": pinned,
                }
        time.sleep(0.2)
    return {
        "ok": False,
        "error": "exact_job_route_timeout",
        "expected_job_id": expected_job_id,
        "observed_job_ids": sorted(observed_job_ids),
        "latest_exact_client": latest_exact,
    }


def wait_for_chat_ready(timeout: float = 15) -> dict:
    """Accept either a dedicated chat route or an in-page conversation layer."""
    deadline = time.monotonic() + timeout
    latest: dict = {}
    # Poll with a short backoff instead of hammering the bridge every 200 ms.
    # This keeps the slow-overlay allowance while cutting dozens of redundant
    # chat-state reads from a normal transition.
    delay = 0.0
    while time.monotonic() < deadline:
        status = request_json("GET", "/api/status")
        chat_client = matching_client(
            status,
            lambda item: "/web/geek/chat" in str(item.get("url", "")),
        )
        if chat_client:
            pin_client("client", str(chat_client.get("id") or ""))
        state = command("chat_state", timeout=min(2.0, max(0.5, deadline - time.monotonic())))
        latest = state
        signals = (state.get("data") or {}).get("signals") or {}
        if state.get("ok") and signals.get("conversation_selected"):
            return {"ok": True, "state": state.get("data") or {}}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if delay <= 0:
            delay = 0.5
        else:
            delay = min(2.0, round(delay * 1.6, 2))
        time.sleep(min(delay, remaining))
    return {"ok": False, "error": "chat_not_ready", "last_state": latest}


def wait_for_search(city_code: str, keyword: str, timeout: float = 8) -> dict:
    # BOSS can briefly keep the exact jobs URL while adding an
    # ``_security_check`` marker.  That URL is not a usable listing client
    # yet, but it may clear on its own shortly afterwards.  Wait once more
    # within a bounded grace window; never bypass or solve the challenge.
    deadline = time.monotonic() + max(0.1, float(timeout))
    grace_deadline: float | None = None
    security_handoff_seen = False
    latest: dict = {}
    while True:
        status = request_json("GET", "/api/status")
        search_clients = [
            client for client in status.get("clients") or []
            if _matches_search_client(client, city_code, keyword)
        ]
        ready_clients = [
            client for client in search_clients
            if not _is_security_check_client(client)
        ]
        # Prefer a ready exact route when more than one content client is
        # visible; a newer security-marked registration must not mask a
        # usable matching tab.
        latest = max(
            ready_clients or search_clients,
            key=lambda item: item.get("last_seen") or 0,
            default={},
        )
        if search_clients and not ready_clients:
            security_handoff_seen = True
        if status.get("connected") and ready_clients and latest:
            pinned = pin_client("client-base", str(latest.get("id") or ""))
            return {"ok": True, "client": latest, "pin": pinned}
        now = time.monotonic()
        if now >= deadline:
            if security_handoff_seen and grace_deadline is None:
                # One extra bounded read-only window.  It is deliberately
                # independent of the route/client pin and still fails closed
                # if the security marker remains present.
                grace_deadline = now + min(8.0, max(2.0, float(timeout)))
            elif grace_deadline is not None and now >= grace_deadline:
                break
        time.sleep(0.2)
    return {
        "ok": False,
        "error": "search_route_timeout",
        "city_code": city_code,
        "keyword": keyword,
        "current_client": latest,
        "security_handoff_seen": security_handoff_seen,
    }


def _matches_search_client(client: dict, city_code: str, keyword: str) -> bool:
    parsed = urlparse(str(client.get("url", "")))
    query = parse_qs(parsed.query)
    return (
        parsed.path == "/web/geek/jobs"
        and query.get("city", [""])[0] == city_code
        and query.get("query", [""])[0] == keyword
    )


def _is_security_check_client(client: dict) -> bool:
    """Return whether a client URL is currently marked as a security handoff.

    This is only a readiness signal.  Callers must continue to fail closed on
    a persistent marker; the bridge never attempts to solve or bypass it.
    """
    parsed = urlparse(str(client.get("url", "")))
    query = parse_qs(parsed.query)
    return (
        any(str(key).lower().startswith("_security_check") for key in query)
        or "security" in parsed.path.lower()
    )


def _matches_recommend_city_client(client: dict, city_code: str) -> bool:
    parsed = urlparse(str(client.get("url", "")))
    query = parse_qs(parsed.query)
    return (
        parsed.path == "/web/geek/jobs"
        and query.get("city", [""])[0] == city_code
        and not query.get("query", [""])[0]
    )


def wait_for_recommend_city(city: str, timeout: float = 8) -> dict:
    city_code = CITY_CODES[city]
    deadline = time.monotonic() + timeout
    latest: dict = {}
    while time.monotonic() < deadline:
        status = request_json("GET", "/api/status")
        latest = matching_client(
            status,
            lambda item: _matches_recommend_city_client(item, city_code),
        )
        if status.get("connected") and latest:
            pinned = pin_client("client-base", str(latest.get("id") or ""))
            tabs = command("expectation_tabs", timeout=min(2.0, timeout))
            data = tabs.get("data") or {}
            selected_recommend = any(
                tab.get("label") == "推荐" and tab.get("selected")
                for tab in data.get("tabs") or []
            )
            if tabs.get("ok") and (
                data.get("active_feed") == "推荐" or selected_recommend
            ):
                return {"ok": True, "city": city, "client": latest, "pin": pinned}
        time.sleep(0.2)
    return {
        "ok": False,
        "error": "recommend_city_route_timeout",
        "city": city,
        "current_client": latest,
    }


def configure_recommend_city(city: str, timeout: float = 15) -> dict:
    policy = boss_ledger.batch_policy()
    if not policy.get("active") or policy.get("mode") != "recommend-city":
        return {"ok": False, "error": "recommend_city_batch_required"}
    status = request_json("GET", "/api/status")
    active_client = status.get("client") or {}
    recovery = (
        city == policy.get("current_city")
        and not _matches_recommend_city_client(active_client, CITY_CODES[city])
    )
    if not recovery:
        guard = boss_ledger.can_select_batch_city(city)
        if not guard.get("ok"):
            return guard
    clicked = command("click_text", {"text": "推荐"}, min(timeout, 5))
    if not clicked.get("ok"):
        return clicked
    selected = command("set_city_filter", {"city": city}, min(timeout, 8))
    if not selected.get("ok"):
        return selected
    route = wait_for_recommend_city(city, min(timeout, 8))
    if not route.get("ok"):
        return route
    if recovery:
        recorded = {
            "ok": True,
            "next_city": policy.get("next_city"),
            "refreshed": False,
            "recovered": True,
        }
    else:
        recorded = boss_ledger.select_batch_city(city)
        if not recorded.get("ok"):
            return recorded
    return {
        "ok": True,
        "mode": "recommend-city",
        "feed": "推荐",
        "city": city,
        "next_city": recorded.get("next_city"),
        "refreshed": recorded.get("refreshed"),
        "recovered": bool(recorded.get("recovered")),
    }


def configure_search_city(keyword: str, city: str, timeout: float = 15) -> dict:
    policy = boss_ledger.batch_policy()
    if not policy.get("active") or policy.get("mode") != "search-city":
        return {"ok": False, "error": "search_city_batch_required"}
    if keyword != policy.get("keyword"):
        return {"ok": False, "error": "batch_keyword_mismatch", "expected": policy.get("keyword"), "requested": keyword}
    status = request_json("GET", "/api/status")
    active_client = status.get("client") or {}
    # A browser reload, extension re-registration, or user-visible page change
    # can leave the active batch city persisted while the bridge is on another
    # route. Recover only that same persisted city; never use this exception to
    # skip the deterministic city order or enter a new city early.
    recovery = (
        city == policy.get("current_city")
        and not _matches_search_client(active_client, CITY_CODES[city], keyword)
    )
    if not recovery:
        guard = boss_ledger.can_select_batch_city(city)
        if not guard.get("ok"):
            return guard
    city_code = CITY_CODES[city]
    result = command("search_jobs", {"keyword": keyword, "city_code": city_code}, timeout)
    if not result.get("ok"):
        return result
    route = wait_for_search(city_code, keyword, min(timeout, 8))
    if not route.get("ok"):
        return route
    if city == policy.get("current_city") and recovery:
        # Extension reloads can leave the bridge on a generic jobs route while
        # the active batch still owns a resolved/pending shortlist. Restore
        # only the verified current route; do not reset shortlist state or
        # require the city-deep-dive gate again.
        return {
            "ok": True,
            "mode": "search-city",
            "keyword": keyword,
            "city": city,
            "next_city": policy.get("next_city"),
            "recovered": True,
            "preserved_state": True,
        }
    if city == policy.get("current_city") and not recovery:
        return {
            "ok": True,
            "mode": "search-city",
            "keyword": keyword,
            "city": city,
            "next_city": policy.get("next_city"),
            "recovered": False,
            "already_current": True,
        }
    recorded = boss_ledger.select_batch_city(city, allow_current_recovery=recovery)
    if not recorded.get("ok"):
        return recorded
    return {
        "ok": True,
        "mode": "search-city",
        "keyword": keyword,
        "city": city,
        "next_city": recorded.get("next_city"),
        "recovered": recovery,
    }


def validate_recommend_listing(result: dict) -> dict:
    policy = boss_ledger.batch_policy()
    if not policy.get("active") or policy.get("mode") != "recommend-city":
        return {"ok": True}
    city = str(policy.get("current_city") or "")
    if not city:
        return {"ok": False, "error": "recommend_city_not_selected"}
    client = result.get("client") or (result.get("data") or {})
    if not _matches_recommend_city_client(client, CITY_CODES[city]):
        return {"ok": False, "error": "recommend_city_route_mismatch", "expected_city": city}
    feed = str((result.get("data") or {}).get("feed") or "")
    if not (feed.endswith("|推荐") or feed.endswith("|recommend")):
        return {"ok": False, "error": "recommend_feed_mismatch", "feed": feed[:240]}
    return {"ok": True}


def validate_batch_listing(result: dict) -> dict:
    policy = boss_ledger.batch_policy()
    if not policy.get("active"):
        return {"ok": True}
    if policy.get("mode") == "recommend-city":
        return validate_recommend_listing(result)
    if policy.get("mode") == "search-city":
        city = str(policy.get("current_city") or "")
        keyword = str(policy.get("keyword") or "")
        client = result.get("client") or (result.get("data") or {})
        if not city or not _matches_search_client(client, CITY_CODES[city], keyword):
            return {"ok": False, "error": "search_city_route_mismatch", "expected_city": city, "expected_keyword": keyword}
        return {"ok": True}
    return {"ok": False, "error": "unknown_batch_mode"}


def switch_job_conversation(company: str, old_title: str, new_title: str, timeout: float = 12) -> dict:
    duplicate = boss_ledger.find_duplicate(company=company, job_title=new_title)
    if duplicate:
        return {"ok": False, "error": "same_company_same_role_already_contacted"}
    roles = boss_ledger.batch_company_roles(company)
    if int(roles.get("count") or 0) >= 2:
        return {"ok": False, "error": "batch_company_role_limit_reached", **roles}
    before = command("chat_state", timeout=min(timeout, 4))
    if not before.get("ok"):
        return before
    identity = str((before.get("data") or {}).get("identity_context") or "")
    if company not in identity or old_title not in identity:
        return {"ok": False, "error": "existing_conversation_identity_mismatch"}
    clicked = command("click_prompt_action", {"text": "沟通新职位"}, min(timeout, 5))
    if not clicked.get("ok"):
        return clicked
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = command("chat_state", timeout=min(3, timeout))
        if last.get("ok"):
            data = last.get("data") or {}
            current = str(data.get("identity_context") or "")
            if company in current and new_title in current and data.get("signals", {}).get("conversation_selected"):
                return {"ok": True, "company": company, "old_title": old_title, "new_title": new_title}
        time.sleep(0.25)
    return {"ok": False, "error": "new_role_conversation_timeout", "last_state": last}


def compact_listing(result: dict) -> dict:
    data = result.get("data") or {}
    cards = []
    for job in data.get("jobs") or []:
        summary = re.sub(r"\s+", " / ", str(job.get("summary") or "")).strip(" / ")[:260]
        title = re.sub(r"\s+", " ", str(job.get("title") or "")).strip()
        cards.append(f"{job.get('job_id', '')}|{title}|{summary}")
    result["data"] = {
        key: value
        for key, value in data.items()
        if key != "jobs"
    }
    result["data"]["cards"] = cards
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Compact BOSS local bridge")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    sub.add_parser("status")
    lane_start = sub.add_parser("lane-start")
    lane_start.add_argument("--client-id", default="")
    sub.add_parser("lane-status")
    sub.add_parser("lane-reset")
    page_summary = sub.add_parser("page-summary")
    sub.add_parser("expectation-tabs")
    list_jobs = sub.add_parser("list-jobs")
    list_jobs.add_argument("--offset", type=int, default=0)
    list_jobs.add_argument("--limit", type=int, default=20)
    list_jobs.add_argument("--json", action="store_true")
    list_jobs.add_argument("--replay-last", action="store_true")
    list_jobs.add_argument("--next-shortlist", action="store_true")
    recommend_city = sub.add_parser("recommend-city")
    recommend_city.add_argument("city", choices=tuple(CITY_CODES))
    load_more = sub.add_parser("load-more")
    load_more.add_argument("--rounds", type=int, default=1)
    job_detail = sub.add_parser("job-detail")
    job_detail.add_argument("--expected-job-id", default="")
    company_check = sub.add_parser("company-check")
    company_check.add_argument("--expected-job-id", default="")
    sub.add_parser("search-state")
    search = sub.add_parser("search-jobs")
    search.add_argument("keyword")
    search.add_argument("--city", choices=tuple(CITY_CODES))
    sub.add_parser("close-tab")
    close_details = sub.add_parser("close-details")
    filters = sub.add_parser("filter-options")
    filters.add_argument("filter")
    select_filter = sub.add_parser("set-filter")
    select_filter.add_argument("value")
    sub.add_parser("reset-filters")
    chat = sub.add_parser("chat-state")
    chat.add_argument("--message", default="")
    sub.add_parser("chat-debug")
    click = sub.add_parser("click-text")
    click.add_argument("text")
    click.add_argument("--contains", action="store_true")
    click.add_argument("--expected-job-id", default="")
    item = sub.add_parser("click-item")
    item.add_argument("text")
    click_job = sub.add_parser("click-job")
    click_job.add_argument("job_id")
    switch_job = sub.add_parser("switch-job")
    switch_job.add_argument("--company", required=True)
    switch_job.add_argument("--old-title", required=True)
    switch_job.add_argument("--new-title", required=True)
    fill = sub.add_parser("fill-message")
    fill.add_argument("text")
    page_summary.add_argument("--verbose", action="store_true")
    job_detail.add_argument("--full", action="store_true")
    chat.add_argument("--verbose", action="store_true")
    for name in (
        "page-summary", "expectation-tabs", "list-jobs", "recommend-city", "load-more", "job-detail", "company-check",
        "search-state", "search-jobs", "close-tab",
        "filter-options", "set-filter", "reset-filters",
        "chat-state", "chat-debug", "click-text", "click-item", "click-job", "switch-job", "fill-message",
    ):
        sub.choices[name].add_argument("--timeout", type=float, default=15)
    close_details.add_argument("--timeout", type=float, default=15)
    args = parser.parse_args()

    try:
        if args.cmd == "serve":
            if not TOKEN: raise ValueError("bridge_session_missing: run_prepare")
            RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            print(f"BOSS bridge listening on {BASE_URL}", flush=True)
            ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
            return 0
        if args.cmd == "status":
            result = request_json("GET", "/api/status")
        elif args.cmd == "lane-start":
            result = pin_client(
                "client-base" if args.client_id else "active",
                client_id=args.client_id,
                require_path="/web/geek/jobs",
            )
        elif args.cmd == "lane-status":
            status = request_json("GET", "/api/status")
            result = {
                "ok": bool(status.get("pin_connected")),
                "pinned_client_id": status.get("pinned_client_id"),
                "base_client_id": status.get("base_client_id"),
                "client_count": status.get("client_count"),
                "client": status.get("client"),
            }
        elif args.cmd == "lane-reset":
            result = pin_client("clear")
        elif args.cmd == "page-summary":
            result = command(
                "page_summary",
                {"verbose": args.verbose},
                args.timeout,
            )
        elif args.cmd == "expectation-tabs":
            result = command("expectation_tabs", timeout=args.timeout)
        elif args.cmd == "list-jobs":
            if args.replay_last:
                result = {"ok": True, "data": boss_ledger.last_batch_listing()}
            elif args.next_shortlist:
                result = {"ok": True, "data": boss_ledger.next_shortlist_listing()}
            else:
                allowance = boss_ledger.can_fetch_new_listing()
                if not allowance.get("ok"):
                    result = allowance
                else:
                    result = command(
                        "list_jobs",
                        {"offset": args.offset, "limit": args.limit},
                        args.timeout,
                    )
            if result.get("ok"):
                if not args.replay_last and not args.next_shortlist:
                    validation = validate_batch_listing(result)
                    if not validation.get("ok"):
                        result = validation
                if result.get("ok") and not args.replay_last and not args.next_shortlist:
                    raw_data = result.get("data") or {}
                    filtered_data = boss_ledger.filter_listing(raw_data)
                    result["data"] = boss_ledger.filter_batch_listing(
                        raw_data,
                        filtered_data,
                    )
                if not args.json:
                    result = compact_listing(result)
        elif args.cmd == "recommend-city":
            result = configure_recommend_city(args.city, args.timeout)
        elif args.cmd == "load-more":
            allowance = boss_ledger.can_fetch_new_listing()
            if not allowance.get("ok"):
                result = allowance
            else:
                result = load_more_bounded(args.rounds, args.timeout)
                if result.get("ok"):
                    result["city_gate"] = boss_ledger.record_city_load(result)
        elif args.cmd == "job-detail":
            allowance = boss_ledger.can_review_jd(args.expected_job_id)
            if not allowance.get("ok"):
                result = allowance
            else:
                result = command(
                    "job_detail",
                    {"full": args.full, "expected_job_id": args.expected_job_id},
                    args.timeout,
                )
                if result.get("ok"):
                    result["review_budget"] = boss_ledger.record_jd_review(
                        args.expected_job_id
                    )
        elif args.cmd == "company-check":
            result = command(
                "company_check",
                {"expected_job_id": args.expected_job_id},
                args.timeout,
            )
        elif args.cmd == "search-state":
            result = command("search_state", timeout=args.timeout)
        elif args.cmd == "search-jobs":
            policy = boss_ledger.batch_policy()
            if policy.get("active") and policy.get("mode") == "recommend-city":
                result = {"ok": False, "error": "keyword_search_disabled_recommend_city_mode"}
            elif policy.get("active") and policy.get("mode") == "search-city":
                if not args.city:
                    result = {"ok": False, "error": "search_city_required"}
                else:
                    result = configure_search_city(args.keyword, args.city, args.timeout)
            else:
                city_code = CITY_CODES.get(args.city or "", "")
                result = command(
                    "search_jobs",
                    {"keyword": args.keyword, "city_code": city_code},
                    args.timeout,
                )
                if result.get("ok") and city_code:
                    route = wait_for_search(city_code, args.keyword, min(args.timeout, 8))
                    result["route"] = route
                    if not route.get("ok"):
                        result = route
        elif args.cmd == "close-tab":
            status = request_json("GET", "/api/status")
            current = status.get("client") or {}
            base_id = str(status.get("base_client_id") or "")
            current_id = str(current.get("id") or "")
            current_path = urlparse(str(current.get("url") or "")).path
            if not base_id:
                result = {"ok": False, "error": "base_jobs_provenance_required_before_close"}
            elif not current_id or current_id == base_id:
                result = {"ok": False, "error": "refuse_to_close_base_jobs_tab"}
            elif current_path not in {"/web/geek/chat"} and "/job_detail/" not in current_path:
                result = {"ok": False, "error": "refuse_to_close_unverified_route", "current_path": current_path}
            else:
                result = command("close_current_tab", timeout=args.timeout)
            if result.get("ok"):
                route = pin_client("base", require_path="/web/geek/jobs")
                if not route.get("ok"):
                    route = wait_for_route("/web/geek/jobs", min(args.timeout, 8))
                result["route"] = route
                if not route.get("ok"):
                    result = route
        elif args.cmd == "close-details":
            status = request_json("GET", "/api/status")
            targets = [
                client
                for client in status.get("clients") or []
                if "/job_detail/" in str(client.get("url") or "")
            ]
            outcomes = []
            for client in targets:
                outcome = command(
                    "close_current_tab",
                    timeout=args.timeout,
                    target_client_id=str(client.get("id") or ""),
                )
                outcomes.append({
                    "client_id": client.get("id"),
                    "job_id": job_id_from_url(client.get("url")),
                    "ok": bool(outcome.get("ok")),
                    "error": outcome.get("error"),
                })
            restored = pin_client("base", require_path="/web/geek/jobs")
            result = {
                "ok": all(item.get("ok") for item in outcomes) and restored.get("ok"),
                "found": len(targets),
                "closed": sum(1 for item in outcomes if item.get("ok")),
                "outcomes": outcomes,
                "base_restored": bool(restored.get("ok")),
            }
        elif args.cmd == "filter-options":
            result = command("filter_options", {"filter": args.filter}, args.timeout)
        elif args.cmd == "set-filter":
            result = command("set_filter", {"value": args.value}, args.timeout)
        elif args.cmd == "reset-filters":
            result = command("reset_filters", timeout=args.timeout)
        elif args.cmd == "chat-state":
            result = command(
                "chat_state",
                {"message": args.message, "verbose": args.verbose},
                args.timeout,
            )
        elif args.cmd == "chat-debug":
            result = command("chat_debug", timeout=args.timeout)
        elif args.cmd == "click-text":
            before_status = request_json("GET", "/api/status")
            before_clients = {
                str(client.get("id") or ""): str(client.get("url") or "")
                for client in before_status.get("clients") or []
            }
            clicked_after = time.time()
            policy = boss_ledger.batch_policy()
            expectation_label = (
                args.text == "推荐" or
                re.fullmatch(r"[^()\n]{2,24}\([^()\n]{2,10}\)", args.text) is not None
            )
            if (
                policy.get("active") and policy.get("mode") == "recommend-city" and
                expectation_label and args.text != "推荐"
            ):
                result = {"ok": False, "error": "non_recommend_pool_disabled", "requested_pool": args.text}
            elif args.text == "查看更多信息" and not args.expected_job_id:
                result = {"ok": False, "error": "expected_job_id_required"}
            elif args.text in {"立即沟通", "继续沟通"}:
                active = before_status.get("client") or {}
                active_path = urlparse(str(active.get("url") or "")).path
                base_id = str(before_status.get("base_client_id") or "")
                active_id = str(active.get("id") or "")
                if "/job_detail/" not in active_path:
                    result = {"ok": False, "error": "communication_requires_detail_tab", "current_path": active_path}
                elif not base_id or not active_id or active_id == base_id:
                    result = {"ok": False, "error": "communication_requires_derived_detail_tab"}
                else:
                    result = command(
                        "click_text",
                        {"text": args.text, "exact": not args.contains, "expected_job_id": args.expected_job_id},
                        args.timeout,
                    )
            else:
                result = command(
                    "click_text",
                    {
                        "text": args.text,
                        "exact": not args.contains,
                        "expected_job_id": args.expected_job_id,
                    },
                    args.timeout,
                )
            route_by_text = {
                "职位": "/web/geek/jobs",
                "消息": "/web/geek/chat",
                "查看职位": "/job_detail/",
            }
            if result.get("ok") and args.text == "查看更多信息":
                route = wait_for_job_route(
                    args.expected_job_id,
                    before_clients,
                    clicked_after,
                    min(args.timeout, 12),
                )
                result["route"] = route
                if not route.get("ok"):
                    result = route
            if result.get("ok") and args.text in {"立即沟通", "继续沟通"}:
                ready = wait_for_chat_ready(min(args.timeout, 15))
                result["chat"] = ready
                if not ready.get("ok"):
                    result = ready
            expected_route = route_by_text.get(args.text)
            if result.get("ok") and expected_route:
                route = wait_for_route(expected_route, min(args.timeout, 8))
                result["route"] = route
                if not route.get("ok"):
                    result = route
        elif args.cmd == "click-item":
            result = command("click_item_text", {"text": args.text}, args.timeout)
        elif args.cmd == "switch-job":
            result = switch_job_conversation(args.company, args.old_title, args.new_title, args.timeout)
        elif args.cmd == "click-job":
            policy = boss_ledger.batch_policy()
            if (
                policy.get("active") and policy.get("mode") in {"recommend-city", "search-city"} and
                not boss_ledger.job_in_last_shortlist(args.job_id)
            ):
                result = {"ok": False, "error": "job_not_in_current_batch_shortlist", "job_id": args.job_id}
            else:
                before_status = request_json("GET", "/api/status")
                before_clients = {
                    str(client.get("id") or ""): str(client.get("url") or "")
                    for client in before_status.get("clients") or []
                }
                clicked_after = time.time()
                result = command("click_job_id", {"job_id": args.job_id}, args.timeout)
                if result.get("ok") and (result.get("data") or {}).get("opened_cached_link"):
                    route = wait_for_job_route(
                        args.job_id,
                        before_clients,
                        clicked_after,
                        min(args.timeout, 12),
                    )
                    result["route"] = route
                    if not route.get("ok"):
                        result = route
        elif args.cmd == "fill-message":
            result = command("fill_message", {"text": args.text}, args.timeout)
        else:
            return 2
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0 if result.get("ok") else 1
    except (HTTPError, URLError) as exc:
        print(json.dumps(
            {"ok": False, "error": f"bridge_unavailable: {exc}"},
            ensure_ascii=False,
            separators=(",", ":"),
        ))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
