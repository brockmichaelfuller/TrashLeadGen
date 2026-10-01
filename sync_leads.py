"""Durable backups for output/leads.csv, since Render's free plan has no persistent disk and
silently loses everything on every restart (redeploy, or just the container cycling after ~15
minutes idle).

Two independent, optional backends -- each a no-op unless its environment variables are set, so
running with neither configured behaves exactly like before:

- GitHub: commits the CSV to this repo after every change, and restores the latest commit back to
  disk when a fresh (empty) container starts. Needs GITHUB_TOKEN (a personal access token with
  Contents read/write on the repo) and GITHUB_REPO ("owner/name").
- Supabase: upserts every row (keyed on phone) into a Postgres `leads` table after every change, so
  the data is visible and durable outside of this app entirely, and survives a restart with no
  restore-then-import round trip -- it's just a normal database. Needs SUPABASE_DB_URL (a Postgres
  connection string; use the "Transaction pooler" one from Project Settings > Database, since it's
  IPv4-compatible and Render needs that). The `leads` table must already exist -- see README's
  "Making data permanent on Render" section for the create-table SQL.

Both fail silently (logging to stderr) rather than raising -- a GitHub or Supabase outage should
never block a scrape or an edit from saving locally.
"""
import base64
import csv
import os
import sys

import requests

GITHUB_API = "https://api.github.com"
GITHUB_CSV_PATH = "output/leads.csv"

SUPABASE_TABLE = "leads"

# Defense in depth against the restore-failure overwrite bug (see restore()'s docstring): even if a
# scrape somehow runs against a wrongly-empty local database, these two columns can never be blanked
# out in Supabase by an incoming empty value -- only a genuinely non-empty call outcome overwrites a
# previous one. rejected_at is deliberately NOT in this set: Undo has to be able to set it back to
# "" within seconds of a Remove, and that's a legitimate blank that must reach Supabase -- the
# primary defense for rejected_at is restore_if_empty() refusing to proceed on a failed restore in
# the first place, not this backstop.
NEVER_BLANK_COLUMNS = {"status", "notes"}

last_backup_error = None  # most recent backup failure message; None once sync() succeeds cleanly


def _warn(action, error):
    global last_backup_error
    last_backup_error = f"{action} failed: {error}"
    print(f"sync_leads: {last_backup_error}", file=sys.stderr)


def pull_github(local_path):
    """Overwrite local_path with the last copy committed to GitHub, if configured and present."""
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPO")
    if not token or not repo:
        return
    try:
        # The "raw" media type returns the file's actual bytes directly, instead of the default
        # JSON envelope's base64 "content" field -- which GitHub leaves empty for any file over
        # 1MB, silently restoring an empty leads.csv once the list grew past that size.
        response = requests.get(
            f"{GITHUB_API}/repos/{repo}/contents/{GITHUB_CSV_PATH}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.raw+json"},
            timeout=15,
        )
        if response.status_code == 200:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(response.content)
    except requests.RequestException as error:
        _warn("GitHub pull", error)


def push_github(local_path):
    """Commit the current local_path contents to GitHub, creating or updating the file as needed."""
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPO")
    if not token or not repo or not local_path.exists():
        return
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    url = f"{GITHUB_API}/repos/{repo}/contents/{GITHUB_CSV_PATH}"
    content = base64.b64encode(local_path.read_bytes()).decode()
    try:
        # The scraper subprocess and the web app's own debounce timer can each call this
        # independently, so two pushes can genuinely race: both read the same starting sha, and
        # whichever PUTs second gets a 409 (sha now stale) even though its content is still valid
        # to commit. One retry -- re-reading the sha the other push just created -- covers that
        # ordinary case without piling on indefinitely for a real, persistent conflict.
        for attempt in range(2):
            existing = requests.get(url, headers=headers, timeout=15)
            body = {"message": "Update scraped leads", "content": content}
            if existing.status_code == 200:
                body["sha"] = existing.json()["sha"]
            response = requests.put(url, headers=headers, json=body, timeout=15)
            if response.status_code in (200, 201):
                return
            if response.status_code != 409 or attempt == 1:
                _warn("GitHub push", f"HTTP {response.status_code}: {response.text[:200]}")
                return
    except requests.RequestException as error:
        _warn("GitHub push", error)


