# Rehearsal Schedule Web — Copilot Instructions

**Purpose**: Flask app that allocates musical works to rehearsals, orders them, assigns timings, and lets admins edit timelines; members view published schedules and export PDFs.

**Core components**
- [app.py](app.py): routes, auth, schedule CRUD, compute triggers, timeline/history, PDF export, invitations; relies on JSON files in [site/data/](site/data/).
- [core.py](core.py): time parsing (`minutes_from_timecell`), rehearsal/work prep, bundle/order logic; dynamically loads external scripts [1-Import-Time_per_work_per_rehearsal.py](1-Import-Time_per_work_per_rehearsal.py), [2-Orchestration_organisation.py](2-Orchestration_organisation.py), [3-Organised_rehearsal_with_time.py](3-Organised_rehearsal_with_time.py) (restart app after changes). Optional [4 - Final Compile and PDF.py](4%20-%20Final%20Compile%20and%20PDF.py) for XLSX compatibility.
- UI: templates in [templates/](templates/) (admin, edit, view, member dashboard); client JS in [static/](static/) (timeline drag/swap in `editor.js`, older timeline in [static/Retired/timeline_new.js](static/Retired/timeline_new.js)).

**Data model and storage**
- Schedules are individual JSON files in [site/data/schedules/](site/data/schedules/) with shape:
  - `id`, `ensemble_id`, `name`, `status`, `G` (grid minutes), `works`, `rehearsals`, `allocation`, `schedule`, `timed`, `timed_history`, `audit_log`, `last_notified_at`, `generated_at`.
  - Column order persisted via `works_cols` / `rehearsals_cols`; `G` defaults to `G_MINUTES` env (5).
- `default_schedule_id.txt` points at the active schedule; legacy `project.json` is auto-migrated.
- Auth data in [site/data/users.json](site/data/users.json), ensembles/memberships/invitations alongside.

**Persistence rules**
- Always read via `read_json()` and write via `write_json_atomic()` to avoid corruption; JSON serialization uses `_json_safe` to handle pandas/numpy types.
- When mutating rehearsals, keep helper fixes: `ensure_include_in_allocation_column`, `ensure_section_column`, `ensure_event_type_column`, `normalize_section_column`, `sanitize_df_records`, `clean_timed_data`.
- `make_new_schedule()` seeds audit/log/timed_history fields; `add_audit_entry` snapshots timeline edits (last 50 kept).

**Compute pipeline**
- Upload/import works + rehearsals → `prepare_works_df`/`prepare_rehearsals_df` normalize rows (durations, truthy flags, start/end times, break minutes, section defaults) → external scripts compute allocation/player load/grouping → `generate_schedule` orders bundles by player load + orchestration similarity → `generate_timed_schedule` assigns concrete times.
- Timeline edits: PUT `/api/s/<id>/timed_edit` sends full `timed` array + action description; server snapshots to `timed_history`, cleans sections, and writes JSON atomically. No multi-editor conflict handling (last write wins).

**Auth/roles and membership**
- Roles: admins manage schedules, ensembles, invitations; members view published schedules for ensembles they belong to.
- `ADMIN_EMAIL` auto-promotes on first login. Members can be pending; admins invite via `/admin/ensembles/<id>/invitations` (codes stored in invitations.json, can disable/expire).
- Published schedules are visible to members; drafts only to admins. `/my` shows member calendar and can export PDF via ReportLab.

**Notifications/Email**
- Brevo SMTP/API optional (`BREVO_*` env). Audit log cap via `AUDIT_LOG_LIMIT`. If PDF or email features change, check member-facing routes near the bottom of [app.py](app.py).

**Running locally**
- Env hints: `DATA_DIR`, `SECRET_KEY`, `EDIT_TOKEN` (admin auth token for API endpoints), `ADMIN_EMAIL`, `G_MINUTES`, `BREVO_*`, `PROJECT_FILE` (legacy), `PORT`.
- Start dev server: `EDIT_TOKEN=... flask --app app run --debug` (or `python app.py`); `use_reloader=False` avoids watcher overload. Data persists under DATA_DIR; ensure schedules dir exists.

**Working style**
- Keep JSON schemas backward-compatible; migration helpers add missing columns/fields on load.
- Prefer pandas-aware utilities for time/duration parsing and truthy flags; avoid raw string math.
- When extending compute, add outputs to schedule dict and persist via `save_schedule()`; restart to reload external script changes.
