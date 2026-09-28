"""Web front end for lead_scraper.py.

    python app.py            # then open http://127.0.0.1:8000

Standard library only. Serves static/index.html and a small JSON API; scrapes by
running the scraper as a subprocess so the CLI and the UI share one code path.
"""
import argparse
import base64
import csv
import hmac
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
from lead_scraper import STATE_GROUPS, STATES, missing_fields  # noqa: E402
import db  # noqa: E402
import sync_leads  # noqa: E402
DB_PATH = ROOT / "output" / "leads.db"
INDEX_PATH = ROOT / "static" / "index.html"
MAX_LOG_LINES = 500
SCRAPER_SCRIPT = "lead_scraper.py"
# Split into state groups since a full nationwide run (one slow request per state) can outlast
# Render's free-plan idle window.
GROUPS = [{"id": str(i + 1), "label": f"{g[0]}–{g[-1]} ({len(g)} states)", "states": g}
          for i, g in enumerate(STATE_GROUPS)]

lock = threading.Lock()
job = {"proc": None, "log": [], "started": False, "scope": "", "states": None, "stopped": False}

# Backing up on every edit used to mean a synchronous GitHub commit + Sheets rewrite inside the
# request -- typing a 50-character note (saved every 600ms) could fire off several of each. Backups
# now run on a short delay after the *last* edit instead, so a burst of saves results in at most one
# push; a failure gets one automatic retry, and the most recent failure (if any) is surfaced via
# /api/status instead of only ever going to stderr.
SYNC_DEBOUNCE_SECONDS = 5
SYNC_RETRY_SECONDS = 30
backup_state = {"error": None}
_sync_timer = None


def _run_sync():
    global _sync_timer
    error = db.sync_backup(DB_PATH)
    with lock:
        backup_state["error"] = error
        _sync_timer = None
    if error:
        _schedule_sync(SYNC_RETRY_SECONDS)


def _schedule_sync(delay):
    global _sync_timer
    with lock:
        if _sync_timer is not None:
            _sync_timer.cancel()
        _sync_timer = threading.Timer(delay, _run_sync)
        _sync_timer.daemon = True
        _sync_timer.start()


def schedule_sync():
    _schedule_sync(SYNC_DEBOUNCE_SECONDS)


def read_leads(db_path):
    """All non-rejected leads, each flagged complete (name, phone, email and timezone) or partial,
    with what's missing. Rejected rows (see delete_lead) are kept in the database but never surfaced
    here."""
    rows = db.all_leads(db_path)
    for row in rows:
        missing = missing_fields(row)
        row["complete"] = not missing
        row["missing"] = ", ".join(missing)
    return rows


def update_lead(db_path, phone, updates):
    """Set status/notes on the row with this phone number. Returns False if the phone isn't found."""
    return db.update_fields(db_path, phone, updates)


def delete_lead(db_path, phone):
    """Mark the row with this phone number rejected -- for a lead that never should have matched
    (wrong business type), as opposed to a real hauler marked "Do not contact". The row is kept
    (just hidden from read_leads and everything built on it) rather than removed outright, so its
    phone permanently blocks the scraper from re-adding it on a later run. Returns False if the
    phone isn't found."""
    return db.update_fields(db_path, phone, {"rejected_at": date.today().isoformat()})


def undelete_lead(db_path, phone):
    """Undo a delete within the same page load (see the "Undo" link in the UI right after removing
    a lead) by clearing rejected_at. Returns False if the phone isn't found or wasn't rejected."""
    if not db.is_rejected(db_path, phone):
        return False
    return db.update_fields(db_path, phone, {"rejected_at": ""})


# Leads marked with either of these are kept in the UI (for the record) but left out of every
# export and copy action, so a "do not contact" or declined lead can't accidentally get dialed.
DO_NOT_EXPORT_STATUSES = {"Not interested", "Do not contact"}

# The only values the "Interested?" dropdown offers -- reusing sync_leads' canonical list (it
# already has to know these exactly, to lay out the Google Sheet's sections) instead of a second
# copy that could quietly drift out of sync with it.
VALID_STATUSES = set(sync_leads.STATUS_SECTIONS)


def export_csv(db_path):
    """Every non-rejected lead ready to call (has a name and phone), except one marked "Not
    interested" or "Do not contact" -- the same set the page's Copy buttons use. There's no
    complete/partial split: a lead missing an email is still callable, so leaving it out of the
    default export silently dropped otherwise-good leads from outreach."""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=db.COLUMNS, extrasaction="ignore", restval="")
    writer.writeheader()
    for row in read_leads(db_path):
        if row.get("status") not in DO_NOT_EXPORT_STATUSES:
            writer.writerow(row)
    return out.getvalue().encode()


