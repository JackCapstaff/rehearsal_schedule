# Rehearsal Schedule Web — Copilot Instructions

## Purpose
Flask web app that imports musical works/rehearsals from Excel, intelligently allocates and orders sessions based on orchestration/player load, assigns concrete times, and serves admin/member views with PDF export and optional email notifications.

## Architecture Overview
- **[app.py](app.py)** (6973 lines): Monolithic Flask app containing all routes, auth, schedule CRUD, timeline/history snapshots, invitations, PDF/email helpers; reads/writes JSON under [site/data/](site/data/).
- **[core.py](core.py)** (400 lines): Time parsing and normalization helpers, allocation/timing pipeline entrypoints; dynamically imports external allocation scripts at runtime via `importlib.util.module_from_spec`.
- **External allocation scripts** (numbered 1-3): Compute allocation/player load/grouping using pandas DataFrames. **Critical**: Flask reloader doesn't catch changes—restart app after editing:
  - [1-Import-Time_per_work_per_rehearsal.py](1-Import-Time_per_work_per_rehearsal.py) (790 lines): Parses works/rehearsals, allocates minutes per work across rehearsals using specialist section matching (Percs/Piano/Harp/Brass/Soloist), respects min/max per-appearance constraints (ALPHA_MIN/BETA_MAX), applies spread penalty to avoid clustering.
  - [2-Orchestration_organisation.py](2-Orchestration_organisation.py) (344 lines): Computes player load estimates from orchestration columns (WIND/BRASS/STRING/PERC/PIANO/HARP weights), groups/bundles works by parent-work relationships, resolves column aliases.
  - [3-Organised_rehearsal_with_time.py](3-Organised_rehearsal_with_time.py) (474 lines): Assigns concrete start/end times to allocated bundles, inserts breaks intelligently at internal boundaries (MIN_BEFORE_AFTER, MICRO_ITEM guardrails), handles multiple time formats (HH:MM, Excel fractions, AM/PM).
  - [4 - Final Compile and PDF.py](4%20-%20Final%20Compile%20and%20PDF.py): Optional legacy XLSX flow, not used in main web pipeline.
- **UI**: Jinja2 templates in [templates/](templates/) (admin_home/edit/view/member_dashboard); timeline drag/swap/reorder logic in [static/editor.js](static/editor.js) (4749 lines, STATE-based); legacy timeline in [static/Retired/timeline_new.js](static/Retired/timeline_new.js).

## Data Model & Persistence
### Storage layout
- JSON files under [site/data/](site/data/): `users.json`, `ensembles.json`, `memberships.json`, `invitations.json`
- Schedules live in [site/data/schedules/](site/data/schedules/) as `sched_<uuid>.json`
- Active schedule id in [site/data/default_schedule_id.txt](site/data/default_schedule_id.txt)
- Legacy [project.json](project.json) auto-migrates to schedules/ on first run

### Schedule JSON shape
```python
{
  "id": "sched_<uuid>", "ensemble_id": str, "name": str, "status": "draft"|"published",
  "G": int,  # grid minutes (default 5)
  "works": [{"Work": int, "Title": str, ...orchestration cols...}, ...],
  "works_cols": ["Work", "Title", ...],  # column ordering
  "rehearsals": [{"Rehearsal": int, "Date": str, "Start": str, "End": str, "Break": int, "Percs": "Y", ...}, ...],
  "rehearsals_cols": ["Rehearsal", "Date", ...],
  "allocation": [{"Work": int, "Rehearsal": int, "Minutes": float, ...}, ...],  # from script 1
  "schedule": [{"Rehearsal": int, "Order": int, "Work": int, ...}, ...],  # bundled order from script 2
  "timed": [{"Rehearsal": int, "Order": int, "Work": int, "Start": str, "End": str, ...}, ...],  # final timeline from script 3
  "timed_history": [{"timestamp": int, "action": str, "snapshot": [...]}],  # undo/audit trail
  "audit_log": [{"timestamp": int, "action": str, ...}],  # capped at AUDIT_LOG_LIMIT (500)
  "last_notified_at": int|None, "generated_at": int
}
```

