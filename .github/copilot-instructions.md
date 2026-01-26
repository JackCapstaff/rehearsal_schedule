# Rehearsal Schedule Web - Copilot Instructions

## Architecture Overview

**Rehearsal Schedule Web** is a Flask-based scheduling application for orchestras/ensembles. It combines three external compute scripts (1-3) with a web UI to allocate musical works across rehearsals, generate time-based schedules, and allow interactive timeline editing.

### Data Flow
1. **Input**: Works table (Title, Duration, Difficulty, instrument demands) + Rehearsals table (Dates, times, breaks)
2. **Allocation** (Script 1-2): Distribute works across rehearsals, respecting capacities and constraints
3. **Scheduling** (Script 3): Order works within rehearsals by player load and orchestration similarity
4. **Timing**: Compute exact start times with break placement
5. **Timeline Editor**: Web UI for drag-to-reorder, swap, and undo

### Multi-Tenant Model
- **Ensembles**: Independent orchestras (via user membership)
- **Schedules**: Per-ensemble (not global project.json)
- **Users**: Auth with roles (admin, member)
- **Memberships**: Link users to ensembles (active/pending)

## Key Files & Responsibilities

| File | Purpose |
|------|---------|
| [app.py](app.py) | Flask app (2395 lines): Auth, schedules, timed edits, compute APIs |
| [core.py](core.py) | Time parsing, utility functions (used during compute) |
| [1-3 Scripts](.) | External modules (loaded dynamically): allocate, group, orchestrate |
| [site/data/](site/data/) | Persistent JSON: users, ensembles, memberships, schedules |
| [templates/](templates/) | Jinja2 HTML (member_dashboard, edit, view, admin_home) |
| [static/](static/) | JS (timeline_new.js for drag/swap/grid), CSS |

## Critical Patterns

### 1. Schedule as JSON Document
Each schedule (schedules/<id>.json) is a complete snapshot:
```python
{
  "id": "sched_...",
  "ensemble_id": "...",
  "works": [...],  # raw table rows
  "rehearsals": [...],  # raw table rows
  "works_cols", "rehearsals_cols": [...],  # column order (persists schema)
  "allocation": [...],  # output from Script 1-2
  "schedule": [...],  # output from Script 3
  "timed": [...],  # final timing with start times
  "timed_history": [{"timestamp", "action", "description", "timed"}],  # undo snapshots
  "G": 5  # grid granularity in minutes
}
```

### 2. Atomic JSON Writes
**Always use `write_json_atomic()`** to prevent corruption on crash:
```python
def write_json_atomic(path, data):
    tmp = f"{path}.tmp.{uuid.uuid4().hex}"
    with open(tmp, "w") as f:
        json.dump(data, f, ...)
    os.replace(tmp, path)  # atomic swap
```

### 3. Dynamic Script Loading
Scripts 1-3 are loaded once at startup via `importlib.util`:
```python
mod1 = load_module_from_path("script1", SCRIPT1)  # normalise_works_columns, compute_required_minutes, etc.
mod2 = load_module_from_path("script2", SCRIPT2)  # gather_resolved_groups, estimate_player_load, parse_group_and_movement
mod3 = load_module_from_path("script3", SCRIPT3)  # signature_for_work, transition_cost, etc.
```
If scripts change, they must be reloaded (restart the app).

### 4. Time Parsing Flexibility
`minutes_from_timecell()` handles: "19:00", "7:00 PM", 1900, 1.0 (0-1 fraction), pandas Timestamp, etc. Used for both `Start Time` and `End Time` columns.

### 5. Bundle Ordering Algorithm
Works are grouped by **GroupKey** (inferred from title) and ordered by:
1. **Descending player load** (hardest pieces first)
2. **Orchestration similarity** (minimize instrument setup changes via `transition_cost`)
3. **Break placement** (favors longer first half)

### 6. Timeline Editor with History
- Client drags blocks, sends full updated `timed` array + action description
- `/api/s/<id>/timed_edit` (PUT) saves snapshot to `timed_history` before updating
- Reverting restores a historical version; last 50 kept
- **No conflict resolution**: Last-write-wins (single-editor assumption)

## Auth & Permission Model

| Endpoint | Admin? | Member? | Published? |
|----------|--------|---------|------------|
| POST /admin/* | ✓ | ✗ | N/A |
| GET /admin | ✓ | ✗ | N/A |
| GET /s/<id> (view) | ✓ | ✓ (if published) | Required for members |
| PUT /api/s/<id>/timed_edit | ✓ | ✗ | N/A |
| GET /api/member/rehearsals | ✓ | ✓ | N/A |
| GET /register, /login | Anyone | | N/A |

- `ADMIN_EMAIL` env var: Auto-promote user to admin if email matches
- Memberships can be pending (not yet added to ensemble)

## Common Workflows

### Adding a New Compute Feature
1. Modify Script 1-3 code (external files)
2. Update `prepare_works_for_allocator()` or `prepare_rehearsals_for_allocator()` if input shape changes
3. Call function in new API endpoint; save result to schedule dict
4. Restart Flask to reload script modules

### Fixing Data Persistence Issues
1. Check `write_json_atomic()` is called (not `open().write()`)
2. Ensure `_json_safe()` handles all custom types (pandas Timestamp, numpy scalars, etc.)
3. Test with corrupt/empty JSON files via `read_json(path, default)` fallback

### Editing Timeline Interactively
1. Client sends updated `timed` rows + action description via PUT `/api/s/<id>/timed_edit`
2. Current `timed` saved to `timed_history` as snapshot
3. New `timed` replaces old; last 50 history entries kept
4. Revert restores from history (history entry moved to end for redo)

## Development Tips

- **Dates**: Stored as ISO strings in JSON; use `pd.to_datetime(..., errors="coerce")` when parsing
- **Nullable columns**: Use `fillna()` and `.notna()` to avoid NaN propagation
- **Config via env vars**: `DATA_DIR`, `SECRET_KEY`, `EDIT_TOKEN`, `ADMIN_EMAIL`, `G_MINUTES`
- **Tracing**: Optional OpenTelemetry (Flask instrumented if available)
- **Testing schedules**: Manual POST to `/api/s/<id>/run_allocation` then `/api/s/<id>/generate_schedule`