def is_running():
    proc = job["proc"]
    return proc is not None and proc.poll() is None


def pump_output(proc):
    for raw_line in proc.stdout:
        line = raw_line.rstrip()
        # A backup failure/success during a scrape (see lead_scraper._attempt_state) is routed
        # through the same backup_state an edit-triggered backup uses, instead of only ever showing
        # up as a raw "sync_leads: GitHub push failed: HTTP 409: {...}"-style line buried in the
        # plain-language run log with no banner to show for it.
        if line.startswith("BACKUP_ERROR: "):
            with lock:
                backup_state["error"] = line[len("BACKUP_ERROR: "):]
            continue
        if line == "BACKUP_OK":
            with lock:
                backup_state["error"] = None
            continue
        with lock:
            job["log"].append(line)
            del job["log"][:-MAX_LOG_LINES]
    proc.wait()


# Matches every per-state outcome line lead_scraper.py prints, across both its first pass
# ("[3/13] CO: ...") and its automatic retry rounds ("retry succeeded: CO: ..." / "still failing
# after retry: CO: ..."), capturing the state and whether that line was a skip or a success.
STATE_OUTCOME_RE = re.compile(
    r"^(?:\[\d+/\d+\]|retry succeeded:|still failing after retry:) (\S+): (skipped\b|\d)")


def parse_failed_states(log_lines):
    """States lead_scraper.py currently considers failed, read straight from its per-state log lines
    as they're printed -- not just from its end-of-run summary, so a run that crashes or gets killed
    partway through (before reaching that summary) still leaves every skipped state retryable. Since
    a state can fail its first pass and then succeed on an automatic retry, this tracks each state's
    *most recent* outcome rather than just collecting every state ever mentioned as skipped."""
    order, failed = [], {}
    for line in log_lines:
        match = STATE_OUTCOME_RE.match(line)
        if not match:
            continue
        state, outcome = match.group(1), match.group(2) == "skipped"
        if state not in failed:
            order.append(state)
        failed[state] = outcome
    return [state for state in order if failed[state]]


_ABORT_LINE_RE = re.compile(r"^Can't reach the map data service.*\((blocked from reaching|unable to reach) it\)")


def run_aborted_early(log_lines):
    """Whether lead_scraper.py's run() gave up early instead of finishing its planned states --
    see CONSECUTIVE_PERSISTENT_FAILURE_ABORT_THRESHOLD. This is the one situation where a run that
    neither crashed nor was stopped by the user still leaves states unattempted, so the page can
    give it a headline of its own instead of reading like an ordinary "some states failed" finish.
    Returns "blocked" or "unreachable", or None if the run didn't abort early."""
    for line in log_lines:
        match = _ABORT_LINE_RE.match(line)
        if match:
            return "blocked" if match.group(1).startswith("blocked") else "unreachable"
    return None


def parse_finished_states(log_lines):
    """States lead_scraper.py has logged any outcome for so far (success or skip), in the order
    first seen. Used to tell which of a run's planned states were actually reached before it ended
    -- whether that's because it finished normally, crashed, or the user clicked Stop -- so the page
    can say what's left instead of just "something went wrong"."""
    order = []
    for line in log_lines:
        match = STATE_OUTCOME_RE.match(line)
        if match and match.group(1) not in order:
            order.append(match.group(1))
    return order


# Lines lead_scraper.py prints that are meant for someone running it directly from a terminal -- a
# server file path, "rerun to retry" CLI wording -- and have no place in the plain-language web log.
_CLI_ONLY_LINE_RE = re.compile(r"^Done\. |^Failed states \(rerun to retry\):")


def user_facing_log(log_lines):
    return [line for line in log_lines if not _CLI_ONLY_LINE_RE.match(line)]


