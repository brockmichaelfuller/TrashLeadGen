"""End-to-end tests against a real running app.Handler server -- these are what actually exercise
auth, HTTP status codes, and JSON error shapes the way a real client sees them, as opposed to
calling app's route-handling functions directly."""
import csv
import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import app


class ServerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.leads_path = Path(cls.tmpdir.name) / "leads.csv"
        cls._orig_leads_path = app.LEADS_PATH
        app.LEADS_PATH = cls.leads_path
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=5)
        cls.server.server_close()
        app.LEADS_PATH = cls._orig_leads_path
        cls.tmpdir.cleanup()

    def setUp(self):
        with self.leads_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["company_name", "phone", "status", "notes", "rejected_at"])
            writer.writeheader()
            writer.writerow({"company_name": "Acme Waste", "phone": "111", "status": "", "notes": "", "rejected_at": ""})
            writer.writerow({"company_name": "Declined Co", "phone": "222", "status": "Do not contact",
                              "notes": "already said no", "rejected_at": ""})
        with app.lock:
            app.job.update(proc=None, log=[], started=False, scope="", states=None, stopped=False)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            payload = json.dumps(body).encode() if body is not None else None
            hdrs = dict(headers or {})
            if payload is not None:
                hdrs["Content-Type"] = "application/json"
            conn.request(method, path, body=payload, headers=hdrs)
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, data
        finally:
            conn.close()

    def get_json(self, path):
        status, data = self.request("GET", path)
        return status, json.loads(data)

    def post_json(self, path, body):
        status, data = self.request("POST", path, body)
        return status, json.loads(data)


class AuthTests(ServerTestCase):
    @patch.dict("os.environ", {"APP_PASSWORD": "secret"}, clear=False)
    def test_rejects_requests_with_no_credentials(self):
        status, _ = self.request("GET", "/api/leads")
        self.assertEqual(status, 401)

    @patch.dict("os.environ", {"APP_PASSWORD": "secret"}, clear=False)
    def test_accepts_the_right_password(self):
        import base64
        creds = base64.b64encode(b"anyone:secret").decode()
        status, _ = self.request("GET", "/api/leads", headers={"Authorization": f"Basic {creds}"})
        self.assertEqual(status, 200)

    def test_open_when_no_password_is_configured(self):
        status, _ = self.request("GET", "/api/leads")
        self.assertEqual(status, 200)


class LeadsEndpointTests(ServerTestCase):
    def test_returns_current_leads(self):
        status, leads = self.get_json("/api/leads")
        self.assertEqual(status, 200)
        self.assertEqual({l["phone"] for l in leads}, {"111", "222"})

    def test_rejected_leads_are_not_returned(self):
        self.post_json("/api/lead/delete", {"phone": "111"})
        status, leads = self.get_json("/api/leads")
        self.assertEqual({l["phone"] for l in leads}, {"222"})


class UpdateLeadEndpointTests(ServerTestCase):
    def test_valid_status_update_succeeds(self):
        status, body = self.post_json("/api/lead", {"phone": "111", "status": "Interested"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_invalid_status_is_rejected_and_leaves_data_unchanged(self):
        status, body = self.post_json("/api/lead", {"phone": "111", "status": "maybe"})
        self.assertEqual(status, 400)
        self.assertIn("error", body)
        _, leads = self.get_json("/api/leads")
        self.assertEqual(next(l for l in leads if l["phone"] == "111")["status"], "")

    def test_missing_phone_is_rejected(self):
        status, body = self.post_json("/api/lead", {"status": "Interested"})
        self.assertEqual(status, 400)

    def test_unknown_phone_is_404(self):
        status, body = self.post_json("/api/lead", {"phone": "999", "notes": "hi"})
        self.assertEqual(status, 404)


class DeleteUndeleteEndpointTests(ServerTestCase):
    def test_delete_then_undelete_round_trip(self):
        status, _ = self.post_json("/api/lead/delete", {"phone": "111"})
        self.assertEqual(status, 200)
        _, leads = self.get_json("/api/leads")
        self.assertEqual({l["phone"] for l in leads}, {"222"})

        status, _ = self.post_json("/api/lead/undelete", {"phone": "111"})
        self.assertEqual(status, 200)
        _, leads = self.get_json("/api/leads")
        self.assertEqual({l["phone"] for l in leads}, {"111", "222"})

    def test_delete_missing_phone_is_400(self):
        status, _ = self.post_json("/api/lead/delete", {})
        self.assertEqual(status, 400)

    def test_delete_unknown_phone_is_404(self):
        status, _ = self.post_json("/api/lead/delete", {"phone": "999"})
        self.assertEqual(status, 404)


class ExportEndpointTests(ServerTestCase):
    def test_export_excludes_do_not_contact_and_not_interested(self):
        status, data = self.request("GET", "/api/export.csv")
        self.assertEqual(status, 200)
        rows = list(csv.DictReader(data.decode().splitlines()))
        self.assertEqual([r["phone"] for r in rows], ["111"])

    def test_export_includes_a_lead_marked_interested(self):
        self.post_json("/api/lead", {"phone": "111", "status": "Interested"})
        status, data = self.request("GET", "/api/export.csv")
        rows = list(csv.DictReader(data.decode().splitlines()))
        self.assertEqual({r["phone"] for r in rows}, {"111"})


class RunEndpointTests(ServerTestCase):
    def test_unknown_group_is_rejected_without_starting_anything(self):
        status, body = self.post_json("/api/run", {"group": "nonexistent"})
        self.assertEqual(status, 400)
        self.assertIn("error", body)


class StatusEndpointTests(ServerTestCase):
    def test_shape_when_idle(self):
        status, body = self.get_json("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["running"], False)
        self.assertEqual(body["started"], False)
        self.assertEqual(body["failedStates"], [])
        self.assertEqual(body["notReachedStates"], [])


if __name__ == "__main__":
    unittest.main()
