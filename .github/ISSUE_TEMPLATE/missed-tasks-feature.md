## Problem

Currently, when the daily standup runs, it shows tasks that were due **yesterday** without asking if they were actually completed. This creates a gap in task tracking where:

- Tasks with a due date of yesterday show up in "Due Today"
- The user isn't prompted to confirm if they actually did these tasks yesterday
- Tasks silently carry over without verification

## Expected Behavior

Before generating the standup, the system should:

1. **Identify missed tasks**: Find tasks where `due < today` and task is NOT marked done
2. **Ask the user**: Before showing the standup, ask: "Did you complete these yesterday?" and list the missed tasks
3. **Allow quick completion**: User can say "yes" to mark them all done, or "no" to keep them as-is
4. **Generate standup**: After handling missed tasks, proceed with normal standup

## Current Implementation

The `standup.py` script:
- Calls `load_tasks()` which parses the task file
- Uses `check_due_date()` to filter tasks
- Shows "Due Today" tasks based on `due_date <= today`
- No proactive checking for overdue/missed tasks

## Suggested Implementation

### Option A: Modify `standup.py`

Add a pre-processing step before generating the standup:

```python
def get_missed_tasks(tasks_data) -> list:
    """Return tasks where due date was yesterday and not done."""
    yesterday = (datetime.now() - timedelta(days=1)).date()
    missed = []
    for task in tasks_data.get('all', []):
        if task.get('due') and not task['done']:
            due_date = datetime.strptime(task['due'], '%Y-%m-%d').date()
            if due_date == yesterday:
                missed.append(task)
    return missed

def ask_about_missed_tasks(missed: list) -> bool:
    """Ask user if they completed missed tasks yesterday.
    
    Returns True if user confirmed completion (mark done),
    False if user says no (keep as-is).
    """
    if not missed:
        return False
    
    # Format list for user
    task_list = '\n'.join(f"  - {t['title']}" for t in missed)
    
    # User interaction would happen here via OpenClaw messaging
    # This is where the agent would ask: "Did you complete these?"
    
    return user_confirmed  # True = mark done, False = keep
```

### Option B: New Standalone Script

Create `scripts/catchup.py` that runs before standup:

```bash
# In cron job or standup workflow
python3 scripts/catchup.py  # Asks about missed tasks
python3 scripts/standup.py   # Then shows standup
```

## User Experience

**Current:**
```
📋 Daily Standup — February 4, 2026
⏰ **Due Today:**
  • Task A (was due yesterday)
  • Task B (was due yesterday)
```

**Expected:**
```
🤖 You had 2 tasks due yesterday that weren't marked complete:
  • Task A
  • Task B

Did you complete these yesterday? (yes/no)

[User: "yes"]
→ Tasks A and B marked done
→ Proceed with standup...
```

## Implementation Notes

1. **Single interaction**: One question for all missed tasks (not per-task)
2. **Quick response**: "yes" marks all done, "no" keeps them
3. **No blocking**: If user doesn't respond, proceed with standup anyway
4. **Separate workflows**: Work tasks and personal tasks could have separate catch-up flows

## Related Files

- `scripts/standup.py` - Main standup generator
- `scripts/utils.py` - `load_tasks()`, `check_due_date()` functions
- `scripts/tasks.py` - Task management CLI

## Priority

Medium - Improves task tracking accuracy and reduces manual maintenance.
