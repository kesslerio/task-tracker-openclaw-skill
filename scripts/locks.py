#!/usr/bin/env python3
"""Shared low-level file lock helpers."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None


def sidecar_lock_path(target: Path) -> Path:
    canonical = Path(target).expanduser().resolve(strict=False)
    return canonical.with_name(canonical.name + ".lock")


@contextmanager
def sidecar_flock(target: Path) -> Iterator[None]:
    """Hold an exclusive sidecar flock; guards process-level concurrency only."""
    lock_path = sidecar_lock_path(target)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock_handle:
        if fcntl is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
