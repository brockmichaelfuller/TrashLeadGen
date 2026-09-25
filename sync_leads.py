"""Durable backups for output/leads.csv, since Render's free plan has no persistent disk and
silently loses everything on every restart (redeploy, or just the container cycling after ~15
minutes idle).

Two independent, optional backends -- each a no-op unless its environment variables are set, so
running with neither configured behaves exactly like before:

- GitHub: commits the CSV to this repo after every change, and restores the latest commit back to
  disk when a fresh (empty) container starts. Needs GITHUB_TOKEN (a personal access token with
  Contents read/write on the repo) and GITHUB_REPO ("owner/name").
- Google Sheets: overwrites a sheet with the current CSV content after every change, so the data
  is visible and durable outside of this app entirely. Needs GOOGLE_SERVICE_ACCOUNT_JSON (the full
  service-account key, as one JSON string) and GOOGLE_SHEET_ID.

Both fail silently (logging to stderr) rather than raising -- a GitHub or Google outage should
never block a scrape or an edit from saving locally.
"""
import base64
import csv
import json
import os
import sys

import requests

GITHUB_API = "https://api.github.com"
GITHUB_CSV_PATH = "output/leads.csv"

_sheets_session = None
_sheets_session_tried = False

# A lead counts as "complete" for sorting purposes once it has enough info to actually act on --
# company name, a way to reach them, and their timezone (for knowing when to call).
COMPLETE_FIELDS = ("company_name", "phone", "email", "timezone")

# Sections shown in the sheet, top to bottom, each separated by a blank row. A lead with no status
# yet (the normal state right after scraping) sits first since it's what still needs a decision;
# any status that isn't one of these (shouldn't happen -- the site's dropdown only offers these
# four) is grouped last rather than dropped.
STATUS_SECTIONS = ["", "Interested", "Not interested", "Do not contact"]


def _group_and_sort(rows):
    """Reorder CSV data rows (header stays first) into per-status sections in STATUS_SECTIONS
    order, each separated by a blank row, with complete leads (see COMPLETE_FIELDS) grouped first
    within every section. If there's no "status" column, falls back to just a complete-first sort."""
    if len(rows) < 2:
        return rows
    header, data = rows[0], rows[1:]
    try:
        complete_indexes = [header.index(field) for field in COMPLETE_FIELDS]
    except ValueError:
        return rows  # header doesn't have the expected columns -- leave order alone
    def is_complete(row):
        return all(idx < len(row) and row[idx].strip() for idx in complete_indexes)

    if "status" not in header:
        return [header] + sorted(data, key=lambda row: not is_complete(row))

    status_idx = header.index("status")
    def status_of(row):
        return row[status_idx].strip() if status_idx < len(row) else ""

    sections = {name: [] for name in STATUS_SECTIONS}
    other = []
    for row in data:
        s = status_of(row)
        (sections[s] if s in sections else other).append(row)
    groups = [group for group in (sections[name] for name in STATUS_SECTIONS) if group]
    if other:
        groups.append(other)
    groups = [sorted(group, key=lambda row: not is_complete(row)) for group in groups]

    result = [header]
    for i, group in enumerate(groups):
        if i > 0:
            result.append([])  # blank separator row between sections
        result.extend(group)
    return result


def _warn(action, error):
    print(f"sync_leads: {action} failed: {error}", file=sys.stderr)


def pull_github(local_path):
    """Overwrite local_path with the last copy committed to GitHub, if configured and present."""
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPO")
    if not token or not repo:
        return
    try:
        response = requests.get(
            f"{GITHUB_API}/repos/{repo}/contents/{GITHUB_CSV_PATH}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=15,
        )
        if response.status_code == 200:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(base64.b64decode(response.json()["content"]))
    except (requests.RequestException, ValueError, KeyError) as error:
        _warn("GitHub pull", error)


