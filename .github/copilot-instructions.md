# Rehearsal Schedule Web — Copilot Instructions

- Purpose: Flask app that imports works/rehearsals, allocates them, orders/segments rehearsals, assigns times, and serves admin/member views with PDF export and optional email notifications.

- Architecture and key files
	- [app.py](app.py): all routes, auth, schedule CRUD, timeline/history snapshots, invitations, PDF/email helpers; reads/writes JSON under [site/data/](site/data/).
	- [core.py](core.py): time parsing and normalization helpers, allocation/timing pipeline entrypoints; dynamically imports [1-Import-Time_per_work_per_rehearsal.py](1-Import-Time_per_work_per_rehearsal.py), [2-Orchestration_organisation.py](2-Orchestration_organisation.py), [3-Organised_rehearsal_with_time.py](3-Organised_rehearsal_with_time.py). Restart the app after editing these scripts. Optional [4 - Final Compile and PDF.py](4%20-%20Final%20Compile%20and%20PDF.py) for legacy XLSX flow.
	- UI: templates in [templates/](templates/) (admin/edit/view/member), timeline drag/swap logic in [static/editor.js](static/editor.js); legacy timeline in [static/Retired/timeline_new.js](static/Retired/timeline_new.js).

- Data layout
	- JSON storage under [site/data/](site/data/); schedules live in [site/data/schedules/](site/data/schedules/). Active schedule id in [site/data/default_schedule_id.txt](site/data/default_schedule_id.txt); legacy [project.json](project.json) is auto-migrated on first run.
	- Schedule shape: `id`, `ensemble_id`, `name`, `status`, `G`, `works`, `rehearsals`, `allocation`, `schedule`, `timed`, `timed_history`, `audit_log`, `last_notified_at`, `generated_at`, plus column order fields like `rehearsals_cols`/`works_cols`.
	- Users, ensembles, memberships, invitations stored alongside (e.g., [site/data/users.json](site/data/users.json)).

- Persistence invariants
	- Always load via `read_json` and save with `write_json_atomic` to avoid corruption; serialization relies on `_json_safe` for pandas/numpy types.
	- When touching rehearsal/timed rows, run the normalizers: `ensure_include_in_allocation_column`, `ensure_section_column`, `ensure_event_type_column`, `normalize_section_column`, `sanitize_df_records`, `clean_timed_data`. These keep sections/defaults consistent and avoid NaN/inf.
	- `make_new_schedule` seeds audit/log/timed_history fields; `add_audit_entry` keeps the audit list trimmed to `AUDIT_LOG_LIMIT`.

- Compute and timing pipeline
	- Import works/rehearsals → `prepare_works_df` / `prepare_rehearsals_df` (duration parsing, truthy flags, start/end/break minutes, section defaults) → external scripts compute allocation/player load/grouping → `generate_schedule` orders bundles → `generate_timed_schedule` assigns concrete times. Grid minutes default to `G_MINUTES` env (5).
	- Timeline edits: PUT `/api/s/<id>/timed_edit` posts the full `timed` array plus action description; server snapshots to `timed_history`, cleans sections, and writes atomically. Last write wins—no merge safety.

- Auth and membership
	- Roles: admins manage schedules/ensembles/invitations; members can view published schedules for their ensembles.
	- `ADMIN_EMAIL` auto-promotes on first login. Invitations live in [site/data/invitations.json](site/data/invitations.json); codes can be disabled/expired. Draft schedules visible only to admins; published ones to members. `/my` serves member dashboard/calendar and PDF export.

- Notifications and PDFs
	- Brevo email optional via `BREVO_*` env; defaults come from `BREVO_FROM_EMAIL` and `BREVO_PRIMARY_TO`. BCC mode on by default; plain text body auto-derived from HTML. Audit log cap set by `AUDIT_LOG_LIMIT`.
	- PDF generation uses ReportLab in routes near the bottom of [app.py](app.py); ensure any new fields are added to rendered tables.

- Local development
	- Env hints: `DATA_DIR`, `SECRET_KEY`, `EDIT_TOKEN` (admin token for API routes), `ADMIN_EMAIL`, `G_MINUTES`, `BREVO_*`, `PROJECT_FILE`, `PORT`.
	- Start: `EDIT_TOKEN=... flask --app app run --debug` (or `python app.py`); prefer `use_reloader=False` if imports are heavy. DATA_DIR is created automatically; schedules dir must exist.
	- External script changes require an app restart to be re-imported.

- CI/CD
	- GitHub Actions workflow [main_rehearsalschedule.yml](.github/workflows/main_rehearsalschedule.yml) builds on Ubuntu with Python 3.12, installs `requirements.txt`, uploads artifact (excludes `antenv/`), then deploys to Azure Web App via `azure/webapps-deploy`. Oryx build is enabled by default on App Service unless disabled in app settings.

- Patterns and gotchas
	- Keep JSON schema backward-compatible; migrations on load fill missing columns/fields.
	- Avoid raw string arithmetic; use pandas helpers for times and truthy parsing in `core.py`.
	- No concurrency control on timeline edits—prefer single editor or explicit snapshots (`timed_history`) when automating.
