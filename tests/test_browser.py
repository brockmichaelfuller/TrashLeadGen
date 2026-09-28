"""Drives the real page in a real (headless) browser against a real running app.Handler server --
these are what actually exercise the client-side JS (headline wording, Stop/Retry/Continue, the
email filter, row-level errors, the inline Remove confirm, copy/export contents) that the rest of
the suite can't touch at all, since none of it runs in Python. Uses tests/fixtures/fake_scraper.py
in place of the real lead_scraper.py, so Run/Stop/Retry exercise the real subprocess/log-parsing
code path end to end without ever hitting Overpass over the network -- see that file for the
special state codes (SLOW<n>, FAIL, BLOCKED) these tests use to control its behavior deterministically.
"""
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

import app
import db

FIXTURE_SCRAPER = "tests/fixtures/fake_scraper.py"


@unittest.skipUnless(sync_playwright, "playwright not installed -- see requirements-dev.txt")
class BrowserTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._orig_db_path = app.DB_PATH
        cls._orig_scraper_script = app.SCRAPER_SCRIPT
        app.SCRAPER_SCRIPT = FIXTURE_SCRAPER
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.thread.join(timeout=5)
        cls.server.server_close()
        app.DB_PATH = cls._orig_db_path
        app.SCRAPER_SCRIPT = cls._orig_scraper_script

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.db_path = Path(self.tmpdir.name) / "leads.db"
        app.DB_PATH = self.db_path
        with app.lock:
            app.job.update(proc=None, log=[], started=False, scope="", states=None, stopped=False)
        app.backup_state["error"] = None
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)

    def goto(self):
        self.page.goto(f"http://127.0.0.1:{self.port}/")

    def start_run(self, states):
        """Kicks off a run with explicit state codes -- the same POST /api/run {"states": [...]}
        the page's own Retry/Continue buttons use, but not reachable through Run scrape's own group
        picker (which only ever offers real US state groups). Issued as a fetch() from the page
        itself so it carries the browser's own Origin header, same as a real button click would --
        followed by an immediate poll(), same as $("run").onclick does, so a short-lived run (or one
        the JS's own 2s polling interval just missed the phase of) is never missed entirely."""
        self.page.evaluate(
            "states => fetch('/api/run', {method: 'POST', headers: {'Content-Type': 'application/json'}, "
            "body: JSON.stringify({states})}).then(() => poll())",
            states)


class LeadsTableTests(BrowserTestCase):
    def test_shows_the_empty_state_with_no_leads(self):
        self.goto()
        expect(self.page.locator("#empty")).to_be_visible()
        expect(self.page.locator("#empty")).to_have_text("No leads yet. Click Run scrape to collect them.")

    def test_renders_leads_with_and_without_email_together(self):
        db.insert_if_new(self.db_path, {"phone": "111", "company_name": "Has Email Co", "email": "a@b.com", "state": "CO"})
        db.insert_if_new(self.db_path, {"phone": "222", "company_name": "No Email Co", "state": "TX"})
        self.goto()
        expect(self.page.locator("#rows tr")).to_have_count(2)
        expect(self.page.locator("#statTotal")).to_have_text("2")
        expect(self.page.locator("#statEmails")).to_have_text("1")

        self.page.locator("#emailFilter").check()
        expect(self.page.locator("#rows tr")).to_have_count(1)
        expect(self.page.locator("#rows")).to_contain_text("Has Email Co")

    def test_rejected_leads_are_not_shown(self):
        db.insert_if_new(self.db_path, {"phone": "111", "company_name": "Kept Co"})
        db.insert_if_new(self.db_path, {"phone": "222", "company_name": "Rejected Co"})
        db.update_fields(self.db_path, "222", {"rejected_at": "2026-09-28"})
        self.goto()
        expect(self.page.locator("#rows tr")).to_have_count(1)
        expect(self.page.locator("#rows")).to_contain_text("Kept Co")

    def test_copy_phones_copies_every_non_excluded_lead_including_one_missing_email(self):
        db.insert_if_new(self.db_path, {"phone": "(555) 111-1111", "company_name": "A", "email": "a@b.com"})
        db.insert_if_new(self.db_path, {"phone": "(555) 222-2222", "company_name": "B"})  # no email
        db.insert_if_new(self.db_path, {"phone": "(555) 333-3333", "company_name": "C", "status": "Do not contact"})
        self.page.context.grant_permissions(["clipboard-read", "clipboard-write"])
        self.goto()
        expect(self.page.locator("#rows tr")).to_have_count(3)
        self.page.locator("#copy").click()
        clipboard = self.page.evaluate("navigator.clipboard.readText()")
        self.assertEqual(set(clipboard.split("\n")), {"(555) 111-1111", "(555) 222-2222"})

    def test_download_csv_includes_every_non_excluded_lead(self):
        db.insert_if_new(self.db_path, {"phone": "111", "company_name": "A", "email": "a@b.com"})
        db.insert_if_new(self.db_path, {"phone": "222", "company_name": "B"})
        self.goto()
        with self.page.expect_download() as dl_info:
            self.page.locator("#download").click()
        csv_text = Path(dl_info.value.path()).read_text()
        self.assertIn("111", csv_text)
        self.assertIn("222", csv_text)


