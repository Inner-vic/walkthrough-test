"""Serialize CLI credential access for one project home and Feishu application.

The OS owns the lock, so a terminated process cannot leave a stale lock behind.
The process-local RLock lets maintenance call worker checks in the same thread.
No environment variable can bypass the lock in a child process.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from lark_auth_store import begin_replica, export_replica, import_replica

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class LarkAuthLockTimeout(TimeoutError):
    """Another credential operation did not finish within the bounded wait."""


_registry_guard = threading.Lock()
_thread_locks: dict[str, threading.RLock] = {}
_held = threading.local()


class AuthCredentialGuard:
    def __init__(self, settings: dict, replica_hash: str | None):
        self.settings = settings
        self.replica_hash = replica_hash

    def begin(self) -> None:
        begin_replica(self.settings, self.replica_hash)

    def commit(self) -> dict:
        result = export_replica(self.settings, self.replica_hash)
        if result.get("sha256"):
            self.replica_hash = result["sha256"]
        return result


def _lock_path(settings: dict) -> Path:
    configured_home = str(settings.get("lark_home") or "").strip()
    home = Path(configured_home or Path.home()).expanduser().resolve()
    app = str(settings.get("expected_lark_app_id") or settings.get("app_id") or "default").strip()
    key = hashlib.sha256((os.path.normcase(str(home)) + "\0" + app).encode("utf-8")).hexdigest()
    # Scheduled tasks may inherit a different TEMP than the business process.
    # Production must lock inside the explicit home shared by both callers.
    directory = home / ".lark-cli" / "locks" / "cc-connect" if configured_home else (
        Path(tempfile.gettempdir()) / "cc-connect-lark-auth-locks"
    )
    return directory / (key + ".lock")


def _try_os_lock(stream) -> bool:
    stream.seek(0)
    try:
        if os.name == "nt":
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (BlockingIOError, PermissionError):
        return False
    except OSError as exc:
        if exc.errno in (11, 13, 35, 36):
            return False
        raise


def _release_os_lock(stream) -> None:
    stream.seek(0)
    if os.name == "nt":
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def lark_auth_lock(settings: dict, timeout: float = 60):
    """Hold a reentrant, crash-released credential lock around CLI operations."""
    if timeout < 0:
        raise ValueError("Credential lock timeout cannot be negative")
    path = _lock_path(settings)
    key = str(path)
    with _registry_guard:
        thread_lock = _thread_locks.setdefault(key, threading.RLock())
    deadline = time.monotonic() + timeout
    if not thread_lock.acquire(timeout=timeout):
        raise LarkAuthLockTimeout("飞书凭据正在更新，请稍后重试。")
    try:
        held = getattr(_held, "keys", None)
        if held is None:
            held = _held.keys = {}
        if key in held:
            guard = held[key]
            guard.begin()
            yield guard
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            while not _try_os_lock(stream):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LarkAuthLockTimeout("飞书凭据正在更新，请稍后重试。")
                time.sleep(min(0.05, remaining))
            guard = None
            try:
                replica_hash = import_replica(settings)
                guard = AuthCredentialGuard(settings, replica_hash)
                held[key] = guard
                try:
                    yield guard
                finally:
                    guard.commit()
            finally:
                held.pop(key, None)
                _release_os_lock(stream)
    finally:
        thread_lock.release()