### Persistence invariants (critical!)
- **Always** load via `read_json(path, default)` and save with `write_json_atomic(path, data)` to avoid corruption; uses temp file + rename pattern
- Serialization relies on `_json_safe(obj)` custom JSON encoder for pandas/numpy types (handles np.int64, np.float64, pd.Timestamp, datetime, NaN→null, inf→1e308)
- When touching rehearsal/timed rows, **must** run normalizers to avoid NaN/inf/missing defaults:
  - `sanitize_df_records(records)`: strips NaN/inf from all fields
  - `ensure_include_in_allocation_column(rehearsals)`: fills missing "Include_in_allocation" → "Y"
  - `ensure_section_column(rehearsals)`: fills missing "Section" from Percs/Piano/Harp/Brass/Strings flags or "Full"
  - `ensure_event_type_column(rehearsals)`: fills missing "Event_type" → "Rehearsal"
  - `normalize_section_column(rehearsals)`: standardizes "Section" values to allowed set
  - `clean_timed_data(timed)`: applies all sanitizers to timed rows
- `make_new_schedule()` seeds audit/log/timed_history; `add_audit_entry()` trims to `AUDIT_LOG_LIMIT`

## Compute Pipeline (4-step flow)
1. **Import**: User uploads Excel (Works + Rehearsals sheets) → server reads via `pd.read_excel` → stores as `schedule["works"]` / `schedule["rehearsals"]`
2. **Prepare**: `core.prepare_works_df(works)` / `core.prepare_rehearsals_df(rehearsals)` parses durations (`minutes_from_timecell`), truthy flags (`parse_truthy`), start/end/break minutes, section defaults
3. **Allocate**: `mod1.allocate_rehearsal_minutes(works_df, rehearsals_df, G)` (script 1) → `allocation` DataFrame with minutes per work/rehearsal
4. **Group**: `mod2.generate_schedule(allocation_df, works_df, rehearsals_df)` (script 2) → `schedule` DataFrame bundled/ordered by player load
5. **Time**: `mod3.generate_timed_schedule(schedule_df, rehearsals_df, G)` (script 3) → `timed` DataFrame with concrete start/end times and break insertion

Grid minutes default to `G_MINUTES` env var (5). All durations quantized to G-minute multiples.

### Timeline edits (interactive)
- `PUT /api/s/<id>/timed_edit` posts full `timed` array + `action` description
- Server snapshots current state to `timed_history`, applies normalizers, writes atomically
- **No merge safety**: last write wins—prefer single editor or explicit snapshots when automating

## Auth & Membership
- **Roles**: Admins manage schedules/ensembles/invitations; members view published schedules for their ensembles
- `ADMIN_EMAIL` env var auto-promotes matching user to admin on first login
- Invitations: codes in [site/data/invitations.json](site/data/invitations.json) can be disabled/expired; used for ensemble member onboarding
- **Draft vs Published**: Draft schedules admin-only; published schedules visible to members of that ensemble
- `/my` route serves member dashboard/calendar and PDF export; `/admin/*` routes for admin CRUD

## API Patterns
- Token auth via `EDIT_TOKEN` env var (query param `?token=...` or `EDIT_TOKEN` global in editor.js)
- Frontend: `apiGet(path)` / `apiPost(path, bodyObj)` in [static/editor.js](static/editor.js) append token automatically
- Routes: `/api/s/<schedule_id>/*` for schedule-specific ops; `/api/upload`, `/api/allocate`, etc. for global ops
- JSON responses; errors return non-200 with text body

## Notifications & PDFs
- **Email**: Brevo API via `BREVO_API_KEY` env; defaults from `BREVO_FROM_EMAIL` / `BREVO_PRIMARY_TO`
  - BCC mode on by default; plain text body auto-derived from HTML via `html2text` pattern
  - Send via `send_brevo_email(to, subject, html_body, plain_body_opt)`
- **PDF**: ReportLab in lower sections of [app.py](app.py); `generate_member_pdf(schedule, member)` / `generate_conductor_report(schedule)`
  - When extending: add new fields to table rendering loops (see `drawTable` calls)
  - Uses UK date formatting via `uk_date_format` Jinja filter (e.g., "Sunday 18th January, 2026")

## Local Development
### Required env vars
```bash
DATA_DIR=site/data           # JSON storage location
SECRET_KEY=...               # Flask session secret (change in prod!)
EDIT_TOKEN=changeme          # Admin API token (change in prod!)
ADMIN_EMAIL=you@example.com  # Auto-promote to admin
G_MINUTES=5                  # Allocation grid minutes
BREVO_API_KEY=...            # Optional email
BREVO_FROM_EMAIL=...
BREVO_PRIMARY_TO=...
PROJECT_FILE=...             # Legacy, defaults to DATA_DIR/project.json
PORT=5000
```