def start_run(group_id="all", explicit_states=None):
    """One scrape: the whole U.S., one ~13-state group, or (for the "retry failed" button) an
    explicit list of state codes."""
    if explicit_states:
        states = [s.upper() for s in explicit_states]
        # "retry: RI, CT, DE" (the raw scope value) used to show up verbatim in the page's headline
        # as "Scraping retry: RI, CT, DE…" -- this reads like an internal label, not a sentence.
        label = f"{len(states)} state{'' if len(states) == 1 else 's'} ({', '.join(states)})"
    else:
        group = next((g for g in GROUPS if g["id"] == group_id), None)
        if group_id and group_id != "all" and not group:
            return "Unknown group."
        states, label = (group["states"], group["label"]) if group else (None, "the entire U.S.")
    if states is None:
        # Materialize the real planned list (instead of letting the scraper fall back to its own
        # default) so progress/stop reporting always has something concrete to compare against.
        states = list(STATES)
    with lock:
        if is_running():
            return "A run is already in progress."
        cmd = [sys.executable, "-u", str(ROOT / SCRAPER_SCRIPT), "--output", str(DB_PATH)]
        if states:
            cmd += ["--states", *states]
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        job.update(proc=proc, log=[], started=True, scope=label, states=states, stopped=False)
    threading.Thread(target=pump_output, args=(proc,), daemon=True).start()
    return None


def is_authorized(header):
    """HTTP Basic auth against APP_USERNAME (optional comma-separated list) and APP_PASSWORD. Open when no password is set (local only)."""
    password = os.environ.get("APP_PASSWORD")
    if not password:
        return True
    try:
        user, supplied = base64.b64decode((header or "").split(" ", 1)[1]).decode().split(":", 1)
    except (IndexError, ValueError):
        return False
    allowed = [u.strip().lower() for u in os.environ.get("APP_USERNAME", "").split(",") if u.strip()]
    user_ok = user.lower() in allowed if allowed else True  # comma-separated list, case-insensitive
    return hmac.compare_digest(supplied.encode(), password.encode()) and user_ok


# Failed-auth rate limiting -- once HOST is public (0.0.0.0), APP_PASSWORD is the only thing between
# the internet and this app, and Basic Auth has no built-in lockout, so nothing previously stopped an
# unlimited-speed password guess loop. Simple in-memory per-IP tracking; fine for a single instance.
RATE_LIMIT_MAX_FAILURES = 8
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_LOCKOUT_SECONDS = 60
_auth_failures = {}  # ip -> [failure timestamps within the window]
_auth_lock = threading.Lock()


def _is_rate_limited(ip):
    now = time.time()
    with _auth_lock:
        failures = [t for t in _auth_failures.get(ip, []) if now - t < RATE_LIMIT_WINDOW_SECONDS]
        _auth_failures[ip] = failures
        return len(failures) >= RATE_LIMIT_MAX_FAILURES and now - failures[-1] < RATE_LIMIT_LOCKOUT_SECONDS


def _record_auth_failure(ip):
    with _auth_lock:
        _auth_failures.setdefault(ip, []).append(time.time())


def _record_auth_success(ip):
    with _auth_lock:
        _auth_failures.pop(ip, None)


