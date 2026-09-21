"""Local web front end for overture_scraper.py.

    python app.py            # then open http://127.0.0.1:8000

Standard library only. Serves static/index.html and a small JSON API; scrapes by
running overture_scraper.py as a subprocess so the CLI and the UI share one code path.
"""
import argparse
import base64
import csv
import hmac
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
LEADS_PATH = ROOT / "output" / "leads.csv"
INDEX_PATH = ROOT / "static" / "index.html"
MAX_LOG_LINES = 500

lock = threading.Lock()
job = {"proc": None, "log": [], "started": False}


def read_csv(path):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def is_running():
    proc = job["proc"]
    return proc is not None and proc.poll() is None


def pump_output(proc):
    for line in proc.stdout:
        with lock:
            job["log"].append(line.rstrip())
            del job["log"][:-MAX_LOG_LINES]
    proc.wait()


def start_run():
    """One scrape of the entire U.S."""
    with lock:
        if is_running():
            return "A run is already in progress."
        cmd = [sys.executable, "-u", str(ROOT / "overture_scraper.py"), "--output", str(LEADS_PATH)]
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        job.update(proc=proc, log=[], started=True)
    threading.Thread(target=pump_output, args=(proc,), daemon=True).start()
    return None


def is_authorized(header):
    """HTTP Basic auth against APP_USERNAME (optional) and APP_PASSWORD. Open when no password is set (local only)."""
    password = os.environ.get("APP_PASSWORD")
    if not password:
        return True
    try:
        user, supplied = base64.b64decode((header or "").split(" ", 1)[1]).decode().split(":", 1)
    except (IndexError, ValueError):
        return False
    username = os.environ.get("APP_USERNAME")
    user_ok = hmac.compare_digest(user.encode(), username.encode()) if username else True
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
        path = self.path.split("?")[0]
        if path == "/":
            self.send_body(INDEX_PATH.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/leads":
            self.send_json(read_csv(LEADS_PATH))
        elif path == "/api/status":
            with lock:
                self.send_json({"running": is_running(), "started": job["started"], "log": job["log"],
                            "failed": job["proc"] is not None and (job["proc"].poll() or 0) != 0})
        elif path == "/api/export.csv":
            body = LEADS_PATH.read_bytes() if LEADS_PATH.exists() else b""
            self.send_body(body, "text/csv", extra={"Content-Disposition": 'attachment; filename="leads.csv"'})
        else:
            self.send_body(b"Not found", "text/plain", 404)

    def do_POST(self):
        if not self.require_auth():
            return
        path = self.path.split("?")[0]
        data = self.read_json()
        if path == "/api/run":
            error = start_run()
            return self.send_json({"error": error}, 400) if error else self.send_json({"ok": True})
        if path == "/api/stop":
            with lock:
                if is_running():
                    job["proc"].terminate()
            return self.send_json({"ok": True})
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
    server = ThreadingHTTPServer((host, args.port), Handler)
    print(f"TrashLeadGen UI: http://{host}:{args.port}  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
