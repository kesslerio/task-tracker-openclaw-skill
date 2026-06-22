"""Contract 1: flock on the shared append path.

Invariant: many concurrent append_event() writers produce a ledger where every
line is valid JSON, no line is torn/interleaved, and every event is present.
"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from task_ledger import append_event, new_event


def _make_event(i: int) -> dict:
    # A fat payload makes a torn write far more likely if flock were absent.
    return new_event(
        "agent_action",
        task_id=f"tsk_{i:08d}",
        source="agent_autonomous",
        reason="x" * 2000,
        metadata={"i": i, "pad": "y" * 2000},
    )


def test_concurrent_appends_never_tear_lines(tmp_path):
    ledger = tmp_path / "events.jsonl"
    writers = 40

    def write(i: int) -> None:
        append_event(_make_event(i), path=ledger)

    with ThreadPoolExecutor(max_workers=writers) as pool:
        list(pool.map(write, range(writers)))

    raw = ledger.read_text(encoding="utf-8")
    lines = raw.splitlines()
    # Every line must be complete, valid JSON -- no interleaving/torn lines.
    parsed = [json.loads(line) for line in lines]
    assert len(parsed) == writers
    seen = sorted(event["metadata"]["i"] for event in parsed)
    assert seen == list(range(writers))
    # Trailing newline present, no empty/torn fragments.
    assert raw.endswith("\n")
    assert all(line for line in lines)


def test_append_event_holds_exclusive_flock_during_write(tmp_path, monkeypatch):
    """Prove the flock critical section is actually wired and exclusive.

    On Linux a single O_APPEND write() is already hard to tear, so the append
    tests above cannot by themselves prove the lock is load-bearing. This test
    asserts the mechanism directly: while one append_event holds the lock, a
    second process's non-blocking LOCK_EX on the SIDECAR lock file fails -- i.e. the
    lock is held EXCLUSIVELY for the duration of the write. (H10 moved the lock to a
    ``<ledger>.lock`` sidecar so the retention prune can atomically os.replace the
    data file; the exclusion guarantee is unchanged, only which inode carries it.)
    """
    import fcntl
    import task_ledger

    ledger = tmp_path / "events.jsonl"
    ledger.touch()
    sidecar = task_ledger._ledger_lock_path(ledger)
    lock_contended = {"value": None}

    real_flock = fcntl.flock

    def probing_flock(fd, op):
        # Intercept the LOCK_EX taken inside append_event; while we hold it, try a
        # non-blocking exclusive lock from a separate fd on the SIDECAR -- it must
        # fail (EAGAIN).
        result = real_flock(fd, op)
        if op == fcntl.LOCK_EX:
            with open(sidecar, "a", encoding="utf-8") as other:
                try:
                    real_flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    lock_contended["value"] = False  # got it -> not exclusive: bug
                    real_flock(other.fileno(), fcntl.LOCK_UN)
                except BlockingIOError:
                    lock_contended["value"] = True  # blocked -> exclusive: correct
        return result

    monkeypatch.setattr(task_ledger.fcntl, "flock", probing_flock)
    append_event(new_event("agent_action", source="agent_autonomous"), path=ledger)

    assert lock_contended["value"] is True, "append_event did not hold an exclusive flock"


def test_concurrent_appends_via_processes_stay_intact(tmp_path):
    """Cross-process variant: flock is a kernel lock, so separate processes also
    serialise. Uses subprocesses to prove the lock is not merely the GIL."""
    import subprocess
    import textwrap

    ledger = tmp_path / "events.jsonl"
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    prog = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(scripts_dir)!r})
        from task_ledger import append_event, new_event
        idx = int(sys.argv[1])
        append_event(
            new_event("agent_action", task_id=f"tsk_{{idx}}", source="agent_autonomous",
                      reason="z" * 4000, metadata={{"i": idx}}),
            path={str(ledger)!r},
        )
        """
    )
    procs = [subprocess.Popen([sys.executable, "-c", prog, str(i)]) for i in range(20)]
    for proc in procs:
        assert proc.wait() == 0

    parsed = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert sorted(event["metadata"]["i"] for event in parsed) == list(range(20))