def request_origin_is_trusted(headers, host):
    """CSRF defense for state-changing (POST) requests: Basic Auth credentials, once entered, are
    cached by the browser and resent automatically to the same origin -- including from a background
    request a *different*, malicious site makes the visitor's browser send. Reject a POST whose
    Origin/Referer names a different host than the one serving this request; allow it (fail open)
    when neither header is present, since not every legitimate API client sends them."""
    origin = headers.get("Origin")
    referer = headers.get("Referer")
    for value in (origin, referer):
        if not value:
            continue
        try:
            netloc = urlparse(value).netloc
        except ValueError:
            return False
        if netloc and netloc != host:
            return False
    return True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def require_auth(self):
        ip = self.client_address[0]
        if _is_rate_limited(ip):
            self.send_body(b"Too many failed attempts. Try again in a minute.", "text/plain", 429)
            return False
        if is_authorized(self.headers.get("Authorization")):
            _record_auth_success(ip)
            return True
        if os.environ.get("APP_PASSWORD"):  # only meaningful (and only worth counting) once a password is set
            _record_auth_failure(ip)
        self.send_body(b"Password required", "text/plain", 401, {"WWW-Authenticate": 'Basic realm="TrashLeadGen"'})
        return False

    def require_trusted_origin(self):
        if request_origin_is_trusted(self.headers, self.headers.get("Host", "")):
            return True
        self.send_body(b"Cross-site request blocked", "text/plain", 403)
        return False

    def send_body(self, body, content_type, status=200, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Baseline hardening: this page is never meant to be framed, sniffed into an unintended
        # content type, or to leak its (auth-bearing) URL via the Referer header on outbound links.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, status=200):
        self.send_body(json.dumps(data).encode(), "application/json", status)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    def do_GET(self):
        if not self.require_auth():
            return
        path = self.path.split("?")[0]
        if path == "/":
            self.send_body(INDEX_PATH.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/leads":
            self.send_json(read_leads(DB_PATH))
        elif path == "/api/status":
            with lock:
                running = is_running()
                finished = parse_finished_states(job["log"])
                planned = job["states"]
                not_reached = [] if running or not planned else [s for s in planned if s not in finished]
                # A nonzero exit only means "something went wrong" if the user didn't cause it by
                # clicking Stop -- terminate() makes the process exit nonzero too, and that's expected.
                crashed = (not running and not job["stopped"] and job["proc"] is not None
                           and (job["proc"].poll() or 0) != 0)
                self.send_json({"running": running, "started": job["started"],
                            "log": user_facing_log(job["log"]), "scope": job["scope"],
                            "stopped": job["stopped"], "crashed": crashed,
                            "failedStates": [] if running else parse_failed_states(job["log"]),
                            "notReachedStates": not_reached,
                            "abortedEarly": None if running else run_aborted_early(job["log"]),
                            "finishedCount": len(finished),
                            "plannedCount": len(planned) if planned else None,
                            "backupError": backup_state["error"],
                            "backupsPaused": db.backups_paused(DB_PATH)})
        elif path == "/api/groups":
            self.send_json({"groups": GROUPS})
        elif path == "/api/export.csv":
            self.send_body(export_csv(DB_PATH), "text/csv",
                           extra={"Content-Disposition": 'attachment; filename="leads.csv"'})
        elif path == "/api/debug.log":
            debug_path = DB_PATH.parent / "debug.log"
            body = debug_path.read_bytes() if debug_path.exists() else b"(empty)"
            self.send_body(body, "text/plain; charset=utf-8")
        else:
            self.send_body(b"Not found", "text/plain", 404)

    def do_POST(self):
        if not self.require_auth():
            return
        if not self.require_trusted_origin():
            return
        path = self.path.split("?")[0]
        data = self.read_json()
        if path == "/api/run":
            error = start_run(data.get("group", "all"), data.get("states"))
            return self.send_json({"error": error}, 400) if error else self.send_json({"ok": True})
        if path == "/api/stop":
            with lock:
                if is_running():
                    job["proc"].terminate()
                    job["stopped"] = True
            return self.send_json({"ok": True})
        if path == "/api/backups":
            db.set_backups_paused(DB_PATH, bool(data.get("paused")))
            return self.send_json({"ok": True})
        if path == "/api/lead":
            phone = (data.get("phone") or "").strip()
            updates = {k: data.get(k, "") for k in ("status", "notes") if k in data}
            if not phone or not updates:
                return self.send_json({"error": "phone and at least one of status/notes are required"}, 400)
            if "status" in updates and updates["status"] not in VALID_STATUSES:
                return self.send_json({"error": f"status must be one of {sorted(VALID_STATUSES)}"}, 400)
            with lock:
                ok = update_lead(DB_PATH, phone, updates)
            if ok:
                schedule_sync()
            return self.send_json({"ok": True}) if ok else self.send_json({"error": "Lead not found"}, 404)
        if path == "/api/lead/delete":
            phone = (data.get("phone") or "").strip()
            if not phone:
                return self.send_json({"error": "phone is required"}, 400)
            with lock:
                ok = delete_lead(DB_PATH, phone)
            if ok:
                schedule_sync()
            return self.send_json({"ok": True}) if ok else self.send_json({"error": "Lead not found"}, 404)
        if path == "/api/lead/undelete":
            phone = (data.get("phone") or "").strip()
            if not phone:
                return self.send_json({"error": "phone is required"}, 400)
            with lock:
                ok = undelete_lead(DB_PATH, phone)
            if ok:
                schedule_sync()
            return self.send_json({"ok": True}) if ok else self.send_json({"error": "Lead not found"}, 404)
        self.send_json({"error": "Not found"}, 404)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    args = parser.parse_args()
    # Localhost by default. On a host like Render set HOST=0.0.0.0, which requires APP_PASSWORD because
    # the UI can start scrapes and export the lead list.
    host = os.environ.get("HOST", "127.0.0.1")
    if host != "127.0.0.1" and not os.environ.get("APP_PASSWORD"):
        sys.exit("Refusing to listen on the network without APP_PASSWORD set.")
    db.restore_if_empty(DB_PATH)  # recover the last backup, since a fresh host starts with no database
    db.import_audit_log_rejections(DB_PATH)
    server = ThreadingHTTPServer((host, args.port), Handler)
    print(f"TrashLeadGen UI: http://{host}:{args.port}  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