class StatusAndNotesTests(BrowserTestCase):
    def test_changing_status_saves_and_persists_across_a_reload(self):
        # statusCell's onchange handler calls renderLeads() right after a successful save (to
        # re-sort/re-dim the row for its new status), which replaces the "Saved" tag element with a
        # fresh one before a test could ever catch its brief "show" state -- so this checks the
        # thing that actually matters (the save itself, and the value surviving a reload) instead.
        db.insert_if_new(self.db_path, {"phone": "111", "company_name": "A"})
        self.goto()
        self.page.locator("td.interested select").select_option("Not interested")
        expect(self.page.locator("#rows tr")).to_have_class("excluded", timeout=5000)  # re-rendered (dimmed), so the save landed
        self.assertEqual(db.all_leads(self.db_path)[0]["status"], "Not interested")
        self.page.reload()
        expect(self.page.locator("td.interested select")).to_have_value("Not interested")

    def test_typing_notes_and_blurring_saves_and_shows_the_saved_tag(self):
        db.insert_if_new(self.db_path, {"phone": "111", "company_name": "A"})
        self.goto()
        notes = self.page.locator(".notes-input")
        notes.fill("Left a voicemail")
        notes.blur()
        # notesCell's success path (unlike statusCell's) never calls renderLeads(), so this tag is
        # never replaced out from under the assertion -- the "show" state is safe to check directly.
        # nth(1): the row's second .saved-tag in DOM order (tr.append(statusCell(l), notesCell(l))).
        expect(self.page.locator(".saved-tag").nth(1)).to_have_class("saved-tag show")
        self.assertEqual(db.all_leads(self.db_path)[0]["notes"], "Left a voicemail")
        self.page.reload()
        expect(self.page.locator(".notes-input")).to_have_value("Left a voicemail")

    def test_a_failed_save_shows_inline_on_the_row_not_only_in_the_scrape_card(self):
        db.insert_if_new(self.db_path, {"phone": "111", "company_name": "A"})
        self.goto()
        self.page.route("**/api/lead", lambda route: route.fulfill(
            status=400, content_type="application/json", body=json.dumps({"error": "simulated failure"})))
        self.page.locator("td.interested select").select_option("Interested")
        # Scoped to #rows -- #undoErr also carries the shared .row-err class, sitting earlier in the
        # DOM (in the header), so an unscoped .row-err selector would match that empty span first.
        expect(self.page.locator("#rows .row-err").first).to_have_text("simulated failure")


