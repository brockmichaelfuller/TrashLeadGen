"""SQLite storage for leads, replacing the old CSV-file-plus-advisory-lock approach.

`phone` is the primary key, so the database itself is what prevents duplicates now -- no more
in-memory "seen" bookkeeping racing a concurrent writer, and no more fcntl-based locking between
the scraper subprocess and the web app: SQLite's own WAL journal mode plus a busy_timeout give real
transactional concurrency for free. Every function here opens and closes its own short-lived
connection, which is what makes this safe to call from any thread or process without extra care.

GitHub/Google Sheets backup (sync_leads.py) is untouched and still speaks CSV -- export_to_csv/
import_from_csv are the bridge at that boundary, so the already-tested backup/restore logic never
has to know the local store changed.
"""
import csv
import json
import os
import sqlite3
import threading
from datetime import date
from pathlib import Path

import sync_leads

AUDIT_LOG_PATH = Path(__file__).parent / "audit_log.json"

COLUMNS = ["company_name", "phone", "email", "website", "address", "city", "state", "timezone",
           "source", "date_collected", "status", "notes", "rejected_at"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    phone TEXT PRIMARY KEY,
    company_name TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    website TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT '',
    timezone TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    date_collected TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    rejected_at TEXT NOT NULL DEFAULT ''
)
"""

_INSERT_COLUMNS = ", ".join(COLUMNS)
_INSERT_PLACEHOLDERS = ", ".join(f":{c}" for c in COLUMNS)


def connect(db_path):
    """A fresh, short-lived connection with the schema ensured. WAL mode lets the scraper subprocess
    write while the web app reads/writes without either blocking the other; busy_timeout makes a
    write that does collide wait a moment and retry instead of raising "database is locked"."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute(_SCHEMA)
    return conn


def _as_dict(row):
    return {k: row[k] for k in row.keys()}


def all_leads(db_path, include_rejected=False):
    """Every lead, newest-inserted first (matches the old CSV's "most recently appended" ordering
    that the site's default no-sort view relied on)."""
    with connect(db_path) as conn:
        clause = "" if include_rejected else "WHERE rejected_at = ''"
        rows = conn.execute(f"SELECT * FROM leads {clause} ORDER BY rowid").fetchall()
        return [_as_dict(r) for r in rows]


def existing_phones(db_path):
    with connect(db_path) as conn:
        return {r["phone"] for r in conn.execute("SELECT phone FROM leads").fetchall()}


def insert_if_new(db_path, row):
    """Insert a scraped row if its phone isn't already present. Returns True if it was inserted --
    the primary key constraint is what actually guarantees no duplicate ever lands, even if two
    writers raced to insert the same phone at once."""
    values = {c: (row.get(c) or "") for c in COLUMNS}
    with connect(db_path) as conn:
        cur = conn.execute(f"INSERT OR IGNORE INTO leads ({_INSERT_COLUMNS}) VALUES ({_INSERT_PLACEHOLDERS})", values)
        conn.commit()
        return cur.rowcount > 0


def update_fields(db_path, phone, updates):
    """Set arbitrary fields on the row with this phone. Returns False if the phone isn't found."""
    if not updates:
        return False
    with connect(db_path) as conn:
        if conn.execute("SELECT 1 FROM leads WHERE phone = ?", (phone,)).fetchone() is None:
            return False
        set_clause = ", ".join(f"{k} = :{k}" for k in updates)
        conn.execute(f"UPDATE leads SET {set_clause} WHERE phone = :phone", {**updates, "phone": phone})
        conn.commit()
        return True


def is_rejected(db_path, phone):
    with connect(db_path) as conn:
        row = conn.execute("SELECT rejected_at FROM leads WHERE phone = ?", (phone,)).fetchone()
        return bool(row and row["rejected_at"])


def reject_phones(db_path, entries):
    """Ensure every phone in `entries` (phone -> company_name) is rejected: inserts a placeholder
    row with rejected_at set if the phone isn't known yet, or sets rejected_at on it if it exists
    but isn't rejected yet. Never overwrites an already-set rejected_at. Used to import leads that
    were rejected before permanent rejection existed (deleted outright by older code, so no
    rejected_at was ever recorded for them) -- without this, a later scrape treats their phone as
    new and adds them right back, silently undoing a decision that was already made."""
    if not entries:
        return
    today = date.today().isoformat()
    with connect(db_path) as conn:
        for phone, company_name in entries.items():
            conn.execute(
                "INSERT INTO leads (phone, company_name, rejected_at) VALUES (?, ?, ?) "
                "ON CONFLICT(phone) DO UPDATE SET rejected_at = excluded.rejected_at WHERE rejected_at = ''",
                (phone, company_name, today))
        conn.commit()


