"""End-to-end tests against a real running app.Handler server -- these are what actually exercise
auth, HTTP status codes, and JSON error shapes the way a real client sees them, as opposed to
calling app's route-handling functions directly. Needs a real Postgres (see tests/helpers.py),
since every request here that touches a lead goes straight to it -- there's no local database to
substitute in its place any more."""
import csv
import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import app
import db
from tests.helpers import clear_leads_table, requires_db


@requires_db
class ServerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=5)
        cls.server.server_close()

    def setUp(self):
        clear_leads_table()
        db.insert_if_new({"company_name": "Acme Waste", "phone": "111"})
        db.insert_if_new({"company_name": "Declined Co", "phone": "222",
                           "status": "Do not contact", "notes": "already said no"})
        with app.lock:
            app.job.update(proc=None, log=[], started=False, scope="", states=None, stopped=False)
        with app._auth_lock:
            app._auth_failures.clear()

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

    def test_the_invalid_status_error_is_plain_english_not_a_python_repr(self):
        # Hit live: this used to read "status must be one of ['', 'Do not contact', ...]" -- a
        # Python list repr (quotes, brackets) leaking straight into a message an end user sees.
        status, body = self.post_json("/api/lead", {"phone": "111", "status": "maybe"})
        self.assertEqual(status, 400)
        self.assertNotIn("[", body["error"])
        self.assertIn("Interested", body["error"])
        self.assertIn("(none)", body["error"])

    def test_a_non_string_phone_is_rejected_cleanly_instead_of_crashing(self):
        # Hit live: data.get("phone") on a malformed request can be any JSON type -- a bare
        # `.strip()` on a non-string crashed the request instead of returning a 400.
        status, body = self.post_json("/api/lead", {"phone": 111, "status": "Interested"})
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_a_non_string_status_is_rejected(self):
        status, body = self.post_json("/api/lead", {"phone": "111", "status": 1})
        self.assertEqual(status, 400)

    def test_notes_over_the_length_limit_is_rejected(self):
        status, body = self.post_json("/api/lead", {"phone": "111", "notes": "x" * (app.MAX_NOTES_LENGTH + 1)})
        self.assertEqual(status, 400)
        _, leads = self.get_json("/api/leads")
        self.assertEqual(next(l for l in leads if l["phone"] == "111")["notes"], "")

    def test_a_json_array_body_is_rejected_cleanly_instead_of_crashing(self):
        status, body = self.post_json("/api/lead", ["not", "an", "object"])
        self.assertEqual(status, 400)
        self.assertIn("error", body)


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

    def test_export_includes_a_lead_missing_email(self):
        # There is no complete/partial split any more -- a lead with a name and phone but no email
        # is still callable, and used to be silently left out of the default export because it only
        # covered the "complete" tab's leads.
        status, data = self.request("GET", "/api/export.csv")
        rows = {r["phone"]: r for r in csv.DictReader(data.decode().splitlines())}
        self.assertIn("111", rows)
        self.assertEqual(rows["111"]["email"], "")


class RunEndpointTests(ServerTestCase):
    def test_unknown_group_is_rejected_without_starting_anything(self):
        status, body = self.post_json("/api/run", {"group": "nonexistent"})
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_states_sent_as_a_single_string_is_rejected_instead_of_scraping_one_letter_per_state(self):
        # Hit live: a string is iterable in Python -- unguarded, {"states": "CO"} would silently
        # become the four single-letter "states" C, O (plus whatever junk fell out of upper-casing
        # them), instead of a clear error.
        status, body = self.post_json("/api/run", {"states": "CO"})
        self.assertEqual(status, 400)
        self.assertFalse(app.is_running())


class StatusEndpointTests(ServerTestCase):
    def test_shape_when_idle(self):
        status, body = self.get_json("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["running"], False)
        self.assertEqual(body["started"], False)
        self.assertEqual(body["failedStates"], [])
        self.assertEqual(body["notReachedStates"], [])


class SecurityHeaderTests(ServerTestCase):
    def test_responses_carry_baseline_hardening_headers(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", "/api/leads")
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
            self.assertEqual(resp.getheader("X-Frame-Options"), "DENY")
            self.assertEqual(resp.getheader("Referrer-Policy"), "no-referrer")
        finally:
            conn.close()


class CsrfProtectionTests(ServerTestCase):
    def test_a_post_with_a_mismatched_origin_is_blocked(self):
        status, _ = self.request("POST", "/api/lead", body={"phone": "111", "status": "Interested"},
                                  headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        _, leads = self.get_json("/api/leads")
        self.assertEqual(next(l for l in leads if l["phone"] == "111")["status"], "")

    def test_a_post_with_a_matching_origin_succeeds(self):
        status, _ = self.request("POST", "/api/lead", body={"phone": "111", "status": "Interested"},
                                  headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)

    def test_a_post_with_no_origin_or_referer_is_allowed(self):
        # Not every legitimate API client sends these headers -- this check only rejects a request
        # that actively names a different origin, it doesn't require one to be present.
        status, _ = self.post_json("/api/lead", {"phone": "111", "status": "Interested"})
        self.assertEqual(status, 200)


class AuthRateLimitTests(ServerTestCase):
    @patch.dict("os.environ", {"APP_PASSWORD": "secret"}, clear=False)
    def test_locks_out_after_repeated_failed_attempts(self):
        for _ in range(app.RATE_LIMIT_MAX_FAILURES):
            status, _ = self.request("GET", "/api/leads", headers={"Authorization": "Basic bm9wZTpub3Blbg=="})
            self.assertEqual(status, 401)
        status, _ = self.request("GET", "/api/leads", headers={"Authorization": "Basic bm9wZTpub3Blbg=="})
        self.assertEqual(status, 429)

    @patch.dict("os.environ", {"APP_PASSWORD": "secret"}, clear=False)
    def test_a_correct_password_is_not_rate_limited(self):
        import base64
        creds = base64.b64encode(b"anyone:secret").decode()
        for _ in range(app.RATE_LIMIT_MAX_FAILURES + 2):
            status, _ = self.request("GET", "/api/leads", headers={"Authorization": f"Basic {creds}"})
            self.assertEqual(status, 200)


class DatabaseErrorHandlingTests(ServerTestCase):
    """Supabase is this app's only storage -- a request that can't reach it has to fail cleanly
    (a JSON 503), not crash the request or leak a raw traceback to whoever's looking at the page."""

    def test_a_db_error_on_a_get_returns_a_clean_503_not_a_crash(self):
        with patch("app.db.all_leads", side_effect=Exception("connection refused")):
            status, body = self.get_json("/api/leads")
        self.assertEqual(status, 503)
        self.assertIn("error", body)

    def test_a_db_error_on_a_post_returns_a_clean_503_not_a_crash(self):
        with patch("app.db.update_fields", side_effect=Exception("connection refused")):
            status, body = self.post_json("/api/lead", {"phone": "111", "status": "Interested"})
        self.assertEqual(status, 503)
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
