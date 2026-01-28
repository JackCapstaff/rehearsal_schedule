import os
import re
import json
import math
import subprocess
import importlib.util
import uuid
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import datetime as _dt

import numpy as np
import pandas as pd
from flask import Flask, render_template, request, redirect, url_for, jsonify, abort, send_file, session
from werkzeug.security import generate_password_hash, check_password_hash

# Tracing (OpenTelemetry)
try:
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor, ConsoleSpanExporter
    from opentelemetry.instrumentation.flask import FlaskInstrumentor
except Exception:
    trace = None
    FlaskInstrumentor = None

if trace is not None:
    resource = Resource.create({"service.name": "rehearsal_web"})
    tracer_provider = TracerProvider(resource=resource)
    trace.set_tracer_provider(tracer_provider)
    tracer_provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    _tracer = trace.get_tracer(__name__)
else:
    _tracer = None

# ----------------------------
# Config
# ----------------------------
SCRIPT1 = "1-Import-Time_per_work_per_rehearsal.py"
SCRIPT2 = "2-Orchestration_organisation.py"
SCRIPT3 = "3-Organised_rehearsal_with_time.py"
SCRIPT4 = "4 - Final Compile and PDF.py"  # optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_DIR = os.path.join(BASE_DIR, "site", "data")
DATA_DIR = os.environ.get("DATA_DIR", DEFAULT_DATA_DIR)
DATA_DIR = os.path.abspath(DATA_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
SCHEDULES_DIR = os.path.join(DATA_DIR, "schedules")
os.makedirs(SCHEDULES_DIR, exist_ok=True)

# If PROJECT_FILE is not explicitly set, store it in the persistent DATA_DIR.
PROJECT_FILE = os.environ.get("PROJECT_FILE", os.path.join(DATA_DIR, "project.json"))
EDIT_TOKEN = os.environ.get("EDIT_TOKEN", "changeme")  # set in Azure App Settings!
DEFAULT_G = int(os.environ.get("G_MINUTES", "5"))

TIMED_XLSX_OUT = "timed_rehearsal.xlsx"  # for script4 compatibility

ADMIN_EMAIL = (os.environ.get("ADMIN_EMAIL") or "").strip().lower()





# ----------------------------
# App
# ----------------------------
app = Flask(__name__)
if FlaskInstrumentor is not None:
    try:
        FlaskInstrumentor().instrument_app(app)
    except Exception:
        pass
app.secret_key = os.environ.get("SECRET_KEY", "dev-change-me")  # set in Azure App Settings

# Custom Jinja filter for UK date format with ordinal suffix
@app.template_filter('uk_date_format')
def uk_date_format(date_value):
    """Format date as 'Sunday 18th January, 2026'"""
    if not date_value or pd.isna(date_value):
        return ""
    
    try:
        # Convert to datetime if it isn't already
        if isinstance(date_value, str):
            dt = pd.to_datetime(date_value)
        elif hasattr(date_value, 'to_pydatetime'):
            dt = date_value.to_pydatetime()
        else:
            dt = date_value
        
        # Get day with ordinal suffix
        day = dt.day
        if 10 <= day % 100 <= 20:
            suffix = 'th'
        else:
            suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')
        
        # Format: Sunday 18th January, 2026
        return dt.strftime(f'%A {day}{suffix} %B, %Y')
    except:
        return str(date_value)


def schedule_path(schedule_id: str) -> str:
    return os.path.join(SCHEDULES_DIR, f"{schedule_id}.json")

def list_schedule_ids() -> list[str]:
    if not os.path.exists(SCHEDULES_DIR):
        return []
    return sorted(
        [fn[:-5] for fn in os.listdir(SCHEDULES_DIR) if fn.endswith(".json")]
    )

def load_schedule(schedule_id: str) -> dict:
    p = schedule_path(schedule_id)
    s = read_json(p, None) or {}  # see safe read_json note below
    
    # Ensure all rehearsals have the "Include in allocation" column
    if "rehearsals" in s:
        s["rehearsals"] = ensure_include_in_allocation_column(s["rehearsals"])
    
    # Ensure all rehearsals have the "Section" column
    if "rehearsals" in s:
        s["rehearsals"] = ensure_section_column(s["rehearsals"])
    
    # Ensure all rehearsals have the "Event Type" column
    if "rehearsals" in s:
        s["rehearsals"] = ensure_event_type_column(s["rehearsals"])
    
    # Ensure rehearsals_cols includes "Event Type", "Section", "Include in allocation" (for old schedules)
    if "rehearsals" in s and s["rehearsals"] and isinstance(s["rehearsals"], list) and len(s["rehearsals"]) > 0:
        # If rehearsals_cols doesn't exist or is incomplete, rebuild it from actual rehearsal data
        if "rehearsals_cols" not in s or not s["rehearsals_cols"]:
            # Use all columns from the first rehearsal row (if it's a dict)
            if isinstance(s["rehearsals"][0], dict):
                s["rehearsals_cols"] = list(s["rehearsals"][0].keys())
        else:
            cols = s["rehearsals_cols"]
            # Add missing columns that should always be present
            for required_col in ["Include in allocation", "Section", "Event Type"]:
                if required_col not in cols:
                    if required_col == "Event Type" and "Include in allocation" in cols:
                        # Insert "Event Type" after "Include in allocation"
                        idx = cols.index("Include in allocation") + 1
                        cols.insert(idx, "Event Type")
                    else:
                        cols.append(required_col)
            s["rehearsals_cols"] = cols
    
    return s

def save_schedule(s: dict):
    if "id" not in s:
        raise ValueError("Schedule missing id")
    write_json_atomic(schedule_path(s["id"]), s)

def migrate_unnamed_columns_to_day():
    """Rename 'Unnamed: 2' to 'Day' in all schedules"""
    schedule_ids = list_schedule_ids()
    migrated_count = 0
    
    for schedule_id in schedule_ids:
        schedule = load_schedule(schedule_id)
        modified = False
        
        # Migrate rehearsals_cols
        if "rehearsals_cols" in schedule:
            cols = schedule["rehearsals_cols"]
            if "Unnamed: 2" in cols:
                schedule["rehearsals_cols"] = ["Day" if c == "Unnamed: 2" else c for c in cols]
                modified = True
        
        # Migrate rehearsal objects
        if "rehearsals" in schedule:
            for rehearsal in schedule["rehearsals"]:
                if "Unnamed: 2" in rehearsal:
                    rehearsal["Day"] = rehearsal.pop("Unnamed: 2")
                    modified = True
        
        if modified:
            save_schedule(schedule)
            migrated_count += 1
    
    return migrated_count

def make_new_schedule(ensemble_id: str, name: str, created_by: str | None = None) -> dict:
    sid = f"sched_{uuid.uuid4().hex[:10]}"
    return {
        "id": sid,
        "ensemble_id": ensemble_id,
        "name": name or "Untitled",
        "status": "draft",          # draft | published
        "created_by": created_by,
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
        "G": DEFAULT_G,
        "works": [],
        "rehearsals": [],
        "allocation": [],
        "schedule": [],
        "timed": [],
        "timed_history": [],
        "generated_at": None,
    }

DEFAULT_SCHEDULE_ID_FILE = os.path.join(DATA_DIR, "default_schedule_id.txt")

def get_default_schedule_id() -> str | None:
    if not os.path.exists(DEFAULT_SCHEDULE_ID_FILE):
        return None
    try:
        return open(DEFAULT_SCHEDULE_ID_FILE, "r", encoding="utf-8").read().strip() or None
    except Exception:
        return None

def set_default_schedule_id(sid: str):
    with open(DEFAULT_SCHEDULE_ID_FILE, "w", encoding="utf-8") as f:
        f.write(sid)

def default_schedule():
    sid = get_default_schedule_id()
    if not sid:
        migrate_project_json_if_needed()
        sid = get_default_schedule_id()
    if not sid:  # type: ignore
        return {}, None
    return load_schedule(sid), sid  # type: ignore

def migrate_project_json_if_needed():
    # If schedules already exist, do nothing
    if list_schedule_ids():
        return

    # If old project.json exists, migrate it
    if os.path.exists(PROJECT_FILE):
        old = read_json(PROJECT_FILE, None)
        if isinstance(old, dict):
            ensembles = load_ensembles()
            default_ens = ensembles[0]["id"] if ensembles else "default"

            s = make_new_schedule(default_ens, "Migrated Schedule", created_by=None)
            # carry over old fields
            for k in ["G", "works", "rehearsals", "allocation", "schedule", "timed", "timed_history", "generated_at"]:
                if k in old:
                    s[k] = old[k]
            save_schedule(s)
            set_default_schedule_id(s["id"])
            return

    # Otherwise, create a blank default schedule
    ensembles = load_ensembles()
    default_ens = ensembles[0]["id"] if ensembles else "default"
    s = make_new_schedule(default_ens, "Default Schedule", created_by=None)
    save_schedule(s)
    set_default_schedule_id(s["id"])




# ----------------------------
# JSON storage helpers (atomic writes)
# ----------------------------
def read_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
            if not raw:
                return default
            return json.loads(raw)
    except (json.JSONDecodeError, OSError):
        # If file is corrupt/empty, fall back safely
        return default
    

def write_json_atomic(path: str, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{uuid.uuid4().hex}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=_json_safe)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _json_safe(obj):
    # pandas Timestamp / datetime / date
    try:
        import pandas as pd
        if isinstance(obj, pd.Timestamp):
            # keep timezone if present, else ISO
            return obj.isoformat()
    except Exception:
        pass

    if isinstance(obj, (_dt.datetime, _dt.date)):
        return obj.isoformat()

    # numpy scalars
    try:
        import numpy as np
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            v = float(obj)
            # handle NaN/inf
            if v != v or v in (float("inf"), float("-inf")):
                return None
            return v
        if isinstance(obj, (np.bool_,)):
            return bool(obj)
    except Exception:
        pass

    # fallback: string-ify unknown objects
    return str(obj)


def sanitize_df_records(records: List[dict]) -> List[dict]:
    """Convert NaN/inf values in a list of dicts to None (null in JSON)."""
    if not isinstance(records, list):
        return records
    
    result = []
    for row in records:
        if not isinstance(row, dict):
            result.append(row)
            continue
        
        sanitized = {}
        for k, v in row.items():
            # Check for NaN or inf
            if isinstance(v, float):
                if v != v or v in (float("inf"), float("-inf")):  # NaN check: NaN != NaN
                    sanitized[k] = None
                else:
                    sanitized[k] = v
            else:
                sanitized[k] = v
        result.append(sanitized)
    
    return result


def clean_timed_data(timed: List[dict]) -> List[dict]:
    """Clean timed data: fill missing Section values with 'Full Ensemble'."""
    if not isinstance(timed, list):
        return timed
    
    # Convert to DataFrame for easier manipulation
    if not timed:
        return timed
    
    df = pd.DataFrame(timed)
    if "Section" not in df.columns:
        df["Section"] = "Full Ensemble"
    else:
        # Fill any NaN or empty Section values with "Full Ensemble"
        df["Section"] = df["Section"].fillna("Full Ensemble")
        df["Section"] = df["Section"].astype(str).replace("nan", "Full Ensemble")
        # Also replace empty strings
        df.loc[df["Section"].str.strip() == "", "Section"] = "Full Ensemble"
    
    # Convert back to list of dicts
    return df.to_dict(orient="records")


def ensure_include_in_allocation_column(rehearsals: List[dict]) -> List[dict]:
    """Ensure all rehearsals have 'Include in allocation' column, defaulting to 'Y'."""
    if not isinstance(rehearsals, list):
        return rehearsals
    
    if not rehearsals:
        return rehearsals
    
    # Add missing column with default value 'Y'
    for row in rehearsals:
        if "Include in allocation" not in row or pd.isna(row.get("Include in allocation")):
            row["Include in allocation"] = "Y"
    
    return rehearsals


def normalize_section_column(rehearsals: List[dict]) -> List[dict]:
    """Normalize 'Ensemble' column to 'Section' if present, then clean up.
    
    This allows Excel files to use either "Ensemble" or "Section" column names.
    If both exist, "Section" takes priority. If neither exists, defaults to "Full Ensemble".
    """
    if not isinstance(rehearsals, list):
        return rehearsals
    
    if not rehearsals:
        return rehearsals
    
    # Check if first row has "Ensemble" but not "Section"
    for row in rehearsals:
        if "Ensemble" in row and "Section" not in row:
            # Rename "Ensemble" to "Section"
            row["Section"] = row.pop("Ensemble")
        elif "Ensemble" in row and "Section" in row:
            # Both exist, keep "Section" and remove "Ensemble"
            row.pop("Ensemble")
    
    return rehearsals


def ensure_section_column(rehearsals: List[dict]) -> List[dict]:
    """Ensure all rehearsals have 'Section' column, defaulting to 'Full Ensemble'."""
    if not isinstance(rehearsals, list):
        return rehearsals
    
    if not rehearsals:
        return rehearsals
    
    # Add missing column with default value 'Full Ensemble'
    for row in rehearsals:
        if "Section" not in row or not row.get("Section") or pd.isna(row.get("Section")):
            row["Section"] = "Full Ensemble"
        else:
            # Clean up section value
            row["Section"] = str(row.get("Section", "Full Ensemble")).strip()
            if not row["Section"]:
                row["Section"] = "Full Ensemble"
    
    return rehearsals


def ensure_event_type_column(rehearsals: List[dict]) -> List[dict]:
    """Ensure all rehearsals have 'Event Type' column, defaulting to 'Rehearsal'.
    
    Automatically detects event type from alternative field names:
    - "event", "Event", "event type", "Event Type", "Event_Type", "event_type", etc.
    This allows users to have an 'event' field in their Excel import that auto-populates Event Type.
    """
    if not isinstance(rehearsals, list):
        return rehearsals
    
    if not rehearsals:
        return rehearsals
    
    # Possible alternate field names for event type (case-insensitive)
    ALTERNATE_FIELDS = ["event", "event type", "event_type", "eventtype"]
    
    # Add missing column with default value 'Rehearsal'
    for row in rehearsals:
        event_type_found = None
        
        # FIRST: Check for alternate field names (like "event", "Event") - prioritize these
        for key in row.keys():
            key_lower = key.lower().replace(" ", "").replace("_", "")
            if key_lower in ALTERNATE_FIELDS:
                value = row.get(key, "")
                # Check if value is not empty and not NaN
                if value is not None and value != "" and not pd.isna(value):
                    event_type_found = str(value).strip()
                    break
        
        # SECOND: If no alternate field found, check if Event Type is already set and valid
        if not event_type_found:
            existing_value = row.get("Event Type", "")
            if existing_value and not pd.isna(existing_value):
                event_type_found = str(existing_value).strip()
        
        # Now normalize the found value to standard values
        if event_type_found:
            if event_type_found.lower() in ["concert", "sectional", "rehearsal"]:
                row["Event Type"] = event_type_found.lower().capitalize()
            else:
                row["Event Type"] = "Rehearsal"
        else:
            # Default to Rehearsal
            row["Event Type"] = "Rehearsal"
    
    return rehearsals


def upsert_concert_timed_rows(
    timed_data: List[dict],
    rehearsal_num: int,
    concert_info: dict,
    section: str = "Full Ensemble",
) -> List[dict]:
    """Update or insert timed rows for a concert rehearsal without wiping other edits."""
    if rehearsal_num is None:
        return clean_timed_data(timed_data)

    concert_id = concert_info.get("id")
    concert_date = concert_info.get("date")
    concert_time = concert_info.get("time") or concert_info.get("start_time")
    concert_title = concert_info.get("title") or "Concert"
    updated_any = False

    for row in timed_data:
        if int(row.get("Rehearsal", 0)) == int(rehearsal_num):
            row["Event Type"] = "Concert"
            if concert_id:
                row["concert_id"] = concert_id
            if concert_date:
                row["Date"] = concert_date
            if concert_time:
                row["Time in Rehearsal"] = concert_time
            row["Section"] = row.get("Section") or section or "Full Ensemble"
            if not row.get("Title"):
                row["Title"] = concert_title
            updated_any = True

    if not updated_any:
        timed_data.append({
            "Rehearsal": rehearsal_num,
            "Date": concert_date or "",
            "Title": concert_title,
            "Time in Rehearsal": concert_time or "19:00",
            "Break Start (HH:MM)": "",
            "Break End (HH:MM)": "",
            "Section": section or "Full Ensemble",
            "Event Type": "Concert",
            "concert_id": concert_id,
        })

    return clean_timed_data(timed_data)


def auto_number_rehearsals(rehearsals: List[dict]) -> List[dict]:
    """Auto-assign sequential Rehearsal numbers if missing or empty."""
    if not isinstance(rehearsals, list) or not rehearsals:
        return rehearsals
    
    # Check if Rehearsal column needs numbering
    df = pd.DataFrame(rehearsals)
    if "Rehearsal" not in df.columns:
        # Add Rehearsal column with sequential numbers
        df["Rehearsal"] = range(1, len(df) + 1)
    else:
        # Fill empty/NaN rehearsal numbers with sequential numbers
        empty_mask = df["Rehearsal"].astype(str).str.strip().eq("") | df["Rehearsal"].isna()
        if empty_mask.any():
            # Get existing numbers to avoid conflicts
            existing_nums = pd.to_numeric(df["Rehearsal"], errors="coerce").dropna().astype(int).unique()
            next_num = max(existing_nums.tolist() + [0]) + 1
            
            # Fill empty ones with sequential numbers
            fill_count = 0
            for idx in df[empty_mask].index:
                df.at[idx, "Rehearsal"] = str(next_num + fill_count)
                fill_count += 1
    
    return df.fillna("").to_dict(orient="records")


def users_path() -> str:
    return os.path.join(DATA_DIR, "users.json")


def ensembles_path() -> str:
    return os.path.join(DATA_DIR, "ensembles.json")


def memberships_path() -> str:
    return os.path.join(DATA_DIR, "memberships.json")


def load_users() -> List[dict]:
    return read_json(users_path(), [])


def save_users(users: List[dict]):
    write_json_atomic(users_path(), users)


def load_ensembles() -> List[dict]:
    ensembles = read_json(ensembles_path(), [])
    if not ensembles:
        # Seed a starter ensemble so the app is usable immediately.
        ensembles = [{"id": "default", "name": "Default Ensemble"}]
        write_json_atomic(ensembles_path(), ensembles)
    return ensembles


def save_ensembles(ensembles: List[dict]):
    write_json_atomic(ensembles_path(), ensembles)


def get_ensemble_by_id(ensemble_id: str) -> Optional[dict]:
    """Get a single ensemble by ID."""
    ensembles = load_ensembles()
    return next((e for e in ensembles if e.get("id") == ensemble_id), None)


def ensure_ensemble_concerts(ensembles: List[dict]) -> List[dict]:
    """Ensure each ensemble has a 'concerts' array."""
    for ens in ensembles:
        if "concerts" not in ens:
            ens["concerts"] = []
    return ensembles


def add_concert_to_ensemble(ensemble_id: str, concert_data: dict) -> dict:
    """Add a concert to an ensemble and save."""
    ensembles = load_ensembles()
    ensembles = ensure_ensemble_concerts(ensembles)
    
    ensemble = next((e for e in ensembles if e.get("id") == ensemble_id), None)
    if not ensemble:
        raise ValueError(f"Ensemble {ensemble_id} not found")
    
    # Create concert object with auto-generated fields
    concert = {
        "id": f"concert_{uuid.uuid4().hex[:10]}",
        "title": concert_data.get("title"),
        "date": concert_data.get("date"),
        "time": concert_data.get("time"),
        "venue": concert_data.get("venue"),
        "uniform": concert_data.get("uniform"),
        "programme": concert_data.get("programme", ""),
        "other_info": concert_data.get("other_info", ""),
        "schedule_id": concert_data.get("schedule_id"),  # Optional link to rehearsal schedule
        "status": concert_data.get("status", "scheduled"),  # scheduled | completed | cancelled
        "created_at": int(time.time()),
    }
    
    ensemble["concerts"].append(concert)
    save_ensembles(ensembles)
    return concert


def _format_concert_title(ensemble_name: str, date_str: str) -> str:
    """Generate default concert title with ordinal date suffix."""
    try:
        dt = pd.to_datetime(date_str)
        day = dt.day
        if 10 <= day % 100 <= 20:
            suffix = 'th'
        else:
            suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')
        formatted_date = dt.strftime(f"%A %B {day}{suffix}")
        return f"{{ensemble_name}} - Concert - {formatted_date}"
    except Exception:
        return f"{{ensemble_name}} - Concert"


def update_concert(ensemble_id: str, concert_id: str, concert_data: dict) -> Optional[dict]:
    """Update a concert in an ensemble."""
    ensembles = load_ensembles()
    ensembles = ensure_ensemble_concerts(ensembles)
    
    ensemble = next((e for e in ensembles if e.get("id") == ensemble_id), None)
    if not ensemble:
        return None
    
    concert = next((c for c in ensemble.get("concerts", []) if c.get("id") == concert_id), None)
    if not concert:
        return None
    
    # Track if date changed
    old_date = concert.get("date")
    new_date = concert_data.get("date", old_date)
    date_changed = old_date != new_date
    ensemble_name = ensemble.get("name", "Unknown Ensemble")

    # Recompute default title if the concert still uses the default pattern or no title was provided
    old_default_title = _format_concert_title(ensemble_name, old_date) if old_date else None
    new_default_title = _format_concert_title(ensemble_name, new_date) if new_date else None
    incoming_title = concert_data.get("title")
    should_update_title = (
        not incoming_title
        or (old_default_title and concert.get("title") == old_default_title)
    )
    
    # Update fields (preserve id and created_at)
    if should_update_title and new_default_title:
        concert["title"] = new_default_title
    else:
        concert["title"] = concert_data.get("title", concert.get("title"))
    concert["date"] = new_date
    concert["time"] = concert_data.get("time", concert["time"])
    concert["venue"] = concert_data.get("venue", concert["venue"])
    concert["uniform"] = concert_data.get("uniform", concert["uniform"])
    concert["programme"] = concert_data.get("programme", concert["programme"])
    concert["other_info"] = concert_data.get("other_info", concert["other_info"])
    concert["schedule_id"] = concert_data.get("schedule_id", concert.get("schedule_id"))
    concert["status"] = concert_data.get("status", concert["status"])
    
    save_ensembles(ensembles)
    
    # SYNC: Update corresponding rehearsal row in schedule
    schedule_id = concert.get("schedule_id")
    if schedule_id:
        schedule = load_schedule(schedule_id)
        if schedule:
            updated_schedule = False
            timed_data = schedule.get("timed", [])
            for reh_row in schedule.get("rehearsals", []):
                # Find rehearsal row by concert_id link (fallback to Event Type match without id)
                if reh_row.get("concert_id") == concert_id or (
                    reh_row.get("Event Type") == "Concert" and not reh_row.get("concert_id")
                ):
                    print(f"[UPDATE CONCERT] Syncing concert {concert_id} changes to rehearsal row")
                    # Update rehearsal row with concert data
                    reh_row["Date"] = new_date
                    reh_row["Start Time"] = concert["time"]
                    reh_row["Venue"] = concert["venue"]
                    reh_row["Uniform"] = concert["uniform"]
                    if not reh_row.get("concert_id"):
                        reh_row["concert_id"] = concert_id
                    updated_schedule = True

                    rehearsal_num = int(reh_row.get("Rehearsal", 0))

                    # Update or insert timed rows for this concert without wiping timeline edits
                    timed_data = upsert_concert_timed_rows(
                        timed_data,
                        rehearsal_num,
                        {
                            "id": concert_id,
                            "date": new_date,
                            "time": concert.get("time"),
                            "title": concert.get("title"),
                        },
                        reh_row.get("Section", "Full Ensemble"),
                    )
                    break
            
            if updated_schedule:
                schedule["timed"] = timed_data
                schedule["updated_at"] = int(time.time())
                save_schedule(schedule)
                print(f"[UPDATE CONCERT] Updated schedule {schedule_id} rehearsal table")
    
    return concert


def delete_concert(ensemble_id: str, concert_id: str) -> bool:
    """Delete a concert from an ensemble."""
    ensembles = load_ensembles()
    ensemble = next((e for e in ensembles if e.get("id") == ensemble_id), None)
    if not ensemble:
        return False
    
    concerts = ensemble.get("concerts", [])
    original_len = len(concerts)
    ensemble["concerts"] = [c for c in concerts if c.get("id") != concert_id]
    
    if len(ensemble["concerts"]) < original_len:
        save_ensembles(ensembles)
        return True
    return False


def get_ensemble_concerts(ensemble_id: str) -> List[dict]:
    """Get all concerts for an ensemble, sorted by date."""
    ensemble = get_ensemble_by_id(ensemble_id)
    if not ensemble:
        return []
    concerts = ensemble.get("concerts", [])
    # Sort by date descending (upcoming first)
    concerts.sort(key=lambda x: x.get("date", ""), reverse=True)
    return concerts


def load_memberships() -> List[dict]:
    return read_json(memberships_path(), [])


def save_memberships(memberships: List[dict]):
    write_json_atomic(memberships_path(), memberships)


# ----------------------------
# Session auth helpers (Phase 1)
# ----------------------------
def current_user() -> Optional[dict]:
    uid = session.get("user_id")
    if not uid:
        return None
    return next((u for u in load_users() if u.get("id") == uid), None)


def login_required_or_redirect():
    if not current_user():
        return redirect(url_for("login_view", next=request.path))
    return None


def admin_required_or_403():
    u = current_user()
    if not u:
        return redirect(url_for("login_view", next=request.path))
    if not u.get("is_admin"):
        abort(403)
    return None


def is_member(user_id: str, ensemble_id: str) -> bool:
    mem = load_memberships()
    return any(
        m for m in mem
        if m.get("user_id") == user_id
        and m.get("ensemble_id") == ensemble_id
        and m.get("status", "active") == "active"
    )


def ensemble_member_required_or_403(ensemble_id: str):
    u = current_user()
    if not u:
        return redirect(url_for("login_view", next=request.path))
    if u.get("is_admin"):
        return None
    if not is_member(u["id"], ensemble_id):
        abort(403)
    return None

def normalize_email(s: str) -> str:
    return (s or "").strip().lower()

def slugify_id(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s or uuid.uuid4().hex[:8]

def ensure_admin_if_matches_email(user: dict) -> bool:
    """Returns True if user was modified."""
    global ADMIN_EMAIL
    if ADMIN_EMAIL and normalize_email(user.get("email") or "") == ADMIN_EMAIL and not user.get("is_admin"):
        user["is_admin"] = True
        return True
    return False

def set_user_admin_flag(user_id: str, is_admin: bool):
    users = load_users()
    u = next((x for x in users if x.get("id") == user_id), None)
    if not u:
        abort(404)
    u["is_admin"] = bool(is_admin)
    save_users(users)

def upsert_membership(user_id: str, ensemble_id: str, role: str = "member", status: str = "active"):
    mem = load_memberships()
    m = next((x for x in mem if x.get("user_id") == user_id and x.get("ensemble_id") == ensemble_id), None)
    if m:
        m["role"] = role
        m["status"] = status
    else:
        mem.append({"user_id": user_id, "ensemble_id": ensemble_id, "role": role, "status": status})
    save_memberships(mem)

def remove_membership(user_id: str, ensemble_id: str):
    mem = load_memberships()
    mem = [m for m in mem if not (m.get("user_id") == user_id and m.get("ensemble_id") == ensemble_id)]
    save_memberships(mem)


# ----------------------------
# Utilities / loading script modules
# ----------------------------
def load_module_from_path(name: str, path: str):
    if not os.path.exists(path):
        raise FileNotFoundError(f"Missing required file: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise RuntimeError(f"Could not load module spec for {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod1 = load_module_from_path("script1", SCRIPT1)
mod2 = load_module_from_path("script2", SCRIPT2)
mod3 = load_module_from_path("script3", SCRIPT3)
mod4_exists = os.path.exists(SCRIPT4)


def safe_int(x, default=0):
    try:
        if pd.isna(x):
            return default
        return int(float(x))
    except Exception:
        return default


def safe_float(x, default=0.0) -> float:
    try:
        if pd.isna(x):
            return default
        return float(x)
    except Exception:
        return default


def parse_truthy(x) -> bool:
    if pd.isna(x):
        return False
    s = str(x).strip().upper()
    return s in {"Y", "YES", "TRUE", "T", "1"}


def minutes_from_timecell(val) -> Optional[int]:
    if pd.isna(val):
        return None
    if hasattr(val, "hour") and hasattr(val, "minute"):
        try:
            return int(val.hour) * 60 + int(val.minute)
        except Exception:
            pass

    s = str(val).strip().replace("：", ":")
    if re.match(r"^\d{1,2}\.\d{2}(\s*(AM|PM|am|pm))?$", s):
        s = s.replace(".", ":")

    m = re.match(r"^\s*(\d{1,2}):(\d{2})", s)
    if m:
        hh = int(m.group(1)) % 24
        mm = int(m.group(2))
        if re.search(r"pm$", s, flags=re.I) and hh < 12:
            hh += 12
        if re.search(r"am$", s, flags=re.I) and hh == 12:
            hh = 0
        return hh * 60 + mm

    m = re.fullmatch(r"^(\d{3,4})$", s)
    if m:
        v = int(m.group(1))
        hh = v // 100
        mm = v % 100
        if 0 <= hh < 24 and 0 <= mm < 60:
            return hh * 60 + mm

    try:
        f = float(s)
        if 0 <= f < 1:
            return int(round(f * 24 * 60))
        if 1 <= f < 24 * 60 + 1:
            return int(round(f))
    except Exception:
        pass

    t = pd.to_datetime(s, errors="coerce")
    if pd.notna(t):
        return int(t.hour) * 60 + int(t.minute)
    return None


def parse_break_minutes(val) -> int:
    if pd.isna(val):
        return 0
    num = pd.to_numeric(val, errors="coerce")
    if pd.notna(num):
        return int(round(float(num)))
    t = pd.to_datetime(val, errors="coerce")
    if pd.notna(t):
        return int(t.hour) * 60 + int(t.minute)
    s = str(val).strip()
    if ":" in s:
        p = s.split(":")
        if len(p) == 2 and p[0].isdigit() and p[1].isdigit():
            return int(p[0]) * 60 + int(p[1])
    return 0


def hhmm_from_minutes(m: int) -> str:
    m = int(m)
    h = (m // 60) % 24
    mm = m % 60
    return f"{h:02d}:{mm:02d}"


# ----------------------------
# Project persistence
# ----------------------------
def default_works_df() -> pd.DataFrame:
    cols = [
        "Title", "Duration", "Difficulty", "Rehearsal Time Required",
        "Flute", "Oboe", "Clarinet", "Bassoon",
        "Horn", "Trumpet", "Trombone", "Tuba",
        "Violin 1", "Violin 2", "Viola", "Cello", "Bass",
        "Percussion", "Timpani", "Piano", "Harp", "Soloist"
    ]
    return pd.DataFrame(columns=cols)


def default_rehearsals_df() -> pd.DataFrame:
    cols = ["Rehearsal", "Date", "Day", "Include in allocation", "Event Type", "Section", "Start Time", "End Time", "Break", "Percs", "Piano", "Harp", "Brass", "Soloist"]
    return pd.DataFrame(columns=cols)


def load_project() -> dict:
    return read_json(
        PROJECT_FILE,
        {
            "G": DEFAULT_G,
            "works": [],
            "rehearsals": [],
            "allocation": [],
            "schedule": [],
            "timed": [],
            "timed_history": [],
            "generated_at": None,
        },
    )


def save_project(p: dict):
    write_json_atomic(PROJECT_FILE, p)


def get_frames(s: dict) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    # Load works with saved column order if available
    works_cols = s.get("works_cols")
    works = pd.DataFrame(s.get("works", []))
    if works_cols:
        # Use saved columns in saved order, fill missing columns with empty strings
        works = works.reindex(columns=works_cols).fillna("")
    else:
        # Fall back to defaults
        default_cols = default_works_df().columns.tolist()
        # Only keep existing columns and add missing ones from defaults
        existing_cols = [c for c in works.columns if c in default_cols]
        missing_cols = [c for c in default_cols if c not in works.columns]
        if missing_cols:
            for col in missing_cols:
                works[col] = ""
        works = works[existing_cols + missing_cols]
    
    # Load rehearsals with saved column order if available
    rehearsals_cols = s.get("rehearsals_cols")
    rehearsals = pd.DataFrame(s.get("rehearsals", []))
    if rehearsals_cols:
        # Use saved columns in saved order, fill missing columns with empty strings
        rehearsals = rehearsals.reindex(columns=rehearsals_cols).fillna("")
    else:
        # Fall back to defaults
        default_cols = default_rehearsals_df().columns.tolist()
        # Only keep existing columns and add missing ones from defaults
        existing_cols = [c for c in rehearsals.columns if c in default_cols]
        missing_cols = [c for c in default_cols if c not in rehearsals.columns]
        if missing_cols:
            for col in missing_cols:
                rehearsals[col] = ""
        rehearsals = rehearsals[existing_cols + missing_cols]
    
    alloc = pd.DataFrame(s.get("allocation", []))
    sched = pd.DataFrame(s.get("schedule", []))
    return works, rehearsals, alloc, sched


# ----------------------------
# Auth helper
# ----------------------------
def require_edit_token():
    tok = request.args.get("token") or request.headers.get("X-Edit-Token")
    if tok != EDIT_TOKEN:
        abort(403)



META_COLS = {
    "title", "composer", "duration", "difficulty", "soloist",
    "notes", "piece", "work"
}

def infer_instrument_columns(works_df: pd.DataFrame) -> list[str]:
    cols = []
    for c in works_df.columns:
        key = str(c).strip().lower()
        if key in META_COLS:
            continue
        # if any value looks numeric, treat as instrument demand col
        series = works_df[c]
        # coerce to numeric, ignore blanks
        numeric = pd.to_numeric(series.replace("", pd.NA), errors="coerce")
        if numeric.notna().any():
            cols.append(c)
    return cols

def find_duration_col(cols):
    # common variants
    candidates = ["duration", "mins", "minutes", "time", "length"]
    for c in cols:
        if str(c).strip().lower() in candidates:
            return c
    return None


# ----------------------------
# Core compute helpers (allocator + schedule + timing)
# ----------------------------
def prepare_works_for_allocator(works_df: pd.DataFrame) -> pd.DataFrame:
    w = works_df.copy()
    
    # Ensure Title column exists
    if "Title" not in w.columns:
        raise ValueError("Works table must have a 'Title' column")
    
    w["Title"] = w["Title"].astype(str).str.strip()
    w = w[w["Title"].str.len() > 0].copy()

    if w.empty:
        raise ValueError("Works table has no valid works (all titles are empty)")

    w["Duration"] = pd.to_numeric(w["Duration"], errors="coerce").fillna(0.0)
    w["Difficulty"] = pd.to_numeric(w["Difficulty"], errors="coerce").fillna(1.0)

    try:
        w2 = mod1.normalise_works_columns(w)
    except Exception:
        w2 = w.copy()
        w2["duration_norm"] = w2["Duration"].astype(float)
        w2["difficulty_norm"] = w2["Difficulty"].astype(float).clip(lower=0.1)

    for c in (
        mod1.WIND_COLS + mod1.BRASS_COLS + mod1.STRING_COLS +
        mod1.PERC_COLS + mod1.PIANO_COLS + mod1.HARP_COLS + mod1.SOLOIST_COLS
    ):
        if c not in w2.columns:
            w2[c] = 0
    return w2.reset_index(drop=True)


def prepare_all_rehearsals(rehearsals_df: pd.DataFrame) -> pd.DataFrame:
    """Prepare all rehearsals (including non-allocated ones) for timed schedule generation.
    Unlike prepare_rehearsals_for_allocator, this does NOT filter by "Include in allocation"."""
    r = rehearsals_df.copy()
    print(f"[TIMING] Input rehearsals: {len(r)} rows")
    
    # Ensure required columns exist
    if "Rehearsal" not in r.columns:
        r = r.reindex(columns=default_rehearsals_df().columns, fill_value="")
        print(f"[TIMING] WARNING: 'Rehearsal' column was missing, reindexed with defaults")
    
    # Process rehearsal numbers
    r = r[r["Rehearsal"].astype(str).str.strip().ne("")].copy()
    r["Rehearsal"] = pd.to_numeric(r["Rehearsal"], errors="coerce").astype("Int64")
    r = r[r["Rehearsal"].notna()].copy()
    
    # DO NOT FILTER by "Include in allocation" - we want all rehearsals for the timed schedule
    print(f"[TIMING] Processing {len(r)} rehearsals (including non-allocated)")
    
    # Debug: Show Date column before conversion
    print(f"[TIMING] Date column before conversion:")
    for idx, row in r.iterrows():
        reh_num = row.get("Rehearsal")
        date_val = row.get("Date")
        print(f"  Rehearsal {reh_num}: Date = {repr(date_val)} (type: {type(date_val).__name__})")
    
    r["Date"] = pd.to_datetime(r.get("Date"), errors="coerce").dt.date  # type: ignore
    
    # Debug: Show Date column after conversion
    print(f"[TIMING] Date column after conversion:")
    for idx, row in r.iterrows():
        reh_num = row.get("Rehearsal")
        date_val = row.get("Date")
        print(f"  Rehearsal {reh_num}: Date = {repr(date_val)} (type: {type(date_val).__name__})")

    start_min = r.get("Start Time").apply(minutes_from_timecell) if "Start Time" in r.columns else pd.Series([None]*len(r))  # type: ignore
    end_min = r.get("End Time").apply(minutes_from_timecell) if "End Time" in r.columns else pd.Series([None]*len(r))  # type: ignore
    start_min = start_min.fillna(19 * 60).astype(int)
    end_min = end_min.fillna(21 * 60 + 30).astype(int)

    gross = (end_min - start_min).astype(int)
    gross = gross.where(gross >= 0, gross + 24 * 60)

    br = r.get("Break").apply(parse_break_minutes) if "Break" in r.columns else pd.Series([0]*len(r))  # type: ignore
    r["Break (minutes)"] = br.astype(int)
    r["Duration"] = (gross - r["Break (minutes)"]).clip(lower=0).astype(int)

    for c in ["Percs", "Piano", "Harp", "Brass", "Soloist"]:
        if c not in r.columns:
            r[c] = False
        r[c] = r[c].apply(parse_truthy)

    hh = (start_min // 60).astype(int).astype(str).str.zfill(2)
    mm = (start_min % 60).astype(int).astype(str).str.zfill(2)
    hhmm = hh + ":" + mm
    date_str = np.where(pd.notna(pd.Series(r["Date"])), pd.Series(r["Date"]).astype(str), "2000-01-01")
    r["Start DateTime"] = pd.to_datetime(date_str + " " + hhmm, errors="coerce").fillna(pd.to_datetime("2000-01-01 19:00"))

    # Preserve Section column if it exists, otherwise default to "Full Ensemble"
    if "Section" not in r.columns:
        r["Section"] = "Full Ensemble"
    else:
        r["Section"] = r["Section"].fillna("Full Ensemble").astype(str)
        r.loc[r["Section"].str.strip() == "", "Section"] = "Full Ensemble"

    return r.sort_values("Rehearsal").reset_index(drop=True)


def prepare_rehearsals_for_allocator(rehearsals_df: pd.DataFrame) -> pd.DataFrame:
    r = rehearsals_df.copy()
    print(f"[ALLOCATOR] Input rehearsals: {len(r)} rows")
    
    # Ensure required columns exist
    if "Rehearsal" not in r.columns:
        # Reindex to include all default columns
        r = r.reindex(columns=default_rehearsals_df().columns, fill_value="")
        print(f"[ALLOCATOR] WARNING: 'Rehearsal' column was missing, reindexed with defaults")
    
    if len(r) > 0:
        print(f"[ALLOCATOR] Include in allocation values: {r.get('Include in allocation', pd.Series()).unique().tolist()}")
    
    r = r[r["Rehearsal"].astype(str).str.strip().ne("")].copy()
    r["Rehearsal"] = pd.to_numeric(r["Rehearsal"], errors="coerce").astype("Int64")
    r = r[r["Rehearsal"].notna()].copy()
    
    # Filter out rehearsals where "Include in allocation" is not "Y"
    if "Include in allocation" in r.columns:
        print(f"[ALLOCATOR] Before filtering: {len(r)} rehearsals")
        r = r[r["Include in allocation"].apply(parse_truthy)].copy()
        print(f"[ALLOCATOR] After filtering: {len(r)} rehearsals")
    
    r["Date"] = pd.to_datetime(r.get("Date"), errors="coerce").dt.date  # type: ignore

    start_min = r.get("Start Time").apply(minutes_from_timecell) if "Start Time" in r.columns else pd.Series([None]*len(r))  # type: ignore
    end_min = r.get("End Time").apply(minutes_from_timecell) if "End Time" in r.columns else pd.Series([None]*len(r))  # type: ignore
    start_min = start_min.fillna(19 * 60).astype(int)
    end_min = end_min.fillna(21 * 60 + 30).astype(int)

    gross = (end_min - start_min).astype(int)
    gross = gross.where(gross >= 0, gross + 24 * 60)

    br = r.get("Break").apply(parse_break_minutes) if "Break" in r.columns else pd.Series([0]*len(r))  # type: ignore
    r["Break (minutes)"] = br.astype(int)
    r["Duration"] = (gross - r["Break (minutes)"]).clip(lower=0).astype(int)

    for c in ["Percs", "Piano", "Harp", "Brass", "Soloist"]:
        if c not in r.columns:
            r[c] = False
        r[c] = r[c].apply(parse_truthy)

    hh = (start_min // 60).astype(int).astype(str).str.zfill(2)
    mm = (start_min % 60).astype(int).astype(str).str.zfill(2)
    hhmm = hh + ":" + mm
    date_str = np.where(pd.notna(pd.Series(r["Date"])), pd.Series(r["Date"]).astype(str), "2000-01-01")
    r["Start DateTime"] = pd.to_datetime(date_str + " " + hhmm, errors="coerce").fillna(pd.to_datetime("2000-01-01 19:00"))

    # Preserve Section column if it exists, otherwise default to "Full Ensemble"
    if "Section" not in r.columns:
        r["Section"] = "Full Ensemble"
    else:
        r["Section"] = r["Section"].fillna("Full Ensemble").astype(str)
        r.loc[r["Section"].str.strip() == "", "Section"] = "Full Ensemble"

    return r.sort_values("Rehearsal").reset_index(drop=True)
def run_allocation_compute(works_df: pd.DataFrame, rehearsals_df: pd.DataFrame, G: int) -> Tuple[pd.DataFrame, List[str]]:
    works = prepare_works_for_allocator(works_df)
    rehe = prepare_rehearsals_for_allocator(rehearsals_df)
    
    print(f"[ALLOCATION] Works count: {len(works)}, Rehearsals count: {len(rehe)}")
    if len(rehe) > 0:
        print(f"[ALLOCATION] Rehearsal numbers: {rehe['Rehearsal'].unique().tolist()}")
        print(f"[ALLOCATION] First rehearsal: {rehe.iloc[0].to_dict() if len(rehe) > 0 else 'N/A'}")

    if works.empty or rehe.empty:
        raise ValueError("Need at least 1 work and 1 rehearsal.")

    if rehe["Rehearsal"].nunique() < 2:
        raise ValueError("Need at least 2 rehearsals (first/last constraint).")

    tokens_per = (rehe["Duration"].astype(float) // G).astype(int)
    snapped_caps = (tokens_per * G).astype(int)
    snapped_total = int(snapped_caps.sum())
    if snapped_total <= 0:
        raise ValueError("Total capacity is 0 minutes after snapping. Check times/breaks.")

    req = mod1.compute_required_minutes(works, snapped_total, G)
    export_df, warnings = mod1.allocate_across_rehearsals(works, rehe, req, G)
    return export_df, list(warnings or [])


def estimate_playerload_map(works_df: pd.DataFrame) -> Dict[str, float]:
    works = works_df.copy()
    works["Title"] = works["Title"].astype(str)
    groups = mod2.gather_resolved_groups(works)
    out = {}
    if "Title" not in works.columns:
        return out
    for _, row in works.iterrows():
        t = str(row.get("Title", "")).strip()
        if not t:
            continue
        out[t] = float(mod2.estimate_player_load(row, groups))
    return out


def group_hint_map(works_df: pd.DataFrame) -> Dict[str, Optional[str]]:
    hint = {}
    try:
        group_col = mod2._first_matching_col(works_df, mod2.GROUP_ALIASES)
    except Exception:
        group_col = None
    if group_col and group_col in works_df.columns:
        for _, r in works_df.iterrows():
            t = str(r.get("Title", "")).strip()
            if not t:
                continue
            v = r.get(group_col)
            hint[t] = str(v).strip() if pd.notna(v) and str(v).strip() else None
    return hint


def parse_group_and_movement(title: str, group_hint: Optional[str]):
    try:
        return mod2.parse_group_and_movement(title, group_hint)
    except Exception:
        s = str(title).strip()
        group = group_hint.strip() if group_hint else s.split(":")[0].strip()
        return group or s, None, None


def build_signature_map(works_df: pd.DataFrame) -> Dict[str, Dict[str, int]]:
    sig_map: Dict[str, Dict[str, int]] = {}
    for _, r in works_df.iterrows():
        title = str(r.get("Title", "")).strip()
        if not title:
            continue
        try:
            sig = mod3.signature_for_work(r)
        except Exception:
            sig = {"Percs": 0, "PercProfile": 0, "Piano": 0, "Harp": 0, "Winds": 0, "Brass": 0, "Strings": 0}
        sig_map[title] = {k: int(sig.get(k, 0)) for k in ["Percs","PercProfile","Piano","Harp","Winds","Brass","Strings"]}
    return sig_map


@dataclass
class Bundle:
    key: str
    items: pd.DataFrame
    mins: int
    playerload: float
    sig: Dict[str, int]


def build_bundles_for_rehearsal(df_r: pd.DataFrame, sig_map: Dict[str, Dict[str, int]]) -> List[Bundle]:
    bundles: List[Bundle] = []
    for gk, grp in df_r.groupby("GroupKey", sort=False):
        grp2 = grp.copy()
        if "MovementOrder" in grp2.columns:
            grp2["MovementOrder"] = pd.to_numeric(grp2["MovementOrder"], errors="coerce")
            grp2 = grp2.sort_values(["MovementOrder", "Title"], na_position="last")
        mins = int(pd.to_numeric(grp2["Rehearsal Time (minutes)"], errors="coerce").fillna(0).sum())
        playerload = float(pd.to_numeric(grp2["PlayerLoad"], errors="coerce").fillna(0).max())

        sig = {"Percs": 0, "PercProfile": 0, "Piano": 0, "Harp": 0, "Winds": 0, "Brass": 0, "Strings": 0}
        for t in grp2["Title"].astype(str).tolist():
            s = sig_map.get(t)
            if not s:
                continue
            for k in sig.keys():
                sig[k] = max(int(sig[k]), int(s.get(k, 0)))
        bundles.append(Bundle(key=str(gk), items=grp2, mins=mins, playerload=playerload, sig=sig))
    return bundles


def order_bundles_descending_load_with_similarity(
    bundles: List[Bundle],
    transition_cost_fn,
    increase_penalty_weight: float = 100.0
) -> List[Bundle]:
    if not bundles:
        return []
    remaining = bundles[:]
    remaining.sort(key=lambda b: (b.playerload, b.mins), reverse=True)
    ordered = [remaining.pop(0)]

    while remaining:
        last = ordered[-1]
        last_load = last.playerload
        best_i = 0
        best_key = None
        for i, cand in enumerate(remaining):
            inc = max(0.0, cand.playerload - last_load)
            inc_pen = inc * increase_penalty_weight
            tc = transition_cost_fn(last.sig, cand.sig)
            key = (inc_pen, tc, -cand.playerload, -cand.mins)
            if best_key is None or key < best_key:
                best_key = key
                best_i = i
        ordered.append(remaining.pop(best_i))

    return ordered


def build_schedule_from_allocation(works_df: pd.DataFrame, alloc_df: pd.DataFrame) -> pd.DataFrame:
    alloc = alloc_df.copy()
    alloc = alloc[~alloc["Title"].astype(str).str.startswith("[Summary]")].copy()

    reh_cols = [c for c in alloc.columns if str(c).strip().startswith("Rehearsal ")]
    col_to_num = {}
    for c in reh_cols:
        try:
            col_to_num[c] = int(str(c).split()[-1])
        except Exception:
            pass
    if not col_to_num:
        raise ValueError("No 'Rehearsal N' columns found in allocation output.")

    pl_map = estimate_playerload_map(works_df)
    gh_map = group_hint_map(works_df)
    sig_map = build_signature_map(prepare_works_for_allocator(works_df))  # signature_for_work expects normalized-like row

    rows = []
    for _, r in alloc.iterrows():
        title = str(r.get("Title", "")).strip()
        if not title:
            continue
        for col, rnum in col_to_num.items():
            mins = pd.to_numeric(r.get(col, 0), errors="coerce")
            if pd.isna(mins) or mins <= 0:
                continue
            group_hint = gh_map.get(title)
            group_title, _, mov_ord = parse_group_and_movement(title, group_hint)

            rows.append({
                "Rehearsal": int(rnum),
                "Title": title,
                "Rehearsal Time (minutes)": int(round(float(mins))),
                "PlayerLoad": float(pl_map.get(title, 0.0)),
                "GroupKey": str(group_title),
                "MovementOrder": mov_ord if mov_ord is not None else np.nan,
            })

    if not rows:
        raise ValueError("No scheduled minutes found to build a schedule.")

    sched = pd.DataFrame(rows)
    sched["Rehearsal"] = pd.to_numeric(sched["Rehearsal"], errors="coerce").astype("Int64")
    sched["MovementOrder"] = pd.to_numeric(sched["MovementOrder"], errors="coerce")

    ordered_rows = []
    for rnum, df_r in sched.groupby("Rehearsal", sort=True):
        bundles = build_bundles_for_rehearsal(df_r, sig_map)
        ordered_bundles = order_bundles_descending_load_with_similarity(
            bundles,
            transition_cost_fn=mod3.transition_cost,
            increase_penalty_weight=100.0,
        )
        for b in ordered_bundles:
            ordered_rows.append(b.items)

    return pd.concat(ordered_rows, ignore_index=True)


def choose_break_offset_favor_longer_first_half(durations: List[int]) -> int:
    if not durations:
        return 0
    boundaries = [0]
    for m in durations:
        boundaries.append(boundaries[-1] + int(m))
    total = boundaries[-1]
    if len(boundaries) <= 2:
        return 0
    ideal = total / 2.0
    candidates = range(1, len(boundaries) - 1)

    def key(i: int):
        b = boundaries[i]
        return (abs(2 * b - total), 0 if b >= ideal else 1, -b)

    best_idx = min(candidates, key=key)
    return int(boundaries[best_idx])


def compute_timed_df(schedule_df: pd.DataFrame, rehearsals_prepared: pd.DataFrame) -> pd.DataFrame:
    if schedule_df.empty and rehearsals_prepared.empty:
        return pd.DataFrame()

    rehe = rehearsals_prepared.set_index("Rehearsal", drop=False)
    out_rows = []
    processed_rehearsals = set()

    # Process rehearsals that have allocated works
    for rnum, df_r in schedule_df.groupby("Rehearsal", sort=True):
        rnum_i = int(rnum)  # type: ignore
        processed_rehearsals.add(rnum_i)
        
        if rnum_i not in rehe.index:
            continue

        rrow = rehe.loc[rnum_i]
        
        # Get Event Type from the rehearsal row
        event_type = str(rrow.get("Event Type", "Rehearsal")).strip() if pd.notna(rrow.get("Event Type")) else "Rehearsal"
        if not event_type:
            event_type = "Rehearsal"
        
        # Skip adding scheduled works to concerts - concerts only get placeholder rows
        if event_type == "Concert":
            continue
        
        date = rrow.get("Date")
        start_dt = pd.to_datetime(rrow.get("Start DateTime"), errors="coerce")  # type: ignore
        if pd.isna(start_dt):
            start_dt = pd.to_datetime("2000-01-01 19:00")

        break_mins = safe_int(rrow.get("Break (minutes)", 0), 0)
        
        # Get Section from the rehearsal row
        section = str(rrow.get("Section", "Full Ensemble")).strip() if pd.notna(rrow.get("Section")) else "Full Ensemble"
        if not section:
            section = "Full Ensemble"

        durs = [safe_int(x, 0) for x in df_r["Rehearsal Time (minutes)"].tolist()]
        durs = [d for d in durs if d > 0]

        break_offset = 0
        if break_mins > 0 and len(durs) >= 2:
            break_offset = choose_break_offset_favor_longer_first_half(durs)

        elapsed = 0

        for _, item in df_r.iterrows():
            mins = safe_int(item["Rehearsal Time (minutes)"], 0)
            if mins <= 0:
                continue

            if break_mins > 0 and break_offset > 0 and elapsed == break_offset:
                br_start = start_dt + pd.Timedelta(minutes=elapsed)
                br_end = br_start + pd.Timedelta(minutes=break_mins)
                out_rows.append({
                    "Rehearsal": rnum_i,
                    "Date": date,
                    "Title": "Break",
                    "Time in Rehearsal": br_start.strftime("%H:%M"),
                    "Break Start (HH:MM)": br_start.strftime("%H:%M"),
                    "Break End (HH:MM)": br_end.strftime("%H:%M"),
                    "Section": section,
                    "Event Type": event_type,
                })
                elapsed += break_mins

            it_start = start_dt + pd.Timedelta(minutes=elapsed)
            out_rows.append({
                "Rehearsal": rnum_i,
                "Date": date,
                "Title": str(item["Title"]),
                "Time in Rehearsal": it_start.strftime("%H:%M"),
                "Break Start (HH:MM)": "",
                "Break End (HH:MM)": "",
                "Section": section,
                "Event Type": event_type,
            })
            elapsed += mins

    # Add placeholder rows for rehearsals without allocated works (e.g., sectionals)
    print(f"[TIMED] Processed rehearsals: {processed_rehearsals}")
    print(f"[TIMED] All rehearsals in prepared data: {rehearsals_prepared['Rehearsal'].unique().tolist()}")
    
    for _, rrow in rehearsals_prepared.iterrows():
        rnum_i = safe_int(rrow.get("Rehearsal"), None)
        if rnum_i is None or rnum_i in processed_rehearsals:
            continue
        
        print(f"[TIMED] Adding placeholder for rehearsal {rnum_i}")
        
        date = rrow.get("Date")
        print(f"[TIMED]   Date from rrow: {repr(date)} (type: {type(date).__name__})")
        
        # Ensure date is in a format that pd.to_datetime can handle later
        # Handle NaT explicitly
        if pd.isna(date):
            print(f"[TIMED]   Date is NaT/NaN - using placeholder date")
            date_for_placeholder = "2000-01-01"  # Fallback date
        elif isinstance(date, str):
            # Keep string as-is (e.g., "2026-02-15")
            date_for_placeholder = date
        elif hasattr(date, 'strftime'):
            # Convert date/datetime objects to ISO string
            date_for_placeholder = date.strftime("%Y-%m-%d")
        else:
            date_for_placeholder = str(date)
        
        print(f"[TIMED]   Date for placeholder: {repr(date_for_placeholder)} (type: {type(date_for_placeholder).__name__})")
        
        start_dt = pd.to_datetime(rrow.get("Start DateTime"), errors="coerce")
        if pd.isna(start_dt):
            # Use the actual date from rehearsal and default time
            start_time_str = rrow.get("Start Time", "14:00")
            if pd.notna(start_time_str) and start_time_str:
                start_dt = pd.to_datetime(f"{date_for_placeholder} {start_time_str}", errors="coerce")
            if pd.isna(start_dt):
                start_dt = pd.to_datetime(f"{date_for_placeholder} 14:00")
        
        section = str(rrow.get("Section", "Full Ensemble")).strip() if pd.notna(rrow.get("Section")) else "Full Ensemble"
        if not section:
            section = "Full Ensemble"
        
        event_type = str(rrow.get("Event Type", "Rehearsal")).strip() if pd.notna(rrow.get("Event Type")) else "Rehearsal"
        if not event_type:
            event_type = "Rehearsal"
        
        # Determine display title based on event type
        if event_type == "Concert":
            display_title = "Concert"
        elif event_type == "Sectional":
            display_title = "Sectional Rehearsal"
        else:
            display_title = "Rehearsal"
        
        # Add a placeholder row for this rehearsal/concert/sectional
        # For concerts, this creates a visible column in the timeline editor
        out_rows.append({
            "Rehearsal": rnum_i,
            "Date": date_for_placeholder,  # Use normalized date
            "Title": display_title,
            "Time in Rehearsal": start_dt.strftime("%H:%M"),
            "Break Start (HH:MM)": "",
            "Break End (HH:MM)": "",
            "Section": section,
            "Event Type": event_type,
        })

    out = pd.DataFrame(out_rows)
    print(f"[TIMED] Before date conversion, sample dates: {[r.get('Date') for r in out_rows[:3]]}")
    out["Date"] = pd.to_datetime(out["Date"], errors="coerce")
    print(f"[TIMED] After date conversion, sample dates: {out['Date'].head(3).tolist()}")
    print(f"[TIMED] NaT count in Date column: {out['Date'].isna().sum()}")
    out["Rehearsal"] = pd.to_numeric(out["Rehearsal"], errors="coerce").astype("Int64")
    return out


# ----------------------------
# Routes
# ----------------------------

from flask import request, redirect, url_for

@app.before_request
def require_login_for_everything():
    # allow static assets
    if request.endpoint == "static":
        return None

    # allow login/register endpoints
    allowed = {
        "login_view", "login_post",
        "register_view", "register_post",
    }
    if request.endpoint in allowed:
        return None

    # allow None endpoints (very rare) without crashing
    if request.endpoint is None:
        return None

    # if not logged in, bounce to login
    if not current_user():
        return redirect(url_for("login_view", next=request.path))


@app.get("/api/s/<schedule_id>/state")
def api_schedule_state(schedule_id):
    r = admin_required_or_403()
    if r: return r
    s = load_schedule(schedule_id)
    if not s: abort(404)
    
    # Debug: Log that Event Type is being passed through
    rehearsals = s.get("rehearsals", [])
    if rehearsals and len(rehearsals) > 0:
        first_reh = rehearsals[0]
        has_event_type = "Event Type" in first_reh
        print(f"[API STATE] Returning {len(rehearsals)} rehearsals, Event Type present: {has_event_type}, first reh: {list(first_reh.keys())[:5]}...")
    
    # Get timed data and add concert_id links (same as /data endpoint)
    timed_data = s.get("timed", [])
    print(f"\n[API STATE] Schedule: {schedule_id}")
    print(f"[API STATE] Timed data rows: {len(timed_data)}")
    print(f"[API STATE] Rehearsals: {len(rehearsals)}")
    
    # Add concert_id to timed rows for concert rehearsals
    ensembles = load_ensembles()
    print(f"[API STATE] Loaded {len(ensembles)} ensembles")
    ensemble = next((e for e in ensembles if e["id"] == s.get("ensemble_id")), None)
    print(f"[API STATE] Looking for ensemble_id: {s.get('ensemble_id')}")
    print(f"[API STATE] Found ensemble: {ensemble['name'] if ensemble else 'None'}")
    
    schedule_concerts = []  # Initialize outside if block
    if ensemble:
        concerts = ensemble.get("concerts", [])
        print(f"[API STATE] Total concerts in ensemble: {len(concerts)}")
        print(f"[API STATE] All concerts: {[{'id': c.get('id'), 'title': c.get('title'), 'schedule_id': c.get('schedule_id')} for c in concerts]}")
        schedule_concerts = [c for c in concerts if c.get("schedule_id") == schedule_id]
        print(f"[API STATE] Concerts for schedule {schedule_id}: {len(schedule_concerts)}")
        for c in schedule_concerts:
            print(f"  - {c.get('id')}: {c.get('title')} on {c.get('date')}")
        # Link concerts into rehearsals by concert_id or by date as a fallback
        if rehearsals:
            for reh in rehearsals:
                if reh.get("Event Type") == "Concert":
                    # If rehearsal already has concert_id, ensure it exists; otherwise, try to find by date
                    cid = reh.get("concert_id")
                    concert = None
                    if cid:
                        concert = next((c for c in schedule_concerts if c.get("id") == cid), None)
                    if not concert:
                        reh_date_norm = str(reh.get("Date", ""))[:10]
                        concert = next((c for c in schedule_concerts if str(c.get("date", ""))[:10] == reh_date_norm), None)
                        if concert:
                            reh["concert_id"] = concert.get("id")
                    # If we found a concert, push master fields into rehearsal for display consistency
                    if concert:
                        reh["Date"] = concert.get("date", reh.get("Date"))
                        reh["Start Time"] = concert.get("time", reh.get("Start Time"))
                        reh["Venue"] = concert.get("venue", reh.get("Venue", ""))
                        reh["Uniform"] = concert.get("uniform", reh.get("Uniform", ""))
                    else:
                        print(f"[API STATE] Concert rehearsal without linked concert: Rehearsal {reh.get('Rehearsal')} Date {reh.get('Date')}")

            # Also propagate concert_id into timed rows
            for timed_row in timed_data:
                reh_num = timed_row.get("Rehearsal")
                if reh_num:
                    reh = next((r for r in rehearsals if int(r.get("Rehearsal", 0)) == reh_num), None)
                    if reh and reh.get("Event Type") == "Concert":
                        cid = reh.get("concert_id")
                        if cid:
                            timed_row["concert_id"] = cid
    
    return jsonify({
        "works_cols": s.get("works_cols", []),
        "rehearsals_cols": s.get("rehearsals_cols", []),
        "works": s.get("works", []),
        "rehearsals": s.get("rehearsals", []),
        "allocation": s.get("allocation", []),
        "schedule": s.get("schedule", []),
        "timed": timed_data,
        "concerts": schedule_concerts,  # Always use schedule_concerts (initialized above)
        "ensemble_id": s.get("ensemble_id"),
    })

@app.get("/api/s/<schedule_id>/data")
def api_schedule_data(schedule_id):
    """Get current schedule data (timed, rehearsals, schedule, allocation)."""
    r = admin_required_or_403()
    if r: return r
    
    s = load_schedule(schedule_id)
    if not s: abort(404)
    
    # Clean the timed data before returning
    timed_data = clean_timed_data(s.get("timed", []))
    
    # Add concert_id to timed rows for concert rehearsals
    rehearsals_list = s.get("rehearsals", [])
    ensembles = load_ensembles()
    ensemble = next((e for e in ensembles if e["id"] == s.get("ensemble_id")), None)
    
    schedule_concerts = []
    if ensemble and rehearsals_list:
        # Build a map of rehearsal date -> concert
        concerts = ensemble.get("concerts", [])
        schedule_concerts = [c for c in concerts if c.get("schedule_id") == schedule_id]
        
        for timed_row in timed_data:
            reh_num = timed_row.get("Rehearsal")
            if reh_num:
                # Find rehearsal by number
                reh = next((r for r in rehearsals_list if int(r.get("Rehearsal", 0)) == reh_num), None)
                if reh and reh.get("Event Type") == "Concert":
                    # Find the concert for this rehearsal by matching date
                    reh_date = reh.get("Date")
                    # Normalize dates for comparison (strip time component)
                    reh_date_normalized = str(reh_date)[:10] if reh_date else None
                    
                    # Try to match concert by date (comparing just YYYY-MM-DD)
                    concert = None
                    for c in concerts:
                        concert_date_normalized = str(c.get("date", ""))[:10]
                        if concert_date_normalized == reh_date_normalized and c.get("schedule_id") == schedule_id:
                            concert = c
                            break
                    
                    if concert:
                        timed_row["concert_id"] = concert.get("id")
    
    return jsonify({
        "timed": timed_data,
        "rehearsals": s.get("rehearsals", []),
        "schedule": s.get("schedule", []),
        "allocation": s.get("allocation", []),
        "concerts": schedule_concerts,  # Add concerts for editor
    })


@app.get("/api/s/<schedule_id>/concerts")
def api_schedule_concerts(schedule_id):
    """Get concerts linked to this schedule."""
    r = admin_required_or_403()
    if r: return r
    
    s = load_schedule(schedule_id)
    if not s: abort(404)
    
    # Get linked concerts from the ensemble
    ensembles = load_ensembles()
    ensemble = next((e for e in ensembles if e["id"] == s.get("ensemble_id")), None)
    
    if not ensemble:
        return jsonify({"concerts": []})
    
    # Get rehearsal dates from this schedule for matching
    s = load_schedule(schedule_id)
    rehearsal_dates = set()
    if s:
        for reh in s.get("rehearsals", []):
            if reh.get("Date"):
                try:
                    dt = pd.to_datetime(reh.get("Date"))
                    rehearsal_dates.add(dt.strftime("%Y-%m-%d 00:00:00"))
                except:
                    pass
    
    # Include concerts that match this schedule_id OR have dates matching rehearsals in this schedule
    all_concerts = ensemble.get("concerts", [])
    linked_concerts = []
    for c in all_concerts:
        if c.get("status") == "scheduled":
            concert_date_str = c.get("date", "")
            if c.get("schedule_id") == schedule_id or concert_date_str in rehearsal_dates:
                linked_concerts.append(c)
    
    return jsonify({"concerts": linked_concerts})


@app.put("/api/s/<schedule_id>/concert/<concert_id>")
def api_update_schedule_concert(schedule_id, concert_id):
    """Update a concert linked to this schedule."""
    r = admin_required_or_403()
    if r: return r
    
    s = load_schedule(schedule_id)
    if not s: abort(404)
    
    data = request.get_json() or {}
    ensemble_id = s.get("ensemble_id")
    
    # Update the concert
    concert = update_concert(ensemble_id, concert_id, data)
    if not concert:
        return jsonify({"ok": False, "error": "Concert not found"}), 404

    # Ensure response JSON has no NaN/inf values that would break JSON.parse in the client
    concert_clean = {k: _json_safe(v) for k, v in concert.items()}
    return jsonify({"ok": True, "concert": concert_clean})


@app.post("/api/s/<schedule_id>/save_inputs")
def api_schedule_save_inputs(schedule_id):
    r = admin_required_or_403()
    if r: return r
    payload = request.get_json(force=True)

    s = load_schedule(schedule_id)
    if not s: abort(404)

    # PERSISTENCE FIX: Ensure all values are strings to prevent loss of data
    works = payload.get("works", [])
    rehearsals = payload.get("rehearsals", [])
    
    # Sanitize works - ensure all values are strings
    works = [{k: (str(v) if v not in (None, "") else "") for k, v in row.items()} for row in works]
    rehearsals = [{k: (str(v) if v not in (None, "") else "") for k, v in row.items()} for row in rehearsals]
    
    s["works"] = works
    s["rehearsals"] = rehearsals
    # PERSISTENCE FIX: Save column metadata so it persists across page reloads
    if payload.get("works_cols"):
        s["works_cols"] = payload.get("works_cols")
    if payload.get("rehearsals_cols"):
        s["rehearsals_cols"] = payload.get("rehearsals_cols")
    if payload.get("clear_computed"):
        s["allocation"] = []
        s["schedule"] = []
        s["timed"] = []
    
    # Sync concert dates: update ensemble.concerts when concert rehearsal dates change
    ensembles = load_ensembles()
    ensemble = next((e for e in ensembles if e["id"] == s.get("ensemble_id")), None)
    if ensemble and "concerts" in ensemble:
        for reh in rehearsals:
            event_type = reh.get("Event Type", "")
            if event_type == "Concert":
                reh_date = reh.get("Date", "")
                concert_id = reh.get("concert_id")
                
                # Normalize the date
                if reh_date:
                    try:
                        dt = pd.to_datetime(reh_date)
                        normalized_date = dt.strftime("%Y-%m-%d 00:00:00")
                    except:
                        normalized_date = str(reh_date)
                    
                    # Find and update the concert
                    if concert_id:
                        concert = next((c for c in ensemble["concerts"] if c.get("id") == concert_id), None)
                        if concert:
                            print(f"[SAVE INPUTS] Updating concert {concert_id} date from {concert.get('date')} to {normalized_date}")
                            concert["date"] = normalized_date
        
        save_ensembles(ensembles)
    
    s["updated_at"] = int(time.time())
    save_schedule(s)
    return jsonify({"ok": True})


@app.post("/api/s/<schedule_id>/run_allocation")
def api_schedule_run_allocation(schedule_id):
    r = admin_required_or_403()
    if r: return r

    s = load_schedule(schedule_id)
    if not s: abort(404)

    works, rehearsals, _, _ = get_frames(s)
    G = int(s.get("G", DEFAULT_G))
    
    # Filter to only include rehearsals where Event Type is "Rehearsal"
    # This excludes Sectionals and Concerts from allocation
    print(f"[ALLOCATION] Pre-filter rehearsals: {len(rehearsals)} rows")
    rehearsals_for_allocation = rehearsals[rehearsals['Event Type'].str.lower() == 'rehearsal'].copy()
    print(f"[ALLOCATION] Post-filter rehearsals (Event Type='Rehearsal' only): {len(rehearsals_for_allocation)} rows")
    if len(rehearsals_for_allocation) < 2:
        return jsonify({
            "ok": False, 
            "error": f"Need at least 2 'Rehearsal' type events for allocation. Found {len(rehearsals_for_allocation)} rehearsals (Sectionals and Concerts are excluded from allocation)."
        }), 400

    try:
        alloc_df, warnings = run_allocation_compute(works, rehearsals_for_allocation, G)
        s["allocation"] = sanitize_df_records(alloc_df.to_dict(orient="records"))
        s["warnings"] = warnings
        s["updated_at"] = int(time.time())
        save_schedule(s)
        return jsonify({"ok": True, "warnings": warnings})
    except ValueError as e:
        print(f"[ALLOCATION] ValueError: {str(e)}")
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        import traceback
        error_detail = traceback.format_exc()
        print(f"[ALLOCATION] Exception: {str(e)}\n{error_detail}")
        return jsonify({"ok": False, "error": f"Allocation computation failed: {str(e)}"}), 500


@app.post("/api/s/<schedule_id>/generate_schedule")
def api_schedule_generate(schedule_id):
    r = admin_required_or_403()
    if r: return r

    s = load_schedule(schedule_id)
    if not s: abort(404)

    works, rehearsals, alloc, _ = get_frames(s)
    if alloc.empty:
        return jsonify({"ok": False, "error": "No allocation yet"}), 400

    sched = build_schedule_from_allocation(works, alloc)
    s["schedule"] = sanitize_df_records(sched.to_dict(orient="records"))

    # Use prepare_all_rehearsals to include non-allocated rehearsals (sectionals)
    rehe_prep = prepare_all_rehearsals(rehearsals)
    timed = compute_timed_df(sched, rehe_prep)
    s["timed"] = sanitize_df_records(timed.to_dict(orient="records"))
    s["generated_at"] = pd.Timestamp.utcnow().isoformat() + "Z"
    s["updated_at"] = int(time.time())
    save_schedule(s)

    return jsonify({"ok": True})

@app.post("/api/s/<schedule_id>/import_csv")
def api_schedule_import_csv(schedule_id):
    r = admin_required_or_403()
    if r: return r

    kind = (request.args.get("kind") or "").lower()
    if kind not in {"works", "rehearsals"}:
        return jsonify({"ok": False, "error": "kind must be works or rehearsals"}), 400

    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file uploaded"}), 400

    f = request.files["file"]
    df = pd.read_csv(f.stream)  # type: ignore

    s = load_schedule(schedule_id)
    if not s: abort(404)

    if kind == "works":
        cols = list(default_works_df().columns)
        df = df.reindex(columns=[c for c in df.columns if c in cols])
        df = df.reindex(columns=cols, fill_value="")
        s["works"] = df.fillna("").to_dict(orient="records")
    else:
        cols = list(default_rehearsals_df().columns)
        df = df.reindex(columns=[c for c in df.columns if c in cols])
        df = df.reindex(columns=cols, fill_value="")
        rehearsals_data = df.fillna("").to_dict(orient="records")
        # Auto-number rehearsals if missing or empty
        rehearsals_data = auto_number_rehearsals(rehearsals_data)
        # Ensure Include in allocation column is present
        rehearsals_data = ensure_include_in_allocation_column(rehearsals_data)
        # Ensure Section column is present
        rehearsals_data = ensure_section_column(rehearsals_data)
        # Ensure Event Type column is present
        rehearsals_data = ensure_event_type_column(rehearsals_data)
        s["rehearsals"] = rehearsals_data

    # importing should clear computed artifacts
    s["allocation"] = []
    s["schedule"] = []
    s["timed"] = []
    s["updated_at"] = int(time.time())
    save_schedule(s)

    return jsonify({"ok": True})


@app.put("/api/s/<schedule_id>/timed_edit")
def api_timed_edit(schedule_id):
    """
    Update timed schedule with drag-reorder or resize edits.
    Payload: {
        "timed": [...updated timed rows...],
        "action": "reorder" | "resize" | "delete",
        "description": "human-readable description of change"
    }
    """
    r = admin_required_or_403()
    if r: return r

    s = load_schedule(schedule_id)
    if not s: abort(404)

    data = request.get_json() or {}
    timed_updates = data.get("timed", [])
    action = data.get("action", "edit")
    description = data.get("description", f"Timed edit ({action})")

    if not isinstance(timed_updates, list):
        return jsonify({"ok": False, "error": "timed must be a list"}), 400

    # Save current timed as history entry before updating
    if s.get("timed"):
        history_entry = {
            "timestamp": int(time.time()),
            "action": action,
            "description": description,
            "timed": s["timed"],  # snapshot before edit
        }
        if "timed_history" not in s:
            s["timed_history"] = []
        s["timed_history"].append(history_entry)
        # Keep last 50 versions to avoid unbounded growth
        if len(s["timed_history"]) > 50:
            s["timed_history"] = s["timed_history"][-50:]

    # Update timed schedule and clean up missing Section values
    timed_updates = clean_timed_data(timed_updates)
    s["timed"] = timed_updates
    s["updated_at"] = int(time.time())
    
    # Recalculate allocation from the new timed data
    # Group timed data by (Title, Rehearsal) and sum durations
    if timed_updates:
        timed_df = pd.DataFrame(timed_updates)
        
        # Build allocation: group by Title, then sum time per rehearsal
        alloc_rows = []
        
        # Get the original allocation to preserve "Required Minutes" from original
        orig_alloc = s.get("allocation", [])
        orig_by_title = {row.get("Title"): row for row in orig_alloc if row.get("Title") != "Break"}
        
        # Get all unique titles and rehearsals
        titles = set(timed_df[timed_df["Title"] != "Break"]["Title"].unique())
        rehearsals = sorted(set(int(r) for r in timed_df[timed_df["Title"] != "Break"]["Rehearsal"].unique()))
        
        for title in sorted(titles):
            alloc_row = {"Title": title}
            
            # Get Required Minutes from original allocation (cached persistent value)
            orig_row = orig_by_title.get(title, {})
            required_mins = orig_row.get("Required Minutes", 0)
            alloc_row["Required Minutes"] = required_mins
            
            # Sum time allocated in each rehearsal
            total_allocated = 0
            for rnum in rehearsals:
                col = f"Rehearsal {rnum}"
                time_in_reh = timed_df[(timed_df["Title"] == title) & (timed_df["Rehearsal"] == str(rnum))]["Rehearsal Time (minutes)"]
                
                # Handle both column names
                if time_in_reh.empty:
                    time_in_reh = timed_df[(timed_df["Title"] == title) & (timed_df["Rehearsal"] == str(rnum))]["Time in Rehearsal"]
                
                time_val = int(time_in_reh.sum()) if not time_in_reh.empty else 0
                alloc_row[col] = time_val
                total_allocated += time_val
            
            # Calculate Time Remaining
            alloc_row["Time Remaining"] = required_mins - total_allocated
            alloc_rows.append(alloc_row)
        
        s["allocation"] = alloc_rows
    
    # Update the schedule with new durations from timed, but preserve all other metadata
    # This keeps the original ordering and player load calculations intact
    if s.get("timed") and s.get("schedule"):
        timed_df = pd.DataFrame(s.get("timed", []))
        
        # Build a map of timed durations keyed by (Title, Rehearsal as int)
        timed_map = {}
        for _, row in timed_df.iterrows():
            title = row.get("Title", "").strip()
            rehearsal = int(row.get("Rehearsal", 0)) if row.get("Rehearsal") else 0
            # Use the duration column (could be "Rehearsal Time (minutes)" or "Time in Rehearsal")
            duration = row.get("Rehearsal Time (minutes)") or row.get("Time in Rehearsal") or 0
            if duration and not pd.isna(duration):
                try:
                    duration = int(duration)
                except (ValueError, TypeError):
                    duration = 0
            else:
                duration = 0
            timed_map[(title, rehearsal)] = duration
        
        print(f"DEBUG: Built timed_map with {len(timed_map)} entries")
        if timed_map:
            sample_keys = list(timed_map.keys())[:3]
            print(f"  Sample keys: {sample_keys}")
        
        # Update schedule rows with new durations, preserve all other columns
        updated_count = 0
        for sched_row in s.get("schedule", []):
            title = sched_row.get("Title", "").strip()
            if title != "Break":
                rehearsal = int(sched_row.get("Rehearsal", 0)) if sched_row.get("Rehearsal") else 0
                key = (title, rehearsal)
                
                if key in timed_map:
                    new_duration = timed_map[key]
                    # Update the duration columns
                    sched_row["Rehearsal Time (minutes)"] = new_duration
                    if "Time in Rehearsal" in sched_row:
                        sched_row["Time in Rehearsal"] = new_duration
                    updated_count += 1
                else:
                    print(f"  WARNING: No timed entry found for {key}")
        
        print(f"  Updated {updated_count} schedule rows with new durations")
    
    save_schedule(s)
    
    # Log for verification
    print(f"✓ Saved {len(timed_updates)} timed entries to schedule {schedule_id}")
    if timed_updates:
        print(f"  First 3 entries: {timed_updates[:3]}")
    print(f"  Updated allocation: {len(s.get('allocation', []))} rows")
    print(f"  Updated schedule with new durations: {len(s.get('schedule', []))} rows (preserved metadata)")

    return jsonify({"ok": True, "history_len": len(s.get("timed_history", []))})


@app.post("/api/s/<schedule_id>/timed_revert")
def api_timed_revert(schedule_id):
    """
    Revert timed schedule to a previous version from history.
    Payload: {"history_index": <int>}
    history_index=-1 means most recent, -2 means second most recent, etc.
    """
    r = admin_required_or_403()
    if r: return r

    s = load_schedule(schedule_id)
    if not s: abort(404)

    data = request.get_json() or {}
    history_index = data.get("history_index", -1)

    history = s.get("timed_history", [])
    if not history:
        return jsonify({"ok": False, "error": "No history available"}), 400

    if history_index < -len(history) or history_index >= 0:
        return jsonify({"ok": False, "error": "Invalid history index"}), 400

    # Save current state as a new history entry (for redo capability)
    if s.get("timed"):
        history_entry = {
            "timestamp": int(time.time()),
            "action": "revert",
            "description": f"Reverted to version {history_index}",
            "timed": s["timed"],
        }
        history.append(history_entry)

    # Restore the requested version
    target_entry = history[history_index]
    s["timed"] = target_entry["timed"]
    s["updated_at"] = int(time.time())
    save_schedule(s)

    return jsonify({"ok": True, "restored_at": target_entry["timestamp"]})


@app.get("/api/s/<schedule_id>/timed_history")
def api_timed_history(schedule_id):
    """Get list of timed edits with timestamps and descriptions."""
    r = admin_required_or_403()
    if r: return r

    s = load_schedule(schedule_id)
    if not s: abort(404)

    history = s.get("timed_history", [])
    # Return without the full 'timed' data, just metadata
    return jsonify([
        {
            "index": i - len(history),  # negative indices for reverting
            "timestamp": h["timestamp"],
            "action": h["action"],
            "description": h["description"],
        }
        for i, h in enumerate(history)
    ])


@app.put("/api/s/<schedule_id>/section/<int:rehearsal_num>")
def api_update_section(schedule_id, rehearsal_num):
    """Update the Section field for a specific rehearsal."""
    r = admin_required_or_403()
    if r: return r
    
    s = load_schedule(schedule_id)
    if not s: abort(404)
    
    data = request.get_json() or {}
    section = data.get("section", "Full Ensemble").strip()
    
    # Update all rows for this rehearsal with the new section
    timed = s.get("timed", [])
    for row in timed:
        if int(row.get("Rehearsal", 0)) == rehearsal_num:
            row["Section"] = section if section else "Full Ensemble"
    
    s["timed"] = timed
    s["updated_at"] = int(time.time())
    save_schedule(s)
    
    return jsonify({"ok": True, "section": section})


@app.put("/api/s/<schedule_id>/rehearsal-event-type/<int:rehearsal_num>")
def api_update_rehearsal_event_type(schedule_id, rehearsal_num):
    """Update Event Type and Section for a specific rehearsal."""
    r = admin_required_or_403()
    if r: return r
    
    s = load_schedule(schedule_id)
    if not s: abort(404)
    
    data = request.get_json() or {}
    event_type = data.get("Event Type", "Rehearsal").strip()
    section = data.get("Section", "Full Ensemble").strip()
    
    # Update rehearsals table
    rehearsals = s.get("rehearsals", [])
    for row in rehearsals:
        if int(row.get("Rehearsal", 0)) == rehearsal_num:
            row["Event Type"] = event_type if event_type else "Rehearsal"
            row["Section"] = section if section else "Full Ensemble"
    
    # Update timed data
    timed = s.get("timed", [])
    for row in timed:
        if int(row.get("Rehearsal", 0)) == rehearsal_num:
            row["Section"] = section if section else "Full Ensemble"
    
    s["rehearsals"] = rehearsals
    s["timed"] = timed
    s["updated_at"] = int(time.time())
    save_schedule(s)
    
    return jsonify({"ok": True, "event_type": event_type, "section": section})


@app.put("/api/s/<schedule_id>/rehearsal/<int:rehearsal_num>")
def api_update_rehearsal(schedule_id, rehearsal_num):
    """Update rehearsal details (date, time, event, include_in_allocation, section, venue, uniform, event_type) in rehearsals table."""
    r = admin_required_or_403()
    if r: return r
    
    s = load_schedule(schedule_id)
    if not s: abort(404)
    
    data = request.get_json() or {}
    print(f"[DEBUG] api_update_rehearsal called with data: {data}")
    
    # Update rehearsals table
    rehearsals_data = s.get("rehearsals", [])
    updated_row = None
    for row in rehearsals_data:
        if int(row.get("Rehearsal", 0)) == rehearsal_num:
            print(f"[DEBUG] Found rehearsal {rehearsal_num}")
            # Update the fields provided
            if "date" in data:
                row["Date"] = data["date"]
            if "start_time" in data:
                row["Start Time"] = data["start_time"]
            if "end_time" in data:
                row["End Time"] = data["end_time"]
            if "event" in data:
                row["Event"] = data["event"]
            if "include_in_allocation" in data:
                row["Include in allocation"] = data["include_in_allocation"]
            if "section" in data:
                row["Section"] = data["section"] if data["section"] else "Full Ensemble"
            if "event_type" in data:
                row["Event Type"] = data["event_type"]
            if "venue" in data:
                row["Venue"] = data["venue"]
            if "uniform" in data:
                row["Uniform"] = data["uniform"]
            if "time" in data:
                row["Time"] = data["time"]
            updated_row = row
            break
    
    s["rehearsals"] = rehearsals_data
    
    # Re-ensure Event Type column is set based on Event field if event_type wasn't explicitly provided
    if "event_type" not in data:
        s["rehearsals"] = ensure_event_type_column(s["rehearsals"])
    
    # If this is a Concert event type, sync with concert object
    if updated_row and (updated_row.get("Event Type") == "Concert" or 
                       (data.get("event_type") == "Concert") or
                       (data.get("event") and str(data.get("event")).strip().lower() == "concert")):
        ensembles = load_ensembles()
        ensemble = next((e for e in ensembles if e["id"] == s.get("ensemble_id")), None)
        if ensemble:
            concert_date = updated_row.get("Date", "")
            concert_time = updated_row.get("Start Time", "")
            concert_venue = data.get("venue", updated_row.get("Venue", ""))
            concert_uniform = data.get("uniform", updated_row.get("Uniform", ""))
            concert_id = updated_row.get("concert_id")
            
            # Normalize date for concert storage
            if concert_date:
                try:
                    dt = pd.to_datetime(concert_date)
                    concert_date = dt.strftime("%Y-%m-%d 00:00:00")
                    updated_row["Date"] = concert_date  # Update rehearsal row with normalized date
                except:
                    concert_date = str(concert_date)
            
            # Try to find existing concert by concert_id or date
            concert = None
            if concert_id:
                concert = next((c for c in ensemble.get("concerts", []) if c.get("id") == concert_id), None)
            
            if not concert and concert_date:
                # Try to find by schedule_id + old date (in case date is being changed)
                concert = next((c for c in ensemble.get("concerts", []) 
                               if c.get("schedule_id") == schedule_id and 
                               c.get("date") != concert_date), None)
            
            if concert:
                # Update existing concert
                print(f"[UPDATE REHEARSAL] Updating existing concert {concert.get('id')}")
                
                # Regenerate title with new date
                try:
                    dt = pd.to_datetime(concert_date)
                    day = dt.day
                    if 10 <= day % 100 <= 20:
                        suffix = 'th'
                    else:
                        suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')
                    formatted_date = dt.strftime(f'%A %B {day}{suffix}')
                    concert["title"] = f"{ensemble.get('name', 'Unknown Ensemble')} - Concert - {formatted_date}"
                except:
                    pass
                
                concert["date"] = concert_date
                concert["time"] = concert_time
                if concert_venue:
                    concert["venue"] = concert_venue
                if concert_uniform:
                    concert["uniform"] = concert_uniform
                # Store concert_id in rehearsal row for easy linking
                updated_row["concert_id"] = concert["id"]
            else:
                # Create new concert
                print(f"[UPDATE REHEARSAL] Creating new concert for {concert_date}")
                concert_id = f"concert_{uuid.uuid4().hex[:12]}"
                
                # Format date for title
                try:
                    dt = pd.to_datetime(concert_date)
                    day = dt.day
                    if 10 <= day % 100 <= 20:
                        suffix = 'th'
                    else:
                        suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')
                    formatted_date = dt.strftime(f'%A %B {day}{suffix}')
                except:
                    formatted_date = str(concert_date)
                
                new_concert = {
                    "id": concert_id,
                    "title": f"{ensemble.get('name', 'Unknown Ensemble')} - Concert - {formatted_date}",
                    "date": concert_date,
                    "time": concert_time,
                    "venue": concert_venue,
                    "uniform": concert_uniform,
                    "programme": "",
                    "other_info": "",
                    "status": "scheduled",
                    "schedule_id": schedule_id,
                    "is_auto_generated": True
                }
                if "concerts" not in ensemble:
                    ensemble["concerts"] = []
                ensemble["concerts"].append(new_concert)
                updated_row["concert_id"] = concert_id
                print(f"[UPDATE REHEARSAL] Created concert {concert_id}")
            
            save_ensembles(ensembles)
    
    # Track event type changes so we can adjust timed rows (e.g., strip works when converting to a concert)
    event_type_changed = False
    if "event_type" in data and updated_row:
        old_event_type = None
        # Find the old event type value
        for row in s.get("rehearsals", []):
            if int(row.get("Rehearsal", 0)) == rehearsal_num:
                old_event_type = row.get("Event Type", "Rehearsal")
                break
        
        new_event_type = data["event_type"]
        if old_event_type != new_event_type:
            event_type_changed = True
            print(f"[UPDATE REHEARSAL] Event type changed from {old_event_type} to {new_event_type}, updating timed rows")
    
    # Update timed schedule - sync dates and section from rehearsals table
    timed_data = s.get("timed", [])

    new_event_type = updated_row.get("Event Type", "Rehearsal") if updated_row else "Rehearsal"
    section_value = updated_row.get("Section") if updated_row else None
    section_value = section_value if section_value else "Full Ensemble"

    start_time_value = data.get("start_time") or data.get("time")
    if not start_time_value and updated_row:
        start_time_value = updated_row.get("Start Time") or updated_row.get("Time")
    updated_existing_row = False

    if event_type_changed and new_event_type == "Concert":
        # Converting to a concert: drop prior timed rows for this rehearsal and add a concert placeholder
        timed_data = [row for row in timed_data if int(row.get("Rehearsal", 0)) != rehearsal_num]
        timed_data = upsert_concert_timed_rows(
            timed_data,
            rehearsal_num,
            {
                "id": updated_row.get("concert_id") if updated_row else None,
                "date": updated_row.get("Date") if updated_row else data.get("date"),
                "time": start_time_value,
                "title": (updated_row.get("Event") if updated_row else None) or "Concert",
            },
            section_value,
        )
    else:
        # Update existing timed rows in place
        for row in timed_data:
            if int(row.get("Rehearsal", 0)) == rehearsal_num:
                if "date" in data:
                    row["Date"] = data["date"]
                if "section" in data:
                    row["Section"] = section_value
                if event_type_changed:
                    row["Event Type"] = new_event_type
                    if new_event_type != "Concert":
                        row.pop("concert_id", None)
                if new_event_type == "Concert":
                    row["Event Type"] = "Concert"
                    if updated_row and updated_row.get("concert_id"):
                        row["concert_id"] = updated_row.get("concert_id")
                    if start_time_value:
                        row["Time in Rehearsal"] = start_time_value
                else:
                    # If event label was edited, normalize and propagate
                    if "event" in data and data["event"]:
                        event_val = str(data["event"]).strip().lower()
                        if event_val in ["concert", "sectional", "rehearsal"]:
                            row["Event Type"] = event_val.capitalize()
                updated_existing_row = True

        # If no timed rows existed for this concert, add a placeholder so it shows immediately
        if new_event_type == "Concert" and not updated_existing_row:
            timed_data = upsert_concert_timed_rows(
                timed_data,
                rehearsal_num,
                {
                    "id": updated_row.get("concert_id") if updated_row else None,
                    "date": updated_row.get("Date") if updated_row else data.get("date"),
                    "time": start_time_value,
                    "title": (updated_row.get("Event") if updated_row else None) or "Concert",
                },
                section_value,
            )

    s["timed"] = clean_timed_data(timed_data)
    
    s["updated_at"] = int(time.time())
    save_schedule(s)
    
    return jsonify({"ok": True, "rehearsal": rehearsal_num, "updated": data})


@app.get("/s/<schedule_id>")
def schedule_view(schedule_id):
    s = load_schedule(schedule_id)
    if not s:
        abort(404)

    u = current_user()
    # Permissions:
    if u and u.get("is_admin"):
        pass
    else:
        if s.get("status") != "published":
            abort(404)  # hide drafts
        r = ensemble_member_required_or_403(s["ensemble_id"])
        if r: return r

    # Get ensemble name and concerts
    ensembles = load_ensembles()
    ensemble = next((e for e in ensembles if e["id"] == s.get("ensemble_id")), None)
    ensemble_name = ensemble["name"] if ensemble else s.get("ensemble_id", "Schedule")
    
    # Get all concerts for this ensemble (filter by schedule_id if set, but also include concerts without schedule_id)
    linked_concerts = []
    if ensemble:
        # Get all concerts that either match this schedule_id OR have no schedule_id but match dates in this schedule
        all_concerts = ensemble.get("concerts", [])
        
        # Get rehearsal dates from this schedule for matching
        rehearsal_dates = set()
        rehearsal_dates_norm = set()
        for reh in s.get("rehearsals", []):
            if reh.get("Date"):
                try:
                    dt = pd.to_datetime(reh.get("Date"))
                    rehearsal_dates.add(dt.strftime("%Y-%m-%d 00:00:00"))
                    rehearsal_dates_norm.add(dt.strftime("%Y-%m-%d"))
                except:
                    pass
        
        for c in all_concerts:
            # Include if: linked to this schedule OR concert date matches a rehearsal in this schedule
            if c.get("status") == "scheduled":
                concert_date_str = c.get("date", "")
                concert_date_norm = str(concert_date_str)[:10]
                if (
                    c.get("schedule_id") == schedule_id
                    or concert_date_str in rehearsal_dates
                    or concert_date_norm in rehearsal_dates_norm
                ):
                    linked_concerts.append(c)
        
        # Sort by date
        concerts_with_dates = []
        for c in linked_concerts:
            try:
                concert_date = pd.to_datetime(c.get("date", "")).date() if c.get("date") else None
                concerts_with_dates.append((concert_date, c))
            except:
                concerts_with_dates.append((None, c))
        concerts_with_dates.sort(key=lambda x: x[0] if x[0] else "")
        linked_concerts = [c for _, c in concerts_with_dates]

    # Get timed data and clean it to ensure sections are populated
    timed_data = clean_timed_data(s.get("timed", []))
    
    # Add Event Type from rehearsals table to timed data
    rehearsals = s.get("rehearsals", [])
    event_type_map = {}
    for reh_row in rehearsals:
        reh_num = reh_row.get("Rehearsal")
        if reh_num is not None:
            event_type_map[int(reh_num)] = reh_row.get("Event Type", "Rehearsal")
    
    for row in timed_data:
        reh_num = int(row.get("Rehearsal", 0))
        row["Event Type"] = event_type_map.get(reh_num, "Rehearsal")

    # Repair concert timed rows to use the concert's real date/time/title instead of placeholders
    concert_map = {c.get("id"): c for c in linked_concerts if c.get("id")}
    for row in timed_data:
        if row.get("Event Type") != "Concert":
            continue
        cid = row.get("concert_id")
        concert = concert_map.get(cid)
        if not concert:
            continue
        date_val = row.get("Date")
        # Treat 2000-01-01 placeholder or missing/empty as stale
        if not date_val or str(date_val).startswith("2000-01-01"):
            row["Date"] = concert.get("date", date_val)
        time_val = row.get("Time in Rehearsal")
        if (not time_val or time_val == "13:00:00") and concert.get("time"):
            row["Time in Rehearsal"] = concert.get("time")
        title_val = row.get("Title")
        if not title_val or title_val == "Concert":
            row["Title"] = concert.get("title", title_val)

    # Fallback: ensure every linked concert shows in the schedule view
    # If a concert has no timed rows (e.g., date change without regenerate), add a placeholder timed row
    concert_timed_reh_nums = {int(r.get("Rehearsal", 0)) for r in timed_data if r.get("Event Type") == "Concert"}
    concert_ids_in_timed = {r.get("concert_id") for r in timed_data if r.get("Event Type") == "Concert" and r.get("concert_id")}

    for idx, concert in enumerate(linked_concerts):
        if concert.get("id") in concert_ids_in_timed:
            continue

        # Try to find a matching concert rehearsal by concert_id or date
        match_reh = None
        for reh in rehearsals:
            if reh.get("Event Type") != "Concert":
                continue
            if reh.get("concert_id") and reh.get("concert_id") == concert.get("id"):
                match_reh = reh
                break
            # fallback match by date
            reh_date_norm = str(reh.get("Date", ""))[:10]
            if reh_date_norm and reh_date_norm == str(concert.get("date", ""))[:10]:
                match_reh = reh
                break

        if match_reh:
            reh_num = int(match_reh.get("Rehearsal", 0) or 0)
        else:
            # create a virtual rehearsal number far from real ones to avoid collision
            reh_num = 900000 + idx

        if reh_num in concert_timed_reh_nums:
            continue

        timed_data.append({
            "Rehearsal": reh_num,
            "Date": concert.get("date", ""),
            "Title": concert.get("title", "Concert"),
            "Time in Rehearsal": concert.get("time", "19:00"),
            "Break Start (HH:MM)": "",
            "Break End (HH:MM)": "",
            "Section": "Full Ensemble",
            "Event Type": "Concert",
            "concert_id": concert.get("id")
        })
        concert_timed_reh_nums.add(reh_num)

    
    # Debug logging
    print(f"\n[SCHEDULE VIEW] Schedule: {schedule_id}")
    print(f"[SCHEDULE VIEW] Linked concerts: {len(linked_concerts)}")
    for c in linked_concerts:
        print(f"  - {c.get('title')} on {c.get('date')}")
    print(f"[SCHEDULE VIEW] Timed data rows: {len(timed_data)}")
    concert_rows = [r for r in timed_data if r.get('Event Type') == 'Concert']
    print(f"[SCHEDULE VIEW] Concert event type rows: {len(concert_rows)}")
    for cr in concert_rows:
        print(f"  - Rehearsal {cr.get('Rehearsal')}, Date: {cr.get('Date')}")
    
    # Build rehearsal grouping and a chronological order for rendering
    by_reh = {}
    for row in timed_data:
        rnum = row.get("Rehearsal")
        if rnum is None:
            continue
        by_reh.setdefault(rnum, []).append(row)

    def reh_sort_key(item):
        rnum, rows = item
        date_val = None
        if rows:
            date_val = rows[0].get("Date")
        try:
            dt = pd.to_datetime(date_val) if date_val is not None else None
        except Exception:
            dt = None
        return (dt if dt is not None else pd.Timestamp.max, int(rnum))

    reh_order = sorted(by_reh.items(), key=reh_sort_key)

    timed = pd.DataFrame(timed_data)
    response = render_template(
        "view.html",
        timed=timed.to_dict(orient="records"),
        has_schedule=not timed.empty,
        ensemble_name=ensemble_name,
        schedule_id=schedule_id,
        user=u,
        concerts=linked_concerts,
        reh_order=reh_order,
    )
    # Prevent caching so members see fresh data
    return response, 200, {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}


@app.post("/admin/schedules/create")
def admin_create_schedule():
    r = admin_required_or_403()
    if r: return r

    name = (request.form.get("name") or "Untitled").strip()
    ensemble_id = request.form.get("ensemble_id")
    if not ensemble_id:
        abort(400)

    u = current_user()
    s = make_new_schedule(ensemble_id, name, created_by=u["id"] if u else None)
    save_schedule(s)
    return redirect(url_for("admin_edit_schedule", schedule_id=s["id"]))

@app.post("/admin/s/<schedule_id>/set_status")
def admin_set_schedule_status(schedule_id):
    r = admin_required_or_403()
    if r: return r

    s = load_schedule(schedule_id)
    if not s:
        abort(404)

    status = request.form.get("status")
    if status not in {"draft", "published"}:
        abort(400)

    s["status"] = status
    s["updated_at"] = int(time.time())
    save_schedule(s)
    return redirect(url_for("admin_edit_schedule", schedule_id=schedule_id))


@app.get("/admin")
def admin_home():
    r = admin_required_or_403()
    if r: return r

    users = load_users()
    ensembles = load_ensembles()
    mem = load_memberships()

    # decorate: memberships by user
    mem_by_user = {}
    ens_by_id = {e["id"]: e for e in ensembles}
    for m in mem:
        mem_by_user.setdefault(m["user_id"], []).append(m)

    schedules = []
    for sid in list_schedule_ids():
        s = load_schedule(sid)
        if s:
            schedules.append({
                "id": s.get("id"),
                "name": s.get("name", "Untitled"),
                "ensemble_id": s.get("ensemble_id"),
                "status": s.get("status", "draft"),
                "updated_at": s.get("updated_at"),
            })

    schedules.sort(key=lambda x: (x["ensemble_id"] or "", x["name"] or ""))

    return render_template(
        "admin_home.html",
        user=current_user(),
        users=users,
        ensembles=ensembles,
        mem_by_user=mem_by_user,
        ens_by_id=ens_by_id,
        schedules=schedules,   # ✅ add this
    )


@app.post("/admin/migrate-day-column")
def admin_migrate_day_column():
    r = admin_required_or_403()
    if r: return r
    
    count = migrate_unnamed_columns_to_day()
    return {"success": True, "migrated": count}


@app.post("/admin/s/<schedule_id>/delete")
def admin_delete_schedule(schedule_id):
    r = admin_required_or_403()
    if r: return r

    p = schedule_path(schedule_id)
    if os.path.exists(p):
        os.remove(p)
    return redirect(url_for("admin_home"))



@app.post("/admin/ensembles/create")
def admin_create_ensemble():
    r = admin_required_or_403()
    if r: return r

    name = (request.form.get("name") or "").strip()
    if not name:
        abort(400)

    ensembles = load_ensembles()
    new_id = slugify_id(name)

    if any(e["id"] == new_id for e in ensembles):
        # ensure uniqueness
        new_id = f"{new_id}-{uuid.uuid4().hex[:4]}"

    ensembles.append({"id": new_id, "name": name})
    save_ensembles(ensembles)
    return redirect(url_for("admin_home"))

@app.post("/admin/ensembles/<ensemble_id>/rename")
def admin_rename_ensemble(ensemble_id):
    r = admin_required_or_403()
    if r: return r

    new_name = (request.form.get("name") or "").strip()
    if not new_name:
        abort(400)

    ensembles = load_ensembles()
    e = next((x for x in ensembles if x["id"] == ensemble_id), None)
    if not e:
        abort(404)

    e["name"] = new_name
    save_ensembles(ensembles)
    return redirect(url_for("admin_home"))

@app.post("/admin/ensembles/<ensemble_id>/delete")
def admin_delete_ensemble(ensemble_id):
    r = admin_required_or_403()
    if r: return r

    # remove ensemble
    ensembles = [e for e in load_ensembles() if e["id"] != ensemble_id]
    save_ensembles(ensembles)

    # remove memberships pointing at it
    mem = [m for m in load_memberships() if m.get("ensemble_id") != ensemble_id]
    save_memberships(mem)

    # NOTE: when we add per-ensemble schedules, we’ll also delete schedules for this ensemble here.
    return redirect(url_for("admin_home"))

@app.post("/admin/users/<user_id>/set_admin")
def admin_set_admin(user_id):
    r = admin_required_or_403()
    if r: return r

    val = request.form.get("is_admin") == "true"
    set_user_admin_flag(user_id, val)
    return redirect(url_for("admin_home"))

@app.post("/admin/memberships/upsert")
def admin_membership_upsert():
    r = admin_required_or_403()
    if r: return r

    user_id = request.form.get("user_id")
    ensemble_id = request.form.get("ensemble_id")
    role = request.form.get("role", "member")
    status = request.form.get("status", "active")

    if not user_id or not ensemble_id:
        abort(400)
    if role not in {"member", "admin"}:
        abort(400)
    if status not in {"active", "pending"}:
        abort(400)

    upsert_membership(user_id, ensemble_id, role=role, status=status)
    return redirect(url_for("admin_home"))

@app.post("/admin/memberships/remove")
def admin_membership_remove():
    r = admin_required_or_403()
    if r: return r

    user_id = request.form.get("user_id")
    ensemble_id = request.form.get("ensemble_id")
    if not user_id or not ensemble_id:
        abort(400)
    remove_membership(user_id, ensemble_id)
    return redirect(url_for("admin_home"))


# ----------------------------
# Concert Management Routes (Admin)
# ----------------------------

@app.get("/admin/ensembles/<ensemble_id>/concerts")
def admin_view_concerts(ensemble_id):
    """View all concerts for an ensemble."""
    r = admin_required_or_403()
    if r: return r

    ensemble = get_ensemble_by_id(ensemble_id)
    if not ensemble:
        abort(404)

    concerts = get_ensemble_concerts(ensemble_id)
    schedules = [
        {"id": s.get("id"), "name": s.get("name", "Untitled")}
        for s in [load_schedule(sid) for sid in list_schedule_ids()]
        if s and s.get("ensemble_id") == ensemble_id
    ]

    return render_template("concerts.html", ensemble=ensemble, concerts=concerts, schedules=schedules)


@app.get("/admin/ensembles/<ensemble_id>/concerts/add")
def admin_add_concert_form(ensemble_id):
    """Show form to add a new concert."""
    r = admin_required_or_403()
    if r: return r

    ensemble = get_ensemble_by_id(ensemble_id)
    if not ensemble:
        abort(404)

    schedules = [
        {"id": s.get("id"), "name": s.get("name", "Untitled")}
        for s in [load_schedule(sid) for sid in list_schedule_ids()]
        if s and s.get("ensemble_id") == ensemble_id
    ]

    return render_template("concert_form.html", ensemble=ensemble, concert=None, schedules=schedules)


@app.post("/admin/ensembles/<ensemble_id>/concerts/add")
def admin_add_concert(ensemble_id):
    """Create a new concert."""
    r = admin_required_or_403()
    if r: return r

    ensemble = get_ensemble_by_id(ensemble_id)
    if not ensemble:
        abort(404)

    concert_data = {
        "date": request.form.get("date"),
        "time": request.form.get("time"),
        "venue": request.form.get("venue"),
        "uniform": request.form.get("uniform"),
        "programme": request.form.get("programme"),
        "other_info": request.form.get("other_info"),
        "schedule_id": request.form.get("schedule_id") or None,
        "status": request.form.get("status", "scheduled"),
    }

    try:
        add_concert_to_ensemble(ensemble_id, concert_data)
        return redirect(url_for("admin_view_concerts", ensemble_id=ensemble_id))
    except Exception as e:
        abort(400)


@app.get("/admin/ensembles/<ensemble_id>/concerts/<concert_id>/edit")
def admin_edit_concert_form(ensemble_id, concert_id):
    """Show form to edit a concert."""
    r = admin_required_or_403()
    if r: return r

    ensemble = get_ensemble_by_id(ensemble_id)
    if not ensemble:
        abort(404)

    concert = next((c for c in ensemble.get("concerts", []) if c.get("id") == concert_id), None)
    if not concert:
        abort(404)

    # Normalize date/time for HTML inputs
    if concert.get("date"):
        try:
            dt = pd.to_datetime(concert["date"])
            concert["date"] = dt.strftime("%Y-%m-%d")
        except Exception:
            concert["date"] = str(concert.get("date", ""))[:10]
    if concert.get("time"):
        try:
            t = pd.to_datetime(concert["time"]).time()
            concert["time"] = t.strftime("%H:%M")
        except Exception:
            concert["time"] = str(concert.get("time", ""))[:5]

    schedules = [
        {"id": s.get("id"), "name": s.get("name", "Untitled")}
        for s in [load_schedule(sid) for sid in list_schedule_ids()]
        if s and s.get("ensemble_id") == ensemble_id
    ]

    return render_template("concert_form.html", ensemble=ensemble, concert=concert, schedules=schedules)


@app.post("/admin/ensembles/<ensemble_id>/concerts/<concert_id>/edit")
def admin_edit_concert(ensemble_id, concert_id):
    """Update a concert."""
    r = admin_required_or_403()
    if r: return r

    ensemble = get_ensemble_by_id(ensemble_id)
    if not ensemble:
        abort(404)

    concert_data = {
        "date": request.form.get("date"),
        "time": request.form.get("time"),
        "venue": request.form.get("venue"),
        "uniform": request.form.get("uniform"),
        "programme": request.form.get("programme"),
        "other_info": request.form.get("other_info"),
        "schedule_id": request.form.get("schedule_id") or None,
        "status": request.form.get("status", "scheduled"),
    }

    try:
        update_concert(ensemble_id, concert_id, concert_data)
        return redirect(url_for("admin_view_concerts", ensemble_id=ensemble_id))
    except Exception as e:
        abort(400)


@app.post("/admin/ensembles/<ensemble_id>/concerts/<concert_id>/delete")
def admin_delete_concert(ensemble_id, concert_id):
    """Delete a concert."""
    r = admin_required_or_403()
    if r: return r

    if delete_concert(ensemble_id, concert_id):
        return redirect(url_for("admin_view_concerts", ensemble_id=ensemble_id))
    else:
        abort(404)


@app.get("/debug/paths")
def debug_paths():
    return {
        "DATA_DIR": DATA_DIR,
        "users_path": users_path(),
        "ensembles_path": ensembles_path(),
        "memberships_path": memberships_path(),
    }


# ---- Auth: register/login/logout (Phase 1)
@app.get("/register")
def register_view():
    ensembles = load_ensembles()
    return render_template("register.html", ensembles=ensembles)


@app.post("/register")
def register_post():
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    ensemble_id = (request.form.get("ensemble_id") or "").strip()

    if not email or not password or not ensemble_id:
        abort(400)

    users = load_users()
    if any(u for u in users if u.get("email") == email):
        return render_template("register.html", ensembles=load_ensembles(), error="Email already registered."), 400
    

    uid = uuid.uuid4().hex
    users.append({
        "id": uid,
        "email": email,
        "password_hash": generate_password_hash(password),
        "is_admin": False,
        "created_at": int(time.time()),
    })

    for user in users:
        ensure_admin_if_matches_email(user)
    save_users(users)

    memberships = load_memberships()
    memberships.append({"user_id": uid, "ensemble_id": ensemble_id, "status": "active"})
    save_memberships(memberships)

    session["user_id"] = uid
    return redirect(url_for("my_view"))


@app.get("/login")
def login_view():
    return render_template("login.html")


@app.post("/login")
def login_post():
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""

    u = next((x for x in load_users() if x.get("email") == email), None)
    if not u or not check_password_hash(u.get("password_hash", ""), password):
        return render_template("login.html", error="Invalid email or password."), 403
    
    changed = ensure_admin_if_matches_email(u)
    if changed:
        users = load_users()
        for x in users:
            if x.get("id") == u["id"]:
                x["is_admin"] = True
        save_users(users)

    session["user_id"] = u["id"]
    nxt = request.args.get("next")
    return redirect(nxt or url_for("my_view"))


@app.get("/logout")
def logout_view():
    session.clear()
    return redirect(url_for("login_view"))


@app.get("/my")
def my_view():
    redir = login_required_or_redirect()
    if redir:
        return redir
    u = current_user()
    
    print(f"\n=== my_view called ===")
    print(f"Current user: {u.get('email') if u else 'None'}")
    
    # Get user's ensembles (for concert display)
    ensembles = load_ensembles()
    memberships = load_memberships()
    
    if u and u.get("is_admin"):
        user_ensembles = ensembles
        print(f"User is admin, showing all {len(ensembles)} ensembles")
    else:
        my_ids = {m.get("ensemble_id") for m in memberships if m.get("user_id") == (u or {}).get("id") and m.get("status", "active") == "active"}
        user_ensembles = [e for e in ensembles if e.get("id") in my_ids]
        print(f"User is member of {len(user_ensembles)} ensembles: {[e['id'] for e in user_ensembles]}")
    
    # Get all concerts for user's ensembles
    all_concerts = []
    for ensemble in user_ensembles:
        concerts = ensemble.get("concerts", [])
        print(f"Ensemble '{ensemble.get('name')}' has {len(concerts)} concerts")
        for concert in concerts:
            concert_with_ensemble = concert.copy()
            concert_with_ensemble["ensemble_id"] = ensemble.get("id")
            concert_with_ensemble["ensemble_name"] = ensemble.get("name")
            all_concerts.append(concert_with_ensemble)
            print(f"  Added concert: {concert.get('date')} at {concert.get('venue')}")
    
    # Sort by date
    all_concerts.sort(key=lambda x: x.get("date", ""), reverse=True)
    
    print(f"Total concerts to display: {len(all_concerts)}")
    print(f"=== my_view complete ===\n")
    
    return render_template("member_dashboard.html", user=u, concerts=all_concerts)


@app.get("/api/ensembles/<ensemble_id>/concerts")
def api_get_ensemble_concerts(ensemble_id):
    """Get all concerts for an ensemble (JSON API)."""
    r = admin_required_or_403()
    if r: return r
    
    concerts = get_ensemble_concerts(ensemble_id)
    return jsonify(concerts)


@app.post("/api/ensembles/<ensemble_id>/concerts")
def api_create_concert(ensemble_id):
    """Create a concert via JSON API."""
    r = admin_required_or_403()
    if r: return r
    
    payload = request.get_json(force=True)
    try:
        concert = add_concert_to_ensemble(ensemble_id, payload)
        return jsonify(concert), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.put("/api/ensembles/<ensemble_id>/concerts/<concert_id>")
def api_update_concert(ensemble_id, concert_id):
    """Update a concert via JSON API."""
    r = admin_required_or_403()
    if r: return r
    
    payload = request.get_json(force=True)
    try:
        concert = update_concert(ensemble_id, concert_id, payload)
        if not concert:
            return jsonify({"error": "Concert not found"}), 404
        return jsonify(concert)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.delete("/api/ensembles/<ensemble_id>/concerts/<concert_id>")
def api_delete_concert(ensemble_id, concert_id):
    """Delete a concert via JSON API."""
    r = admin_required_or_403()
    if r: return r
    
    if delete_concert(ensemble_id, concert_id):
        return jsonify({"success": True})
    else:
        return jsonify({"error": "Concert not found"}), 404


@app.post("/api/admin/cleanup-duplicate-concerts")
def api_cleanup_duplicate_concerts():
    """Remove auto_concert_* duplicates that were created before the deduplication fix."""
    r = admin_required_or_403()
    if r: return r
    
    ensembles = load_ensembles()
    removed_count = 0
    
    for ensemble in ensembles:
        if "concerts" not in ensemble:
            continue
        
        # Remove any concerts with IDs starting with "auto_concert_"
        original_count = len(ensemble["concerts"])
        ensemble["concerts"] = [c for c in ensemble["concerts"] if not c.get("id", "").startswith("auto_concert_")]
        removed_count += original_count - len(ensemble["concerts"])
    
    save_ensembles(ensembles)
    
    return jsonify({
        "success": True,
        "removed_count": removed_count,
        "message": f"Removed {removed_count} duplicate auto-generated concerts"
    })


@app.get("/api/member/rehearsals")
def api_member_rehearsals():
    redir = login_required_or_redirect()
    if redir:
        return jsonify({"error": "Not logged in"}), 401
    
    u = current_user()
    print(f"\n=== api_member_rehearsals called ===")
    print(f"Current user: {u.get('email') if u else 'None'}")
    print(f"Is admin: {u.get('is_admin') if u else False}")
    
    ensembles = load_ensembles()
    memberships = load_memberships()

    if u and u.get("is_admin"):
        my_ensembles = ensembles
        print(f"User is admin, showing all {len(ensembles)} ensembles")
    else:
        my_ids = {m.get("ensemble_id") for m in memberships if m.get("user_id") == (u or {}).get("id") and m.get("status", "active") == "active"}
        my_ensembles = [e for e in ensembles if e.get("id") in my_ids]
        print(f"User is member of {len(my_ensembles)} ensembles: {[e['id'] for e in my_ensembles]}")

    # Get published schedules for user's ensembles
    schedule_summaries = []
    all_schedule_ids = list_schedule_ids()
    print(f"Total schedules in system: {len(all_schedule_ids)}")
    
    for sid in all_schedule_ids:
        try:
            s = load_schedule(sid)
        except Exception as e:
            print(f"  [ERROR] Failed to load schedule {sid}: {e}")
            import traceback
            traceback.print_exc()
            continue
        if not s:
            continue

        if u and u.get("is_admin"):
            schedule_summaries.append(s)
            print(f"  Admin: including schedule {sid} ({s.get('name')})")
        else:
            if s.get("status") != "published":
                print(f"  Schedule {sid} not published (status: {s.get('status')}), skipping for member")
                continue
            if any(e["id"] == s.get("ensemble_id") for e in my_ensembles):
                schedule_summaries.append(s)
                print(f"  Member: including published schedule {sid} for ensemble {s.get('ensemble_id')}")
            else:
                print(f"  Schedule {sid} is for ensemble {s.get('ensemble_id')}, not in user's ensembles")

    print(f"Found {len(schedule_summaries)} schedules to process")

    # Extract rehearsal data with dates and times
    rehearsals_list = []
    ensemble_map = {e["id"]: e["name"] for e in ensembles}
    
    for sched in schedule_summaries:
        schedule_id = sched["id"]
        ensemble_id = sched.get("ensemble_id")
        ensemble_name = ensemble_map.get(ensemble_id, "Unknown")
        
        print(f"\nProcessing schedule {schedule_id} ({ensemble_name})")
        
        # Build a map of rehearsal number -> event type from the rehearsals table
        rehearsals_table = sched.get("rehearsals", [])
        event_type_map = {}
        for reh_row in rehearsals_table:
            reh_num = reh_row.get("Rehearsal")
            # Skip invalid rehearsal numbers (None, empty string, NaN)
            if reh_num is not None and reh_num != "" and not pd.isna(reh_num):
                try:
                    event_type_map[int(reh_num)] = reh_row.get("Event Type", "Rehearsal")
                except (ValueError, TypeError):
                    # Skip if conversion to int fails
                    pass
        
        # Get timed schedule data directly from the schedule
        try:
            timed_data = sched.get("timed", [])
            print(f"  Timed data items: {len(timed_data)}")
            
            if not timed_data:
                print(f"  No timed data, skipping")
                continue
            
            # Create a DataFrame to group by rehearsal
            timed_df = pd.DataFrame(timed_data)
            if timed_df.empty:
                print(f"  Empty DataFrame after conversion, skipping")
                continue
            
            # Group by rehearsal number
            if "Rehearsal" not in timed_df.columns:
                print(f"  No Rehearsal column in DataFrame, skipping")
                print(f"  Available columns: {list(timed_df.columns)}")
                continue
                
            grouped = timed_df.groupby("Rehearsal")
            print(f"  Found {len(grouped)} rehearsals")
            
            for reh_num, group in grouped:
                # Ensure reh_num is a clean integer
                try:
                    reh_num_int = int(reh_num)
                except (ValueError, TypeError):
                    print(f"    Skipping invalid rehearsal number: {reh_num}")
                    continue
                
                # Get date from first row
                if "Date" not in group.columns:
                    print(f"    Rehearsal {reh_num_int}: No Date column, skipping")
                    continue
                    
                date_val = group.iloc[0]["Date"]
                if pd.isna(date_val):
                    print(f"    Rehearsal {reh_num_int}: NaN date, skipping")
                    continue
                
                # Parse the date - it could be a string like "2026-01-16T00:00:00" or a datetime
                try:
                    if isinstance(date_val, str):
                        # Parse ISO format string
                        date_obj = pd.to_datetime(date_val).date()
                    else:
                        # Assume it's already a datetime-like object
                        date_obj = pd.to_datetime(date_val).date()
                    date_str = date_obj.isoformat()
                except Exception as e:
                    print(f"    Rehearsal {reh_num_int}: Error parsing date '{date_val}': {e}")
                    continue
                
                # Get event type from rehearsals table
                event_type = event_type_map.get(reh_num_int, "Rehearsal")
                
                # Get section if specified (first row's Section value, default to "Full Ensemble")
                section = "Full Ensemble"
                if "Section" in group.columns:
                    section_val = group.iloc[0]["Section"]
                    if pd.notna(section_val):
                        section = str(section_val).strip()
                
                # Build items list for this rehearsal
                items = []
                for _, row in group.iterrows():
                    title = row.get("Title", "")
                    time_in_reh = row.get("Time in Rehearsal", "")
                    
                    if title == "Break":
                        items.append({
                            "is_break": True,
                            "time": time_in_reh
                        })
                    else:
                        items.append({
                            "is_break": False,
                            "title": title,
                            "time": time_in_reh
                        })
                
                rehearsals_list.append({
                    "schedule_id": schedule_id,
                    "ensemble_name": ensemble_name,
                    "rehearsal_num": reh_num_int,
                    "date": date_str,
                    "event_type": event_type,
                    "section": section,
                    "items": items
                })
                print(f"    Rehearsal {reh_num_int}: {date_str}, Event Type: {event_type}, Section: {section}, {len(items)} items")
                
        except Exception as e:
            print(f"  Error processing schedule {schedule_id}: {e}")
            import traceback
            traceback.print_exc()
            continue
    
    # Sort by date
    rehearsals_list.sort(key=lambda x: (x["date"], x["ensemble_name"], x["rehearsal_num"]))
    
    # Build concerts list (for standalone display, separate from rehearsals)
    concerts_list = []
    
    for ensemble in ensembles:
        ens_id = ensemble["id"]
        ens_name = ensemble["name"]
        
        # Check if user has access to this ensemble
        if not (u and u.get("is_admin")) and not any(e["id"] == ens_id for e in my_ensembles):
            continue
        
        # Get scheduled concerts for this ensemble
        for concert in ensemble.get("concerts", []):
            if concert.get("status") == "scheduled":
                try:
                    # Parse concert date for sorting
                    concert_date = concert.get("date", "")
                    if isinstance(concert_date, str):
                        concert_date_obj = pd.to_datetime(concert_date).date()
                    else:
                        concert_date_obj = pd.to_datetime(concert_date).date()
                    concert_date_str = concert_date_obj.isoformat()
                except:
                    concert_date_str = concert.get("date", "")
                
                concerts_list.append({
                    "type": "concert",
                    "id": concert.get("id"),  # Add 'id' for JavaScript compatibility
                    "concert_id": concert.get("id"),
                    "ensemble_id": ens_id,  # Add ensemble_id
                    "ensemble_name": ens_name,
                    "title": concert.get("title", "Concert"),
                    "date": concert_date_str,
                    "time": concert.get("time"),
                    "venue": concert.get("venue"),
                    "uniform": concert.get("uniform"),
                    "programme": concert.get("programme", ""),
                    "other_info": concert.get("other_info", ""),
                    "schedule_id": concert.get("schedule_id")
                })
    
    # Combine concerts and rehearsals, sort by date
    all_events = []
    for rehearsal in rehearsals_list:
        all_events.append({
            "type": "rehearsal",
            "date": rehearsal["date"],
            "sort_key": (rehearsal["date"], rehearsal["ensemble_name"], 0, rehearsal["rehearsal_num"]),
            "data": rehearsal
        })
    for concert in concerts_list:
        all_events.append({
            "type": "concert",
            "date": concert["date"],
            "sort_key": (concert["date"], concert["ensemble_name"], -1),  # Concerts come before rehearsals on same date
            "data": concert
        })
    
    all_events.sort(key=lambda x: x["sort_key"])
    
    print(f"\n=== Total events to return: {len(all_events)} ({len(rehearsals_list)} rehearsals + {len(concerts_list)} concerts) ===\n")
    
    return jsonify({"events": all_events, "rehearsals": rehearsals_list, "concerts": concerts_list})


@app.get("/")
def home():
    return redirect(url_for("my_view"))



@app.get("/admin/s/<schedule_id>/edit")
def admin_edit_schedule(schedule_id):
    r = admin_required_or_403()
    if r:
        return r

    s = load_schedule(schedule_id)
    if not s:
        abort(404)

    works, rehearsals, alloc, sched = get_frames(s)

    # lightweight preview timed
    timed = pd.DataFrame(s.get("timed", []))
    if timed.empty and not sched.empty and not rehearsals.empty:
        # Use prepare_all_rehearsals to include non-allocated rehearsals (sectionals)
        timed = compute_timed_df(sched, prepare_all_rehearsals(rehearsals))
    
    # Clean timed data to ensure Section field has proper defaults
    if not timed.empty:
        cleaned_timed = clean_timed_data(s.get("timed", []))
        s["timed"] = cleaned_timed
        # Reload into DataFrame so template gets clean data
        timed = pd.DataFrame(cleaned_timed)

    # Resolve ensemble display name and get concerts
    ensembles = load_ensembles()
    ens = next((e for e in ensembles if e["id"] == s.get("ensemble_id")), None)
    ensemble_name = ens["name"] if ens else s.get("ensemble_id", "")
    
    # Get concerts for this schedule
    schedule_concerts = []
    if ens:
        concerts = ens.get("concerts", [])
        schedule_concerts = [c for c in concerts if c.get("schedule_id") == schedule_id]
    
    # Add concert_id to timed rows for concert rehearsals (same logic as /state endpoint)
    timed_data = s.get("timed", [])
    if ens and not rehearsals.empty:
        for timed_row in timed_data:
            reh_num = timed_row.get("Rehearsal")
            if reh_num:
                # Find rehearsal by number
                reh_dict = next((r for r in rehearsals.to_dict(orient="records") if int(r.get("Rehearsal", 0)) == reh_num), None)
                if reh_dict and reh_dict.get("Event Type") == "Concert":
                    # Find the concert for this rehearsal by matching date
                    reh_date = reh_dict.get("Date")
                    # Normalize dates for comparison (strip time component)
                    reh_date_normalized = str(reh_date)[:10] if reh_date else None
                    
                    # Try to match concert by date (comparing just YYYY-MM-DD)
                    concert = None
                    for c in schedule_concerts:
                        concert_date_normalized = str(c.get("date", ""))[:10]
                        if concert_date_normalized == reh_date_normalized:
                            concert = c
                            break
                    
                    if concert:
                        timed_row["concert_id"] = concert.get("id")
                        print(f"[EDIT PAGE] Added concert_id {concert.get('id')} to timed row for rehearsal {reh_num}")
        
        # Update timed DataFrame with concert_id links
        timed = pd.DataFrame(timed_data)

    print("ADMIN CHECK:", current_user())

    return render_template(
        "edit.html",
        schedule_id=s.get("id"),
        schedule_name=s.get("name", "Untitled"),
        ensemble_name=ensemble_name,
        status=s.get("status", "draft"),
        works=works.fillna("").to_dict(orient="records"),
        works_cols=s.get("works_cols") or list(default_works_df().columns),
        rehearsals_cols=s.get("rehearsals_cols") or list(default_rehearsals_df().columns),
        rehearsals=rehearsals.fillna("").to_dict(orient="records"),
        allocation=alloc.fillna("").to_dict(orient="records") if not alloc.empty else [],
        schedule=sched.fillna("").to_dict(orient="records") if not sched.empty else [],
        timed=timed.fillna("").to_dict(orient="records") if not timed.empty else [],
        concerts=schedule_concerts,  # Add concerts for timeline editor
        pdf_enabled=mod4_exists,
    )


@app.get("/edit")
def edit_view():
    require_edit_token()
    s, sid = default_schedule()
    return redirect(url_for("admin_edit_schedule", schedule_id=sid))


# ---- API: save inputs
@app.post("/api/save_inputs")
def api_save_inputs():
    require_edit_token()
    payload = request.get_json(force=True)

    s, sid = default_schedule()

    s["G"] = int(payload.get("G", s.get("G", DEFAULT_G)))

    s["works"] = payload.get("works", [])
    # Auto-number rehearsals if needed
    rehearsals_data = payload.get("rehearsals", [])
    rehearsals_data = auto_number_rehearsals(rehearsals_data)
    s["rehearsals"] = rehearsals_data

    # Keep computed artifacts unless explicitly clearing
    if payload.get("clear_computed"):
        s["allocation"] = []
        s["schedule"] = []
        s["timed"] = []

    save_schedule(s)
    return jsonify({"ok": True})


# ---- API: run allocation
@app.post("/api/run_allocation")
def api_run_allocation():
    require_edit_token()
    s, sid = default_schedule()

    works, rehearsals, _, _ = get_frames(s)
    G = int(s.get("G", DEFAULT_G))

    alloc_df, warnings = run_allocation_compute(works, rehearsals, G)
    s["allocation"] = sanitize_df_records(alloc_df.to_dict(orient="records"))
    s["warnings"] = warnings
    save_schedule(s)
    return jsonify({"ok": True, "warnings": warnings})


# ---- API: generate schedule
@app.post("/api/generate_schedule")
def api_generate_schedule():
    require_edit_token()
    s, sid = default_schedule()

    works, rehearsals, alloc, _ = get_frames(s)
    if alloc.empty:
        return jsonify({"ok": False, "error": "No allocation yet"}), 400

    sched = build_schedule_from_allocation(works, alloc)
    s["schedule"] = sanitize_df_records(sched.to_dict(orient="records"))

    # Use prepare_all_rehearsals to include non-allocated rehearsals (sectionals)
    rehe_prep = prepare_all_rehearsals(rehearsals)
    timed = compute_timed_df(sched, rehe_prep)
    s["timed"] = sanitize_df_records(timed.to_dict(orient="records"))
    s["generated_at"] = pd.Timestamp.utcnow().isoformat() + "Z"

    save_schedule(s)
    return jsonify({"ok": True})

@app.post("/api/s/<schedule_id>/import_xlsx")
def api_schedule_import_xlsx(schedule_id):
    r = admin_required_or_403()
    if r:
        return r

    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file uploaded"}), 400

    f = request.files["file"]

    # Load schedule up front
    s = load_schedule(schedule_id)
    if not s:
        abort(404)

    # Read Excel container
    try:
        xls = pd.ExcelFile(f.stream)  # type: ignore
    except Exception as e:
        return jsonify({"ok": False, "error": f"Could not read Excel file: {e}"}), 400

    sheet_names = list(xls.sheet_names)

    def pick_sheet(names, keywords):
        for name in names:
            low = name.strip().lower()
            if any(k in low for k in keywords):
                return name
        return None

    works_sheet = pick_sheet(sheet_names, ["work"])
    rehe_sheet = pick_sheet(sheet_names, ["rehears"])

    if not works_sheet or not rehe_sheet:
        if len(sheet_names) < 2:
            return jsonify({
                "ok": False,
                "error": "Excel file must contain two sheets (Works and Rehearsals).",
                "sheets_found": sheet_names,
            }), 400
        works_sheet = works_sheet or sheet_names[0]
        rehe_sheet = rehe_sheet or sheet_names[1]

    # --- THE FIX: Load the Dataframes first, THEN replace NaNs ---
    try:
        df_works = pd.read_excel(xls, sheet_name=works_sheet)
        df_rehe = pd.read_excel(xls, sheet_name=rehe_sheet)
        
        # Replace NaN/Inf with None so JSON conversion doesn't break
        df_works = df_works.replace({np.nan: None, np.inf: None, -np.inf: None})
        df_rehe = df_rehe.replace({np.nan: None, np.inf: None, -np.inf: None})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Failed reading sheets: {e}"}), 400

    if df_works is None: df_works = pd.DataFrame()
    if df_rehe is None: df_rehe = pd.DataFrame()

    # Normalize headers
    works_cols = [str(c).strip() for c in df_works.columns]
    rehe_cols = [str(c).strip() for c in df_rehe.columns]
    df_works.columns = works_cols
    df_rehe.columns = rehe_cols

    # Internal helper to handle DateTimes and empty cells
    def df_to_json_safe_strings(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        out = df.copy()
        for col in out.columns:
            try:
                # Convert actual datetime types to string
                if pd.api.types.is_datetime64_any_dtype(out[col]):
                    out[col] = out[col].dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass

        def conv(x):
            if isinstance(x, pd.Timestamp):
                return x.isoformat()
            # This is where we ensure "NaN" becomes "" for the UI
            if x is None or (isinstance(x, float) and math.isnan(x)):
                return ""
            return x

        return out.map(conv)

    df_works = df_to_json_safe_strings(df_works)
    df_rehe = df_to_json_safe_strings(df_rehe)

    # Save to state
    s["works_cols"] = works_cols
    
    # Auto-number rehearsals if missing or empty
    rehearsals_data = df_rehe.to_dict(orient="records")
    rehearsals_data = auto_number_rehearsals(rehearsals_data)
    # Normalize "Ensemble" column to "Section" if present
    rehearsals_data = normalize_section_column(rehearsals_data)
    # Ensure Include in allocation column is present
    rehearsals_data = ensure_include_in_allocation_column(rehearsals_data)
    # Ensure Section column is present
    rehearsals_data = ensure_section_column(rehearsals_data)
    # Ensure Event Type column is present
    rehearsals_data = ensure_event_type_column(rehearsals_data)
    s["rehearsals"] = rehearsals_data
    
    # Auto-create concerts for rehearsals marked as "Concert" event type
    ensembles = load_ensembles()
    ensemble = next((e for e in ensembles if e["id"] == s.get("ensemble_id")), None)
    if ensemble:
        print(f"\n[CONCERT CREATE] Starting concert auto-creation for {len(rehearsals_data)} rehearsals")
        print(f"[CONCERT CREATE] Ensemble: {ensemble.get('name')} (ID: {ensemble.get('id')})")
        concerts_created = 0
        concerts_updated = 0
        
        for idx, reh_row in enumerate(rehearsals_data):
            event_type_value = reh_row.get("Event Type")
            print(f"\n[CONCERT CREATE] Row {idx+1}: Event Type = {repr(event_type_value)} | Date = {reh_row.get('Date')}")
            
            # More robust matching - handle case variations and whitespace
            event_type_normalized = str(event_type_value).strip() if event_type_value else ""
            is_concert = event_type_normalized.lower() == "concert"
            
            print(f"[CONCERT CREATE]   Normalized: '{event_type_normalized}' | Is Concert: {is_concert}")
            
            if is_concert:
                print(f"[CONCERT CREATE] ✓ Processing rehearsal with Event Type=Concert")
                # Check if concert already exists for this date/time combination
                concert_date = reh_row.get("Date", "")
                concert_time = reh_row.get("Start Time", "")
                
                # Normalize date to string format YYYY-MM-DD HH:MM:SS (to match other rehearsals)
                if concert_date:
                    try:
                        dt = pd.to_datetime(concert_date)
                        concert_date = dt.strftime("%Y-%m-%d 00:00:00")  # Include time component for consistency
                        reh_row["Date"] = concert_date  # Update the rehearsal row with normalized date
                    except:
                        concert_date = str(concert_date)
                
                # Find existing concert for this schedule and date (deduplication)
                existing_concert = None
                for c in ensemble.get("concerts", []):
                    if c.get("schedule_id") == schedule_id and c.get("date") == concert_date:
                        existing_concert = c
                        break
                
                if concert_date:
                    concert_id = None
                    print(f"\n[CONCERT CREATE] Processing rehearsal with Event Type=Concert")
                    print(f"[CONCERT CREATE] Date: {concert_date}, Schedule: {schedule_id}")
                    print(f"[CONCERT CREATE] Existing concerts in ensemble: {len(ensemble.get('concerts', []))}")
                    
                    # Format date for title
                    try:
                        dt = pd.to_datetime(concert_date)
                        day = dt.day
                        if 10 <= day % 100 <= 20:
                            suffix = 'th'
                        else:
                            suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')
                        formatted_date = dt.strftime(f'%A %B {day}{suffix}')
                    except:
                        formatted_date = str(concert_date)
                    
                    default_title = f"{ensemble.get('name', 'Unknown Ensemble')} - Concert - {formatted_date}"
                    
                    if existing_concert:
                        # Update existing concert with new data from Excel
                        print(f"[CONCERT CREATE] Updating existing concert: {existing_concert['id']}")
                        existing_concert["title"] = reh_row.get("Concert Title") or existing_concert.get("title", default_title)
                        existing_concert["time"] = reh_row.get("Start Time", existing_concert.get("time", ""))
                        existing_concert["venue"] = reh_row.get("Venue", existing_concert.get("venue", ""))
                        existing_concert["uniform"] = reh_row.get("Uniform", existing_concert.get("uniform", ""))
                        existing_concert["programme"] = reh_row.get("Programme", existing_concert.get("programme", ""))
                        existing_concert["other_info"] = reh_row.get("Other Info", existing_concert.get("other_info", ""))
                        concert_id = existing_concert["id"]
                        concerts_updated += 1
                    else:
                        # Create a new concert with details from Excel
                        concert_id = f"concert_{uuid.uuid4().hex[:12]}"
                        print(f"[CONCERT CREATE] Creating NEW concert: {concert_id}")
                        new_concert = {
                            "id": concert_id,
                            "title": reh_row.get("Concert Title") or default_title,
                            "date": concert_date,
                            "time": reh_row.get("Start Time", ""),
                            "venue": reh_row.get("Venue", ""),
                            "uniform": reh_row.get("Uniform", ""),
                            "programme": reh_row.get("Programme", ""),
                            "other_info": reh_row.get("Other Info", ""),
                            "status": "scheduled",
                            "schedule_id": schedule_id,
                            "is_auto_generated": True
                        }
                        if "concerts" not in ensemble:
                            ensemble["concerts"] = []
                        ensemble["concerts"].append(new_concert)
                        concerts_created += 1
                        print(f"[CONCERT CREATE] Concert added to ensemble. Total concerts: {len(ensemble['concerts'])}")
                    
                    # Link concert_id back to rehearsal row for easy access
                    reh_row["concert_id"] = concert_id
                    print(f"[CONCERT CREATE] Linked concert_id {concert_id} to rehearsal row")
        
        # Save updated ensemble with new/updated concerts
        if concerts_created > 0 or concerts_updated > 0:
            save_ensembles(ensembles)
            print(f"\n[CONCERT CREATE] SUMMARY: Created {concerts_created}, Updated {concerts_updated} concerts")
            print(f"[CONCERT CREATE] Total concerts in ensemble: {len(ensemble.get('concerts', []))}")
        else:
            print(f"\n[CONCERT CREATE] No concerts created or updated")
    
    # Ensure rehearsals_cols always includes "Rehearsal" and "Include in allocation"
    # Use the columns from the processed data to ensure consistency
    if rehearsals_data and len(rehearsals_data) > 0:
        actual_cols = list(rehearsals_data[0].keys())
        # Ensure "Rehearsal" column is in the list
        if "Rehearsal" not in actual_cols:
            actual_cols.insert(0, "Rehearsal")
        s["rehearsals_cols"] = actual_cols
    else:
        # Ensure default columns include "Rehearsal"
        default_cols = list(default_rehearsals_df().columns)
        s["rehearsals_cols"] = default_cols
    
    s["works"] = df_works.to_dict(orient="records")

    # Clear computed artifacts
    s["allocation"] = []
    s["schedule"] = []
    s["timed"] = []
    s["generated_at"] = None
    s["updated_at"] = int(time.time())

    save_schedule(s)

    return jsonify({
        "ok": True,
        "works_sheet": works_sheet,
        "rehearsals_sheet": rehe_sheet,
        "works_rows": int(len(df_works)),
        "rehearsals_rows": int(len(df_rehe)),
    })


# ---- API: update schedule for a single rehearsal (order + minutes)
@app.post("/api/update_rehearsal_schedule")
def api_update_rehearsal_schedule():
    require_edit_token()
    payload = request.get_json(force=True)
    rehearsal = int(payload["rehearsal"])
    items = payload["items"]  # list of {Title, Minutes, GroupKey, PlayerLoad}

    s, sid = default_schedule()

    works, rehearsals, alloc, sched = get_frames(s)
    if sched.empty:
        return jsonify({"ok": False, "error": "No schedule exists"}), 400

    sched = sched.copy()
    sched["Rehearsal"] = pd.to_numeric(sched["Rehearsal"], errors="coerce").astype("Int64")

    # remove old rows for this rehearsal
    sched_other = sched[sched["Rehearsal"] != rehearsal].copy()

    # rebuild rows for this rehearsal
    new_rows = []
    for i, it in enumerate(items):
        new_rows.append({
            "Rehearsal": rehearsal,
            "Title": str(it.get("Title", "")).strip(),
            "Rehearsal Time (minutes)": safe_int(it.get("Minutes", 0), 0),
            "GroupKey": str(it.get("GroupKey", "")).strip() or str(it.get("Title", "")).strip(),
            "PlayerLoad": safe_float(it.get("PlayerLoad", 0.0), 0.0),
            "MovementOrder": np.nan,
        })

    sched_new = pd.DataFrame(new_rows)
    sched2 = pd.concat([sched_other, sched_new], ignore_index=True)
    s["schedule"] = sanitize_df_records(sched2.to_dict(orient="records"))

    # Use prepare_all_rehearsals to include non-allocated rehearsals (sectionals)
    rehe_prep = prepare_all_rehearsals(rehearsals)
    timed = compute_timed_df(sched2, rehe_prep)
    s["timed"] = timed.to_dict(orient="records")
    s["generated_at"] = pd.Timestamp.utcnow().isoformat() + "Z"

    save_schedule(s)
    return jsonify({"ok": True})


# ---- API: export timed excel (for PDF script or download)
@app.get("/export/timed.xlsx")
def export_timed_xlsx():
    s, sid = default_schedule()

    timed = pd.DataFrame(s.get("timed", []))
    if timed.empty:
        abort(404)
    timed.to_excel(TIMED_XLSX_OUT, index=False)
    return send_file(TIMED_XLSX_OUT, as_attachment=True)


# ---- Optional: generate PDF via existing Script 4
@app.post("/api/export_pdf")
def api_export_pdf():
    require_edit_token()
    if not mod4_exists:
        return jsonify({"ok": False, "error": "Script 4 not found"}), 400

    s, sid = default_schedule()

    timed = pd.DataFrame(s.get("timed", []))
    if timed.empty:
        return jsonify({"ok": False, "error": "No timed schedule available"}), 400

    timed.to_excel(TIMED_XLSX_OUT, index=False)

    try:
        subprocess.check_call(["python", SCRIPT4])
    except subprocess.CalledProcessError as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True})


@app.get("/export/pdf")
def download_pdf():
    # Script 4 usually writes DCO_Rehearsal_Schedule.pdf
    pdf_path = "DCO_Rehearsal_Schedule.pdf"
    if not os.path.exists(pdf_path):
        abort(404)
    return send_file(pdf_path, as_attachment=True)


@app.get("/s/<schedule_id>/pdf")
def download_schedule_pdf(schedule_id):
    """Download PDF for a specific schedule - accessible by members"""
    s = load_schedule(schedule_id)
    if not s:
        abort(404)

    u = current_user()
    # Check permissions
    if u and u.get("is_admin"):
        pass
    else:
        if s.get("status") != "published":
            abort(404)
        r = ensemble_member_required_or_403(s["ensemble_id"])
        if r: return r

    # Get ensemble name and concerts
    ensembles = load_ensembles()
    ensemble = next((e for e in ensembles if e["id"] == s.get("ensemble_id")), None)
    ensemble_name = ensemble["name"] if ensemble else s.get("ensemble_id", "Schedule")
    
    # Get linked concerts
    linked_concerts = []
    if ensemble:
        linked_concerts = [c for c in ensemble.get("concerts", []) if c.get("schedule_id") == schedule_id and c.get("status") == "scheduled"]

    # Get timed schedule and clean it to ensure sections are populated
    timed_data = clean_timed_data(s.get("timed", []))
    timed = pd.DataFrame(timed_data)
    if timed.empty:
        return "No schedule data available", 404
    
    # Get rehearsals data to check event types
    rehearsals_data = s.get("rehearsals", [])
    rehearsals_df = pd.DataFrame(rehearsals_data) if rehearsals_data else pd.DataFrame()

    # Create PDF in memory
    from io import BytesIO
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
    from reportlab.lib.enums import TA_CENTER, TA_LEFT

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=1.5*cm, leftMargin=1.5*cm, 
                           topMargin=2*cm, bottomMargin=1.5*cm)
    
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('Title', parent=styles['Heading1'], alignment=TA_CENTER, 
                                 fontSize=18, spaceAfter=12)
    heading_style = ParagraphStyle('Heading', parent=styles['Heading2'], fontSize=14, 
                                   spaceAfter=8, spaceBefore=12)
    
    story = []
    
    # Title
    story.append(Paragraph(f"{ensemble_name}", title_style))
    story.append(Paragraph("Rehearsal Schedule", title_style))
    story.append(Spacer(1, 0.5*cm))
    
    # Build a list of all events (rehearsals and concerts) with their dates for chronological sorting
    all_events = []
    
    # Add rehearsals (but skip concerts - they're added separately from linked_concerts)
    for reh_num, group in timed.groupby("Rehearsal"):
        # Check if this rehearsal is actually a concert in the rehearsals table
        is_concert = False
        if not rehearsals_df.empty:
            reh_row = rehearsals_df[rehearsals_df['Rehearsal'].astype(str) == str(int(reh_num))]
            if not reh_row.empty:
                event_type = reh_row.iloc[0].get('Event Type', 'Rehearsal')
                if event_type == 'Concert':
                    is_concert = True
        
        # Skip concerts - they're handled separately
        if is_concert:
            continue
        
        date_val = group.iloc[0].get("Date")
        event_type = group.iloc[0].get("Event Type", "Rehearsal")
        
        # Ensure date is a Timestamp for consistent sorting
        try:
            if pd.isna(date_val):
                date_ts = pd.NaT
            elif isinstance(date_val, str):
                date_ts = pd.to_datetime(date_val, errors='coerce')
            elif hasattr(date_val, 'strftime'):
                date_ts = pd.Timestamp(date_val)
            else:
                date_ts = pd.to_datetime(date_val, errors='coerce')
        except:
            date_ts = pd.NaT
        
        all_events.append({
            'type': 'rehearsal',
            'reh_num': reh_num,
            'group': group,
            'date': date_ts,
            'event_type': event_type
        })
    
    # Add concerts
    if linked_concerts:
        for concert in linked_concerts:
            concert_date = concert.get('date', '')
            try:
                date_obj = pd.to_datetime(concert_date) if concert_date else pd.NaT
            except:
                date_obj = pd.NaT
            all_events.append({
                'type': 'concert',
                'concert': concert,
                'date': date_obj
            })
    
    # Sort all events chronologically (all dates are now Timestamps or NaT)
    all_events.sort(key=lambda x: x['date'] if not pd.isna(x['date']) else pd.Timestamp.max)
    
    # Render events in chronological order
    for event in all_events:
        if event['type'] == 'concert':
            # Render concert
            concert = event['concert']
            concert_data = []
            
            # Parse date for better formatting
            concert_date = concert.get('date', '')
            try:
                if concert_date:
                    date_obj = pd.to_datetime(concert_date)
                    day = date_obj.day
                    if 10 <= day % 100 <= 20:
                        suffix = 'th'
                    else:
                        suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')
                    formatted_date = date_obj.strftime(f'%A {day}{suffix} %B, %Y')
                else:
                    formatted_date = 'TBA'
            except:
                formatted_date = concert_date
            
            concert_data.append([Paragraph(f"<b>{concert.get('title', 'Untitled Concert')}</b>", styles['Normal'])])
            concert_data.append([Paragraph(f"<b>Date:</b> {formatted_date}", styles['Normal'])])
            concert_data.append([Paragraph(f"<b>Time:</b> {concert.get('time', 'TBA')}", styles['Normal'])])
            
            if concert.get('venue'):
                concert_data.append([Paragraph(f"<b>Venue:</b> {concert.get('venue')}", styles['Normal'])])
            if concert.get('uniform'):
                concert_data.append([Paragraph(f"<b>Uniform:</b> {concert.get('uniform')}", styles['Normal'])])
            if concert.get('programme'):
                concert_data.append([Paragraph(f"<b>Programme:</b> {concert.get('programme')}", styles['Normal'])])
            if concert.get('other_info'):
                concert_data.append([Paragraph(f"<b>Notes:</b> {concert.get('other_info')}", styles['Normal'])])
            
            concert_table = Table(concert_data, colWidths=[16*cm])
            concert_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#dbeafe')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#1e40af')),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 12),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
                ('TOPPADDING', (0, 0), (-1, -1), 8),
                ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#93c5fd')),
                ('BOX', (0, 0), (-1, -1), 2, colors.HexColor('#3b82f6')),
            ]))
            story.append(concert_table)
            story.append(Spacer(1, 0.5*cm))
            
        else:
            # Render rehearsal
            reh_num = event['reh_num']
            group = event['group']
            
            # Get date
            date_val = group.iloc[0].get("Date")
            if not pd.isna(date_val):
                try:
                    if isinstance(date_val, (int, float)):
                        date_obj = pd.to_datetime(date_val, unit='ms')
                    elif hasattr(date_val, 'strftime'):
                        date_obj = date_val
                    else:
                        date_obj = pd.to_datetime(date_val)
                    
                    day = date_obj.day
                    if 10 <= day % 100 <= 20:
                        suffix = 'th'
                    else:
                        suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')
                    date_str = date_obj.strftime(f'%A {day}{suffix} %B, %Y')
                except:
                    date_str = str(date_val)
            else:
                date_str = ""
            
            # Get section
            section = group.iloc[0].get("Section", "Full Ensemble") if not group.empty else "Full Ensemble"
            
            # Rehearsal heading
            reh_heading = f"<b>Rehearsal {int(reh_num)}</b> - {date_str}"
            if section and section != "Full Ensemble":
                reh_heading += f" ({section})"
            story.append(Paragraph(reh_heading, heading_style))
            
            # Table data
            table_data = [['Time', 'Item', 'Break']]
            for _, row in group.iterrows():
                time_str = row.get("Time in Rehearsal", "")
                title = row.get("Title", "")
                break_info = ""
                if row.get("Break Start (HH:MM)"):
                    break_info = f"{row.get('Break Start (HH:MM)')} - {row.get('Break End (HH:MM)')}"
                
                table_data.append([time_str, title, break_info])
            
            # Create table
            table = Table(table_data, colWidths=[3*cm, 10*cm, 4*cm])
            table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 10),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
                ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
                ('GRID', (0, 0), (-1, -1), 1, colors.black),
                ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 1), (-1, -1), 9),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ]))
            
            story.append(table)
            story.append(Spacer(1, 0.5*cm))
    
    # Build PDF
    doc.build(story)
    buffer.seek(0)
    
    filename = f"{ensemble_name.replace(' ', '_')}_Schedule.pdf"
    return send_file(buffer, as_attachment=True, download_name=filename, mimetype='application/pdf')