### Run locally
```bash
# Set EDIT_TOKEN before running
EDIT_TOKEN=mysecret flask --app app run --debug

# Or direct python (use_reloader=False recommended if imports heavy)
python app.py
```

### Testing full pipeline
1. Upload works/rehearsals XLSX via `/admin` or `/api/upload`
2. Click "Allocate" → generates allocation + schedule + timed
3. Edit timeline via drag/drop in `/s/<id>/edit`
4. Verify PDF export and email notifications

### Debugging allocation scripts
- **Must restart Flask after editing 1-3.py** (dynamic imports don't trigger reload)
- Use small datasets first to catch NaN/inf issues
- Check intermediate DataFrames: `allocation`, `schedule`, `timed`
- Validate JSON integrity with `read_json`/`write_json_atomic`

## CI/CD (Azure Web App)
- GitHub Actions workflow [.github/workflows/main_rehearsalschedule.yml](.github/workflows/main_rehearsalschedule.yml)
- Builds on Ubuntu with Python 3.12, installs [requirements.txt](requirements.txt), uploads artifact (excludes `antenv/`)
- Deploys to Azure Web App via `azure/webapps-deploy@v3`
- **Oryx build** enabled by default on App Service (`SCM_DO_BUILD_DURING_DEPLOYMENT=true`) unless disabled in app settings
- Gunicorn start command in [Procfile](Procfile): `web: gunicorn app:app`

## Project-Specific Patterns
### Time parsing (multi-format robustness)
All scripts use `_to_minutes(value)` / `minutes_from_timecell(val)` helpers supporting:
- HH:MM[:SS] (19:15, 19:15:30)
- HH.MM (19.15 → 19:15)
- HHMM (1915 → 19:15)
- Excel time fractions (0.75 → 18:00)
- Python datetime/time objects
- AM/PM strings (7:15 PM → 19:15)

### Truthy parsing
Use `parse_truthy(x)` from [core.py](core.py) for Y/YES/TRUE/T/1 flags (case-insensitive, handles NaN)

### Column aliasing (orchestration)
Scripts 2-3 normalize column names via `resolve_columns(df, desired)` using `norm()` helper (lowercase, no spaces/underscores) and `ALIASES` dict (e.g., "coranglais" → "Cor Anglais")

### Frozen+scrollable tables (UI pattern)
[static/editor.js](static/editor.js) uses dual-pane layout: first 4 cols frozen (left), rest scrollable (right); synchronized scroll; Title column expands to fill space

### State management (frontend)
Global `STATE` object in [editor.js](editor.js) stores works/rehearsals/allocation/schedule/timed; `allocation_original` captured once on first load for comparison view

## Common Gotchas
- **JSON schema backward-compatibility**: Migrations in `load_schedule()` fill missing columns/fields; never remove fields without migration
- **Avoid raw string arithmetic**: Use pandas helpers (`pd.to_datetime`, `pd.to_numeric`) for times and `parse_truthy` for flags
- **No concurrency control**: Timeline edits are last-write-wins; prefer single editor or explicit snapshots (`timed_history`) when automating
- **Dynamic imports don't reload**: External scripts 1-3 require app restart after edits (Flask reloader only watches Python files in main module)
- **NaN/inf serialization**: Always use `_json_safe` encoder; validate with normalizers before saving timed/rehearsals

## File Structure Quick Reference
```
app.py                  # Flask monolith (routes, auth, PDF, email)
core.py                 # Time parsing, pipeline entrypoints, dynamic imports
1-Import-Time_per_work_per_rehearsal.py  # Allocation logic (specialist sections, spread penalty)
2-Orchestration_organisation.py          # Player load, work bundling, column resolution
3-Organised_rehearsal_with_time.py       # Concrete timing, break insertion
requirements.txt        # numpy, pandas, openpyxl, Flask, reportlab, gunicorn
Procfile                # Gunicorn start command for Azure
site/data/              # JSON storage (users, ensembles, schedules/)
  schedules/            # Per-schedule JSON files (sched_<uuid>.json)
  default_schedule_id.txt  # Active schedule pointer
static/
  editor.js             # Timeline editor (drag/swap, STATE management)
  conductor.js          # Conductor report view
  style.css             # Global styles
templates/
  admin_home.html       # Admin dashboard
  edit.html             # Timeline editor UI
  view.html             # Read-only schedule view
  member_dashboard.html # Member schedule list + PDF export
.github/workflows/main_rehearsalschedule.yml  # Azure deploy pipeline
```
