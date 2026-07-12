#!/usr/bin/env python3
"""Two-agent, file-backed delegation mailbox with process-kill wake notifications.

Persistent files (exactly two per role):
  <role>.message.json  - inbox for that role
  <role>.pid           - disposable listener process metadata

A sender atomically writes the recipient's message file, then terminates the
recipient's disposable listener process. The working agent is never the PID
stored in the pid file.

Cross-platform: POSIX (SIGTERM, /proc or ps) and Windows (TerminateProcess,
Get-CimInstance). The message file is authoritative on every platform; the
process-kill wake is only an optimization.

Exit codes:
  0  success / message delivered or consumed
  2  runtime error
  3  receive: inbox empty
  4  wait/await-reply: deadline expired with no message / no valid reply
  5  wait: woke without a pending message (spurious wake)
  6  await-reply: a terminal message arrived that does not match --expect
  7  takeover: refused because a same-task reply is already pending
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROLES = ("delegator", "delegatee")
MESSAGE_TYPES = (
    "assignment",
    "ack",
    "progress",
    "question",
    "response",
    "result",
    "cancel",
    "error",
    "heartbeat",
    "takeover",
)
# Interim types never end an await-reply unless explicitly expected.
INTERIM_TYPES = ("progress", "heartbeat")
PROTOCOL_VERSION = 1
IS_WINDOWS = os.name == "nt"

# Role-specific default deadlines when --timeout is omitted.
ROLE_DEFAULT_TIMEOUT = {"delegatee": 600.0, "delegator": 300.0}


def force_utf8_streams() -> None:
    """Emit UTF-8 on stdout/stderr regardless of the platform locale.

    Message files are always UTF-8, but on Windows a piped stdout defaults to
    the ANSI code page (e.g. cp1252). Without this, printing a message that
    contains any non-cp1252 character (emoji, CJK, ...) raises
    UnicodeEncodeError and the command exits non-zero even though the message
    file was written successfully. stdin is included so `--body-file -` reads a
    UTF-8 body regardless of the console code page.
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def other_role(role: str) -> str:
    return "delegatee" if role == "delegator" else "delegator"


def message_path(bus_dir: Path, role: str) -> Path:
    return bus_dir / f"{role}.message.json"


def pid_path(bus_dir: Path, role: str) -> Path:
    return bus_dir / f"{role}.pid"


def history_path(bus_dir: Path) -> Path:
    return bus_dir / "history.jsonl"


def presence_path(bus_dir: Path, role: str) -> Path:
    return bus_dir / f"{role}.presence.json"


def touch_presence(bus_dir: Path, role: str) -> None:
    atomic_write_json(
        presence_path(bus_dir, role),
        {"role": role, "pid": os.getpid(), "updated_at": utc_now()},
    )


def read_presence(bus_dir: Path, role: str) -> Optional[Dict[str, Any]]:
    try:
        return read_json(presence_path(bus_dir, role))
    except FileNotFoundError:
        return None


def presence_age_seconds(record: Dict[str, Any]) -> Optional[float]:
    raw = record.get("updated_at")
    if not isinstance(raw, str):
        return None
    try:
        seen = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return round((datetime.now(timezone.utc) - seen).total_seconds(), 1)


def emit(payload: Dict[str, Any], pretty: bool) -> None:
    if pretty:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))


def compact(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in payload.items() if v not in (None, [], {}, "")}


