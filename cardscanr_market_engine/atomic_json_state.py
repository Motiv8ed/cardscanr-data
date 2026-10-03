#!/usr/bin/env python3
"""Interprocess-safe JSON state read/modify/write for CardScanR control-plane files.

Uses filelock for cross-process mutual exclusion and temp-file + os.replace for
atomic replacement. Designed for Windows host workers that own canonical runtime
JSON (marketplace ops, eBay availability, control-plane incidents). WSL is used
for X11/Chrome/CDP navigation only and must not mutate these files.

Tempfile ownership: each atomic_write_json call creates exactly one tempfile and
may delete only that path on failure. locked_json_state never glob-cleans
same-prefix temps after releasing the lock (avoids deleting another writer's
active tempfile).
"""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from filelock import FileLock, Timeout

DEFAULT_LOCK_TIMEOUT_SECONDS = 10.0


class AtomicStateError(RuntimeError):
    """Fail-closed control-plane state mutation failure."""


def lock_path_for(state_path: Path) -> Path:
    return state_path.with_suffix(state_path.suffix + ".lock")


def _fsync_file(fh: Any) -> None:
    fh.flush()
    try:
        os.fsync(fh.fileno())
    except OSError:
        pass


def _unlink_owned_temp(tmp_path: Path) -> None:
    """Remove only the exact tempfile owned by the calling writer."""
    try:
        if tmp_path.exists():
            tmp_path.unlink()
    except OSError:
        pass


def atomic_write_json(path: Path, payload: dict[str, Any], *, indent: int = 2) -> Path:
    """Atomically replace JSON at path (owned tempfile in same directory + os.replace).

    On failure, removes only the tempfile created by this call — never glob-cleans
    peer writers' temps.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=indent, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            _fsync_file(fh)
        os.replace(tmp_path, path)
        return path
    except Exception:
        _unlink_owned_temp(tmp_path)
        raise


def read_json_object(path: Path, *, default: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read JSON object. Missing file -> default. Corrupt/unreadable -> AtomicStateError."""
    if not path.exists():
        return dict(default or {})
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AtomicStateError(f"unreadable_state_json:{path.name}:{type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise AtomicStateError(f"invalid_state_json_type:{path.name}")
    return payload


@contextmanager
def locked_json_state(
    path: Path,
    *,
    timeout_seconds: float | None = None,
    default: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Hold an interprocess lock, yield mutable payload, atomically write on success.

    On exception inside the block, the prior canonical file is left unchanged.
    Lock timeout raises AtomicStateError (fail closed).

    Does not glob-clean temporary files after lock release — only atomic_write_json
    may remove the exact tempfile it created.
    """
    target = Path(path)
    timeout = (
        DEFAULT_LOCK_TIMEOUT_SECONDS
        if timeout_seconds is None
        else max(0.05, float(timeout_seconds))
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(lock_path_for(target)), timeout=timeout)
    try:
        lock.acquire()
    except Timeout as exc:
        raise AtomicStateError(f"state_lock_timeout:{target.name}") from exc
    try:
        payload = read_json_object(target, default=default)
        yield payload
        atomic_write_json(target, payload)
    finally:
        try:
            lock.release()
        except Exception:
            pass


def mutate_json_state(
    path: Path,
    mutator: Callable[[dict[str, Any]], dict[str, Any] | None],
    *,
    timeout_seconds: float | None = None,
    default: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply mutator(payload) under lock; mutator may mutate in place or return a new dict."""
    with locked_json_state(path, timeout_seconds=timeout_seconds, default=default) as payload:
        result = mutator(payload)
        if isinstance(result, dict) and result is not payload:
            payload.clear()
            payload.update(result)
        return dict(payload)


def replace_json_state(
    path: Path,
    payload: dict[str, Any],
    *,
    timeout_seconds: float | None = None,
) -> Path:
    """Public locked whole-document replacement (acquires canonical lock)."""
    target = Path(path)
    with locked_json_state(target, timeout_seconds=timeout_seconds, default={}) as current:
        current.clear()
        current.update(payload)
    return target


__all__ = [
    "AtomicStateError",
    "DEFAULT_LOCK_TIMEOUT_SECONDS",
    "atomic_write_json",
    "lock_path_for",
    "locked_json_state",
    "mutate_json_state",
    "read_json_object",
    "replace_json_state",
]
