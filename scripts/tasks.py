#!/usr/bin/env python3
"""
Task Tracker CLI - Supports both Work and Personal tasks.

Usage:
    tasks.py list [--priority high|medium|low] [--status open|done] [--completed-since 24h|7d|30d] [--due today|this-week|overdue|due-or-overdue]
    tasks.py --personal list
    tasks.py add "Task title" [--priority high|medium|low] [--due YYYY-MM-DD]
    tasks.py done "task_id"
    tasks.py blockers [--person NAME]
    tasks.py archive
"""

import argparse
import json
import os
import re
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).parent))
import error_envelope
from daily_notes import extract_completed_tasks
from candidate_review import candidate_review_summary
from evidence_matching import (
    FUZZY_EVIDENCE_LINK_THRESHOLD,
    FUZZY_REVIEW_THRESHOLD,
    build_task_catalog,
    canonical_record as _canonical_record,
    extract_done_lines,
    match_evidence_line,
    safe_load_task_records as _safe_load_task_records,
)
from standup_common import get_calendar_events, flatten_calendar_events
import delegation
from task_identity import audit_payload, print_json as print_identity_json
from task_audit import collect_task_audit, task_audit_summary
from task_lines import remove_task_line
from task_repair import repair_missing_ids
from task_transitions import block_unsafe_query, complete_by_id, print_result
from task_records import (
    active_records,
    record_to_task_dict,
    task_records,
)
from utils import (
    detect_format,
    get_tasks_file,
    get_section_display_name,
    parse_tasks,
    load_tasks,
    check_due_date,
    get_current_quarter,
    ARCHIVE_DIR,
    get_objective_progress,
    _atomic_write,
)

TASK_PRIMITIVES_SCHEMA_VERSION = "v1"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def list_tasks(args):
    """List tasks with optional filters."""
    _, tasks_data = load_tasks(args.personal)
    tasks = tasks_data['all']
    
    # Apply filters
    filtered = tasks

    if args.status == 'done':
        filtered = [t for t in filtered if t['done']]
    elif args.status == 'open':
        filtered = [t for t in filtered if not t['done']]
    
    if args.priority:
        priority_map = {
            'high': {'section': 'q1', 'tags': {'high', 'urgent'}},
            'medium': {'section': 'q2', 'tags': {'medium'}},
            'low': {'section': 'backlog', 'tags': {'low'}},
        }
        target = priority_map.get(args.priority.lower())
        if target:
            filtered = [
                t for t in filtered
                if t.get('section') == target['section'] or t.get('priority') in target['tags']
            ]
    
    if args.due:
        filtered = [t for t in filtered if check_due_date(t.get('due', ''), args.due)]

    if args.completed_since:
        # Note: timestamps are date-only (YYYY-MM-DD), so "24h" actually
        # means "yesterday or today" and "7d" means "last 7 calendar days".
        cutoff_days = {
            '24h': 1,
            '7d': 7,
            '30d': 30,
        }[args.completed_since]
        cutoff_date = datetime.now().date() - timedelta(days=cutoff_days)

        # Completion windows only apply to done tasks.
        filtered = [t for t in filtered if t.get('done')]

        recent_done = []
        for task in filtered:
            completed_date = task.get('completed_date')
            if not completed_date:
                continue
            try:
                parsed_date = datetime.strptime(completed_date, '%Y-%m-%d').date()
            except ValueError:
                continue
            if parsed_date >= cutoff_date:
                recent_done.append(task)

        # Augment with daily notes completions
        notes_dir_raw = os.getenv("TASK_TRACKER_DAILY_NOTES_DIR")
        if notes_dir_raw:
            notes_tasks = extract_completed_tasks(
                notes_dir=Path(notes_dir_raw),
                start_date=cutoff_date,
                end_date=datetime.now().date(),
            )
            board_titles = {t['title'].casefold() for t in recent_done}
            for nt in notes_tasks:
                if nt['title'].casefold() not in board_titles:
                    recent_done.append(nt)

        filtered = recent_done
    
    if not filtered:
        task_type = "Personal" if args.personal else "Work"
        print(f"No {task_type} tasks found matching criteria.")
        return
    
    print(f"\n📋 {('Personal' if args.personal else 'Work')} Tasks ({len(filtered)} items)\n")
    
    current_section = None
    for task in filtered:
        section = task.get('section')
        if section != current_section:
            current_section = section
            print(f"### {get_section_display_name(section, args.personal)}\n")
        
        checkbox = '✅' if task['done'] else '⬜'
        due_str = f" (🗓️{task['due']})" if task.get('due') else ''
        area_str = f" [{task.get('area')}]" if task.get('area') else ''
        
        print(f"{checkbox} **{task['title']}**{due_str}{area_str}")


def _add_destination_is_active(priority: str | None, content: str) -> bool:
    """True when an add with this priority lands in an ACTIVE board section.

    The cap must scope to where the task ACTUALLY lands. ``--priority low`` only
    routes to the inactive Backlog when a ``## ⚪`` Backlog section exists; with no
    Backlog section the add falls back to the active ``## 📋 All Tasks`` area
    (section=None, counted as active), so it MUST be gated. Deriving this from the
    real board content keeps the gate's notion of "active" from drifting from the
    writer's insertion fallback. Reuses ``PRIORITY_TO_SECTION``/``INACTIVE_SECTIONS``
    so it never drifts from ``active_records()`` either.
    """
    from task_records import INACTIVE_SECTIONS
    from utils import PRIORITY_TO_SECTION

    section = PRIORITY_TO_SECTION.get(priority or "medium", "q2")
    if section not in INACTIVE_SECTIONS:
        return True
    # The destination section is nominally inactive (Backlog) -- but only honour
    # that if the board actually has a Backlog header for the writer to target.
    has_backlog = re.search(r'^##\s+⚪(?:\s|$)', content, re.MULTILINE) is not None
    return not has_backlog


def _log_wip_cap_enforced(title: str, summary, routing: str | None) -> None:
    """Append a ``wip_cap_enforced`` ledger event (best-effort).

    H6: an over-cap add is captured to the parking lot (the cap gates promotion,
    not capture). The add is a user-initiated CLI command, so ``source="user_command"``
    -- the routing decision rides in ``proposed_routing`` metadata, not by
    mislabeling the actor source.
    """
    try:
        from task_ledger import append_event, new_event

        metadata = {"task_title": title}
        if summary is not None:
            metadata.update(
                {
                    "current_count": summary.active_count,
                    "estimated_minutes": summary.estimated_minutes,
                    "capacity_minutes": summary.capacity_minutes,
                    "hard_cap": summary.hard_cap,
                }
            )
        if routing:
            metadata["proposed_routing"] = routing
        append_event(
            new_event(
                "wip_cap_enforced",
                actor="niemand-work",
                source="user_command",
                metadata=metadata,
            )
        )
    except Exception:
        pass


def _build_active_task_line(title: str, *, due: str | None = None,
                            area: str | None = None, owner: str | None = None,
                            task_id: str | None = None) -> tuple[str, str]:
    """Build a canonical active-board task line. Returns (task_line, task_id).

    The single home for the active task-line shape so ``add_task`` (capture) and
    the H6 ``promote``/``swap`` paths emit byte-identical lines -- a promoted task
    must look exactly like a directly-added one, never a parking-lot variant.
    """
    task_id = task_id or f"tsk_{uuid.uuid4().hex[:16]}"
    task_line = f'- [ ] **{title}**'
    if due:
        task_line += f' 🗓️{due}'
    task_line += f' task_id::{task_id}'
    if area:
        task_line += f' area:: {area}'
    default_owner = os.getenv('TASK_TRACKER_DEFAULT_OWNER', 'me')
    if owner and owner not in ('me', default_owner):
        task_line += f' owner:: {owner}'
    return task_line, task_id


def _insert_active_task(content: str, priority: str | None, area: str | None,
                        task_line: str) -> str | None:
    """Splice ``task_line`` into the active section for ``priority``.

    Mirrors ``add_task``'s section-resolution fallbacks (legacy priority anchor ->
    All Tasks dept -> first dept header). Returns the new board content, or None
    when no insertion anchor exists. Shared by capture, promote and swap so all
    three insert into the same place.
    """
    priority_patterns = {
        'high': r'^##\s+🔴(?:\s|$)',
        'medium': r'^##\s+🟡(?:\s|$)',
        'low': r'^##\s+⚪(?:\s|$)',
    }
    priority_pattern = priority_patterns.get(priority, r'^##\s+🟡(?:\s|$)')

    def _next_h2_pos(text: str, start_pos: int) -> int:
        match = re.search(r'^##\s+', text[start_pos:], re.MULTILINE)
        if not match:
            return len(text)
        return start_pos + match.start()

    def _advance_after_header(text: str, start_pos: int) -> int:
        insert_at = start_pos
        remaining = text[insert_at:]
        lines = remaining.split('\n')
        skip_lines = 0
        for line in lines:
            if line.strip() == '' or line.startswith('**') or line.startswith('>'):
                skip_lines += 1
            else:
                break
        return insert_at + sum(len(lines[i]) + 1 for i in range(skip_lines))

    # 1) Legacy priority anchors (## 🔴 / ## 🟡 / ## ⚪)
    section_match = re.search(priority_pattern, content, re.MULTILINE)
    insert_pos = None
    if section_match:
        header_end = content.find('\n', section_match.start())
        if header_end == -1:
            header_end = len(content)
        else:
            header_end += 1
        insert_pos = _advance_after_header(content, header_end)
    else:
        # 2) Fallback: ## 📋 All Tasks (or plain ## All Tasks)
        all_tasks_match = re.search(r'^##\s+(?:📋\s+)?All Tasks(?:\s|$)', content, re.MULTILINE)
        if all_tasks_match:
            all_tasks_header_end = content.find('\n', all_tasks_match.start())
            if all_tasks_header_end == -1:
                all_tasks_header_end = len(content)
            else:
                all_tasks_header_end += 1

            all_tasks_section_end = _next_h2_pos(content, all_tasks_header_end)
            all_tasks_body = content[all_tasks_header_end:all_tasks_section_end]

            dept_match = None
            if area:
                area_re = re.escape(area.strip())
                dept_match = re.search(rf'^###.*\b{area_re}\b.*$', all_tasks_body, re.MULTILINE | re.IGNORECASE)
            if not dept_match:
                dept_match = re.search(r'^###\s+.+$', all_tasks_body, re.MULTILINE)

            if dept_match:
                dept_header_end = all_tasks_header_end + dept_match.end()
                if dept_header_end < len(content) and content[dept_header_end] != '\n':
                    nl_pos = content.find('\n', dept_header_end)
                    dept_header_end = len(content) if nl_pos == -1 else nl_pos + 1
                insert_pos = _advance_after_header(content, dept_header_end)
            else:
                insert_pos = _advance_after_header(content, all_tasks_header_end)
        else:
            # 3) Final fallback: first department header anywhere
            dept_match = re.search(r'^###\s+.+$', content, re.MULTILINE)
            if dept_match:
                dept_header_end = content.find('\n', dept_match.start())
                if dept_header_end == -1:
                    dept_header_end = len(content)
                else:
                    dept_header_end += 1
                insert_pos = _advance_after_header(content, dept_header_end)

    if insert_pos is None:
        return None
    return content[:insert_pos] + task_line + '\n' + content[insert_pos:]