def import_audit_log_rejections(db_path):
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
    reject_phones(db_path, deleted)


def export_to_csv(db_path, csv_path):
    """Snapshot the whole table to a CSV -- this is what sync_leads.py backs up and restores.
    Both the web app (on a debounce timer, after an edit) and the scraper subprocess (after every
    state) call this independently, so two exports can genuinely overlap. Written to a temp file
    and atomically renamed into place rather than truncated-and-rewritten in place, so a concurrent
    reader (sync_leads pushing this same path) can never see a half-written file -- os.replace is
    atomic on the same filesystem, and the temp file lives right next to the target for that."""
    with connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM leads ORDER BY rowid").fetchall()
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = csv_path.with_name(f".{csv_path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    with tmp_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row[c] for c in COLUMNS})
    os.replace(tmp_path, csv_path)


def import_from_csv(db_path, csv_path):
    """Populate the database from a CSV -- used only to seed a freshly created (empty) database from
    a restored backup, on a host that's never had a database of its own yet."""
    if not csv_path.exists():
        return
    with csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    with connect(db_path) as conn:
        for row in rows:
            if not (row.get("phone") or "").strip():
                continue  # a stray blank separator row from an older Sheets export, if one slipped through
            values = {c: (row.get(c) or "") for c in COLUMNS}
            conn.execute(f"INSERT OR REPLACE INTO leads ({_INSERT_COLUMNS}) VALUES ({_INSERT_PLACEHOLDERS})", values)
        conn.commit()


def is_empty(db_path):
    if not Path(db_path).exists():
        return True
    with connect(db_path) as conn:
        return conn.execute("SELECT 1 FROM leads LIMIT 1").fetchone() is None


def _pause_flag_path(db_path):
    return Path(db_path).with_name(Path(db_path).name + ".backups_paused")


def backups_paused(db_path):
    """Whether external (GitHub/Sheets) backup pushes are currently paused -- a plain flag file
    next to the database, not an environment variable, so it can be toggled at runtime by anyone
    with access to the page without restarting the service (an env var change on Render restarts
    it). Checked by sync_backup() directly, so it applies the same way regardless of whether the
    web app or the scraper subprocess is the one calling it."""
    return _pause_flag_path(db_path).exists()


def set_backups_paused(db_path, paused):
    flag = _pause_flag_path(db_path)
    if paused:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.touch()
    else:
        flag.unlink(missing_ok=True)


def sync_backup(db_path):
    """Export the database to its companion CSV and back that up -- sync_leads.py only ever speaks
    CSV, so this is the bridge that lets the (already-tested) GitHub/Sheets logic stay untouched.
    The local CSV export always happens (it's just a local file, and keeps it current for whenever
    backups resume); the actual external push is skipped while paused. Returns the failure message
    if the push failed just now, or None (also the case while paused, since that isn't a failure)."""
    csv_path = Path(db_path).with_suffix(".csv")
    export_to_csv(db_path, csv_path)
    if backups_paused(db_path):
        return None
    return sync_leads.sync(csv_path)


def restore_if_empty(db_path):
    """Recover prior runs' data on a fresh (empty) host: pull the last backup into the companion CSV
    and import it -- but only when there's actually nothing here yet, since an existing database is
    always the more current copy. Both app.py (on startup) and lead_scraper.py (at the start of a
    run) call this, so a fresh container ends up with real data regardless of which one runs first.

    Returns the failure message if a configured backend actually errored during restore, or None
    otherwise. Hit live: a transient Supabase outage at start-up used to be treated exactly like a
    genuinely-new install -- the database stayed empty, the page showed "No leads yet", and a scrape
    run from that state re-inserted every lead fresh with blank status/notes/rejected_at, which the
    next backup then upserted straight over Supabase's real values. Callers MUST check this return
    value and refuse to proceed (no scrape, no backup push) rather than treat a non-None result as
    if the database were just empty -- see app.py's restore_state and lead_scraper.py's run()."""
    if not is_empty(db_path):
        return None
    csv_path = Path(db_path).with_suffix(".csv")
    error = sync_leads.restore(csv_path)
    if error:
        return error
    import_from_csv(db_path, csv_path)
    return None
