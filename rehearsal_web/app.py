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
    return read_json(p, None) or {}  # see safe read_json note below

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


def safe_int(x, default=0) -> int:
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
        "Title", "Duration", "Difficulty",
        "Flute", "Oboe", "Clarinet", "Bassoon",
        "Horn", "Trumpet", "Trombone", "Tuba",
        "Violin 1", "Violin 2", "Viola", "Cello", "Bass",
        "Percussion", "Timpani", "Piano", "Harp", "Soloist"
    ]
    return pd.DataFrame(columns=cols)


def default_rehearsals_df() -> pd.DataFrame:
    cols = ["Rehearsal", "Date", "Day", "Start Time", "End Time", "Break", "Percs", "Piano", "Harp", "Brass", "Soloist"]
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
        works = works.reindex(columns=works_cols, fill_value="")
    else:
        # Fall back to defaults
        works = works.reindex(columns=default_works_df().columns, fill_value="")
    
    # Load rehearsals with saved column order if available
    rehearsals_cols = s.get("rehearsals_cols")
    rehearsals = pd.DataFrame(s.get("rehearsals", []))
    if rehearsals_cols:
        # Use saved columns in saved order, fill missing columns with empty strings
        rehearsals = rehearsals.reindex(columns=rehearsals_cols, fill_value="")
    else:
        # Fall back to defaults
        rehearsals = rehearsals.reindex(columns=default_rehearsals_df().columns, fill_value="")
    
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


def prepare_rehearsals_for_allocator(rehearsals_df: pd.DataFrame) -> pd.DataFrame:
    r = rehearsals_df.copy()
    r = r[r["Rehearsal"].astype(str).str.strip().ne("")].copy()
    r["Rehearsal"] = pd.to_numeric(r["Rehearsal"], errors="coerce").astype("Int64")
    r = r[r["Rehearsal"].notna()].copy()

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

    return r.sort_values("Rehearsal").reset_index(drop=True)


def run_allocation_compute(works_df: pd.DataFrame, rehearsals_df: pd.DataFrame, G: int) -> Tuple[pd.DataFrame, List[str]]:
    works = prepare_works_for_allocator(works_df)
    rehe = prepare_rehearsals_for_allocator(rehearsals_df)

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
    if schedule_df.empty:
        return pd.DataFrame()

    rehe = rehearsals_prepared.set_index("Rehearsal", drop=False)
    out_rows = []

    for rnum, df_r in schedule_df.groupby("Rehearsal", sort=True):
        rnum_i = int(rnum)  # type: ignore
        if rnum_i not in rehe.index:
            continue

        rrow = rehe.loc[rnum_i]
        date = rrow.get("Date")
        start_dt = pd.to_datetime(rrow.get("Start DateTime"), errors="coerce")  # type: ignore
        if pd.isna(start_dt):
            start_dt = pd.to_datetime("2000-01-01 19:00")

        break_mins = safe_int(rrow.get("Break (minutes)", 0), 0)

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
            })
            elapsed += mins

    out = pd.DataFrame(out_rows)
    out["Date"] = pd.to_datetime(out["Date"], errors="coerce")
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
    return jsonify({
        "works_cols": s.get("works_cols", []),
        "rehearsals_cols": s.get("rehearsals_cols", []),
        "works": s.get("works", []),
        "rehearsals": s.get("rehearsals", []),
        "allocation": s.get("allocation", []),
        "schedule": s.get("schedule", []),
        "timed": s.get("timed", []),
    })

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

    try:
        alloc_df, warnings = run_allocation_compute(works, rehearsals, G)
        s["allocation"] = sanitize_df_records(alloc_df.to_dict(orient="records"))
        s["warnings"] = warnings
        s["updated_at"] = int(time.time())
        save_schedule(s)
        return jsonify({"ok": True, "warnings": warnings})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
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

    rehe_prep = prepare_rehearsals_for_allocator(rehearsals)
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
        s["rehearsals"] = df.fillna("").to_dict(orient="records")

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

    # Update timed schedule
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
            if duration:
                duration = int(duration)
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

    # Get ensemble name
    ensembles = load_ensembles()
    ensemble = next((e for e in ensembles if e["id"] == s.get("ensemble_id")), None)
    ensemble_name = ensemble["name"] if ensemble else s.get("ensemble_id", "Schedule")

    timed = pd.DataFrame(s.get("timed", []))
    response = render_template("view.html", timed=timed.to_dict(orient="records"), has_schedule=not timed.empty, 
                               ensemble_name=ensemble_name, schedule_id=schedule_id, user=u)
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
    return render_template("member_dashboard.html", user=u)