class RemoveUndoTests(BrowserTestCase):
    def test_remove_requires_a_second_click_to_confirm(self):
        db.insert_if_new(self.db_path, {"phone": "111", "company_name": "Junk Co"})
        self.goto()
        btn = self.page.locator(".removeBtn")
        btn.click()
        expect(btn).to_have_text("Confirm remove?")
        expect(self.page.locator("#rows tr")).to_have_count(1)  # not removed yet

        btn.click()
        expect(self.page.locator("#rows tr")).to_have_count(0)
        expect(self.page.locator("#undoBar")).to_be_visible()
        self.assertTrue(db.is_rejected(self.db_path, "111"))

    def test_a_single_click_does_not_remove_and_reverts_on_its_own(self):
        db.insert_if_new(self.db_path, {"phone": "111", "company_name": "Real Hauler"})
        self.goto()
        self.page.locator(".removeBtn").click()
        expect(self.page.locator(".removeBtn")).to_have_text("Confirm remove?")
        # Reverts on its own after a few seconds without a second click (see removeCell's 4s timer).
        expect(self.page.locator(".removeBtn")).to_have_text("Remove", timeout=6000)
        self.assertFalse(db.is_rejected(self.db_path, "111"))

    def test_undo_brings_the_lead_back(self):
        db.insert_if_new(self.db_path, {"phone": "111", "company_name": "Oops Co"})
        self.goto()
        btn = self.page.locator(".removeBtn")
        btn.click()
        btn.click()
        expect(self.page.locator("#rows tr")).to_have_count(0)
        self.page.locator("#undoBtn").click()
        expect(self.page.locator("#rows tr")).to_have_count(1)
        self.assertFalse(db.is_rejected(self.db_path, "111"))


class RunStopRetryTests(BrowserTestCase):
    def test_a_finished_run_shows_new_leads(self):
        self.goto()
        self.start_run(["CO", "WY"])
        expect(self.page.locator("#progressText")).to_contain_text("Finished scraping", timeout=10000)
        expect(self.page.locator("#rows tr")).to_have_count(2)

    def test_stop_ends_a_run_and_offers_continue(self):
        self.goto()
        self.start_run(["SLOW20", "CO", "WY"])  # SLOW20 sleeps 2s, giving time to click Stop
        expect(self.page.locator("#stop")).to_be_visible(timeout=5000)
        self.page.locator("#stop").click()
        expect(self.page.locator("#progressText")).to_contain_text("Stopped —", timeout=5000)
        expect(self.page.locator("#retryFailed")).to_contain_text("Continue")

    def test_continue_finishes_the_remaining_states(self):
        self.goto()
        self.start_run(["SLOW20", "CO"])
        expect(self.page.locator("#stop")).to_be_visible(timeout=5000)
        self.page.locator("#stop").click()
        expect(self.page.locator("#retryFailed")).to_be_visible(timeout=5000)
        self.page.locator("#retryFailed").click()
        expect(self.page.locator("#progressText")).to_contain_text("Finished scraping", timeout=10000)
        self.assertEqual(len(db.all_leads(self.db_path)), 2)

    def test_a_failed_state_shows_a_matching_retry_button(self):
        self.goto()
        self.start_run(["FAIL", "CO"])
        expect(self.page.locator("#progressText")).to_contain_text("couldn't be reached", timeout=10000)
        expect(self.page.locator("#retryFailed")).to_have_text("Retry 1 failed state (FAIL)")

    def test_an_aborted_run_gets_its_own_headline_with_a_matching_count(self):
        self.goto()
        # Two distinct BLOCKED* codes -- app.py's log parsing keys on the exact state string, and a
        # real run never repeats one, so re-using the same code here wouldn't be a faithful test.
        self.start_run(["BLOCKED1", "BLOCKED2", "CO", "WY"])
        expect(self.page.locator("#progressText")).to_contain_text("Stopped early", timeout=10000)
        expect(self.page.locator("#progressText")).to_contain_text("4 states left to retry")
        expect(self.page.locator("#retryFailed")).to_have_text("Retry 4 failed states (BLOCKED1, BLOCKED2, CO, WY)")


class BackupPauseTests(BrowserTestCase):
    def test_pausing_and_resuming_toggles_the_banner_and_button_text(self):
        self.goto()
        toggle = self.page.locator("#backupToggle")
        expect(toggle).to_have_text("Pause external backups")
        expect(self.page.locator("#backupPausedNote")).to_be_hidden()

        toggle.click()
        expect(self.page.locator("#backupPausedNote")).to_be_visible()
        expect(toggle).to_have_text("Resume external backups")
        self.assertTrue(db.backups_paused(self.db_path))

        toggle.click()
        expect(self.page.locator("#backupPausedNote")).to_be_hidden()
        self.assertFalse(db.backups_paused(self.db_path))


if __name__ == "__main__":
    unittest.main()
