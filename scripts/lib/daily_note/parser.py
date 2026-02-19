"""Markdown task parser."""

import re


def parse_open_tasks(content: str) -> list[str]:
    """
    Parse open [ ] tasks from markdown content.
    
    Rules:
    - Skip [ ] inside code blocks
    - Skip tasks nested under [x] (completed parent)
    - Skip [x] items (don't reintroduce completed tasks)
    - Strip leading '- [ ]' prefix before returning
    """
    tasks = []
    in_code_block = False
    last_indent_level = 0
    completed_parents = set()

    lines = content.splitlines()
    
    for i, line in enumerate(lines):
        stripped = line.strip()
        
        # Track code blocks
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        
        if in_code_block:
            continue
        
        # Calculate indent level (number of leading spaces / 2)
        indent = len(line) - len(line.lstrip())
        indent_level = indent // 2
        
        # Check for completed tasks [x] to track completed parents
        if re.match(r"^\s*- \[x\]", line) or re.match(r"^\s*- \[X\]", line):
            completed_parents.add(indent_level)
            continue
        
        # Check for open tasks [ ]
        match = re.match(r"^(\s*)- \[ \]\s*(.+)$", line)
        if match:
            # Skip if under a completed parent
            if any(p < indent_level for p in completed_parents):
                continue
            
            task_text = match.group(2).strip()
            if task_text:
                tasks.append(task_text)
        
        # Clear completed parents when we go back up in indent
        if indent_level <= last_indent_level:
            completed_parents = {p for p in completed_parents if p < indent_level}
        
        last_indent_level = indent_level
    
    return tasks


def parse_top_priority_tasks(content: str) -> list[str]:
    """
    Parse top-priority open tasks from weekly file.
    
    Looks for tasks in priority sections (## Top, ## Q1, ## High Priority, etc.)
    and returns open [ ] tasks from those sections.
    """
    tasks = []
    in_priority_section = False
    priority_headers = [
        r"^##\s*top\s",
        r"^##\s*top\s*priority",
        r"^##\s*q1",
        r"^##\s*🔴",
        r"^##\s*high\s*priority",
        r"^##\s*urgent",
        r"^##\s*do\s*now",
        r"^##\s*must\s*do",
    ]
    
    lines = content.splitlines()
    
    for line in lines:
        stripped = line.strip()
        
        # Check for section headers
        if stripped.startswith("## "):
            in_priority_section = any(
                re.search(pattern, stripped, re.IGNORECASE) 
                for pattern in priority_headers
            )
            continue
        
        if not in_priority_section:
            continue
        
        # Parse open tasks in priority section
        match = re.match(r"^\s*- \[ \]\s*(.+)$", line)
        if match:
            task_text = match.group(1).strip()
            if task_text:
                tasks.append(task_text)
    
    return tasks