def add_task(args):
    """Add a new task."""
    tasks_file, format = get_tasks_file(args.personal)

    if not tasks_file.exists():
        print(f"❌ Tasks file not found: {tasks_file}")
        return

    content = tasks_file.read_text()

    # Layer-2 active-inventory cap (Contract 6 / Decision #7 + H6): a WRITE-TIME
    # gate. The decision is computed once in focus_core (the canonical capacity
    # layer) AFTER reading the file and BEFORE any board write. H6: capture NEVER
    # blocks -- when the active set is full the new task is ALWAYS captured to the
    # parking lot (the inbox) and the add SUCCEEDS, so a commitment is never pushed
    # out of the system into the user's head. The cap now gates PROMOTION onto the
    # active board (see promote/swap), not capture. The cap is date-INDEPENDENT (it
    # governs total active load, not today's plan), so a skipped morning ritual
    # never silences it; it NEVER force-evicts.
    #
    # Scope: the cap governs the WORK board only (the knobs are sized for the work
    # inventory), matching standup / standup-summary -- a personal add is never
    # gated. And it only fires for adds destined for an ACTIVE section:
    # --priority low routes to the inactive Backlog (excluded from active_records),
    # so it adds zero active load and lands on the board normally.
    from focus_core import evaluate_add

    destination_active = (not args.personal) and _add_destination_is_active(args.priority, content)
    gate = evaluate_add(
        content,
        format,
        args.title,
        destination_active=destination_active,
        personal=args.personal,
    )
    if not gate.allowed:
        # H6 Fix 1: over-cap => capture to the parking lot and SUCCEED. This is now
        # the DEFAULT (no flag required); --force-parking is kept as a harmless
        # alias since the over-cap path already routes to parking. Only a real
        # capture FAILURE (add_item returns an "❌" string -- lot full / no
        # section) is surfaced as a non-zero exit, so a task that truly cannot be
        # saved is never reported as captured.
        from parking_lot import add_item

        # Thread --due / --owner through so a captured task does not silently lose
        # its due date or owner ("saved, not lost"). They ride on the parked line
        # and /promote re-derives them onto the restored active line (H6 Fix 2).
        # The default owner ("me") is suppressed -- the same round-trip rule as
        # _build_active_task_line -- so a normal capture carries no stray owner.
        default_owner = os.getenv('TASK_TRACKER_DEFAULT_OWNER', 'me')
        capture_owner = args.owner if args.owner and args.owner not in ('me', default_owner) else None
        result = add_item(
            tasks_file, args.title, dept=args.area, priority="low",
            due=args.due, owner=capture_owner,
        )
        if result.startswith("❌"):
            print(result)
            sys.exit(2)
        summary = gate.summary
        cap_h = (summary.capacity_minutes // 60) if summary else 0
        active_n = summary.active_count if summary else 0
        print(
            f"📥 Captured to the parking lot — you're at capacity "
            f"({active_n} committed / ~{cap_h}h). It's saved, not lost.\n"
            f"Promote it with /promote <id> when there's room, or "
            f"/swap <out_id> <id> to make room now."
        )
        _log_wip_cap_enforced(args.title, gate.summary, routing="parking_lot")
        return

    task_line, task_id = _build_active_task_line(
        args.title, due=args.due, area=args.area, owner=args.owner
    )
    new_content = _insert_active_task(content, args.priority, args.area, task_line)
    if new_content is None:
        priority_patterns = {
            'high': r'^##\s+🔴(?:\s|$)',
            'medium': r'^##\s+🟡(?:\s|$)',
            'low': r'^##\s+⚪(?:\s|$)',
        }
        priority_pattern = priority_patterns.get(args.priority, r'^##\s+🟡(?:\s|$)')
        print(f"⚠️ Could not find section matching '{priority_pattern}'. Add manually.")
        return

    _atomic_write(tasks_file, new_content)
    task_type = "Personal" if args.personal else "Work"
    print(f"✅ Added {task_type} task: {args.title} ({task_id})")


def _log_promotion_event(event_type: str, *, title: str, summary, extra: dict | None = None) -> None:
    """Append a ``task_promoted`` / ``task_swapped`` ledger event (best-effort).

    A promote/swap is a user CLI command, so ``source="user_command"``. The
    capacity snapshot at the time of the move rides in metadata so an auditor can
    see the committed load the promotion landed against.
    """
    try:
        from task_ledger import append_event, new_event

        metadata = {"task_title": title}
        if summary is not None:
            metadata.update(
                {
                    "current_count": summary.active_count,
                    "estimated_minutes": summary.estimated_minutes,
                    "capacity_minutes": summary.capacity_minutes,
                    "hard_cap": summary.hard_cap,
                }
            )
        if extra:
            metadata.update(extra)
        append_event(
            new_event(
                event_type,
                actor="niemand-work",
                source="user_command",
                metadata=metadata,
            )
        )
    except Exception:
        pass


def _find_parked_item(content: str, item_id: int):
    """Return the parked item dict for ``item_id``, or None.

    Parses the Parking Lot via the same helpers the parking-lot CLI uses, so the
    promote/swap notion of "parked item N" matches ``parking-lot list`` exactly.
    """
    from parking_lot import _find_parking_lot_bounds, _parse_items

    lines = content.split('\n')
    start, end = _find_parking_lot_bounds(lines)
    if start == -1:
        return None
    items = _parse_items(lines, start, end)
    return next((it for it in items if it['id'] == item_id), None)


def _promote_parked_onto_board(tasks_file, fmt: str, item_id: int, *, personal: bool):
    """Capacity-gated move of a parked item onto the active board.

    Returns a result dict ``{ok, message, [exit_code], [title], [summary]}``. The
    move adds the active line FIRST, then removes the parked line, so a crash
    between the two writes leaves the task DOUBLE-placed (recoverable) rather than
    LOST. The capacity gate re-runs ``evaluate_add`` for the to-be-promoted task;
    a full committed set refuses without moving anything.
    """
    from focus_core import evaluate_add
    from parking_lot import drop_item

    content = tasks_file.read_text()
    parked = _find_parked_item(content, item_id)
    if parked is None:
        return {"ok": False, "message": f"❌ Item #{item_id} not found in Parking Lot.", "exit_code": 2}

    title = parked['title']
    area = parked.get('department')
    # Re-derive due/owner from the self-describing parked line so the restored
    # active line carries them (H6 Fix 2: a captured task's due date / owner are
    # "saved, not lost", not silently dropped on promote).
    due = parked.get('due')
    owner = parked.get('owner')
    # Preserve the parked task's canonical id through the promote so it round-trips
    # (capture -> promote, swap-out -> promote-in keep one stable identity).
    id_match = re.search(
        r'(?:task_id|id)::\s*([A-Za-z0-9._:-]*[A-Za-z0-9._-])', parked.get('raw_line') or ''
    )
    carried_id = id_match.group(1) if id_match else None

    # Promotion gate: re-run the canonical capacity check for THIS task. A personal
    # board is never work-cap gated (matching add_task's scope decision).
    destination_active = not personal
    gate = evaluate_add(content, fmt, title, destination_active=destination_active, personal=personal)
    if not gate.allowed:
        summary = gate.summary
        cap_h = (summary.capacity_minutes // 60) if summary else 0
        active_n = summary.active_count if summary else 0
        return {
            "ok": False,
            "message": (
                f"Committed set is full ({active_n} / ~{cap_h}h). "
                f"/swap <out_id> {item_id} to make room, or /done something first."
            ),
            "exit_code": 2,
        }

    # Add to the active board FIRST (so a crash double-places, never loses).
    task_line, _ = _build_active_task_line(
        title, due=due, area=area, owner=owner, task_id=carried_id
    )
    new_content = _insert_active_task(content, "medium", area, task_line)
    if new_content is None:
        return {"ok": False, "message": "⚠️ Could not find a section to promote into.", "exit_code": 2}
    _atomic_write(tasks_file, new_content)

    # Then remove from the parking lot. drop_item re-reads the file, so it sees the
    # active task we just added but removes by the parking-lot item id.
    drop_result = drop_item(tasks_file, item_id)
    if drop_result.startswith("❌"):
        # Active add succeeded but the parking removal failed: the task is on the
        # board (not lost). Surface the partial state honestly.
        return {
            "ok": False,
            "message": (
                f"⚠️ Promoted '{title}' to the active board, but could not remove the "
                f"parked copy ({drop_result}). Drop parking item #{item_id} manually."
            ),
            "exit_code": 2,
        }
    return {"ok": True, "title": title, "summary": gate.summary, "message": f"✅ Promoted '{title}' to the active board."}


def promote_task(args):
    """H6 Fix 2: promote a parked task onto the active board, capacity-gated."""
    tasks_file, fmt = get_tasks_file(args.personal)
    if not tasks_file.exists():
        print(f"❌ Tasks file not found: {tasks_file}")
        sys.exit(2)

    result = _promote_parked_onto_board(tasks_file, fmt, args.id, personal=args.personal)
    print(result["message"])
    if not result["ok"]:
        sys.exit(result.get("exit_code", 2))
    _log_promotion_event("task_promoted", title=result["title"], summary=result.get("summary"))


def _park_active_task(tasks_file, fmt: str, out_id: str, *, personal: bool):
    """Move an active board task (by canonical id) INTO the parking lot.

    Returns ``{ok, message, [exit_code], [title]}``. Parks-out by adding the task
    to the parking lot FIRST, then removing the active line, so a crash between
    writes double-places (recoverable) rather than loses. Refuses cleanly if
    ``out_id`` is not an active task on the board.
    """
    from parking_lot import add_item
    from task_lines import remove_task_line

    content = tasks_file.read_text()
    records = task_records(content, personal=personal, fmt=fmt)
    target = next(
        (r for r in active_records(records) if r.canonical_id == out_id and not r.is_objective),
        None,
    )
    if target is None:
        return {
            "ok": False,
            "message": f"❌ '{out_id}' is not an active task on the board.",
            "exit_code": 2,
        }

    # Add to the parking lot FIRST (so a crash double-places, never loses). Preserve
    # the task's canonical id so the parked copy keeps its identity, and carry the
    # due date / owner onto the parked line so a swap-out is also "saved, not lost"
    # and a later promote-in restores them (H6 Fix 2).
    add_result = add_item(
        tasks_file, target.title, dept=target.department or target.area,
        priority="low", task_id=target.canonical_id,
        due=target.due, owner=target.owner,
    )
    if add_result.startswith("❌"):
        return {"ok": False, "message": add_result, "exit_code": 2}

    # Then remove the active line. The parking add may have shifted line numbers
    # (e.g. a parking lot above the active line), so re-derive the active record
    # from the POST-ADD content by canonical id rather than trusting the stale
    # line number. The parked copy carries the same id, so exclude it by section.
    content_after_add = tasks_file.read_text()
    records_after = task_records(content_after_add, personal=personal, fmt=fmt)
    active_after = next(
        (r for r in active_records(records_after) if r.canonical_id == out_id and not r.is_objective),
        None,
    )
    if active_after is None:
        return {
            "ok": False,
            "message": (
                f"⚠️ Parked '{target.title}' but could not relocate the active copy "
                f"to remove it. Remove it manually."
            ),
            "exit_code": 2,
        }
    updated = remove_task_line(content_after_add, active_after.raw_line, active_after.line_number)
    if updated is None:
        return {
            "ok": False,
            "message": (
                f"⚠️ Parked '{target.title}' but could not remove the active copy. "
                f"Remove it manually."
            ),
            "exit_code": 2,
        }
    _atomic_write(tasks_file, updated)
    # Resolve the parking-lot item id of the just-parked copy (matched by its
    # preserved canonical id) so a swap can roll the park-out BACK if needed.
    parked_id = _parked_item_id_for(updated, out_id)
    return {
        "ok": True,
        "title": target.title,
        "parked_id": parked_id,
        "message": f"✅ Parked '{target.title}'.",
    }


def _parked_item_id_for(content: str, canonical_id: str) -> int | None:
    """Return the parking-lot item id whose line carries ``canonical_id``, or None."""
    from parking_lot import _find_parking_lot_bounds, _parse_items

    lines = content.split('\n')
    start, end = _find_parking_lot_bounds(lines)
    if start == -1:
        return None
    for item in _parse_items(lines, start, end):
        raw = item.get('raw_line') or ''
        if re.search(rf'(?:task_id|id)::\s*{re.escape(canonical_id)}\b', raw):
            return item['id']
    return None


def swap_tasks(args):
    """H6 Fix 3: park out_id (active->parking) AND promote in_id (parking->active).

    All-or-nothing: the FULL post-swap capacity is pre-flighted in memory BEFORE
    any write, so an unequal swap (where the parked-out task frees less than the
    promoted-in task needs) is REFUSED cleanly with the board left byte-identical
    -- never a partial move (out parked, in NOT promoted). Only once the projected
    state is proven to fit does the park-out (which frees a slot so the
    promote-in's gate passes) run, followed by the promote-in. A bad out/in id
    also refuses before any write.
    """
    from focus_core import evaluate_add

    tasks_file, fmt = get_tasks_file(args.personal)
    if not tasks_file.exists():
        print(f"❌ Tasks file not found: {tasks_file}")
        sys.exit(2)

    # Validate BOTH ends before mutating anything: a bad out/in id must not leave a
    # partial move. out_id must be an active task; in_id must be a parked item.
    content = tasks_file.read_text()
    records = task_records(content, personal=args.personal, fmt=fmt)
    out_target = next(
        (r for r in active_records(records) if r.canonical_id == args.out_id and not r.is_objective),
        None,
    )
    if out_target is None:
        print(f"❌ '{args.out_id}' is not an active task on the board. Nothing moved.")
        sys.exit(2)
    in_target = _find_parked_item(content, args.in_id)
    if in_target is None:
        print(f"❌ Parking item #{args.in_id} not found. Nothing moved.")
        sys.exit(2)

    # Pre-flight the promote-in insertion against the CURRENT board (a dry-run, no
    # write). If the parked task has nowhere to land, refuse BEFORE the park-out
    # write so the swap never leaves a partial board (out parked, nothing in).
    if _insert_active_task(content, "medium", in_target.get('department'), "- [ ] probe") is None:
        print("❌ No active section to promote into. Nothing moved.")
        sys.exit(2)

    # Pre-flight the FULL post-swap CAPACITY in memory (no write): would promoting
    # in_id fit AFTER out_id is removed from the committed-active set? Project the
    # state by removing out_id's active line from the board text, then re-use the
    # canonical evaluate_add gate against that projected board. The cap's COUNTING
    # is unchanged -- this only asks the gate about a projected state. If it would
    # NOT fit (the out task frees less room than the in task needs), refuse cleanly
    # and leave the board BYTE-IDENTICAL. The promoted-in task is unestimated (the
    # promote path supplies no estimate), exactly what evaluate_add projects.
    if not args.personal:
        projected = remove_task_line(content, out_target.raw_line, out_target.line_number)
        if projected is not None:
            projected_gate = evaluate_add(
                projected, fmt, in_target["title"],
                destination_active=True, personal=args.personal,
            )
            if not projected_gate.allowed:
                print(
                    f"❌ Swap won't fit: '{in_target['title']}' needs more room than "
                    f"'{out_target.title}' frees. /done another task first, or pick a "
                    f"smaller task. Nothing moved."
                )
                sys.exit(2)

    # Park out FIRST -- this frees a committed slot so the promotion gate passes.
    park_result = _park_active_task(tasks_file, fmt, args.out_id, personal=args.personal)
    if not park_result["ok"]:
        print(park_result["message"])
        sys.exit(park_result.get("exit_code", 2))

    promote_result = _promote_parked_onto_board(tasks_file, fmt, args.in_id, personal=args.personal)
    if not promote_result["ok"]:
        # Belt-and-suspenders: the capacity pre-flight already proved the post-swap
        # state fits, so this should not fire. If it somehow does, roll the park-out
        # BACK by promoting the parked-out task onto the active board so the swap
        # never leaves a partial board (out parked, nothing in).
        parked_id = park_result.get("parked_id")
        rollback = (
            _promote_parked_onto_board(tasks_file, fmt, parked_id, personal=args.personal)
            if parked_id is not None
            else None
        )
        if rollback is None or not rollback.get("ok"):
            print(park_result["message"])
        print(promote_result["message"])
        sys.exit(promote_result.get("exit_code", 2))

    print(f"✅ Swapped: parked '{park_result['title']}', promoted '{promote_result['title']}'.")
    _log_promotion_event(
        "task_swapped",
        title=promote_result["title"],
        summary=promote_result.get("summary"),
        extra={"parked_out": park_result["title"], "out_id": args.out_id, "in_id": args.in_id},
    )


def done_task(args):
    """Complete a task by canonical ID only."""
    query = args.query.strip()
    if not re.fullmatch(r"[A-Za-z0-9._:-]+", query):
        print_result(block_unsafe_query(args.query))
        sys.exit(2)

    result = complete_by_id(query, personal=args.personal, source="user_command")
    print_result(result)
    if not result.get("ok"):
        sys.exit(2)


def show_blockers(args):
    """Show tasks that are blocking others."""
    _, tasks_data = load_tasks(args.personal)
    blockers = [t for t in tasks_data['all'] if t.get('blocks') and not t['done']]
    
    if args.person:
        blockers = [t for t in blockers if args.person.lower() in t['blocks'].lower()]
    
    if not blockers:
        print("No blocking tasks found.")
        return
    
    print(f"\n🚧 Blocking Tasks ({len(blockers)} items)\n")
    
    for task in blockers:
        print(f"⬜ **{task['title']}**")
        print(f"   Blocks: {task['blocks']}")
        if task.get('due'):
            print(f"   Due: {task['due']}")
        print()


def archive_done(args):
    """Archive completed tasks from daily notes into quarterly file.

    Also cleans any stale [x] lines still on the board (backward compat).
    """
    notes_dir_raw = os.getenv("TASK_TRACKER_DAILY_NOTES_DIR")
    if not notes_dir_raw:
        print(
            "❌ TASK_TRACKER_DAILY_NOTES_DIR is not set. "
            "Set it to the directory containing your daily notes (YYYY-MM-DD.md).",
            file=sys.stderr,
        )
        return

    # Collect completions from daily notes (last 30 days by default)
    today = datetime.now().date()
    start = today - timedelta(days=30)
    notes_tasks = extract_completed_tasks(
        notes_dir=Path(notes_dir_raw),
        start_date=start,
        end_date=today,
    )

    # Also collect any stale [x] items still on the board
    tasks_file, format = get_tasks_file(args.personal)
    stale_board: list[dict] = []
    if tasks_file.exists():
        content = tasks_file.read_text()
        tasks_data = parse_tasks(content, args.personal, format)
        stale_board = tasks_data.get('done', [])

    # Merge (deduplicate by title + date)
    all_done: list[dict] = list(notes_tasks)
    seen = {(t['title'].casefold(), t.get('completed_date', '')) for t in all_done}
    for bt in stale_board:
        key = (bt['title'].casefold(), bt.get('completed_date', ''))
        if key not in seen:
            seen.add(key)
            all_done.append(bt)

    if not all_done:
        print("No completed tasks to archive.")
        return

    # Write to quarterly archive, skipping entries already present
    quarter = get_current_quarter()
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archive_file = ARCHIVE_DIR / f"ARCHIVE-{quarter}.md"

    if archive_file.exists():
        archive_content = archive_file.read_text()
    else:
        archive_content = f"# Task Archive - {quarter}\n"

    # Build set of (title, completed_date) already archived to prevent
    # duplicate entries across repeated runs while preserving recurring
    # tasks completed on different dates.
    already_archived: set[tuple[str, str]] = set()
    for line in archive_content.splitlines():
        m = re.match(r'^- ✅ \*\*(.+?)\*\*', line)
        if m:
            title_key = m.group(1).strip().casefold()
            date_m = re.search(r'✅\s*(\d{4}-\d{2}-\d{2})\s*$', line)
            date_key = date_m.group(1) if date_m else ''
            already_archived.add((title_key, date_key))

    new_tasks = [
        t for t in all_done
        if (t['title'].casefold(), t.get('completed_date') or '') not in already_archived
    ]
    if not new_tasks:
        print("All completed tasks are already archived.")
        return

    task_type = "Personal" if args.personal else "Work"
    archive_entry = f"\n## Archived {today.strftime('%Y-%m-%d')} ({task_type})\n\n"
    for task in new_tasks:
        date_suffix = f" ✅ {task['completed_date']}" if task.get('completed_date') else ""
        area_suffix = f" [{task.get('area')}]" if task.get('area') else ""
        archive_entry += f"- ✅ **{task['title']}**{area_suffix}{date_suffix}\n"

    archive_content += archive_entry
    archive_file.write_text(archive_content)

    # Clean stale [x] lines from the board
    removed = 0
    if stale_board and tasks_file.exists():
        board_content = tasks_file.read_text()
        for task in sorted(stale_board, key=lambda t: t.get('line_number') or 0, reverse=True):
            raw_line = task.get('raw_line', '')
            line_number = task.get('line_number')
            updated = remove_task_line(board_content, raw_line, line_number)
            if updated is not None:
                board_content = updated
                removed += 1
        tasks_file.write_text(board_content)

    total = len(new_tasks)
    extra = f" (cleaned {removed} stale lines from board)" if removed else ""
    print(f"✅ Archived {total} {task_type} tasks to {archive_file.name}{extra}")


def cmd_delegated(args):
    """Dispatch delegated subcommands."""
    import delegation

    sub = args.del_command
    path = delegation.resolve_delegation_file()
    delegation.ensure_file(path)

    if sub == 'list':
        if getattr(args, 'json', False):
            print(delegation.list_items_json(path, overdue_only=getattr(args, 'overdue', False)))
        else:
            items = delegation.list_items(path, overdue_only=getattr(args, 'overdue', False))
            if not items:
                print("No delegated tasks.")
                return
            for it in items:
                icon = '⏰' if it.get('status') == 'overdue' else '📋'
                dept = f" #{it['department']}" if it.get('department') else ''
                fu = f" [followup::{it['followup']}]" if it.get('followup') else ''
                print(f"{it['id']:2d}. {icon} {it['title']} → {it.get('assignee', '?')}{dept}{fu}")
    elif sub == 'add':
        item = delegation.add_item(path, args.task, args.to, args.followup, args.dept)
        print(f"✅ Delegated: {item['title']} → {item['assignee']} [followup::{item['followup']}]")
    elif sub == 'complete':
        try:
            item = delegation.complete_item(path, args.id)
            print(f"✅ Completed: {item['title']} → {item.get('assignee', '?')}")
        except ValueError as e:
            print(f"❌ {e}")
            sys.exit(1)
    elif sub == 'extend':
        try:
            item = delegation.extend_item(path, args.id, args.followup)
            print(f"✅ Extended: {item['title']} [new followup::{item['followup']}]")
        except ValueError as e:
            print(f"❌ {e}")
            sys.exit(1)
    elif sub == 'take-back':
        try:
            item = delegation.get_active_item(path, args.id)
            # Re-insert into work tasks first; only delete delegated entry after write succeeds.
            tasks_file, _ = get_tasks_file(personal=False)
            content = tasks_file.read_text()
            dept_tag = f" #{item.get('department')}" if item.get('department') else ''
            task_id = f"tsk_{uuid.uuid4().hex[:16]}"
            task_line = f"- [ ] **{item['title']}** task_id::{task_id}{dept_tag}"
            # Insert at beginning of first section
            lines = content.split('\n')
            insert_at = 0
            for i, line in enumerate(lines):
                if re.match(r'^- \[', line):
                    insert_at = i
                    break
                if line.startswith('## '):
                    insert_at = i + 1
            lines.insert(insert_at, task_line)
            tasks_file.write_text('\n'.join(lines))
            delegation.take_back_item(path, args.id)
            print(f"✅ Took back: {item['title']} (added to {tasks_file.name})")
        except ValueError as e:
            print(f"❌ {e}")
            sys.exit(1)


def cmd_parking_lot(args):
    """Dispatch parking-lot subcommands."""
    from parking_lot import list_items, list_stale, add_item, promote_item, drop_item

    tasks_file, _ = get_tasks_file(args.personal)
    sub = args.pl_command

    if sub == 'list':
        print(list_items(tasks_file))
    elif sub == 'add':
        print(add_item(tasks_file, args.title, dept=args.dept, priority=args.priority))
    elif sub == 'stale':
        print(list_stale(tasks_file))
    elif sub == 'promote':
        print(promote_item(tasks_file, args.id))
    elif sub == 'drop':
        archive_dir = Path(os.getenv(
            'TASK_TRACKER_ARCHIVE_DIR',
            str(tasks_file.parent / 'Done Archive')
        ))
        print(drop_item(tasks_file, args.id, archive_dir=archive_dir))


def cmd_identity_audit(args):
    print_identity_json(audit_payload(personal=args.personal))


def cmd_task_audit(args):
    payload = _new_schema("task-audit")
    payload.update(
        collect_task_audit(
            personal=args.personal,
            stale_days=args.stale_days if args.stale_days is not None else _env_int("TASK_AUDIT_STALE_DAYS", 14),
            candidate_days=args.candidate_days if args.candidate_days is not None else _env_int("TASK_AUDIT_CANDIDATE_DAYS", 7),
            backlog_cap=args.backlog_cap,
            limit=args.limit,
        )
    )
    print(json.dumps(payload, indent=2))


def cmd_identity_repair(args):
    payload = repair_missing_ids(personal=args.personal, apply=args.apply)
    print(json.dumps(payload, indent=2, sort_keys=True))
    if payload.get("blocked"):
        sys.exit(2)


def cmd_promote_from_backlog(args):
    from parking_lot import promote_item
    tasks_file, _ = get_tasks_file(args.personal)
    cap = max(int(args.cap or 1), 1)
    promoted = []
    for _ in range(cap):
        out = promote_item(tasks_file, 1)
        if out.startswith('✅'):
            promoted.append(out)
        else:
            break
    if not promoted:
        print("No backlog items promoted.")
    else:
        for row in promoted:
            print(row)


def cmd_review_backlog(args):
    from parking_lot import list_stale
    old = os.getenv('PARKING_LOT_STALE_DAYS')
    os.environ['PARKING_LOT_STALE_DAYS'] = str(args.stale_days)
    try:
        raw = list_stale(get_tasks_file(args.personal)[0])
    finally:
        if old is None:
            del os.environ['PARKING_LOT_STALE_DAYS']
        else:
            os.environ['PARKING_LOT_STALE_DAYS'] = old
    if args.json:
        print(raw)
        return
    items = json.loads(raw)
    if not items:
        print(f"No stale backlog items (threshold: {args.stale_days}d).")
        return
    print(f"Stale backlog items ({len(items)}):")
    for it in items:
        print(f"- #{it['id']} {it['title']} ({it['age_days']}d)")


def _calendar_classification(task: dict) -> str:
    raw = str(task.get('raw_line') or '').lower()
    title = str(task.get('title') or '').lower()
    if 'status::blocked' in raw or 'depends::' in raw:
        return 'blocked'
    if '#private' in raw or 'private::true' in raw:
        return 'private'
    if 'buffer' in title or 'buffer::true' in raw:
        return 'buffer'
    return 'normal'


def cmd_calendar_sync(args):
    """Calendar sync payload for orchestration consumers."""
    _, tasks_data = load_tasks(args.personal)
    events = flatten_calendar_events(get_calendar_events())
    meetings = []
    for task in tasks_data.get('all', []):
        raw = str(task.get('raw_line') or '')
        if 'meeting::' not in raw:
            continue
        status_match = re.search(r'status::(scheduled|done|canceled|blocked)', raw, flags=re.IGNORECASE)
        status = status_match.group(1).lower() if status_match else ('done' if task.get('done') else 'scheduled')
        meetings.append({
            'title': task.get('title', ''),
            'status': status,
            'classification': _calendar_classification(task),
            'done': bool(task.get('done')),
        })

    payload = {
        'command': 'calendar sync',
        'idempotent': True,
        'events_seen': len(events),
        'meetings': meetings,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Synced {len(meetings)} meeting task(s); events seen: {len(events)}")


def cmd_calendar_resolve(args):
    """Resolve calendar lifecycle from note completions in a date window."""
    _, tasks_data = load_tasks(args.personal)
    today = datetime.now().date()
    if args.window == 'today':
        start = end = today
    else:
        start = end = (today - timedelta(days=1))

    notes_dir_raw = os.getenv("TASK_TRACKER_DAILY_NOTES_DIR")
    completed = extract_completed_tasks(Path(notes_dir_raw), start, end) if notes_dir_raw else []
    done_titles = {t.get('title', '').casefold() for t in completed}

    resolved = []
    for task in tasks_data.get('all', []):
        raw = str(task.get('raw_line') or '')
        if 'meeting::' not in raw:
            continue
        title = task.get('title', '')
        raw_l = raw.lower()
        status = 'done' if title.casefold() in done_titles else 'scheduled'
        if 'status::blocked' in raw_l:
            status = 'blocked'
        if 'status::done' in raw_l or task.get('done'):
            status = 'done'
        if 'status::canceled' in raw_l:
            status = 'canceled'
        resolved.append({'title': title, 'status': status, 'window': args.window})

    payload = {
        'command': 'calendar resolve',
        'window': args.window,
        'resolved': resolved,
        'idempotent': True,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Resolved {len(resolved)} meeting lifecycle item(s) for {args.window}")


def cmd_done_scan(args):
    """Scan completed items in a true rolling time window for standup consumers."""
    window_map = {'24h': timedelta(hours=24), '7d': timedelta(days=7), '30d': timedelta(days=30)}
    cutoff = datetime.now() - window_map[args.window]
    end = datetime.now().date()
    start = (cutoff.date() - timedelta(days=1))

    notes_dir_raw = os.getenv("TASK_TRACKER_DAILY_NOTES_DIR")
    items = []
    if notes_dir_raw:
        raw_items = extract_completed_tasks(Path(notes_dir_raw), start, end)
        for item in raw_items:
            try:
                item_date = datetime.strptime(item.get('completed_date', ''), '%Y-%m-%d').date()
            except ValueError:
                continue
            ts = item.get('timestamp')
            if ts:
                try:
                    item_dt = datetime.strptime(f"{item_date.isoformat()} {ts}", '%Y-%m-%d %H:%M')
                except ValueError:
                    item_dt = datetime.combine(item_date, datetime.max.time())
            else:
                item_dt = datetime.combine(item_date, datetime.min.time())
            if item_dt >= cutoff:
                items.append(item)

    payload = {
        'command': 'done scan',
        'window': args.window,
        'count': len(items),
        'items': items,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Done items ({args.window}): {len(items)}")


def _new_schema(command: str) -> dict:
    return {
        "schema_version": TASK_PRIMITIVES_SCHEMA_VERSION,
        "command": command,
    }


def _safe_load_tasks(personal: bool = False) -> dict:
    """Load tasks, returning an empty skeleton on failures."""
    empty = {
        "all": [],
        "done": [],
        "q1": [],
        "q2": [],
        "q3": [],
        "backlog": [],
        "today": [],
        "objectives": [],
        "team": [],
        "parking_lot": [],
        "due_today": [],
    }
    tasks_file, fmt = get_tasks_file(personal)
    if not tasks_file.exists():
        return empty
    try:
        content = tasks_file.read_text()
    except OSError:
        return empty
    try:
        return parse_tasks(content, personal, fmt)
    except Exception:
        return empty


def _group_tasks_by_area(tasks: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for task in tasks:
        key = task.get("area") or "Uncategorized"
        grouped.setdefault(key, []).append(task)
    return dict(sorted(grouped.items(), key=lambda item: item[0].casefold()))


def _group_tasks_by_category(tasks: list[dict]) -> dict[str, list[dict]]:
    labels = {
        "q1": "Q1",
        "q2": "Q2",
        "q3": "Q3",
        "team": "Team",
        "backlog": "Backlog",
        "today": "Today",
        "objectives": "Objectives",
        "parking_lot": "Parking Lot",
    }
    grouped: dict[str, list[dict]] = {}
    for task in tasks:
        section = task.get("section")
        key = labels.get(section, section or "Uncategorized")
        grouped.setdefault(key, []).append(task)
    return dict(sorted(grouped.items(), key=lambda item: item[0].casefold()))


def _parse_range_inputs(week: str | None, start_raw: str | None, end_raw: str | None) -> tuple[date, date, str]:
    if start_raw or end_raw:
        if not start_raw or not end_raw:
            raise ValueError("Both --start and --end are required together.")
        try:
            start_date = datetime.strptime(start_raw, "%Y-%m-%d").date()
            end_date = datetime.strptime(end_raw, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("Invalid date. Use YYYY-MM-DD.") from exc
        if start_date > end_date:
            start_date, end_date = end_date, start_date
        return start_date, end_date, "custom-range"

    today = datetime.now().date()
    if not week:
        start_date = today - timedelta(days=today.weekday())
        return start_date, start_date + timedelta(days=6), "current-week"

    match = re.fullmatch(r"(\d{4})-W(\d{2})", week)
    if not match:
        raise ValueError("Invalid --week format. Use YYYY-WNN (example: 2026-W07).")
    start_date = date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
    return start_date, start_date + timedelta(days=6), "iso-week"


def cmd_standup_summary(args):
    tasks_data = _safe_load_tasks(args.personal)
    records = _safe_load_task_records(args.personal)
    today = datetime.now().date()

    notes_dir_raw = os.getenv("TASK_TRACKER_DAILY_NOTES_DIR")
    dones: list[dict] = []
    if notes_dir_raw:
        notes_items = extract_completed_tasks(Path(notes_dir_raw), today - timedelta(days=1), today)
        dones = [
            {
                "title": item.get("title", ""),
                "completed_date": item.get("completed_date"),
                "timestamp": item.get("timestamp"),
                "area": item.get("area") or "Uncategorized",
            }
            for item in notes_items
        ]
    else:
        dones = [
            {
                "title": task.get("title", ""),
                "completed_date": task.get("completed_date"),
                "timestamp": None,
                "area": task.get("area") or task.get("department") or "Uncategorized",
            }
            for task in tasks_data.get("done", [])
        ]

    active = active_records(records)
    dos_records = [record for record in active if record.section in {"q1", "q2", "today"}]
    dos = [_canonical_record(record) for record in dos_records]

    # Layer-2 capacity ceiling (U3): surface the active-inventory load against
    # ~1 week of capacity so the /standup consumer can show the cap state. The
    # cap governs the WORK board only (the knobs are sized for the work
    # inventory), so it is omitted for personal standups -- matching the
    # work-only standup.py entrypoint.
    capacity = None
    if not args.personal:
        try:
            from focus_core import capacity_display, summarize_capacity
            capacity_summary = summarize_capacity(records)
            capacity = {
                "active_count": capacity_summary.active_count,
                "estimated_minutes": capacity_summary.estimated_minutes,
                "capacity_minutes": capacity_summary.capacity_minutes,
                "hard_cap": capacity_summary.hard_cap,
                "over_cap": capacity_summary.over_cap,
                "display": capacity_display(capacity_summary),
            }
        except Exception:
            capacity = None

    overdue_records = []
    for record in active:
        if not record.due:
            continue
        try:
            due_date = datetime.strptime(record.due, "%Y-%m-%d").date()
        except ValueError:
            continue
        if due_date < today:
            overdue_records.append(record)
    overdue = [_canonical_record(record) for record in overdue_records]

    carryover_suggestions = []
    for record in overdue_records:
        carryover_suggestions.append(
            {
                "task_id": record.canonical_id,
                "fallback_id": record.fallback_id,
                "missing_task_id": record.missing_task_id,
                "fallback_only": record.fallback_only,
                "title": record.title,
                "reason": "overdue",
                "suggestion": "carry-to-today",
                "due": record.due,
                "area": record.area or record.department or "Uncategorized",
            }
        )

    payload = _new_schema("standup-summary")
    payload.update(
        {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "date": today.isoformat(),
            "dones": dones,
            "dos": dos,
            "overdue": overdue,
            "capacity": capacity,
            "carryover_suggestions": carryover_suggestions,
            "completion_candidates": candidate_review_summary(personal=args.personal),
            "task_audit": task_audit_summary(personal=args.personal),
            "groups": {
                "dones_by_area": _group_tasks_by_area(dones),
                "dos_by_area": _group_tasks_by_area(dos),
                "overdue_by_area": _group_tasks_by_area(overdue),
                "dos_by_category": _group_tasks_by_category(dos),
            },
        }
    )
    print(json.dumps(payload, indent=2))


def cmd_weekly_review_summary(args):
    try:
        start_date, end_date, selection_mode = _parse_range_inputs(args.week, args.start, args.end)
    except ValueError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(2)

    tasks_data = _safe_load_tasks(args.personal)
    records = _safe_load_task_records(args.personal)
    notes_dir_raw = os.getenv("TASK_TRACKER_DAILY_NOTES_DIR")

    done_items: list[dict] = []
    if notes_dir_raw:
        note_tasks = extract_completed_tasks(Path(notes_dir_raw), start_date, end_date)
        done_items = [
            {
                "task_id": None,
                "title": item.get("title", ""),
                "done": True,
                "section": "done",
                "area": item.get("area") or "Uncategorized",
                "priority": item.get("priority"),
                "due": item.get("due"),
                "owner": None,
                "goal": None,
                "completed_date": item.get("completed_date"),
            }
            for item in note_tasks
        ]
    else:
        for task in tasks_data.get("done", []):
            completed = task.get("completed_date")
            if not completed:
                continue
            try:
                completed_date = datetime.strptime(completed, "%Y-%m-%d").date()
            except ValueError:
                continue
            if start_date <= completed_date <= end_date:
                matching = [
                    record for record in records
                    if record.line_number == task.get("line_number")
                    and record.raw_line == task.get("raw_line")
                ]
                row = _canonical_record(matching[0]) if matching else {
                    "task_id": task.get("task_id") or task.get("legacy_id"),
                    "fallback_id": None,
                    "missing_task_id": task.get("task_id") is None,
                    "fallback_only": not (task.get("task_id") or task.get("legacy_id")),
                    "title": task.get("title", ""),
                    "done": bool(task.get("done")),
                    "section": task.get("section"),
                    "area": task.get("area") or task.get("department") or "Uncategorized",
                    "priority": task.get("priority"),
                    "due": task.get("due"),
                    "owner": task.get("owner"),
                    "goal": task.get("goal"),
                }
                row["completed_date"] = completed
                done_items.append(row)

    do_items = []
    for record in active_records(records):
        due_raw = record.due
        if due_raw:
            try:
                due_date = datetime.strptime(due_raw, "%Y-%m-%d").date()
            except ValueError:
                continue
            if due_date < start_date or due_date > end_date:
                continue
        do_items.append(_canonical_record(record))

    if not records:
        for task in tasks_data.get("all", []):
            if task.get("done"):
                continue
            due_raw = task.get("due")
            if due_raw:
                try:
                    due_date = datetime.strptime(due_raw, "%Y-%m-%d").date()
                except ValueError:
                    continue
                if due_date < start_date or due_date > end_date:
                    continue
            do_items.append(
                {
                    "task_id": task.get("task_id") or task.get("legacy_id"),
                    "fallback_id": None,
                    "missing_task_id": task.get("task_id") is None,
                    "fallback_only": not (task.get("task_id") or task.get("legacy_id")),
                    "title": task.get("title", ""),
                    "done": False,
                    "section": task.get("section"),
                    "area": task.get("area") or task.get("department") or "Uncategorized",
                    "priority": task.get("priority"),
                    "due": task.get("due"),
                    "owner": task.get("owner"),
                    "goal": task.get("goal"),
                }
            )

    payload = _new_schema("weekly-review-summary")
    payload.update(
        {
            "range": {
                "mode": selection_mode,
                "week": args.week,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
            "DONE": {
                "items": done_items,
                "by_area": _group_tasks_by_area(done_items),
                "by_category": _group_tasks_by_category(done_items),
            },
            "DO": {
                "items": do_items,
                "by_area": _group_tasks_by_area(do_items),
                "by_category": _group_tasks_by_category(do_items),
            },
            "completion_candidates": candidate_review_summary(personal=args.personal),
            "task_audit": task_audit_summary(personal=args.personal),
        }
    )
    print(json.dumps(payload, indent=2))


def cmd_ingest_daily_log(args):
    if args.file:
        file_path = Path(args.file)
        try:
            source_content = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            payload = _new_schema("ingest-daily-log")
            payload.update(
                {
                    "source": {"type": "file", "path": str(file_path)},
                    "error": {
                        "code": "input-file-unreadable",
                        "message": str(exc),
                    },
                }
            )
            print(json.dumps(payload, indent=2))
            sys.exit(2)
        source = {"type": "file", "path": str(file_path)}
    else:
        source_content = sys.stdin.read()
        source = {"type": "stdin"}

    parsed_lines = extract_done_lines(source_content)
    records = _safe_load_task_records(args.personal)
    catalog = build_task_catalog(records)
    auto_threshold = float(args.auto_threshold)
    review_threshold = float(args.review_threshold)
    if review_threshold > auto_threshold:
        print("❌ --review-threshold cannot be greater than --auto-threshold", file=sys.stderr)
        sys.exit(2)

    matched = [
        match_evidence_line(line, catalog, auto_threshold=auto_threshold, review_threshold=review_threshold)
        for line in parsed_lines
    ]

    counts = {"evidence-link": 0, "needs-review": 0, "no-match": 0}
    for item in matched:
        counts[item["match_metadata"]["decision"]] += 1

    payload = _new_schema("ingest-daily-log")
    payload.update(
        {
            "source": source,
            "thresholds": {
                "evidence_link": auto_threshold,
                "needs_review": review_threshold,
            },
            "totals": {
                "input_lines": len(source_content.splitlines()),
                "parsed_done_lines": len(parsed_lines),
                "evidence_linked": counts["evidence-link"],
                "needs_review": counts["needs-review"],
                "no_match": counts["no-match"],
            },
            "items": matched,
        }
    )
    print(json.dumps(payload, indent=2))


def _candidate_payload(command: str, **fields) -> dict:
    payload = _new_schema(command)
    payload.update(fields)
    return payload


def _print_candidate_result(result: dict, *, command: str, exit_on_error: bool = True) -> None:
    if "schema_version" not in result:
        result = _candidate_payload(command, **result)
    print(json.dumps(result, indent=2, sort_keys=True))
    if exit_on_error and result.get("ok") is False:
        sys.exit(2)


def _visible_completion_candidates(candidates: list[dict], *, include_all: bool = False) -> list[dict]:
    if include_all:
        return candidates
    today = date.today().isoformat()
    return [
        candidate for candidate in candidates
        if candidate.get("status") != "snoozed"
        or (candidate.get("snoozed_until") or "") <= today
    ]


def cmd_completion_candidates(args):
    from completion_candidates import (
        confirm_candidate,
        duplicate_candidate,
        get_candidate,
        mark_shown,
        project_candidates,
        reject_candidate,
        scan_content,
        scan_daily_note,
        scan_file,
        snooze_candidate,
    )
    from task_ledger import MalformedLedgerError

    try:
        if args.candidate_command == "scan":
            if args.file and args.date:
                _print_candidate_result(
                    {
                        "ok": False,
                        "error": {
                            "code": "conflicting-scan-sources",
                            "message": "Use either --file or --date, not both.",
                        },
                    },
                    command="completion-candidates scan",
                )
                return
            if args.file:
                result = scan_file(Path(args.file), personal=args.personal)
                _print_candidate_result(result, command="completion-candidates scan", exit_on_error=False)
                return
            if args.date:
                notes_dir_raw = args.notes_dir or os.getenv("TASK_TRACKER_DAILY_NOTES_DIR")
                if not notes_dir_raw:
                    _print_candidate_result(
                        {"ok": False, "error": {"code": "daily-notes-dir-required"}},
                        command="completion-candidates scan",
                    )
                    return
                notes_dir = Path(notes_dir_raw).expanduser()
                day = datetime.strptime(args.date, "%Y-%m-%d").date()
                result = scan_daily_note(notes_dir, day, personal=args.personal)
                _print_candidate_result(
                    result,
                    command="completion-candidates scan",
                    exit_on_error=False,
                )
                return
            content = sys.stdin.read()
            result = scan_content(content, {"type": "stdin"}, personal=args.personal)
            _print_candidate_result(
                result,
                command="completion-candidates scan",
                exit_on_error=False,
            )
            return

        if args.candidate_command == "list":
            candidates = _visible_completion_candidates(
                project_candidates(include_terminal=args.all, personal=args.personal),
                include_all=args.all,
            )
            if args.mark_shown:
                for candidate in candidates:
                    if candidate.get("status") == "new":
                        mark_shown(candidate["candidate_id"], personal=args.personal)
                candidates = _visible_completion_candidates(
                    project_candidates(include_terminal=args.all, personal=args.personal),
                    include_all=args.all,
                )
            _print_candidate_result(
                {"candidates": candidates, "total": len(candidates)},
                command="completion-candidates list",
                exit_on_error=False,
            )
            return

        if args.candidate_command == "show":
            candidate = get_candidate(args.candidate_id, include_terminal=True, personal=args.personal)
            if candidate is None:
                _print_candidate_result(
                    {"ok": False, "error": {"code": "candidate-not-found"}},
                    command="completion-candidates show",
                )
                return
            if args.mark_shown and candidate.get("status") == "new":
                result = mark_shown(args.candidate_id, personal=args.personal)
                candidate = result.get("candidate")
            _print_candidate_result(
                {"candidate": candidate},
                command="completion-candidates show",
                exit_on_error=False,
            )
            return

        if args.candidate_command == "reject":
            result = reject_candidate(
                args.candidate_id,
                reason=args.reason,
                personal=args.personal,
            )
            _print_candidate_result(result, command="completion-candidates reject")
            return

        if args.candidate_command == "snooze":
            result = snooze_candidate(args.candidate_id, until=args.until, personal=args.personal)
            _print_candidate_result(result, command="completion-candidates snooze")
            return

        if args.candidate_command == "duplicate":
            result = duplicate_candidate(
                args.candidate_id,
                duplicate_of=args.duplicate_of,
                personal=args.personal,
            )
            _print_candidate_result(result, command="completion-candidates duplicate")
            return

        if args.candidate_command == "confirm":
            result = confirm_candidate(
                args.candidate_id,
                task_id=args.task_id,
                personal=args.personal,
            )
            _print_candidate_result(result, command="completion-candidates confirm")
            return
    except MalformedLedgerError as exc:
        _print_candidate_result(
            {
                "ok": False,
                "error": {
                    "code": "malformed-ledger",
                    "malformed": [
                        {
                            "path": item.path,
                            "line_number": item.line_number,
                            "message": item.message,
                            "raw_line": item.raw_line,
                        }
                        for item in exc.malformed
                    ],
                },
            },
            command=f"completion-candidates {args.candidate_command}",
        )
    except OSError as exc:
        _print_candidate_result(
            {"ok": False, "error": {"code": "io-error", "message": str(exc)}},
            command=f"completion-candidates {args.candidate_command}",
        )
    except ValueError as exc:
        _print_candidate_result(
            {"ok": False, "error": {"code": "invalid-input", "message": str(exc)}},
            command=f"completion-candidates {args.candidate_command}",
        )


def cmd_calendar_sync_primitive(args):
    payload = _new_schema("calendar-sync")
    warnings: list[str] = []
    events = []
    meetings = []

    try:
        events = flatten_calendar_events(get_calendar_events())
    except Exception:
        warnings.append("calendar-events-unavailable")

    try:
        records = _safe_load_task_records(args.personal)
        for record in records:
            raw = record.raw_line
            if "meeting::" not in raw:
                continue
            raw_l = raw.lower()
            status = "scheduled"
            if record.done or "status::done" in raw_l:
                status = "done"
            elif "status::canceled" in raw_l:
                status = "canceled"
            elif "status::blocked" in raw_l:
                status = "blocked"

            meetings.append(
                {
                    "task_id": record.canonical_id,
                    "fallback_id": record.fallback_id,
                    "missing_task_id": record.missing_task_id,
                    "fallback_only": record.fallback_only,
                    "title": record.title,
                    "status": status,
                    "classification": _calendar_classification(record_to_task_dict(record)),
                }
            )
    except Exception:
        warnings.append("task-meetings-unavailable")

    lifecycle_map = {
        "scheduled": [m for m in meetings if m["status"] == "scheduled"],
        "done": [m for m in meetings if m["status"] == "done"],
        "blocked": [m for m in meetings if m["status"] == "blocked"],
        "canceled": [m for m in meetings if m["status"] == "canceled"],
    }

    payload.update(
        {
            "idempotent": True,
            "optional_helper": True,
            "warnings": warnings,
            "events_seen": len(events),
            "meetings_seen": len(meetings),
            "lifecycle_map": lifecycle_map,
        }
    )
    print(json.dumps(payload, indent=2))


def _daily_note_link(which: str) -> dict:
    rel_dir = os.getenv('TASK_TRACKER_DAILY_NOTES_RELATIVE_DIR', '01-TODOs/Daily').strip('/')
    vault = os.getenv('TASK_TRACKER_OBSIDIAN_VAULT', 'Obsidian')
    offset = 0 if which == 'today' else -1
    target = date.today() + timedelta(days=offset)
    rel = f"{rel_dir}/{target.isoformat()}.md"
    enc_vault = quote(vault, safe='')
    enc_rel = quote(rel, safe='')
    return {
        'date': target.isoformat(),
        'universal': f"https://obsidian.md/open?vault={enc_vault}&file={enc_rel}",
        'deep': f"obsidian://open?vault={enc_vault}&file={enc_rel}",
    }


def cmd_daily_links(args):
    payload = {
        'command': 'daily links',
        'window': args.window,
        'links': {args.window: _daily_note_link(args.window)},
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(payload['links'][args.window]['deep'])


def _format_completion_pct(value: float) -> str:
    """Format completion percentage for human-readable output."""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.1f}"


def cmd_objectives(args):
    """Show objective-level completion status."""
    content, tasks_data = load_tasks(args.personal)
    parsed_format = detect_format(content)
    if parsed_format != 'objectives':
        print("Objective tracking is only available for Objectives format files.")
        return

    objectives = get_objective_progress(tasks_data)
    if args.at_risk:
        objectives = [
            objective for objective in objectives
            if objective['total_tasks'] > 0 and objective['completed_tasks'] == 0
        ]

    if args.json:
        print(json.dumps(objectives, indent=2))
        return

    if not objectives:
        print("No objectives found.")
        return

    for objective in objectives:
        pct = _format_completion_pct(objective['completion_pct'])
        dept = f" #{objective['department']}" if objective.get('department') else ""
        priority = f" #{objective['priority']}" if objective.get('priority') else ""
        print(
            f"🎯 {objective['title']} — {pct}% "
            f"({objective['completed_tasks']}/{objective['total_tasks']}){dept}{priority}"
        )
        for task in objective['tasks']:
            mark = "✅" if task['done'] else "⬜"
            print(f"  {mark} {task['title']}")
        print()


def main():
    parser = argparse.ArgumentParser(description='Task Tracker CLI (Work & Personal)')
    parser.add_argument('--personal', action='store_true', help='Use Personal Tasks instead of Work Tasks')
    
    subparsers = parser.add_subparsers(dest='command', required=True)
    
    # List command
    list_parser = subparsers.add_parser('list', help='List tasks')
    list_parser.add_argument('--priority', choices=['high', 'medium', 'low'])
    list_parser.add_argument('--status', choices=['open', 'done'])
    list_parser.add_argument('--due', choices=['today', 'this-week', 'overdue', 'due-or-overdue'])
    list_parser.add_argument('--completed-since', choices=['24h', '7d', '30d'])
    list_parser.set_defaults(func=list_tasks)
    
    # Add command
    add_parser = subparsers.add_parser('add', help='Add a task')
    add_parser.add_argument('title', help='Task title')
    add_parser.add_argument('--priority', default='medium', choices=['high', 'medium', 'low'])
    add_parser.add_argument('--due', help='Due date (YYYY-MM-DD)')
    add_parser.add_argument('--owner', default='me')
    add_parser.add_argument('--area', help='Area/category')
    add_parser.add_argument(
        '--force-parking',
        action='store_true',
        help='Deprecated alias: over-cap adds already route to the parking lot by default (H6)',
    )
    add_parser.set_defaults(func=add_task)

    # Promote command (H6): move a parked task onto the active board, capacity-gated.
    promote_parser = subparsers.add_parser(
        'promote',
        help='Promote a parked task onto the active board (capacity-gated)',
    )
    promote_parser.add_argument('id', type=int, help='Parking-lot item id (from parking-lot list)')
    promote_parser.set_defaults(func=promote_task)

    # Swap command (H6): park an active task and promote a parked one in its place.
    swap_parser = subparsers.add_parser(
        'swap',
        help='Park an active task and promote a parked task into the freed slot',
    )
    swap_parser.add_argument('out_id', help='Canonical task_id of the active task to park out')
    swap_parser.add_argument('in_id', type=int, help='Parking-lot item id to promote in')
    swap_parser.set_defaults(func=swap_tasks)

    # Done command
    done_parser = subparsers.add_parser('done', help='Mark task as done by canonical task_id')
    done_parser.add_argument('query', help='Canonical task_id')
    done_parser.set_defaults(func=done_task)

    identity_audit_parser = subparsers.add_parser('identity-audit', help='Read-only canonical identity audit')
    identity_audit_parser.set_defaults(func=cmd_identity_audit)

    task_audit_parser = subparsers.add_parser('task-audit', help='Read-only task health audit')
    task_audit_parser.add_argument(
        '--stale-days',
        type=int,
        default=None,
        help='Days overdue before active tasks are flagged stale',
    )
    task_audit_parser.add_argument(
        '--candidate-days',
        type=int,
        default=None,
        help='Days before unresolved completion candidates are flagged stale',
    )
    task_audit_parser.add_argument(
        '--backlog-cap',
        type=int,
        help='Parking Lot cap for backlog pressure checks',
    )
    task_audit_parser.add_argument(
        '--limit',
        type=int,
        default=5,
        help='Maximum findings included in the summary block',
    )
    task_audit_parser.set_defaults(func=cmd_task_audit)

    identity_repair_parser = subparsers.add_parser('identity-repair', help='Repair missing task_id metadata')
    identity_repair_parser.add_argument('--apply', action='store_true', help='Write safe task_id repairs')
    identity_repair_parser.set_defaults(func=cmd_identity_repair)

    done_scan_parser = subparsers.add_parser('done-scan', help='Scan completed items from daily notes')
    done_scan_parser.add_argument('--window', choices=['24h', '7d', '30d'], default='24h')
    done_scan_parser.add_argument('--json', action='store_true')
    done_scan_parser.set_defaults(func=cmd_done_scan)

    daily_links_parser = subparsers.add_parser('daily-links', help='Generate daily note links')
    daily_links_parser.add_argument('--window', choices=['today', 'yesterday'], default='today')
    daily_links_parser.add_argument('--json', action='store_true')
    daily_links_parser.set_defaults(func=cmd_daily_links)

    standup_summary_parser = subparsers.add_parser(
        'standup-summary',
        help='Return standup primitive summary JSON',
    )
    standup_summary_parser.set_defaults(func=cmd_standup_summary)

    weekly_review_summary_parser = subparsers.add_parser(
        'weekly-review-summary',
        help='Return weekly review primitive summary JSON',
    )
    weekly_review_summary_parser.add_argument('--week', help='ISO week to review (YYYY-WNN)')
    weekly_review_summary_parser.add_argument('--start', help='Start date (YYYY-MM-DD)')
    weekly_review_summary_parser.add_argument('--end', help='End date (YYYY-MM-DD)')
    weekly_review_summary_parser.set_defaults(func=cmd_weekly_review_summary)

    ingest_daily_log_parser = subparsers.add_parser(
        'ingest-daily-log',
        help='Report done-line evidence links for canonical tasks',
    )
    ingest_daily_log_parser.add_argument('--file', help='Input log file; default is stdin')
    ingest_daily_log_parser.add_argument(
        '--auto-threshold',
        type=float,
        default=FUZZY_EVIDENCE_LINK_THRESHOLD,
        help='Fuzzy score threshold for evidence-link suggestions',
    )
    ingest_daily_log_parser.add_argument(
        '--review-threshold',
        type=float,
        default=FUZZY_REVIEW_THRESHOLD,
        help='Fuzzy score threshold for needs-review',
    )
    ingest_daily_log_parser.set_defaults(func=cmd_ingest_daily_log)

    candidates_parser = subparsers.add_parser(
        'completion-candidates',
        help='Manage durable completion evidence candidates',
    )
    candidates_sub = candidates_parser.add_subparsers(
        dest='candidate_command',
        required=True,
    )

    candidates_scan = candidates_sub.add_parser(
        'scan',
        help='Scan done evidence into the candidate inbox',
    )
    candidates_scan.add_argument('--file', help='Input log file; default is stdin')
    candidates_scan.add_argument('--date', help='Daily note date to scan (YYYY-MM-DD)')
    candidates_scan.add_argument(
        '--notes-dir',
        help='Daily notes directory; defaults to TASK_TRACKER_DAILY_NOTES_DIR',
    )
    candidates_scan.set_defaults(func=cmd_completion_candidates)

    candidates_list = candidates_sub.add_parser(
        'list',
        help='List active completion candidates',
    )
    candidates_list.add_argument(
        '--all',
        action='store_true',
        help='Include terminal and future-snoozed candidates',
    )
    candidates_list.add_argument(
        '--mark-shown',
        action='store_true',
        help='Record shown events for new listed candidates',
    )
    candidates_list.set_defaults(func=cmd_completion_candidates)

    candidates_show = candidates_sub.add_parser(
        'show',
        help='Show one completion candidate and its history',
    )
    candidates_show.add_argument('candidate_id', help='Candidate ID')
    candidates_show.add_argument(
        '--mark-shown',
        action='store_true',
        help='Record a shown event for a new candidate',
    )
    candidates_show.set_defaults(func=cmd_completion_candidates)

    candidates_confirm = candidates_sub.add_parser(
        'confirm',
        help='Confirm a candidate through ID-only completion',
    )
    candidates_confirm.add_argument('candidate_id', help='Candidate ID')
    candidates_confirm.add_argument('--task-id', help='Canonical task_id to complete')
    candidates_confirm.set_defaults(func=cmd_completion_candidates)

    candidates_reject = candidates_sub.add_parser(
        'reject',
        help='Reject a completion candidate',
    )
    candidates_reject.add_argument('candidate_id', help='Candidate ID')
    candidates_reject.add_argument('--reason', help='Optional rejection reason')
    candidates_reject.set_defaults(func=cmd_completion_candidates)

    candidates_duplicate = candidates_sub.add_parser(
        'duplicate',
        help='Mark a candidate as a duplicate of another candidate',
    )
    candidates_duplicate.add_argument('candidate_id', help='Candidate ID')
    candidates_duplicate.add_argument(
        '--of',
        dest='duplicate_of',
        required=True,
        help='Canonical candidate ID',
    )
    candidates_duplicate.set_defaults(func=cmd_completion_candidates)

    candidates_snooze = candidates_sub.add_parser(
        'snooze',
        help='Hide a candidate until a future date',
    )
    candidates_snooze.add_argument('candidate_id', help='Candidate ID')
    candidates_snooze.add_argument('--until', required=True, help='Snooze-until date (YYYY-MM-DD)')
    candidates_snooze.set_defaults(func=cmd_completion_candidates)

    calendar_sync_parser = subparsers.add_parser(
        'calendar-sync',
        help='Optional helper payload for calendar lifecycle mapping',
    )
    calendar_sync_parser.set_defaults(func=cmd_calendar_sync_primitive)

    # Blockers command
    blockers_parser = subparsers.add_parser('blockers', help='Show blocking tasks')
    blockers_parser.add_argument('--person', help='Filter by person being blocked')
    blockers_parser.set_defaults(func=show_blockers)
    
    # Archive command
    archive_parser = subparsers.add_parser('archive', help='Archive completed tasks')
    archive_parser.set_defaults(func=archive_done)

    calendar_parser = subparsers.add_parser('calendar', help='Calendar domain commands')
    calendar_sub = calendar_parser.add_subparsers(dest='calendar_command', required=True)

    cal_sync = calendar_sub.add_parser('sync', help='Sync calendar meeting classification/lifecycle')
    cal_sync.add_argument('--json', action='store_true', help='Output as JSON')
    cal_sync.set_defaults(func=cmd_calendar_sync)

    cal_resolve = calendar_sub.add_parser('resolve', help='Resolve calendar task lifecycle')
    cal_resolve.add_argument('--window', choices=['today', 'yesterday'], default='today')
    cal_resolve.add_argument('--json', action='store_true', help='Output as JSON')
    cal_resolve.set_defaults(func=cmd_calendar_resolve)

    objectives_parser = subparsers.add_parser('objectives', help='Show objective progress')
    objectives_parser.add_argument('--json', action='store_true', help='Output as JSON')
    objectives_parser.add_argument(
        '--at-risk',
        action='store_true',
        help='Show only objectives with 0% completion',
    )
    objectives_parser.set_defaults(func=cmd_objectives)
    
    # Parking Lot subcommands
    pl_parser = subparsers.add_parser('parking-lot', help='Manage parking lot (backlog)')
    pl_sub = pl_parser.add_subparsers(dest='pl_command', required=True)

    pl_sub.add_parser('list', help='List parking lot items').set_defaults(func=cmd_parking_lot)

    pl_add = pl_sub.add_parser('add', help='Add item to parking lot')
    pl_add.add_argument('title', help='Task title')
    pl_add.add_argument('--dept', help='Department tag (Dev, Sales, etc.)')
    pl_add.add_argument('--priority', default='low', choices=['urgent', 'high', 'medium', 'low'])
    pl_add.set_defaults(func=cmd_parking_lot)

    pl_sub.add_parser('stale', help='List stale items (JSON)').set_defaults(func=cmd_parking_lot)

    pl_promote = pl_sub.add_parser('promote', help='Promote item to objectives')
    pl_promote.add_argument('id', type=int, help='Item ID from list')
    pl_promote.set_defaults(func=cmd_parking_lot)

    pl_drop = pl_sub.add_parser('drop', help='Drop item (archive as dropped)')
    pl_drop.add_argument('id', type=int, help='Item ID from list')
    pl_drop.set_defaults(func=cmd_parking_lot)

    # Delegated subcommands
    del_parser = subparsers.add_parser('delegated', help='Manage delegated tasks')
    del_sub = del_parser.add_subparsers(dest='del_command', required=True)

    del_list = del_sub.add_parser('list', help='List delegated items')
    del_list.add_argument('--overdue', action='store_true', help='Show only overdue items')
    del_list.add_argument('--json', action='store_true', help='JSON output')
    del_list.set_defaults(func=cmd_delegated)

    del_add = del_sub.add_parser('add', help='Delegate a task')
    del_add.add_argument('task', help='Task title')
    del_add.add_argument('--to', required=True, help='Person to delegate to')
    del_add.add_argument('--followup', required=True, help='Follow-up date (YYYY-MM-DD)')
    del_add.add_argument('--dept', help='Department tag')
    del_add.set_defaults(func=cmd_delegated)

    del_complete = del_sub.add_parser('complete', help='Mark delegation as complete')
    del_complete.add_argument('id', type=int, help='Item ID from list')
    del_complete.set_defaults(func=cmd_delegated)

    del_extend = del_sub.add_parser('extend', help='Extend follow-up date')
    del_extend.add_argument('id', type=int, help='Item ID from list')
    del_extend.add_argument('--followup', required=True, help='New follow-up date (YYYY-MM-DD)')
    del_extend.set_defaults(func=cmd_delegated)

    del_takeback = del_sub.add_parser('take-back', help='Take back delegated task')
    del_takeback.add_argument('id', type=int, help='Item ID from list')
    del_takeback.set_defaults(func=cmd_delegated)

    promote_parser = subparsers.add_parser('promote-from-backlog', help='Promote top backlog item(s)')
    promote_parser.add_argument('--cap', type=int, default=1, help='Max items to promote')
    promote_parser.set_defaults(func=cmd_promote_from_backlog)

    review_parser = subparsers.add_parser('review-backlog', help='Review stale backlog items')
    review_parser.add_argument('--stale-days', type=int, default=int(os.getenv('PARKING_LOT_STALE_DAYS', '30')))
    review_parser.add_argument('--json', action='store_true')
    review_parser.set_defaults(func=cmd_review_backlog)

    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    sys.exit(error_envelope.run_main("tasks", main))