def push_github(local_path):
    """Commit the current local_path contents to GitHub, creating or updating the file as needed."""
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPO")
    if not token or not repo or not local_path.exists():
        return
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    url = f"{GITHUB_API}/repos/{repo}/contents/{GITHUB_CSV_PATH}"
    try:
        existing = requests.get(url, headers=headers, timeout=15)
        body = {"message": "Update scraped leads", "content": base64.b64encode(local_path.read_bytes()).decode()}
        if existing.status_code == 200:
            body["sha"] = existing.json()["sha"]
        response = requests.put(url, headers=headers, json=body, timeout=15)
        if response.status_code not in (200, 201):
            _warn("GitHub push", f"HTTP {response.status_code}: {response.text[:200]}")
    except requests.RequestException as error:
        _warn("GitHub push", error)


def _sheets_session_or_none():
    global _sheets_session, _sheets_session_tried
    if _sheets_session_tried:
        return _sheets_session
    _sheets_session_tried = True
    raw_key = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw_key:
        return None
    try:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_info(
            json.loads(raw_key), scopes=["https://www.googleapis.com/auth/spreadsheets"])
        _sheets_session = AuthorizedSession(creds)
    except Exception as error:  # bad key, bad JSON, missing package -- treat Sheets sync as unavailable
        _warn("Google Sheets auth", error)
        _sheets_session = None
    return _sheets_session


def _drop_rejected(rows):
    """Leave out leads rejected as the wrong business type (see app.py's delete_lead) -- they're
    kept in the CSV so the scraper never re-adds them, but they have no reason to clutter the sheet."""
    if len(rows) < 2 or "rejected_at" not in rows[0]:
        return rows
    idx = rows[0].index("rejected_at")
    return [rows[0]] + [row for row in rows[1:] if not (idx < len(row) and row[idx].strip())]


def push_sheets(local_path):
    """Overwrite the configured sheet's first tab with the current CSV content."""
    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    session = _sheets_session_or_none()
    if not sheet_id or session is None or not local_path.exists():
        return
    with local_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if not rows:
        return
    rows = _drop_rejected(rows)
    rows = _group_and_sort(rows)
    base = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values"
    try:
        # "A1" alone names a single cell, not the whole sheet -- clearing just that one cell left
        # every previous run's rows past the end of a *shorter* new push (e.g. after a deletion)
        # sitting there untouched, so the sheet kept accumulating stale leftover rows.
        session.post(f"{base}/A1:Z100000:clear", timeout=15)
        response = session.put(f"{base}/A1?valueInputOption=RAW", json={"values": rows}, timeout=15)
        if response.status_code != 200:
            _warn("Google Sheets push", f"HTTP {response.status_code}: {response.text[:200]}")
    except requests.RequestException as error:
        _warn("Google Sheets push", error)


def pull_sheets(local_path):
    """Overwrite local_path with whatever's currently in the configured sheet."""
    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    session = _sheets_session_or_none()
    if not sheet_id or session is None:
        return
    try:
        response = session.get(
            f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/A1:Z100000", timeout=15)
        if response.status_code != 200:
            _warn("Google Sheets pull", f"HTTP {response.status_code}: {response.text[:200]}")
            return
        rows = response.json().get("values", [])
        if not rows:
            return
        # Drop wholly-blank rows -- push_sheets inserts them as visual separators between status
        # sections, and Sheets returns them as [] (or a shorter row of blank cells). Without this,
        # each one round-trips back into the CSV as a lead with every field empty.
        header, data = rows[0], [row for row in rows[1:] if any(cell.strip() for cell in row)]
        rows = [header] + data
        local_path.parent.mkdir(parents=True, exist_ok=True)
        with local_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            width = len(rows[0])
            for row in rows:
                writer.writerow(row + [""] * (width - len(row)))  # Sheets drops trailing blank cells
    except requests.RequestException as error:
        _warn("Google Sheets pull", error)


def sync(local_path):
    """Back up local_path everywhere that's configured. Safe to call after every change."""
    push_github(local_path)
    push_sheets(local_path)


def restore(local_path):
    """Recover the last backup onto a fresh host. Tries GitHub first, then Sheets, then gives up
    quietly and leaves local_path as it is (an empty/missing file is normal on a first-ever run)."""
    pull_github(local_path)
    if local_path.exists() and local_path.stat().st_size > 0:
        return
    pull_sheets(local_path)
