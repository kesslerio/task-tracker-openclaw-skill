"""Task deduplication and merging."""

import re


def normalize_task(task: str) -> str:
    """
    Normalize task text for comparison.
    - Strip leading '- [ ]' prefix
    - Strip whitespace
    - Lowercase for case-insensitive comparison
    """
    # Remove leading markdown task prefix
    task = re.sub(r"^\s*-\s*\[\s*\]\s*", "", task)
    # Strip whitespace
    task = task.strip()
    # Lowercase for comparison
    return task.lower()


def merge_tasks(weekly_tasks: list[str], yesterday_tasks: list[str]) -> list[str]:
    """
    Merge weekly and yesterday tasks with deterministic dedup.
    
    Weekly is source of truth; yesterday's note adds in-flight items not in weekly.
    Maintains order: weekly tasks first, then unique yesterday tasks.
    """
    seen = set()
    merged = []
    
    # Add weekly tasks first (source of truth)
    for task in weekly_tasks:
        normalized = normalize_task(task)
        if normalized and normalized not in seen:
            seen.add(normalized)
            merged.append(task)
    
    # Add yesterday tasks that aren't already in the list
    for task in yesterday_tasks:
        normalized = normalize_task(task)
        if normalized and normalized not in seen:
            seen.add(normalized)
            merged.append(task)
    
    return merged
