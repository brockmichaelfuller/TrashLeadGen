"""Web front end for the lead scrapers (overture_scraper.py or lead_scraper.py).

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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
from lead_scraper import COLUMNS, STATE_GROUPS, missing_fields  # noqa: E402
import sync_leads  # noqa: E402
LEADS_PATH = ROOT / "output" / "leads.csv"
INDEX_PATH = ROOT / "static" / "index.html"
MAX_LOG_LINES = 500
# SCRAPER=osm uses the low-memory OpenStreetMap scraper (for small hosts like Render's free plan);
# the default Overture scraper scans several GB and needs a few GB of RAM.
USING_OSM = os.environ.get("SCRAPER", "overture").lower() == "osm"
SCRAPER_SCRIPT = "lead_scraper.py" if USING_OSM else "overture_scraper.py"
# Only the OSM scraper is split into state groups (it's the one that runs one slow request per
# state and can outlast Render's free-plan idle window on a full nationwide run).
GROUPS = [{"id": str(i + 1), "label": f"{g[0]}–{g[-1]} ({len(g)} states)", "states": g}
          for i, g in enumerate(STATE_GROUPS)] if USING_OSM else []

lock = threading.Lock()
job = {"proc": None, "log": [], "started": False, "scope": ""}


def read_csv(path):
    """All leads, each flagged complete (name, phone, email and timezone) or partial, with what's missing."""
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        missing = missing_fields(row)
        row["complete"] = not missing
        row["missing"] = ", ".join(missing)
    return rows


def update_lead(path, phone, updates):
    """Set status/notes on the row with this phone number. Returns False if the phone isn't found."""
    if not path.exists():
        return False
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)
    row = next((r for r in rows if r.get("phone") == phone), None)
    if row is None:
        return False
    row.update(updates)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return True


# Leads marked with either of these are kept in the UI (for the record) but left out of every
# export and copy action, so a "do not contact" or declined lead can't accidentally get dialed.
DO_NOT_EXPORT_STATUSES = {"Not interested", "Do not contact"}


def export_csv(path, which="all"):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=COLUMNS, extrasaction="ignore", restval="")
    writer.writeheader()
    for row in read_csv(path):
        if row.get("status") in DO_NOT_EXPORT_STATUSES:
            continue
        if which == "all" or (which == "complete") == row["complete"]:
            writer.writerow(row)
    return out.getvalue().encode()


def is_running():
    proc = job["proc"]
    return proc is not None and proc.poll() is None


def pump_output(proc):
    for line in proc.stdout:
        with lock:
            job["log"].append(line.rstrip())
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


def start_run(group_id="all", explicit_states=None):
    """One scrape: the whole U.S., one ~13-state group, or (for the "retry failed" button) an
    explicit list of state codes. Groups and explicit states are OSM-only; Overture is one query."""
    if explicit_states:
        if not USING_OSM:
            return "Retrying specific states isn't supported by this scraper."
        states, label = [s.upper() for s in explicit_states], f"retry: {', '.join(explicit_states)}"
    else:
        group = next((g for g in GROUPS if g["id"] == group_id), None)
        if group_id and group_id != "all" and not group:
            return "Unknown group."
        states, label = (group["states"], group["label"]) if group else (None, "the entire U.S.")
    with lock:
        if is_running():
            return "A run is already in progress."
        cmd = [sys.executable, "-u", str(ROOT / SCRAPER_SCRIPT), "--output", str(LEADS_PATH)]
        if states:
            cmd += ["--states", *states]
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        job.update(proc=proc, log=[], started=True, scope=label)
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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def require_auth(self):
        if is_authorized(self.headers.get("Authorization")):
            return True
        self.send_body(b"Password required", "text/plain", 401, {"WWW-Authenticate": 'Basic realm="TrashLeadGen"'})
        return False

    def send_body(self, body, content_type, status=200, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
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
        path, _, query = self.path.partition("?")
        if path == "/":
            self.send_body(INDEX_PATH.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/leads":
            self.send_json(read_csv(LEADS_PATH))
        elif path == "/api/status":
            with lock:
                running = is_running()
                self.send_json({"running": running, "started": job["started"], "log": job["log"],
                            "scope": job["scope"],
                            "failedStates": [] if running else parse_failed_states(job["log"]),
                            "failed": job["proc"] is not None and (job["proc"].poll() or 0) != 0})
        elif path == "/api/groups":
            self.send_json({"groups": GROUPS})
        elif path == "/api/export.csv":
            which = {"complete": "complete", "partial": "partial"}.get(parse_qs(query).get("set", [""])[0], "all")
            self.send_body(export_csv(LEADS_PATH, which), "text/csv",
                           extra={"Content-Disposition": f'attachment; filename="leads-{which}.csv"'})
        elif path == "/api/debug.log":
            debug_path = LEADS_PATH.parent / "debug.log"
            body = debug_path.read_bytes() if debug_path.exists() else b"(empty)"
            self.send_body(body, "text/plain; charset=utf-8")
        else:
            self.send_body(b"Not found", "text/plain", 404)

    def do_POST(self):
        if not self.require_auth():
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
            return self.send_json({"ok": True})
        if path == "/api/lead":
            phone = (data.get("phone") or "").strip()
            updates = {k: data.get(k, "") for k in ("status", "notes") if k in data}
            if not phone or not updates:
                return self.send_json({"error": "phone and at least one of status/notes are required"}, 400)
            with lock:
                ok = update_lead(LEADS_PATH, phone, updates)
            if ok:
                sync_leads.sync(LEADS_PATH)
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
    sync_leads.restore(LEADS_PATH)  # restore the last backup, since a fresh host starts empty
    server = ThreadingHTTPServer((host, args.port), Handler)
    print(f"TrashLeadGen UI: http://{host}:{args.port}  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