def slim_message(message: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Token-lean projection: drop constants and empty fields, keep content."""
    if not isinstance(message, dict):
        return None
    keep = ("message_id", "task_id", "type", "from", "subject", "body", "reply_to")
    slim = {key: message[key] for key in keep if message.get(key) not in (None, "")}
    if message.get("metadata"):
        slim["metadata"] = message["metadata"]
    return slim


def require_peer_or_fail(bus_dir: Path, role: str, pretty: bool) -> int:
    """Exit 8 when the peer role has never used this bus."""
    peer = other_role(role)
    record = read_presence(bus_dir, peer)
    if record is None:
        emit(
            {
                "ok": False,
                "reason": "peer-not-present",
                "peer": peer,
                "hint": f"No {peer} has used this bus yet; start the {peer} first "
                f"(e.g. `init --dir <dir> --role {peer}` or any {peer} command).",
            },
            pretty,
        )
        return 8
    return 0


def append_history(bus_dir: Path, event: str, payload: Dict[str, Any]) -> None:
    """Append one audit record. History is diagnostic: never fail delivery over it."""
    record: Dict[str, Any] = {"event": event, "at": utc_now()}
    record.update(payload)
    try:
        with history_path(bus_dir).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        pass


def empty_mailbox(role: str) -> Dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "role": role,
        "sequence": 0,
        "status": "empty",
        "message": None,
        "updated_at": utc_now(),
    }


def idle_pid_record(role: str) -> Dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "role": role,
        "pid": None,
        "token": None,
        "state": "idle",
        "created_at": None,
        "updated_at": utc_now(),
    }


def atomic_write_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def read_json(path: Path, fallback: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError(f"Expected an object in {path}")
        return data
    except FileNotFoundError:
        if fallback is None:
            raise
        return fallback
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc


def ensure_bus(bus_dir: Path) -> None:
    bus_dir.mkdir(parents=True, exist_ok=True)
    for role in ROLES:
        mp = message_path(bus_dir, role)
        pp = pid_path(bus_dir, role)
        if not mp.exists():
            atomic_write_json(mp, empty_mailbox(role))
        if not pp.exists():
            atomic_write_json(pp, idle_pid_record(role))


def pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if IS_WINDOWS:
        # Never use os.kill(pid, 0) on Windows: any non-CTRL signal calls
        # TerminateProcess, so a "liveness probe" would kill the listener.
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def process_command_line(pid: int) -> Optional[str]:
    """Best-effort command line of a live PID, or None when unreadable."""
    if IS_WINDOWS:
        try:
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine",
                ],
                text=True,
                capture_output=True,
                timeout=20.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        command = result.stdout.strip()
        return command or None

    proc_cmdline = Path(f"/proc/{pid}/cmdline")
    try:
        if proc_cmdline.exists():
            return proc_cmdline.read_bytes().replace(b"\x00", b" ").decode(
                "utf-8", errors="replace"
            )
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            text=True,
            capture_output=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def listener_identity_matches(pid: int, token: str) -> bool:
    """Refuse to signal a PID unless it is our tokenized listener child.

    This protects against PID reuse when a stale record points at an unrelated
    process.
    """
    command = process_command_line(pid)
    if command is None:
        return False
    return "_listener-child" in command and token in command


def terminate_pid(pid: int) -> None:
    # On Windows os.kill with SIGTERM maps to TerminateProcess, which is
    # exactly what a disposable listener needs.
    os.kill(pid, signal.SIGTERM)


def mailbox_pending(mailbox: Dict[str, Any]) -> bool:
    return mailbox.get("status") == "pending" and isinstance(mailbox.get("message"), dict)


def notify_listener(bus_dir: Path, role: str) -> Dict[str, Any]:
    record = read_json(pid_path(bus_dir, role), idle_pid_record(role))
    pid = record.get("pid")
    token = record.get("token")
    if record.get("state") != "listening" or not isinstance(pid, int):
        return {"notified": False, "reason": "no-active-listener"}
    if not pid_alive(pid):
        current = idle_pid_record(role)
        current["state"] = "stale"
        current["token"] = token
        atomic_write_json(pid_path(bus_dir, role), current)
        return {"notified": False, "reason": "stale-listener", "pid": pid}
    if not isinstance(token, str) or not listener_identity_matches(pid, token):
        current = idle_pid_record(role)
        current["state"] = "identity-mismatch"
        current["token"] = token
        atomic_write_json(pid_path(bus_dir, role), current)
        return {
            "notified": False,
            "reason": "listener-identity-mismatch",
            "pid": pid,
        }

    try:
        terminate_pid(pid)
    except (ProcessLookupError, OSError):
        return {"notified": False, "reason": "listener-exited", "pid": pid}
    except PermissionError as exc:
        return {"notified": False, "reason": f"permission-denied: {exc}", "pid": pid}

    return {"notified": True, "signal": "terminate", "pid": pid, "token": token}


def mark_pid_idle_if_token(bus_dir: Path, role: str, token: str, state: str = "idle") -> None:
    path = pid_path(bus_dir, role)
    record = read_json(path, idle_pid_record(role))
    if record.get("token") != token:
        return
    updated = idle_pid_record(role)
    updated["state"] = state
    updated["token"] = token
    atomic_write_json(path, updated)


def resolve_timeout(raw: Optional[float], role: str, *, allow_indefinite: bool) -> Optional[float]:
    """Map CLI --timeout to an effective deadline.

    None (omitted)  -> role default (delegatee 600s, delegator 300s).
    0               -> indefinite when allowed, otherwise an error.
    positive number -> that many seconds.
    """
    if raw is None:
        return ROLE_DEFAULT_TIMEOUT[role]
    if raw == 0:
        if allow_indefinite:
            return None
        raise SystemExit(
            "--timeout 0 is not allowed here: a no-reply decision needs a finite deadline"
        )
    if raw < 0:
        raise SystemExit("--timeout must be zero or a positive number of seconds")
    return float(raw)


def cmd_init(args: argparse.Namespace) -> int:
    bus_dir = Path(args.dir)
    ensure_bus(bus_dir)
    if args.role:
        touch_presence(bus_dir, args.role)
    emit(
        compact(
            {
                "ok": True,
                "directory": str(bus_dir.resolve()),
                "announced": args.role,
            }
        ),
        args.pretty,
    )
    return 0


def parse_metadata(raw: Optional[str]) -> Dict[str, Any]:
    if raw is None:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"--metadata must be valid JSON: {exc}")
    if not isinstance(value, dict):
        raise SystemExit("--metadata must be a JSON object")
    return value


def deliver(
    bus_dir: Path,
    *,
    sender: str,
    recipient: str,
    msg_type: str,
    subject: str,
    body: str,
    task_id: Optional[str],
    reply_to: Optional[str],
    metadata: Dict[str, Any],
    force: bool,
) -> Dict[str, Any]:
    path = message_path(bus_dir, recipient)
    current = read_json(path, empty_mailbox(recipient))
    if mailbox_pending(current) and not force:
        old_id = current.get("message", {}).get("message_id")
        raise SystemExit(
            f"Recipient inbox contains unconsumed message {old_id}. "
            "Wait for acknowledgement/consumption or use --force deliberately."
        )

    message_id = str(uuid.uuid4())
    sequence = int(current.get("sequence", 0)) + 1
    message = {
        "message_id": message_id,
        "task_id": task_id or message_id,
        "sequence": sequence,
        "type": msg_type,
        "from": sender,
        "to": recipient,
        "subject": subject,
        "body": body,
        "reply_to": reply_to,
        "created_at": utc_now(),
        "metadata": metadata,
    }
    envelope = {
        "protocol_version": PROTOCOL_VERSION,
        "role": recipient,
        "sequence": sequence,
        "status": "pending",
        "message": message,
        "updated_at": utc_now(),
    }
    atomic_write_json(path, envelope)
    append_history(bus_dir, "sent", {"message": message})
    touch_presence(bus_dir, sender)
    notification = notify_listener(bus_dir, recipient)
    return {"ok": True, "message": message, "notification": notification}


def resolve_body(args: argparse.Namespace) -> str:
    if getattr(args, "body_file", None):
        if args.body is not None:
            raise SystemExit("Use either --body or --body-file, not both")
        if args.body_file == "-":
            return sys.stdin.read()
        try:
            return Path(args.body_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise SystemExit(f"Cannot read --body-file: {exc}")
    if args.body is None:
        raise SystemExit("Provide --body or --body-file (use --body-file - for stdin)")
    return args.body


def cmd_send(args: argparse.Namespace) -> int:
    bus_dir = Path(args.dir)
    ensure_bus(bus_dir)
    sender = args.from_role
    recipient = args.to_role or other_role(sender)
    if sender == recipient:
        raise SystemExit("Sender and recipient roles must differ")
    result = deliver(
        bus_dir,
        sender=sender,
        recipient=recipient,
        msg_type=args.type,
        subject=args.subject,
        body=resolve_body(args),
        task_id=args.task_id,
        reply_to=args.reply_to,
        metadata=parse_metadata(args.metadata),
        force=args.force,
    )
    emit(
        compact(
            {
                "ok": True,
                "sent": slim_message(result["message"]),
                "notified": result["notification"].get("notified"),
                "notify": result["notification"].get("reason"),
            }
        ),
        args.pretty,
    )
    return 0


def consume_mailbox(bus_dir: Path, role: str, *, mark_consumed: bool) -> Dict[str, Any]:
    path = message_path(bus_dir, role)
    mailbox = read_json(path, empty_mailbox(role))
    if mark_consumed and mailbox_pending(mailbox):
        mailbox["status"] = "consumed"
        mailbox["consumed_at"] = utc_now()
        mailbox["updated_at"] = utc_now()
        atomic_write_json(path, mailbox)
        message = mailbox.get("message") or {}
        append_history(
            bus_dir,
            "consumed",
            {
                "role": role,
                "message_id": message.get("message_id"),
                "task_id": message.get("task_id"),
                "type": message.get("type"),
            },
        )
    return mailbox


def cmd_receive(args: argparse.Namespace) -> int:
    bus_dir = Path(args.dir)
    ensure_bus(bus_dir)
    touch_presence(bus_dir, args.role)
    mailbox = consume_mailbox(bus_dir, args.role, mark_consumed=not args.peek)
    emit(
        compact(
            {
                "status": mailbox.get("status"),
                "message": slim_message(mailbox.get("message")),
            }
        ),
        args.pretty,
    )
    return 0 if mailbox_pending(mailbox) or mailbox.get("status") == "consumed" else 3


def cmd_listener_child(args: argparse.Namespace) -> int:
    # This disposable process is intentionally terminated by the peer.
    # The parent wait process, and therefore the agent, remains alive.
    # On Windows termination is hard (TerminateProcess) and the handler
    # never runs; that is fine because the child holds no state.
    stop = False

    def _stop(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while not stop:
        time.sleep(0.2)
    return 0


def start_listener(bus_dir: Path, role: str) -> Tuple[subprocess.Popen[Any], str]:
    token = str(uuid.uuid4())
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "_listener-child",
        "--token",
        token,
    ]
    popen_kwargs: Dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if IS_WINDOWS:
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        popen_kwargs["start_new_session"] = True
    child = subprocess.Popen(command, **popen_kwargs)
    record = {
        "protocol_version": PROTOCOL_VERSION,
        "role": role,
        "pid": child.pid,
        "token": token,
        "state": "listening",
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    atomic_write_json(pid_path(bus_dir, role), record)
    return child, token


def stop_child(child: subprocess.Popen[Any]) -> None:
    if child.poll() is not None:
        return
    try:
        child.terminate()
        child.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=1.0)
    except (ProcessLookupError, OSError):
        pass


def wait_for_message(
    bus_dir: Path,
    role: str,
    timeout: Optional[float],
    poll_interval: float,
    *,
    peek: bool,
) -> Dict[str, Any]:
    """Block until a message is pending, the wake signal fires, or the deadline.

    Returns {"wake_reason", "delivered", "elapsed_seconds", "mailbox"}.
    `delivered` is True only when a message was pending during THIS call; a
    stale consumed snapshot from an earlier receive never counts. Consumes a
    pending message unless peeking. timeout=None waits indefinitely.
    """
    mailbox = read_json(message_path(bus_dir, role), empty_mailbox(role))
    if mailbox_pending(mailbox):
        mailbox = consume_mailbox(bus_dir, role, mark_consumed=not peek)
        return {
            "wake_reason": "message-already-pending",
            "delivered": True,
            "elapsed_seconds": 0.0,
            "mailbox": mailbox,
        }

    child, token = start_listener(bus_dir, role)
    started = time.monotonic()
    wake_reason = "listener-terminated"
    try:
        # Close the registration race: a message may have been written just
        # before the PID file became visible.
        mailbox = read_json(message_path(bus_dir, role), empty_mailbox(role))
        if mailbox_pending(mailbox):
            wake_reason = "message-arrived-during-registration"
            stop_child(child)
        else:
            while child.poll() is None:
                if timeout is not None and time.monotonic() - started >= timeout:
                    wake_reason = "timeout"
                    stop_child(child)
                    break
                time.sleep(poll_interval)

        mailbox = read_json(message_path(bus_dir, role), empty_mailbox(role))
        delivered = mailbox_pending(mailbox)
        if delivered:
            mailbox = consume_mailbox(bus_dir, role, mark_consumed=not peek)
        return {
            "wake_reason": wake_reason,
            "delivered": delivered,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "mailbox": mailbox,
        }
    finally:
        stop_child(child)
        mark_pid_idle_if_token(bus_dir, role, token, state="idle")


def cmd_wait(args: argparse.Namespace) -> int:
    bus_dir = Path(args.dir)
    ensure_bus(bus_dir)
    touch_presence(bus_dir, args.role)
    if args.require_peer:
        failed = require_peer_or_fail(bus_dir, args.role, args.pretty)
        if failed:
            return failed
    timeout = resolve_timeout(args.timeout, args.role, allow_indefinite=True)
    result = wait_for_message(
        bus_dir, args.role, timeout, args.poll_interval, peek=args.peek
    )
    emit(
        compact(
            {
                "wake": result["wake_reason"],
                "delivered": result["delivered"],
                "elapsed": result["elapsed_seconds"],
                "message": slim_message(result["mailbox"].get("message"))
                if result["delivered"]
                else None,
            }
        ),
        args.pretty,
    )
    if result["delivered"]:
        return 0
    return 4 if result["wake_reason"] == "timeout" else 5


def parse_expect(raw: str) -> List[str]:
    expected = [item.strip() for item in raw.split(",") if item.strip()]
    if not expected:
        raise SystemExit("--expect must list at least one message type")
    unknown = [item for item in expected if item not in MESSAGE_TYPES]
    if unknown:
        raise SystemExit(
            f"--expect contains unknown message types {unknown}; "
            f"valid types: {', '.join(MESSAGE_TYPES)}"
        )
    return expected


def await_reply_core(
    bus_dir: Path,
    role: str,
    task_id: Optional[str],
    expected: List[str],
    timeout: float,
    poll_interval: float,
) -> Tuple[int, Dict[str, Any]]:
    deadline = time.monotonic() + timeout
    interim: List[Optional[Dict[str, Any]]] = []

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 4, compact(
                {
                    "ok": False,
                    "reason": "timeout",
                    "expected": expected,
                    "task_id": task_id,
                    "interim": interim,
                }
            )

        result = wait_for_message(bus_dir, role, remaining, poll_interval, peek=False)
        if not result["delivered"]:
            continue  # spurious wake or timeout; loop re-checks the deadline
        message = result["mailbox"].get("message") or {}
        msg_type = message.get("type")
        task_matches = task_id is None or message.get("task_id") == task_id

        if task_matches and msg_type in expected:
            return 0, compact(
                {
                    "ok": True,
                    "reason": "reply-received",
                    "message": slim_message(message),
                    "interim": interim,
                }
            )
        if task_matches and msg_type in INTERIM_TYPES:
            interim.append(slim_message(message))
            continue
        # Terminal mismatch: wrong task or a decision-changing type. The
        # message is already consumed; hand it to the caller to evaluate.
        return 6, compact(
            {
                "ok": False,
                "reason": "unexpected-message",
                "expected": expected,
                "task_id": task_id,
                "message": slim_message(message),
                "interim": interim,
            }
        )


def cmd_await_reply(args: argparse.Namespace) -> int:
    bus_dir = Path(args.dir)
    ensure_bus(bus_dir)
    touch_presence(bus_dir, args.role)
    if args.require_peer:
        failed = require_peer_or_fail(bus_dir, args.role, args.pretty)
        if failed:
            return failed
    timeout = resolve_timeout(args.timeout, args.role, allow_indefinite=False)
    assert timeout is not None
    expected = parse_expect(args.expect)
    code, payload = await_reply_core(
        bus_dir, args.role, args.task_id, expected, timeout, args.poll_interval
    )
    emit(payload, args.pretty)
    return code


def cmd_request(args: argparse.Namespace) -> int:
    """Send a message and await its reply in one invocation (one agent tool call)."""
    bus_dir = Path(args.dir)
    ensure_bus(bus_dir)
    sender = args.from_role
    if args.require_peer:
        failed = require_peer_or_fail(bus_dir, sender, args.pretty)
        if failed:
            return failed
    timeout = resolve_timeout(args.timeout, sender, allow_indefinite=False)
    assert timeout is not None
    expected = parse_expect(args.expect)
    result = deliver(
        bus_dir,
        sender=sender,
        recipient=other_role(sender),
        msg_type=args.type,
        subject=args.subject,
        body=resolve_body(args),
        task_id=args.task_id,
        reply_to=args.reply_to,
        metadata=parse_metadata(args.metadata),
        force=args.force,
    )
    task_id = result["message"]["task_id"]
    code, reply = await_reply_core(
        bus_dir, sender, task_id, expected, timeout, args.poll_interval
    )
    emit(
        compact(
            {
                "sent": slim_message(result["message"]),
                "notified": result["notification"].get("notified"),
                "reply": reply,
            }
        ),
        args.pretty,
    )
    return code


def cmd_takeover(args: argparse.Namespace) -> int:
    bus_dir = Path(args.dir)
    ensure_bus(bus_dir)
    role = args.role
    peer = other_role(role)

    # Final late-reply check: never take over past a same-task reply that is
    # already durable in our inbox.
    touch_presence(bus_dir, role)
    mailbox = read_json(message_path(bus_dir, role), empty_mailbox(role))
    if mailbox_pending(mailbox):
        message = mailbox.get("message") or {}
        if args.task_id is None or message.get("task_id") == args.task_id:
            emit(
                compact(
                    {
                        "ok": False,
                        "reason": "late-reply-pending",
                        "hint": "Consume this message with `receive` and evaluate it instead of taking over.",
                        "message": slim_message(message),
                    }
                ),
                args.pretty,
            )
            return 7

    result = deliver(
        bus_dir,
        sender=role,
        recipient=peer,
        msg_type="takeover",
        subject=args.subject or f"Takeover of {args.task_id or 'current task'}",
        body=args.reason,
        task_id=args.task_id,
        reply_to=None,
        metadata=parse_metadata(args.metadata),
        # A takeover may replace an unconsumed message to the silent peer;
        # that peer must see the revocation first when it resumes.
        force=True,
    )
    emit(
        compact(
            {
                "ok": True,
                "reason": "takeover-sent",
                "sent": slim_message(result["message"]),
                "notified": result["notification"].get("notified"),
            }
        ),
        args.pretty,
    )
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    bus_dir = Path(args.dir)
    ensure_bus(bus_dir)
    records: List[Dict[str, Any]] = []
    try:
        with history_path(bus_dir).open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a torn concurrent append must not hide the rest
                if not isinstance(record, dict):
                    continue
                message = record.get("message") or {}
                task_id = record.get("task_id") or message.get("task_id")
                msg_type = record.get("type") or message.get("type")
                if args.task_id and task_id != args.task_id:
                    continue
                if args.type and msg_type != args.type:
                    continue
                if args.event and record.get("event") != args.event:
                    continue
                records.append(record)
    except FileNotFoundError:
        pass
    if args.limit > 0:
        records = records[-args.limit :]
    print(json.dumps({"count": len(records), "records": records}, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    bus_dir = Path(args.dir)
    ensure_bus(bus_dir)
    roles: Dict[str, Any] = {}
    for role in ROLES:
        pid_record = read_json(pid_path(bus_dir, role), idle_pid_record(role))
        pid = pid_record.get("pid")
        roles[role] = {
            "mailbox": read_json(message_path(bus_dir, role), empty_mailbox(role)),
            "listener": pid_record,
            "listener_alive": isinstance(pid, int) and pid_alive(pid),
            "presence": read_presence(bus_dir, role),
        }
    print(json.dumps({"directory": str(bus_dir.resolve()), "roles": roles}, indent=2))
    return 0


def cmd_peers(args: argparse.Namespace) -> int:
    """Who has used this bus, and how recently. Verifies the peer started first."""
    bus_dir = Path(args.dir)
    ensure_bus(bus_dir)
    peers: Dict[str, Any] = {}
    for role in ROLES:
        record = read_presence(bus_dir, role)
        if record is None:
            peers[role] = None
        else:
            peers[role] = compact(
                {
                    "pid": record.get("pid"),
                    "last_seen": record.get("updated_at"),
                    "age_seconds": presence_age_seconds(record),
                }
            )
    emit({"peers": peers}, args.pretty)
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    bus_dir = Path(args.dir)
    ensure_bus(bus_dir)
    targets = ROLES if args.role == "all" else (args.role,)
    for role in targets:
        record = read_json(pid_path(bus_dir, role), idle_pid_record(role))
        pid = record.get("pid")
        token = record.get("token")
        if (
            isinstance(pid, int)
            and pid_alive(pid)
            and isinstance(token, str)
            and listener_identity_matches(pid, token)
        ):
            try:
                terminate_pid(pid)
            except (ProcessLookupError, OSError):
                pass
        atomic_write_json(message_path(bus_dir, role), empty_mailbox(role))
        atomic_write_json(pid_path(bus_dir, role), idle_pid_record(role))
        try:
            presence_path(bus_dir, role).unlink()
        except FileNotFoundError:
            pass
    print(json.dumps({"ok": True, "reset": list(targets)}, indent=2))
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    """End-to-end verification of this machine: real subprocesses, real notify chain."""
    tool = str(Path(__file__).resolve())
    work = Path(tempfile.mkdtemp(prefix="delegation-selftest-"))
    bus = str(work / "bus")
    checks: List[Tuple[str, bool, str]] = []

    def run_cli(*cli: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, tool, *cli],
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=timeout,
        )

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    def wait_listening(role: str, timeout: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = read_json(pid_path(Path(bus), role), idle_pid_record(role))
            if record.get("state") == "listening":
                return True
            time.sleep(0.1)
        return False

    def wait_not_pending(role: str, timeout: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            mailbox = read_json(message_path(Path(bus), role), empty_mailbox(role))
            if not mailbox_pending(mailbox):
                return True
            time.sleep(0.1)
        return False

    def wait_pending(role: str, timeout: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            mailbox = read_json(message_path(Path(bus), role), empty_mailbox(role))
            if mailbox_pending(mailbox):
                return True
            time.sleep(0.1)
        return False

    try:
        result = run_cli("init", "--dir", bus)
        check("init", result.returncode == 0, result.stderr)

        result = run_cli(
            "wait", "--dir", bus, "--role", "delegatee", "--require-peer", "--timeout", "1"
        )
        check(
            "require-peer exits 8 before delegator ran",
            result.returncode == 8 and "peer-not-present" in result.stdout,
            result.stdout,
        )

        result = run_cli(
            "send", "--dir", bus, "--from-role", "delegator", "--type", "assignment",
            "--task-id", "T1", "--subject", "s", "--body", "b",
        )
        check(
            "send without listener",
            result.returncode == 0 and "no-active-listener" in result.stdout,
            result.stdout + result.stderr,
        )
        result = run_cli("receive", "--dir", bus, "--role", "delegatee")
        check("receive consumes", result.returncode == 0, result.stderr)

        unicode_body = "Café ☕ 日本語 — ação, naïve, \U0001f680"
        result = run_cli(
            "send", "--dir", bus, "--from-role", "delegator", "--type", "assignment",
            "--task-id", "U1", "--subject", "Résumé", "--body", unicode_body,
        )
        got = run_cli("receive", "--dir", bus, "--role", "delegatee")
        check(
            "non-ASCII body survives send and receive",
            result.returncode == 0 and got.returncode == 0 and unicode_body in got.stdout,
            result.stdout + result.stderr + got.stdout + got.stderr,
        )

        result = run_cli(
            "wait", "--dir", bus, "--role", "delegatee", "--require-peer", "--timeout", "1"
        )
        check(
            "require-peer passes after delegator ran",
            result.returncode == 4,
            result.stdout,
        )
        result = run_cli("peers", "--dir", bus)
        check(
            "peers shows both roles",
            result.returncode == 0
            and '"delegator":{"pid"' in result.stdout
            and '"delegatee":{"pid"' in result.stdout,
            result.stdout,
        )

        waiter = subprocess.Popen(
            [sys.executable, tool, "wait", "--dir", bus, "--role", "delegatee",
             "--timeout", "60"],
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        registered = wait_listening("delegatee")
        result = run_cli(
            "send", "--dir", bus, "--from-role", "delegator", "--type", "response",
            "--task-id", "T1", "--subject", "s2", "--body", "b2",
        )
        out, err = waiter.communicate(timeout=90)
        check(
            "wait woken by notify",
            registered
            and waiter.returncode == 0
            and '"notified":true' in result.stdout
            and ("listener-terminated" in out or "message-arrived-during-registration" in out),
            out + err + result.stdout,
        )

        result = run_cli("wait", "--dir", bus, "--role", "delegator", "--timeout", "1")
        check("wait timeout exits 4", result.returncode == 4, result.stdout)

        run_cli(
            "send", "--dir", bus, "--from-role", "delegator", "--type", "question",
            "--task-id", "T1", "--subject", "q", "--body", "q",
        )
        result = run_cli(
            "send", "--dir", bus, "--from-role", "delegator", "--type", "question",
            "--task-id", "T1", "--subject", "q2", "--body", "q2",
        )
        check("double send refused", result.returncode != 0, result.stdout)
        run_cli("receive", "--dir", bus, "--role", "delegatee")

        awaiter = subprocess.Popen(
            [sys.executable, tool, "await-reply", "--dir", bus, "--role", "delegator",
             "--task-id", "T1", "--expect", "ack", "--timeout", "60"],
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        wait_listening("delegator")
        run_cli(
            "send", "--dir", bus, "--from-role", "delegatee", "--type", "progress",
            "--task-id", "T1", "--subject", "p", "--body", "p",
        )
        wait_not_pending("delegator")
        wait_listening("delegator")
        run_cli(
            "send", "--dir", bus, "--from-role", "delegatee", "--type", "ack",
            "--task-id", "T1", "--subject", "a", "--body", "a",
        )
        out, err = awaiter.communicate(timeout=90)
        check(
            "await-reply with interim progress",
            awaiter.returncode == 0
            and '"reply-received"' in out
            and '"type":"progress"' in out,
            out + err,
        )

        requester = subprocess.Popen(
            [sys.executable, tool, "request", "--dir", bus, "--from-role", "delegator",
             "--type", "question", "--task-id", "T3", "--subject", "q3", "--body", "q3",
             "--expect", "response", "--timeout", "60"],
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        wait_pending("delegatee")
        run_cli("receive", "--dir", bus, "--role", "delegatee")
        wait_listening("delegator")
        run_cli(
            "send", "--dir", bus, "--from-role", "delegatee", "--type", "response",
            "--task-id", "T3", "--subject", "a3", "--body", "a3",
        )
        out, err = requester.communicate(timeout=90)
        check(
            "request combines send and await-reply",
            requester.returncode == 0
            and '"reply-received"' in out
            and '"type":"response"' in out,
            out + err,
        )

        result = run_cli(
            "await-reply", "--dir", bus, "--role", "delegator", "--task-id", "T1",
            "--expect", "result", "--timeout", "2",
        )
        check("await-reply timeout exits 4", result.returncode == 4, result.stdout)

        awaiter = subprocess.Popen(
            [sys.executable, tool, "await-reply", "--dir", bus, "--role", "delegator",
             "--task-id", "T1", "--expect", "result", "--timeout", "60"],
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        wait_listening("delegator")
        run_cli(
            "send", "--dir", bus, "--from-role", "delegatee", "--type", "error",
            "--task-id", "T1", "--subject", "e", "--body", "e",
        )
        out, err = awaiter.communicate(timeout=90)
        check("await-reply unexpected exits 6", awaiter.returncode == 6, out + err)

        run_cli(
            "send", "--dir", bus, "--from-role", "delegatee", "--type", "result",
            "--task-id", "T1", "--subject", "r", "--body", "r",
        )
        result = run_cli(
            "takeover", "--dir", bus, "--role", "delegator", "--task-id", "T1",
            "--reason", "deadline missed",
        )
        check("takeover refused on late reply", result.returncode == 7, result.stdout)
        run_cli("receive", "--dir", bus, "--role", "delegator")

        run_cli(
            "send", "--dir", bus, "--from-role", "delegator", "--type", "assignment",
            "--task-id", "T2", "--subject", "s", "--body", "b",
        )
        result = run_cli(
            "takeover", "--dir", bus, "--role", "delegator", "--task-id", "T2",
            "--reason", "no ack",
        )
        peer = run_cli("receive", "--dir", bus, "--role", "delegatee")
        check(
            "takeover replaces pending assignment",
            result.returncode == 0 and '"type":"takeover"' in peer.stdout,
            result.stdout + peer.stdout,
        )

        result = run_cli("history", "--dir", bus, "--task-id", "T1")
        check(
            "history records task",
            result.returncode == 0 and '"sent"' in result.stdout,
            result.stdout,
        )

        result = run_cli("reset", "--dir", bus)
        check("reset", result.returncode == 0, result.stderr)
        result = run_cli("receive", "--dir", bus, "--role", "delegator")
        check("receive after reset exits 3", result.returncode == 3, result.stdout)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    failed = [name for name, ok, _ in checks if not ok]
    report = {
        "ok": not failed,
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "passed": len(checks) - len(failed),
        "failed": len(failed),
        "checks": [
            {"name": name, "ok": ok, **({"detail": detail[-800:]} if not ok else {})}
            for name, ok, detail in checks
        ],
    }
    print(json.dumps(report, indent=2))
    return 0 if not failed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--dir", default=".delegation")
        p.add_argument("--pretty", action="store_true", help="Indented output (default is compact)")

    p_init = sub.add_parser("init", help="Create the persistent protocol files")
    add_common(p_init)
    p_init.add_argument(
        "--role", choices=ROLES, help="Announce this role's presence so the peer can verify it"
    )
    p_init.set_defaults(func=cmd_init)

    p_send = sub.add_parser("send", help="Write the peer inbox and terminate its listener")
    add_common(p_send)
    p_send.add_argument("--from-role", required=True, choices=ROLES)
    p_send.add_argument("--to-role", choices=ROLES)
    p_send.add_argument("--type", required=True, choices=MESSAGE_TYPES)
    p_send.add_argument("--subject", required=True)
    p_send.add_argument("--body")
    p_send.add_argument(
        "--body-file",
        help="Read the body from a UTF-8 file; use - for stdin. Alternative to --body.",
    )
    p_send.add_argument("--task-id")
    p_send.add_argument("--reply-to")
    p_send.add_argument("--metadata", help="JSON object")
    p_send.add_argument("--force", action="store_true", help="Overwrite an unconsumed inbox")
    p_send.set_defaults(func=cmd_send)

    p_receive = sub.add_parser("receive", help="Read this role's inbox")
    add_common(p_receive)
    p_receive.add_argument("--role", required=True, choices=ROLES)
    p_receive.add_argument("--peek", action="store_true", help="Do not mark pending message consumed")
    p_receive.set_defaults(func=cmd_receive)

    p_wait = sub.add_parser("wait", help="Wait until peer kills the disposable listener")
    add_common(p_wait)
    p_wait.add_argument("--role", required=True, choices=ROLES)
    p_wait.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Seconds. Omitted: 600 for delegatee, 300 for delegator. 0: wait indefinitely.",
    )
    p_wait.add_argument("--poll-interval", type=float, default=0.1)
    p_wait.add_argument("--peek", action="store_true")
    p_wait.add_argument(
        "--require-peer",
        action="store_true",
        help="Exit 8 immediately when the peer role has never used this bus",
    )
    p_wait.set_defaults(func=cmd_wait)

    p_await = sub.add_parser(
        "await-reply",
        help="Wait for a reply of an expected type for a task; exit 4 when none arrives in time",
    )
    add_common(p_await)
    p_await.add_argument("--role", required=True, choices=ROLES)
    p_await.add_argument("--task-id", help="Only messages with this task ID satisfy the wait")
    p_await.add_argument(
        "--expect",
        required=True,
        help="Comma-separated acceptable message types, e.g. ack or result,error",
    )
    p_await.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Seconds. Omitted: 600 for delegatee, 300 for delegator. 0 is rejected.",
    )
    p_await.add_argument("--poll-interval", type=float, default=0.1)
    p_await.add_argument("--require-peer", action="store_true")
    p_await.set_defaults(func=cmd_await_reply)

    p_request = sub.add_parser(
        "request",
        help="send + await-reply in one call: deliver a message and wait for its reply",
    )
    add_common(p_request)
    p_request.add_argument("--from-role", required=True, choices=ROLES)
    p_request.add_argument("--type", required=True, choices=MESSAGE_TYPES)
    p_request.add_argument("--subject", required=True)
    p_request.add_argument("--body")
    p_request.add_argument("--body-file")
    p_request.add_argument("--task-id")
    p_request.add_argument("--reply-to")
    p_request.add_argument("--metadata")
    p_request.add_argument("--force", action="store_true")
    p_request.add_argument(
        "--expect",
        default="ack",
        help="Comma-separated acceptable reply types (default: ack)",
    )
    p_request.add_argument("--timeout", type=float, default=None)
    p_request.add_argument("--poll-interval", type=float, default=0.1)
    p_request.add_argument("--require-peer", action="store_true")
    p_request.set_defaults(func=cmd_request)

    p_peers = sub.add_parser(
        "peers", help="Show which roles have used this bus and how recently"
    )
    add_common(p_peers)
    p_peers.set_defaults(func=cmd_peers)

    p_takeover = sub.add_parser(
        "takeover",
        help="After a missed deadline: final late-reply check, then send a takeover message",
    )
    add_common(p_takeover)
    p_takeover.add_argument("--role", required=True, choices=ROLES)
    p_takeover.add_argument("--task-id")
    p_takeover.add_argument("--subject")
    p_takeover.add_argument("--reason", required=True, help="Why ownership is being reclaimed")
    p_takeover.add_argument("--metadata", help="JSON object")
    p_takeover.set_defaults(func=cmd_takeover)

    p_status = sub.add_parser("status", help="Show mailboxes and listener liveness")
    p_status.add_argument("--dir", default=".delegation")
    p_status.set_defaults(func=cmd_status)

    p_history = sub.add_parser("history", help="Show the append-only sent/consumed audit trail")
    p_history.add_argument("--dir", default=".delegation")
    p_history.add_argument("--task-id", help="Only records for this task")
    p_history.add_argument("--type", choices=MESSAGE_TYPES, help="Only this message type")
    p_history.add_argument("--event", choices=("sent", "consumed"), help="Only this event")
    p_history.add_argument("--limit", type=int, default=20, help="Last N records; 0 for all")
    p_history.set_defaults(func=cmd_history)

    p_selftest = sub.add_parser(
        "selftest", help="Run the end-to-end protocol test in a temporary directory"
    )
    p_selftest.set_defaults(func=cmd_selftest)

    p_reset = sub.add_parser("reset", help="Stop listeners and clear protocol state")
    p_reset.add_argument("--dir", default=".delegation")
    p_reset.add_argument("--role", choices=("all",) + ROLES, default="all")
    p_reset.set_defaults(func=cmd_reset)

    p_child = sub.add_parser("_listener-child", help=argparse.SUPPRESS)
    p_child.add_argument("--token", required=True)
    p_child.set_defaults(func=cmd_listener_child)

    return parser


def main() -> int:
    force_utf8_streams()
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except (RuntimeError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
