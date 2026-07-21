from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

import scripts.process_lock as process_lock_module
from scripts.process_lock import ProcessLockBusy, process_lock


def test_nested_process_lock_fails_closed_and_releases(tmp_path):
    path = tmp_path / "writer.lock"
    with process_lock(path, reason="outer"):
        with pytest.raises(ProcessLockBusy):
            with process_lock(path, reason="inner"):
                pass
    assert not path.exists()
    with process_lock(path, reason="next"):
        assert path.exists()


def test_dead_same_host_owner_is_recovered(tmp_path):
    path = tmp_path / "writer.lock"
    path.write_text(
        json.dumps({
            # A guessed large PID is not guaranteed to be unused on every host.
            # Negative PIDs can never identify an owner created by this module.
            "pid": -1,
            "host": socket.gethostname(),
            "token": "stale",
        }),
        encoding="utf-8",
    )
    with process_lock(path, reason="recovery") as owner:
        assert owner["token"] != "stale"


def test_old_malformed_owner_pid_is_recovered(tmp_path):
    path = tmp_path / "writer.lock"
    path.write_text(
        json.dumps({
            "pid": "not-a-pid",
            "host": socket.gethostname(),
            "token": "malformed",
        }),
        encoding="utf-8",
    )
    old = time.time() - 61
    os.utime(path, (old, old))

    with process_lock(path, reason="malformed-recovery") as owner:
        assert owner["token"] != "malformed"


def test_fresh_malformed_owner_gets_a_write_grace_period(tmp_path):
    path = tmp_path / "writer.lock"
    path.write_text(
        json.dumps({
            "pid": "partially-written",
            "host": socket.gethostname(),
            "token": "creator-may-still-be-writing",
        }),
        encoding="utf-8",
    )

    with pytest.raises(ProcessLockBusy):
        with process_lock(path, reason="must-not-steal-fresh-malformed"):
            pass
    assert json.loads(path.read_text(encoding="utf-8"))["token"] == (
        "creator-may-still-be-writing"
    )


def test_live_same_host_owner_is_not_stolen_only_because_it_is_old(tmp_path):
    path = tmp_path / "writer.lock"
    path.write_text(
        json.dumps({
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "token": "long-running-live-owner",
        }),
        encoding="utf-8",
    )
    old = time.time() - (13 * 3600)
    os.utime(path, (old, old))

    with pytest.raises(ProcessLockBusy):
        with process_lock(path, reason="must-not-steal-live-owner"):
            pass
    assert json.loads(path.read_text(encoding="utf-8"))["token"] == (
        "long-running-live-owner"
    )


def test_release_waits_for_reclaim_guard_before_token_check(tmp_path):
    path = tmp_path / "writer.lock"
    owner = process_lock_module.acquire_process_lock(path, reason="release-race")
    thread_errors = []

    with process_lock_module._stale_reclaim_guard(path) as held:
        assert held

        def release():
            try:
                process_lock_module.release_process_lock(path, owner)
            except BaseException as exc:
                thread_errors.append(exc)

        thread = threading.Thread(target=release)
        thread.start()
        time.sleep(0.05)
        assert thread.is_alive()
        assert path.exists()

    thread.join(timeout=2)
    assert not thread.is_alive()
    assert thread_errors == []
    assert not path.exists()


def test_release_does_not_mask_success_when_shared_guard_is_unavailable(
    tmp_path, monkeypatch
):
    path = tmp_path / "writer.lock"
    owner = process_lock_module.acquire_process_lock(path, reason="nas-disconnect")

    @process_lock_module.contextmanager
    def unavailable_guard(_path):
        raise OSError("shared directory disconnected")
        yield False

    monkeypatch.setattr(
        process_lock_module, "_stale_reclaim_guard", unavailable_guard
    )

    process_lock_module.release_process_lock(path, owner)
    assert path.exists()


def test_concurrent_stale_recovery_never_unlinks_the_new_owner(
    tmp_path, monkeypatch
):
    path = tmp_path / "writer.lock"
    path.write_text(
        json.dumps({
            "pid": -1,
            "host": socket.gethostname(),
            "token": "stale",
        }),
        encoding="utf-8",
    )

    # Force both contenders to make their first stale observation together.
    # Without a serialized recheck, the second contender can then unlink the
    # first contender's newly acquired lock.
    original_read_owner = process_lock_module._read_owner
    first_reads = 0
    first_reads_lock = threading.Lock()
    stale_read_barrier = threading.Barrier(2)

    def synchronized_read_owner(candidate):
        nonlocal first_reads
        synchronize = False
        if Path(candidate) == path:
            with first_reads_lock:
                if first_reads < 2:
                    first_reads += 1
                    synchronize = True
        value = original_read_owner(candidate)
        if synchronize:
            stale_read_barrier.wait(timeout=2)
        return value

    monkeypatch.setattr(process_lock_module, "_read_owner", synchronized_read_owner)

    original_unlink = Path.unlink
    unlink_count = 0
    unlink_count_lock = threading.Lock()
    owner_entered = threading.Event()

    def coordinated_unlink(candidate, *args, **kwargs):
        nonlocal unlink_count
        current_unlink = 0
        if Path(candidate) == path:
            with unlink_count_lock:
                unlink_count += 1
                current_unlink = unlink_count
        if current_unlink == 2:
            # In the broken implementation this is the second contender acting
            # on its stale pre-lock observation.  Delay it until the first
            # contender owns the replacement, making the destructive race
            # deterministic.  In the fixed implementation this is simply the
            # first owner's legitimate release.
            assert owner_entered.wait(timeout=2)
        return original_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", coordinated_unlink)

    active = 0
    overlap = []
    thread_errors = []
    state_lock = threading.Lock()

    def contender(label):
        nonlocal active
        try:
            with process_lock(path, reason=label, wait_seconds=2):
                with state_lock:
                    active += 1
                    if active != 1:
                        overlap.append(active)
                owner_entered.set()
                time.sleep(0.03)
                with state_lock:
                    active -= 1
        except BaseException as exc:  # surfaced in the main test thread below
            with state_lock:
                thread_errors.append(exc)

    threads = [
        threading.Thread(target=contender, args=(f"worker-{index}",))
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert all(not thread.is_alive() for thread in threads)
    assert thread_errors == []
    assert overlap == []
