"""Small cross-process lock with stale-owner recovery for scheduled writers."""

from __future__ import annotations

import errno
import json
import os
import socket
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator


class ProcessLockBusy(RuntimeError):
    def __init__(self, path: Path, owner: dict | None = None):
        self.path = Path(path)
        self.owner = owner or {}
        super().__init__(
            f"writer lock is busy: {self.path} (pid={self.owner.get('pid')})"
        )


def _read_owner(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def _owner_pid(owner: dict | None) -> int | None:
    """Return a lock owner's PID without trusting persisted JSON types."""
    if not owner:
        return None
    try:
        return int(owner.get("pid"))
    except (TypeError, ValueError, OverflowError):
        return None


def _lock_age_seconds(path: Path) -> float:
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return 0.0


def _owner_is_stale(path: Path, owner: dict | None) -> bool:
    age = _lock_age_seconds(path)
    pid = _owner_pid(owner)
    if pid is None or not owner:
        # A creator can briefly be between O_EXCL creation and its JSON write.
        # Only reclaim malformed/incomplete records after a grace period.
        return age > 60
    same_host = (
        str(owner.get("host") or "").casefold()
        == socket.gethostname().casefold()
    )
    if same_host:
        # A valid local PID is authoritative even for unusually long jobs.
        # The age ceiling is only a last-resort recovery policy for owners on a
        # different host whose liveness cannot be queried locally.
        return not _pid_is_running(pid)
    return age > 12 * 3600


@contextmanager
def _stale_reclaim_guard(path: Path) -> Iterator[bool]:
    """Serialize stale-owner rechecks so contenders cannot delete a new lock.

    The guard file is persistent; ownership is the OS byte-range lock, so a
    crashed process releases it automatically.  This works for local paths and
    Windows SMB shares used by the scheduled writers.
    """
    guard_path = path.with_name(f"{path.name}.reclaim")
    guard_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(guard_path, os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        if os.fstat(fd).st_size < 1:
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, b"\0")
            os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            locked = False
        yield locked
    finally:
        if locked:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def acquire_process_lock(
    path: Path,
    *,
    wait_seconds: float = 0,
    reason: str = "scheduled-writer",
) -> dict:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    owner = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "token": uuid.uuid4().hex,
        "reason": reason,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(owner, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            return owner
        except FileExistsError:
            existing = _read_owner(path)
            if _owner_is_stale(path, existing):
                with _stale_reclaim_guard(path) as may_reclaim:
                    if may_reclaim:
                        # Re-read only after taking the guard.  Another
                        # contender may have replaced the stale file while we
                        # were waiting, and that new owner must not be deleted.
                        current = _read_owner(path)
                        if path.exists() and _owner_is_stale(path, current):
                            try:
                                path.unlink()
                            except FileNotFoundError:
                                pass
                        continue
            if time.monotonic() >= deadline:
                raise ProcessLockBusy(path, existing)
            time.sleep(min(1.0, max(0.05, deadline - time.monotonic())))


def release_process_lock(path: Path, owner: dict) -> None:
    path = Path(path)
    deadline = time.monotonic() + 1.0
    while True:
        try:
            with _stale_reclaim_guard(path) as may_release:
                if may_release:
                    # Serialize the token check and unlink with stale recovery.
                    # Otherwise a reclaimer could replace this path between the
                    # read and unlink, and an old owner would delete the new lock.
                    existing = _read_owner(path)
                    if existing and existing.get("token") == owner.get("token"):
                        path.unlink(missing_ok=True)
                    return
        except OSError:
            # A disconnected shared directory must not turn successful work
            # into an exception from the context-manager cleanup path.
            return
        if time.monotonic() >= deadline:
            # Fail closed: leaving our own file for stale-owner recovery is
            # safer than deleting a path whose identity was not rechecked.
            return
        time.sleep(0.01)


@contextmanager
def process_lock(
    path: Path,
    *,
    wait_seconds: float = 0,
    reason: str = "scheduled-writer",
) -> Iterator[dict]:
    owner = acquire_process_lock(path, wait_seconds=wait_seconds, reason=reason)
    try:
        yield owner
    finally:
        release_process_lock(path, owner)
