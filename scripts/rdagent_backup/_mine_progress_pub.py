#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Publish live progress for one explicitly assigned RD-Agent mining attempt."""

from collections import deque
from datetime import datetime
import json
import os
import re
import subprocess
import time
import uuid


DEFAULT_SHARED = r"\/app/qlib_data\csv_tmp"
SHARED = os.environ.get("SHARED_DIR", "").strip() or DEFAULT_SHARED
ST = os.path.join(SHARED, "rdagent_status.json")
REQUEST_ID_ENV = "RDAGENT_PROGRESS_REQUEST_ID"
REQUESTED_AT_ENV = "RDAGENT_PROGRESS_REQUESTED_AT"
ATTEMPT_ID_ENV = "RDAGENT_PROGRESS_ATTEMPT_ID"
LOG_PATH_ENV = "RDAGENT_PROGRESS_LOG_PATH"
STDOUT_LOG_PATH_ENV = "RDAGENT_PROGRESS_STDOUT_LOG_PATH"
LEASE_PATH_ENV = "RDAGENT_PROGRESS_LEASE_PATH"
OWNER_PID_ENV = "RDAGENT_PROGRESS_OWNER_PID"
STALE_WARNING_MINUTES = 20


def configured_log():
    return os.environ.get(LOG_PATH_ENV, "").strip()


def configured_stdout_log():
    log = configured_log()
    if not log:
        return ""
    canonical = f"{log}.stdout.log"
    configured = os.environ.get(STDOUT_LOG_PATH_ENV, "").strip()
    if configured and os.path.normcase(os.path.abspath(configured)) == os.path.normcase(
        os.path.abspath(canonical)
    ):
        return configured
    return canonical


def configured_lease():
    return os.environ.get(LEASE_PATH_ENV, "").strip()


def _owner_alive():
    owner = os.environ.get(OWNER_PID_ENV, "").strip()
    if not owner:
        return True
    if not owner.isdigit():
        return False
    try:
        output = subprocess.run(
            ["tasklist", "/fi", f"pid eq {owner}", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout
        return f'"{owner}"' in output
    except Exception:
        return False


def _read_status():
    try:
        with open(ST, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _environment_identity():
    return (
        os.environ.get(REQUEST_ID_ENV, "").strip(),
        os.environ.get(ATTEMPT_ID_ENV, "").strip(),
    )


def _status_matches_attempt(status, request_id, attempt_id):
    return (
        status.get("state") not in {"done", "error"}
        and str(status.get("request_id", "")).strip() == request_id
        and str(status.get("attempt_id", "")).strip() == attempt_id
    )


def attempt_alive():
    request_id, attempt_id = _environment_identity()
    lease = configured_lease()
    if not request_id or not attempt_id or not lease or not os.path.isfile(lease):
        return False
    if not _owner_alive():
        return False
    status = _read_status()
    # Atomic status replacement should always be readable. On a transient share read
    # failure, keep the publisher alive but never allow it to write from that snapshot.
    return not status or _status_matches_attempt(status, request_id, attempt_id)


def write(msg):
    request_id, attempt_id = _environment_identity()
    if not request_id or not attempt_id or not attempt_alive():
        return False

    current = _read_status()
    if not _status_matches_attempt(current, request_id, attempt_id):
        return False
    requested_at = os.environ.get(REQUESTED_AT_ENV, "").strip() or str(
        current.get("requested_at", "")
    ).strip()
    payload = {
        "state": "running",
        "msg": msg,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "request_id": request_id,
        "attempt_id": attempt_id,
    }
    if requested_at:
        payload["requested_at"] = requested_at

    directory = os.path.dirname(ST) or "."
    temporary = os.path.join(
        directory,
        f".{os.path.basename(ST)}.{os.getpid()}.{uuid.uuid4().hex}.tmp",
    )
    try:
        with open(temporary, "x", encoding="utf-8", newline="") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())

        latest = _read_status()
        if not _status_matches_attempt(latest, request_id, attempt_id):
            return False
        if not attempt_alive():
            return False
        os.replace(temporary, ST)
        return True
    except Exception:
        return False
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _log_snapshot(path):
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            tail = deque(handle, maxlen=220)
        return os.path.getmtime(path), "".join(tail)
    except OSError:
        return None


def format_progress(log, stdout_log=None):
    snapshots = []
    for path in dict.fromkeys((log, stdout_log or f"{log}.stdout.log")):
        snapshot = _log_snapshot(path)
        if snapshot is not None:
            snapshots.append(snapshot)
    if not snapshots:
        return None

    snapshots.sort(key=lambda item: item[0])
    text = "\n".join(item[1] for item in snapshots)
    modified_minutes = max(0.0, (time.time() - snapshots[-1][0]) / 60)

    loops = re.findall(r"Start Loop (\d+), Step (\d+): (\w+)", text)
    loop_text = (
        f"Loop {loops[-1][0]} Step {loops[-1][1]}:{loops[-1][2]}"
        if loops
        else "编码中"
    )
    models = re.findall(r"Using chat model (\S+)", text)
    model_text = f" · 模型{models[-1].replace('openai/', '')}" if models else ""
    factors = re.findall(r"File Factor\[([^\]]+)\]", text)
    factor_text = f" · 因子{factors[-1]}" if factors else ""
    warning = " ⚠️可能卡住" if modified_minutes >= STALE_WARNING_MINUTES else ""

    base = os.path.basename(log).lower()
    route = "🌱基本面挖矿" if base.startswith("minefund_") else "⛏️量价挖矿"
    pool = next((name for name in ("csi1000", "csi500", "csi300") if name in base), "")
    label = f"{route}{(' · ' + pool) if pool else ''}"
    return (
        f"{label} · {loop_text}{model_text}{factor_text}"
        f" · 日志{modified_minutes:.1f}分前{warning}"
    )


def main():
    log = configured_log()
    if not log:
        return
    stdout_log = configured_stdout_log()
    while attempt_alive():
        message = format_progress(log, stdout_log)
        if message is not None:
            write(message)
        time.sleep(15)


if __name__ == "__main__":
    main()
