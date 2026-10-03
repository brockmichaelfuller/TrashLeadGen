"""Shared helpers for tests that need a real Postgres database -- Supabase is this app's only
storage, so anything that exercises db.py, app.py's request handlers, or a real lead_scraper.py/
fake_scraper.py subprocess needs one to actually talk to. CI points SUPABASE_DB_URL at a throwaway
Postgres service container (see .github/workflows/ci.yml) with the schema already created; locally,
point it at a real (ideally disposable, not production) Postgres instance to run these, or just skip
them -- requires_db below makes that automatic rather than a hard failure.
"""
import os
import unittest

import psycopg2

requires_db = unittest.skipUnless(
    os.environ.get("SUPABASE_DB_URL"),
    "SUPABASE_DB_URL not set -- these tests need a real Postgres (see tests/helpers.py)")


def clear_leads_table():
    """Wipe every row between tests. There's one shared `leads` table for the whole test run (unlike
    the old per-test SQLite tempfile), so tests that care about an exact set of rows must start from
    empty -- call this from setUp, not tearDown, so a prior test's failure (which skips tearDown)
    can't leave stale rows poisoning the next one."""
    conn = psycopg2.connect(os.environ["SUPABASE_DB_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE leads")
        conn.commit()
    finally:
        conn.close()