def push_supabase(local_path):
    """Upsert every row in local_path (keyed on phone) into the Supabase `leads` table. A plain
    INSERT ... ON CONFLICT DO UPDATE, unlike Sheets' old clear-then-write-the-whole-sheet approach --
    each row lands or updates independently, so there's no window where a failure mid-push could
    ever leave the table holding less than it already had. Never deletes a row Supabase already
    has, so a row briefly missing from local_path just wouldn't get touched, not erased."""
    conn_str = os.environ.get("SUPABASE_DB_URL")
    if not conn_str or not local_path.exists():
        return
    with local_path.open(newline="", encoding="utf-8") as f:
        rows = [row for row in csv.DictReader(f) if (row.get("phone") or "").strip()]
    if not rows:
        return
    try:
        import psycopg2
        from psycopg2.extras import execute_values
    except ImportError as error:
        _warn("Supabase push", error)
        return
    columns = list(rows[0].keys())
    quoted_columns = ", ".join(f'"{c}"' for c in columns)

    def set_clause(c):
        if c in NEVER_BLANK_COLUMNS:
            return f'"{c}" = CASE WHEN EXCLUDED."{c}" = \'\' THEN {SUPABASE_TABLE}."{c}" ELSE EXCLUDED."{c}" END'
        return f'"{c}" = EXCLUDED."{c}"'

    update_clause = ", ".join(set_clause(c) for c in columns if c != "phone")
    values = [tuple(row.get(c, "") for c in columns) for row in rows]
    try:
        with psycopg2.connect(conn_str, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                execute_values(
                    cur,
                    f'INSERT INTO {SUPABASE_TABLE} ({quoted_columns}) VALUES %s '
                    f'ON CONFLICT (phone) DO UPDATE SET {update_clause}',
                    values,
                )
    except psycopg2.Error as error:
        _warn("Supabase push", f"{type(error).__name__}: {error}")


def pull_supabase(local_path):
    """Overwrite local_path with every row currently in the Supabase `leads` table."""
    conn_str = os.environ.get("SUPABASE_DB_URL")
    if not conn_str:
        return
    try:
        import psycopg2
    except ImportError as error:
        _warn("Supabase pull", error)
        return
    try:
        with psycopg2.connect(conn_str, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {SUPABASE_TABLE}")
                rows = cur.fetchall()
                colnames = [d.name for d in cur.description]
    except psycopg2.Error as error:
        _warn("Supabase pull", f"{type(error).__name__}: {error}")
        return
    if not rows:
        return
    local_path.parent.mkdir(parents=True, exist_ok=True)
    with local_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(colnames)
        writer.writerows(rows)


def sync(local_path):
    """Back up local_path everywhere that's configured. Safe to call after every change. Returns
    the failure message if either backend's push failed just now, or None if both succeeded (or
    neither is configured) -- callers use this to surface a stuck backup instead of it failing
    silently to stderr forever."""
    global last_backup_error
    last_backup_error = None
    push_github(local_path)
    push_supabase(local_path)
    return last_backup_error


def restore(local_path):
    """Recover the last backup onto a fresh host. Supabase is tried first when it's configured --
    that's where leads actually live now -- with GitHub only as a fallback, not preferred over it;
    trying GitHub first used to mean a stale GitHub copy could win even with a fully current
    Supabase available. GitHub is also tried as a fallback when Supabase errors (rather than simply
    being empty), on the theory that a possibly-stale real backup beats no backup at all.

    Returns the failure message if a *configured* backend actually errored while being consulted
    (a connection failure, a bad query -- something going wrong), or None if nothing errored,
    whether that's because a backend had real data, a backend is configured but legitimately empty
    (a true first-ever run), or nothing is configured at all. This distinction is load-bearing: see
    db.restore_if_empty, which must never treat an *error* as "genuinely nothing to restore" --
    conflating the two is what let a scrape run against a wrongly-empty local database and then
    push blank status/notes/rejected_at over real values in Supabase on the next backup."""
    global last_backup_error
    last_backup_error = None
    supabase_error = None
    if os.environ.get("SUPABASE_DB_URL"):
        pull_supabase(local_path)
        if local_path.exists() and local_path.stat().st_size > 0:
            return None
        supabase_error = last_backup_error
    last_backup_error = None
    pull_github(local_path)
    if local_path.exists() and local_path.stat().st_size > 0:
        return None
    return supabase_error or last_backup_error
