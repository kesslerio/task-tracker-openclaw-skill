#!/usr/bin/env python3
"""CLI surface for /undo + /audit (U2), routed via telegram-commands.sh.

Two reactive, owner-only commands surfaced in the 🧭 Identity topic (1909):

    autonomy_cli.py audit [act_<id>] [--since-hours N] [--limit N] [--json]
    autonomy_cli.py undo  act_<id>                                  [--json]

Both are read/reverse-only (rung 0): they inspect or undo a PRIOR gated act and
never push to Telegram themselves. Output is a human-friendly block by default,
or machine JSON with ``--json``. Errors return a structured payload + non-zero
exit so telegram-commands.sh's run_with_envelope logs the detail and prints a
friendly notice -- no traceback ever reaches the relay (NO-RAW-ERROR-LEAK).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from autonomy import find_act_detail, list_acts, undo_act


def _fmt_target(target: dict[str, Any] | None) -> str:
    if not isinstance(target, dict):
        return "-"
    topic = target.get("topic_id") or "-"
    return f"topic:{topic}"


def _render_audit_list(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No autonomous acts in the audit window."
    out = [f"Last {len(rows)} autonomous act(s):"]
    for row in rows:
        flag = "↩ reverted" if row.get("reverted") else row.get("status", "?")
        out.append(
            f"  {row.get('act_id')}  {row.get('act_type')}  "
            f"{row.get('task_id') or '-'}  {_fmt_target(row.get('delivery_target'))}  "
            f"{row.get('timestamp')}  {flag}"
        )
    out.append("Undo a reversible act with: /undo act_<id>")
    return "\n".join(out)


def _cmd_audit(args: argparse.Namespace) -> int:
    if args.act_id:
        detail = find_act_detail(args.act_id)
        if detail is None:
            payload = {"ok": False, "reason": "unknown-act",
                       "message": f"No gated act {args.act_id}."}
            _emit(payload, args.json, human=payload["message"])
            return 1
        _emit({"ok": True, "act": detail}, args.json,
              human=json.dumps(detail, indent=2, sort_keys=True))
        return 0

    rows = list_acts(since_hours=args.since_hours, limit=args.limit)
    _emit({"ok": True, "acts": rows}, args.json, human=_render_audit_list(rows))
    return 0


def _cmd_undo(args: argparse.Namespace) -> int:
    result = undo_act(args.act_id)
    _emit(result, args.json, human=result.get("message", str(result)))
    return 0 if result.get("ok") else 1


def _emit(payload: dict[str, Any], as_json: bool, *, human: str) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(human)


def build_parser() -> argparse.ArgumentParser:
    # A shared parent so --json is accepted in either position
    # (`autonomy_cli.py --json audit` AND `autonomy_cli.py audit --json`).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="emit machine JSON")

    parser = argparse.ArgumentParser(prog="autonomy_cli.py", description=__doc__,
                                     parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    audit = sub.add_parser("audit", parents=[common],
                           help="list recent autonomous acts or detail one")
    audit.add_argument("act_id", nargs="?", default=None, help="act_<id> for full detail")
    # Default window: the board undo window (7d), resolved in list_acts(), so every
    # still-undoable act is listed. None here defers to that default.
    audit.add_argument("--since-hours", type=int, default=None)
    audit.add_argument("--limit", type=int, default=20)
    audit.set_defaults(func=_cmd_audit)

    undo = sub.add_parser("undo", parents=[common],
                          help="undo a reversible autonomous act")
    undo.add_argument("act_id", help="act_<id> to undo")
    undo.set_defaults(func=_cmd_undo)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