@app.get("/my/pdf")
def download_my_schedule_pdf():
    """Download personalized PDF showing all rehearsals for the current user"""
    redir = login_required_or_redirect()
    if redir:
        return redir
    
    u = current_user()
    ensembles = load_ensembles()
    memberships = load_memberships()

    if u and u.get("is_admin"):
        my_ensembles = ensembles
    else:
        my_ids = {m.get("ensemble_id") for m in memberships if m.get("user_id") == (u or {}).get("id") and m.get("status", "active") == "active"}
        my_ensembles = [e for e in ensembles if e.get("id") in my_ids]

    # Get published schedules for user's ensembles
    schedule_summaries = []
    all_schedule_ids = list_schedule_ids()
    
    for sid in all_schedule_ids:
        s = load_schedule(sid)
        if not s:
            continue

        if u and u.get("is_admin"):
            schedule_summaries.append(s)
        else:
            if s.get("status") != "published":
                continue
            if any(e["id"] == s.get("ensemble_id") for e in my_ensembles):
                schedule_summaries.append(s)

    # Extract rehearsal data
    rehearsals_list = []
    ensemble_map = {e["id"]: e["name"] for e in ensembles}
    
    for sched in schedule_summaries:
        schedule_id = sched["id"]
        ensemble_id = sched.get("ensemble_id")
        ensemble_name = ensemble_map.get(ensemble_id, "Unknown")
        
        try:
            timed_data = sched.get("timed", [])
            if not timed_data:
                continue
            
            timed_df = pd.DataFrame(timed_data)
            if timed_df.empty:
                continue
                
            # Group by rehearsal number
            grouped = timed_df.groupby("Rehearsal")
            
            for reh_num, group in grouped:
                date_val = group.iloc[0].get("Date")
                
                # Skip if date is NaT or missing
                if pd.isna(date_val) or date_val is None:
                    print(f"[PDF] Skipping rehearsal {reh_num} - NaT or None date")
                    continue
                    
                try:
                    if isinstance(date_val, (int, float)):
                        date_obj = pd.to_datetime(date_val, unit='ms')
                    elif hasattr(date_val, 'to_pydatetime'):
                        date_obj = date_val.to_pydatetime()
                    else:
                        date_obj = pd.to_datetime(date_val)
                    
                    # Additional check: ensure date_obj is valid
                    if pd.isna(date_obj):
                        print(f"[PDF] Skipping rehearsal {reh_num} - date conversion resulted in NaT")
                        continue
                        
                except Exception as e:
                    print(f"[PDF] Skipping rehearsal {reh_num} - date parsing failed: {e}")
                    continue
                
                # Get section
                section = group.iloc[0].get("Section", "Full Ensemble") if not group.empty else "Full Ensemble"
                
                rehearsals_list.append({
                    "date": date_obj,
                    "ensemble_name": ensemble_name,
                    "rehearsal_num": int(reh_num),
                    "section": section,
                    "items": group.to_dict(orient="records")
                })
        except Exception as e:
            print(f"Error processing schedule {schedule_id}: {e}")
            continue
    
    # Sort by date
    rehearsals_list.sort(key=lambda x: x["date"])
    
    if not rehearsals_list:
        return "No rehearsals found", 404

    # Create PDF
    from io import BytesIO
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.enums import TA_CENTER, TA_LEFT

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=1.5*cm, leftMargin=1.5*cm, 
                           topMargin=2*cm, bottomMargin=1.5*cm)
    
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('Title', parent=styles['Heading1'], alignment=TA_CENTER, 
                                 fontSize=18, spaceAfter=12)
    heading_style = ParagraphStyle('Heading', parent=styles['Heading2'], fontSize=12, 
                                   spaceAfter=6, spaceBefore=10)
    
    story = []
    
    # Title
    story.append(Paragraph("My Rehearsal Schedule", title_style))
    story.append(Paragraph(f"{u.get('email', 'Member')}", ParagraphStyle('Subtitle', parent=styles['Normal'], 
                                                                          alignment=TA_CENTER, fontSize=11, textColor=colors.grey)))
    story.append(Spacer(1, 0.5*cm))
    
    # Group by date for summary
    current_date = None
    for reh in rehearsals_list:
        date_obj = reh["date"]
        
        # Skip if date is NaT (safety check)
        if pd.isna(date_obj):
            print(f"[PDF] Skipping rehearsal {reh.get('rehearsal_num')} in rendering - NaT date")
            continue
        
        # Date heading
        if current_date is None or current_date.date() != date_obj.date():
            current_date = date_obj
            day = date_obj.day
            if 10 <= day % 100 <= 20:
                suffix = 'th'
            else:
                suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')
            date_str = date_obj.strftime(f'%A {day}{suffix} %B, %Y')
            
            story.append(Paragraph(date_str, heading_style))
        
        # Rehearsal info
        reh_info = f"<b>{reh['ensemble_name']}</b> - Rehearsal {reh['rehearsal_num']}"
        if reh.get('section') and reh['section'] != 'Full Ensemble':
            reh_info += f" ({reh['section']})"
        story.append(Paragraph(reh_info, 
                              ParagraphStyle('RehInfo', parent=styles['Normal'], fontSize=10, leftIndent=0.5*cm, spaceAfter=4)))
        
        # Table data
        table_data = [['Time', 'Item']]
        for item in reh['items']:
            time_str = item.get("Time in Rehearsal", "")
            title = item.get("Title", "")
            table_data.append([time_str, title])
        
        # Create table
        table = Table(table_data, colWidths=[3*cm, 12*cm])
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 9),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
            ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 1), (-1, -1), 8),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ]))
        
        story.append(table)
        story.append(Spacer(1, 0.3*cm))
    
    # Build PDF
    doc.build(story)
    buffer.seek(0)
    
    filename = f"My_Rehearsal_Schedule.pdf"
    return send_file(buffer, as_attachment=True, download_name=filename, mimetype='application/pdf')



# ----------------------------
# Main
# ----------------------------
if __name__ == "__main__":
    # For local dev:
    #   EDIT_TOKEN=... flask --app app run --debug
    # Note: use_reloader=False to avoid watchdog issues watching too many site-packages files
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True, use_reloader=False)
