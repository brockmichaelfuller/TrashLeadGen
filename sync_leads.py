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
    base = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values"
    try:
        session.post(f"{base}/A1:clear", timeout=15)
        response = session.put(f"{base}/A1?valueInputOption=RAW", json={"values": rows}, timeout=15)
        if response.status_code != 200:
            _warn("Google Sheets push", f"HTTP {response.status_code}: {response.text[:200]}")
    except requests.RequestException as error:
        _warn("Google Sheets push", error)


def sync(local_path):
    """Back up local_path everywhere that's configured. Safe to call after every change."""
    push_github(local_path)
    push_sheets(local_path)
