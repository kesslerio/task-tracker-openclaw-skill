#!/usr/bin/env python3
"""U7 Obsidian ``## EOD Summary`` writer: the one human-readable EOD artifact (KTD-5).

The JSONL ledger is the canonical machine audit of the evening ritual; the legacy
Lobster ``#### Standup Audit`` / ``#### Provenance`` / ``#### Retro Corrections``
blocks are NOT reproduced. The single user-facing artifact the EOD keeps (user-confirmed,
KTD-5) is a ``## EOD Summary`` section on the day's Obsidian daily note: what got done
today, what is still open, and tomorrow's #1.

This module is a PURE FILE UPSERT -- it has NO delivery, board-mutation, or ledger
dependency, which is exactly why it is the v0.3-U7 split seam (PR-A). It reuses the
section-upsert idiom ``update_weekly_embeds.update_or_append_progress_section`` already
uses: a regex that spans the managed header through the next ``##`` or EOF, replaced in
place with ``count=1``, falling back to an append when the section is absent.

IDEMPOTENT by construction: a re-run REPLACES the ``## EOD Summary`` section, never
appends a second one. The header anchor (``## EOD Summary``) is the single managed marker
-- the same content written twice yields a byte-identical file, and changed content
swaps the section in place. A missing daily note is CREATED (a bare file with just the
section) so a same-day EOD never silently no-ops for lack of a note.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import cos_config

# The single managed section header -- the upsert anchor. A re-run keys off this exact
# header to REPLACE (never append) the section, so the writer is idempotent.
SUMMARY_HEADER = "## EOD Summary"

# The daily-notes directory + filename pattern, mirroring eod_sync / eod_review so the
# EOD summary lands on the SAME ``YYYY-MM-DD.md`` note the rest of the lane reads/writes.
_DAILY_NOTES_DEFAULT = Path.home() / "Obsidian" / "01-TODOs" / "Daily"


def daily_notes_dir() -> Path:
    """The daily-notes directory, env-overridable (the same env the lane already reads)."""
    raw = os.getenv("TASK_TRACKER_DAILY_NOTES_DIR")
    return (Path(raw) if raw else _DAILY_NOTES_DEFAULT).expanduser()


def daily_note_path(date_str: str | None = None) -> Path:
    """The ``YYYY-MM-DD.md`` daily-note path for ``date_str`` (default: local today)."""
    day = date_str or cos_config.local_today().strftime("%Y-%m-%d")
    return daily_notes_dir() / f"{day}.md"


# Span the managed section: its header line through (but not including) the next ``##``
# heading or end-of-file. Mirrors ``update_weekly_embeds._PROGRESS_SECTION_RE`` so the two
# upserts share one proven shape. ``re.escape`` keeps the header literal.
_SUMMARY_SECTION_RE = re.compile(
    rf"(^{re.escape(SUMMARY_HEADER)}\s*\n)"  # opening header
    r"(.*?)"                                  # section body (non-greedy)
    r"(?=^##\s|\Z)",                          # lookahead: next ## heading or EOF
    re.MULTILINE | re.DOTALL,
)


def _bullet_lines(items: list[str], *, empty: str) -> list[str]:
    """Render a bullet list, or a single italic ``empty`` placeholder when there are none."""
    if not items:
        return [f"_{empty}_"]
    return [f"- {item}" for item in items]


def render_summary(
    *,
    done_today: list[str],
    still_open: list[str],
    tomorrow_top: str | None,
) -> str:
    """Build the ``## EOD Summary`` markdown block (no file I/O).

    Three labelled groups -- done today / still open / tomorrow's #1 -- each degrading to
    an explicit italic placeholder so an empty group reads as a deliberate "nothing"
    rather than a missing section. Pure: the same inputs always render byte-identical
    output, which is what makes the upsert idempotent.
    """
    lines: list[str] = [SUMMARY_HEADER, ""]
    lines.append("**Done today**")
    lines.extend(_bullet_lines(done_today, empty="Nothing recorded done today"))
    lines.append("")
    lines.append("**Still open**")
    lines.extend(_bullet_lines(still_open, empty="Board is clear"))
    lines.append("")
    lines.append("**Tomorrow's #1**")
    lines.append(f"- {tomorrow_top}" if tomorrow_top else "_No #1 set_")
    return "\n".join(lines)


def upsert_section(content: str, section: str) -> str:
    """REPLACE the ``## EOD Summary`` section in ``content``, or APPEND it when absent.

    Idempotent: keyed on the single ``## EOD Summary`` header, a re-run replaces the
    existing section in place (``count=1``) rather than appending a duplicate. When the
    note has no such section yet, the block is appended at the end. The replacement
    carries its own trailing blank line so successive sections stay separated.
    """
    replacement = section.rstrip("\n") + "\n\n"
    if _SUMMARY_SECTION_RE.search(content):
        return _SUMMARY_SECTION_RE.sub(replacement, content, count=1)
    base = content.rstrip("\n")
    prefix = (base + "\n\n") if base else ""
    return prefix + replacement


def write_summary(
    *,
    done_today: list[str],
    still_open: list[str],
    tomorrow_top: str | None,
    date_str: str | None = None,
) -> dict[str, Any]:
    """Upsert the ``## EOD Summary`` onto the day's daily note -- IDEMPOTENT.

    Renders the section and writes it to ``YYYY-MM-DD.md`` (creating the note + its
    parent dir if absent). A re-run with the same inputs leaves the file byte-identical
    (``changed: False``); changed inputs swap the section in place. Returns
    ``{"ok", "path", "changed"}``. Never appends a second ``## EOD Summary``.
    """
    section = render_summary(
        done_today=done_today, still_open=still_open, tomorrow_top=tomorrow_top
    )
    path = daily_note_path(date_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    after = upsert_section(before, section)
    changed = after != before
    if changed:
        path.write_text(after, encoding="utf-8")
    return {"ok": True, "path": str(path), "changed": changed}