@app.get("/api/member/rehearsals")
def api_member_rehearsals():
    redir = login_required_or_redirect()
    if redir:
        return jsonify({"error": "Not logged in"}), 401
    
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

    # Extract rehearsal data with dates and times
    rehearsals_list = []
    ensemble_map = {e["id"]: e["name"] for e in ensembles}
    
    for sched in schedule_summaries:
        schedule_id = sched["id"]
        ensemble_id = sched.get("ensemble_id")
        ensemble_name = ensemble_map.get(ensemble_id, "Unknown")
        
        # Get timed schedule data directly from the schedule
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
                # Get date from first row
                date_val = group.iloc[0].get("Date")
                if pd.isna(date_val):
                    continue
                    
                # Convert date to ISO format
                try:
                    # Try parsing as pandas datetime first (handles most formats)
                    if isinstance(date_val, (int, float)):
                        # Timestamp in milliseconds
                        date_obj = pd.to_datetime(date_val, unit='ms').date()
                        date_str = date_obj.isoformat()
                    elif hasattr(date_val, 'date'):
                        # Already a datetime object
                        date_str = date_val.date().isoformat() if hasattr(date_val, 'date') else date_val.isoformat()
                    else:
                        # String - parse with pandas (handles many formats automatically)
                        date_obj = pd.to_datetime(date_val).date()
                        date_str = date_obj.isoformat()
                except Exception as e:
                    print(f"  Failed to parse date '{date_val}' (type: {type(date_val)}): {e}")
                    continue
                
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
                    "rehearsal_num": int(reh_num),
                    "date": date_str,
                    "items": items
                })
        except Exception as e:
            print(f"Error processing schedule {schedule_id}: {e}")
            import traceback
            traceback.print_exc()
            continue
    
    # Sort by date
    rehearsals_list.sort(key=lambda x: (x["date"], x["ensemble_name"], x["rehearsal_num"]))
    
    return jsonify(rehearsals_list)

    # Extract rehearsal data with dates and times
    rehearsals_list = []
    ensemble_map = {e["id"]: e["name"] for e in ensembles}
    
    print(f"Processing {len(schedule_summaries)} schedules for member rehearsals API")
    
    for sched in schedule_summaries:
        schedule_id = sched["id"]
        ensemble_id = sched.get("ensemble_id")
        ensemble_name = ensemble_map.get(ensemble_id, "Unknown")
        
        print(f"Processing schedule {schedule_id} for ensemble {ensemble_name}")
        
        # Get timed schedule data
        try:
            timed_df = extract_timed_schedule(sched)
            if timed_df is None or timed_df.empty:
                print(f"  No timed data for schedule {schedule_id}")
                continue
                
            # Group by rehearsal number
            grouped = timed_df.groupby("Rehearsal")
            
            for reh_num, group in grouped:
                # Get date from first row
                date_val = group.iloc[0].get("Date")
                if pd.isna(date_val):
                    print(f"  Rehearsal {reh_num} has no date")
                    continue
                    
                # Convert date to ISO format
                if isinstance(date_val, str):
                    # Already a string, try to parse it
                    try:
                        date_obj = pd.to_datetime(date_val, dayfirst=True).date()
                        date_str = date_obj.isoformat()
                    except Exception as e:
                        print(f"  Failed to parse date '{date_val}': {e}")
                        continue
                elif hasattr(date_val, 'date'):
                    date_str = date_val.date().isoformat()
                else:
                    print(f"  Unknown date type for rehearsal {reh_num}: {type(date_val)}")
                    continue
                
                print(f"  Found rehearsal {reh_num} on {date_str}")
                
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
                    "rehearsal_num": int(reh_num),
                    "date": date_str,
                    "items": items
                })
        except Exception as e:
            print(f"Error processing schedule {schedule_id}: {e}")
            continue
    
    # Sort by date
    rehearsals_list.sort(key=lambda x: (x["date"], x["ensemble_name"], x["rehearsal_num"]))
    
    return jsonify(rehearsals_list)


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
        timed = compute_timed_df(sched, prepare_rehearsals_for_allocator(rehearsals))

    # Resolve ensemble display name
    ensembles = load_ensembles()
    ens = next((e for e in ensembles if e["id"] == s.get("ensemble_id")), None)
    ensemble_name = ens["name"] if ens else s.get("ensemble_id", "")

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
    s["rehearsals"] = payload.get("rehearsals", [])

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

    rehe_prep = prepare_rehearsals_for_allocator(rehearsals)
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
    s["rehearsals_cols"] = rehe_cols
    s["works"] = df_works.to_dict(orient="records")
    s["rehearsals"] = df_rehe.to_dict(orient="records")

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

    rehe_prep = prepare_rehearsals_for_allocator(rehearsals)
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

    # Get ensemble name
    ensembles = load_ensembles()
    ensemble = next((e for e in ensembles if e["id"] == s.get("ensemble_id")), None)
    ensemble_name = ensemble["name"] if ensemble else s.get("ensemble_id", "Schedule")

    # Get timed schedule
    timed = pd.DataFrame(s.get("timed", []))
    if timed.empty:
        return "No schedule data available", 404

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
    
    # Group by rehearsal
    for reh_num, group in timed.groupby("Rehearsal"):
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
        
        # Rehearsal heading
        story.append(Paragraph(f"<b>Rehearsal {int(reh_num)}</b> - {date_str}", heading_style))
        
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
                if pd.isna(date_val):
                    continue
                    
                try:
                    if isinstance(date_val, (int, float)):
                        date_obj = pd.to_datetime(date_val, unit='ms')
                    elif hasattr(date_val, 'to_pydatetime'):
                        date_obj = date_val.to_pydatetime()
                    else:
                        date_obj = pd.to_datetime(date_val)
                except:
                    continue
                
                rehearsals_list.append({
                    "date": date_obj,
                    "ensemble_name": ensemble_name,
                    "rehearsal_num": int(reh_num),
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
        story.append(Paragraph(f"<b>{reh['ensemble_name']}</b> - Rehearsal {reh['rehearsal_num']}", 
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
