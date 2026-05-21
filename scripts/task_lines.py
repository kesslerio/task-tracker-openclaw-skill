#!/usr/bin/env python3
"""Line-number verified task board text edits."""

from __future__ import annotations


def line_index(lines: list[str], raw_line: str, line_number: int | None) -> int | None:
    if line_number is None:
        return None
    index = line_number - 1
    if index < 0 or index >= len(lines):
        return None
    if lines[index] != raw_line:
        return None
    return index


def leading_indent_width(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def remove_task_line(content: str, raw_line: str, line_number: int | None) -> str | None:
    lines = content.split("\n")
    target_index = line_index(lines, raw_line, line_number)
    if target_index is None:
        return None
    target_indent = leading_indent_width(raw_line)
    remove_until = target_index + 1
    while remove_until < len(lines):
        line = lines[remove_until]
        if not line.strip():
            lookahead = remove_until + 1
            while lookahead < len(lines) and not lines[lookahead].strip():
                lookahead += 1
            if lookahead < len(lines):
                next_indent = leading_indent_width(lines[lookahead])
                if next_indent > target_indent:
                    remove_until += 1
                    continue
            break
        indent = leading_indent_width(line)
        if indent > target_indent:
            remove_until += 1
            continue
        break
    return "\n".join(lines[:target_index] + lines[remove_until:])


def replace_task_line(
    content: str,
    raw_line: str,
    replacement: str,
    line_number: int | None,
) -> str | None:
    lines = content.split("\n")
    target_index = line_index(lines, raw_line, line_number)
    if target_index is None:
        return None
    lines[target_index] = replacement
    return "\n".join(lines)
