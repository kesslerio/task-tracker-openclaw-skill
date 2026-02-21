# Obsidian and Dataview

Task-tracker works with plain markdown files and is commonly used with Obsidian.

## Recommended plugins

- Dataview (recommended for dashboard queries)
- Tasks plugin (optional, richer metadata)
- Templater + Periodic Notes (optional)

## Supported task styles

1. Tasks-plugin style (with `📅`, `🔺`, `✅` markers)
2. Legacy/dataview-style markdown fields (`🗓️`, `area::`, etc.)

See `references/task-format.md` for low-level legacy field examples.

## Example sections

Work-style sections usually map to:

- `## 🔴 Q1: Do Now`
- `## 🟡 Q2: Schedule`
- `## 🟠 Q3: Waiting`
- `## 👥 Team Tasks`
- `## ⚪ Q4: Backlog`

Personal-style sections usually map to:

- `## 🔴 Must Do Today`
- `## 🟡 Should Do This Week`
- `## 🟠 Waiting On`
- `## ⚪ Backlog`

## Dataview snippets

Adjust paths to your vault layout.

### Due today

```dataview
TASK
FROM "03-Areas/Work/Work Tasks.md"
WHERE due = date("today")
SORT due ASC
LIMIT 10
```

### Due this week

```dataview
TASK
FROM "03-Areas/Work/Work Tasks.md"
WHERE due > date("today") AND due <= date("today") + dur(7 days)
SORT due ASC
LIMIT 10
```

### Completed today

```dataview
TASK
FROM "03-Areas/Work/Work Tasks.md"
WHERE completed AND due = date("today")
SORT file.mtime DESC
```
