"""Postgres (Supabase) storage for leads -- this is the only place leads live. Every read and write
goes straight to it; there is no local database, no local cache, and nothing to restore on startup.
`phone` is the primary key, so the database itself is what prevents duplicates -- no in-memory
"seen" bookkeeping racing a concurrent writer.

Each function opens and closes its own short-lived connection, the same pattern the old SQLite
version used -- it's what makes this safe to call from any thread or the scraper subprocess without
extra coordination, since Postgres's own transactional guarantees do the rest. A connection failure
(Supabase unreachable, misconfigured, paused) is not swallowed here -- it raises psycopg2.Error, and
callers (app.py's request handlers, lead_scraper.py's run loop) are responsible for turning that into
something the person using the app can actually see, rather than it silently vanishing into a log no
one's watching. There's no local fallback to silently keep working on: if Supabase is down, saving a
lead is down too, and that has to be visible, not hidden.
"""
import contextlib
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

import psycopg2

AUDIT_LOG_PATH = Path(__file__).parent / "audit_log.json"

LEADS_TABLE = "leads"

COLUMNS = ["company_name", "phone", "email", "website", "address", "city", "state", "timezone",
           "source", "date_collected", "status", "notes", "rejected_at", "updated_at", "created_at"]


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def connect():
    conn_str = os.environ.get("SUPABASE_DB_URL")
    if not conn_str:
        raise RuntimeError(
            "SUPABASE_DB_URL is not set. Supabase is this app's only storage -- it can't run without "
            "a connection string. See README's \"Setting up storage\" section.")
    return psycopg2.connect(conn_str, connect_timeout=10)


@contextlib.contextmanager
def _cursor():
    """A connection used as `with psycopg2.connect(...) as conn:` commits or rolls back on exit, but
    -- easy to miss -- does NOT close the underlying connection. With a connection opened per call
    (and now every single read or write making one, not just occasional backup pushes), that would
    leak a connection on every request. This closes it either way."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _as_dict(columns, row):
    return dict(zip(columns, row))


def all_leads(include_rejected=False):
    """Every lead, oldest-inserted first (the page reverses this to show newest first by default) --
    matches the old SQLite version's "insertion order" contract exactly, just backed by an explicit
    created_at column instead of rowid."""
    query = f"SELECT {', '.join(COLUMNS)} FROM {LEADS_TABLE}"
    if not include_rejected:
        query += " WHERE rejected_at = ''"
    query += " ORDER BY created_at, phone"
    with _cursor() as cur:
        cur.execute(query)
        rows = cur.fetchall()
    return [_as_dict(COLUMNS, row) for row in rows]


def existing_phones():
    with _cursor() as cur:
        cur.execute(f"SELECT phone FROM {LEADS_TABLE}")
        return {row[0] for row in cur.fetchall()}


def insert_if_new(row):
    """Insert a scraped row if its phone isn't already present. Returns True if it was inserted --
    the primary key constraint is what actually guarantees no duplicate ever lands, even if two
    writers raced to insert the same phone at once."""
    now = _now_iso()
    values = {c: (row.get(c) or "") for c in COLUMNS}
    values["updated_at"] = now
    values["created_at"] = now
    columns = list(values.keys())
    placeholders = ", ".join(["%s"] * len(columns))
    query = (f"INSERT INTO {LEADS_TABLE} ({', '.join(columns)}) VALUES ({placeholders}) "
             "ON CONFLICT (phone) DO NOTHING")
    with _cursor() as cur:
        cur.execute(query, [values[c] for c in columns])
        return cur.rowcount > 0


def update_fields(phone, updates):
    """Set arbitrary fields on the row with this phone, stamping updated_at. Returns False if the
    phone isn't found."""
    if not updates:
        return False
    updates = {**updates, "updated_at": _now_iso()}
    set_clause = ", ".join(f"{k} = %s" for k in updates)
    query = f"UPDATE {LEADS_TABLE} SET {set_clause} WHERE phone = %s"
    with _cursor() as cur:
        cur.execute(query, [*updates.values(), phone])
        return cur.rowcount > 0


def is_rejected(phone):
    with _cursor() as cur:
        cur.execute(f"SELECT rejected_at FROM {LEADS_TABLE} WHERE phone = %s", (phone,))
        row = cur.fetchone()
        return bool(row and row[0])


def reject_phones(entries):
    """Ensure every phone in `entries` (phone -> company_name) is rejected: inserts a placeholder
    row with rejected_at set if the phone isn't known yet, or sets rejected_at on it if it exists
    but isn't rejected yet. Never overwrites an already-set rejected_at. Used to import leads that
    were rejected before permanent rejection existed (deleted outright by older code, so no
    rejected_at was ever recorded for them) -- without this, a later scrape treats their phone as
    new and adds them right back, silently undoing a decision that was already made."""
    if not entries:
        return
    today = date.today().isoformat()
    now = _now_iso()
    with _cursor() as cur:
        for phone, company_name in entries.items():
            cur.execute(
                f"INSERT INTO {LEADS_TABLE} (phone, company_name, rejected_at, updated_at, created_at) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (phone) DO UPDATE SET rejected_at = EXCLUDED.rejected_at, "
                f"updated_at = EXCLUDED.updated_at WHERE {LEADS_TABLE}.rejected_at = ''",
                (phone, company_name, today, now, now))


def import_audit_log_rejections():
    """audit_log.json's "deleted" verdicts predate permanent rejection (rejected_at) -- they were
    removed outright by older code, so the database has no record they were ever reviewed and
    rejected. Re-applying them here (cheap and idempotent -- see reject_phones) means a later
    scrape can never re-add one as if it were new. Called on every startup by both app.py and
    lead_scraper.py's own CLI entry point, so it applies regardless of which one runs first."""
    try:
        reviewed = json.loads(AUDIT_LOG_PATH.read_text()).get("reviewed", {})
    except (OSError, ValueError):
        return
    deleted = {phone: entry.get("company", "") for phone, entry in reviewed.items()
               if entry.get("verdict") == "deleted"}
    reject_phones(deleted)
