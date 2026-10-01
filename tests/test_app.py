import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app
import db
from app import (delete_lead, parse_failed_states, parse_finished_states, read_leads, run_aborted_early,
                  undelete_lead, update_lead, user_facing_log)


class ParseFailedStatesTests(unittest.TestCase):
    def test_reads_states_from_a_completed_runs_summary(self):
        log = [
            "[1/3] AL: 2 new companies",
            "[2/3] AK: skipped -- couldn't connect to the map data source",
            "[3/3] AZ: 1 new companies",
            "Done. 3 new rows -> output/leads.db (3 total unique phones)",
            "Failed states (rerun to retry): AK",
        ]
        self.assertEqual(parse_failed_states(log), ["AK"])

    def test_reads_states_even_when_the_run_never_reaches_a_summary(self):
        # A crash or kill partway through a run (no final "Done."/"Failed states" line) must not
        # lose track of what already failed -- the retry button needs this to have anything to offer.
        log = [
            "[1/13] AL: skipped -- couldn't connect to the map data source",
            "[2/13] AK: skipped -- couldn't connect to the map data source",
        ]
        self.assertEqual(parse_failed_states(log), ["AL", "AK"])

    def test_no_failures_is_an_empty_list(self):
        log = ["[1/1] CO: 4 new companies", "Done. 4 new rows -> output/leads.db (4 total unique phones)"]
        self.assertEqual(parse_failed_states(log), [])

    def test_does_not_duplicate_a_state_seen_twice(self):
        log = ["[1/2] AL: skipped -- couldn't connect to the map data source"] * 2
        self.assertEqual(parse_failed_states(log), ["AL"])

    def test_a_state_that_recovers_on_automatic_retry_is_not_left_as_failed(self):
        log = [
            "[1/2] CO: skipped -- couldn't connect to the map data source",
            "[2/2] WY: 1 new companies",
            "1 state(s) had a temporary problem -- retrying automatically in 30s: CO",
            "retry succeeded: CO: 0 new companies",
            "Done. 1 new rows -> output/leads.db (2 total unique phones)",
        ]
        self.assertEqual(parse_failed_states(log), [])

    def test_a_state_that_fails_every_retry_round_still_shows_as_failed(self):
        log = [
            "[1/1] CO: skipped -- couldn't connect to the map data source",
            "1 state(s) had a temporary problem -- retrying automatically in 30s: CO",
            "still failing after retry: CO: skipped -- couldn't connect to the map data source",
            "Failed states (rerun to retry): CO",
        ]
        self.assertEqual(parse_failed_states(log), ["CO"])

    def test_a_state_that_fails_in_the_outer_handler_still_shows_as_failed(self):
        # A failure outside _attempt_state itself (the logging/sleep code around it) used to print
        # without the "[i/n]" prefix the other two outcome formats use, so it silently never made it
        # onto the retry button. Now prefixed the same way as every other outcome line.
        log = ["[1/2] AL: skipped -- the map data source had a temporary problem",
               "[2/2] AK: 1 new companies"]
        self.assertEqual(parse_failed_states(log), ["AL"])


class ParseFinishedStatesTests(unittest.TestCase):
    def test_reads_states_with_any_outcome_success_or_skip(self):
        log = [
            "[1/3] AL: 2 new companies",
            "[2/3] AK: skipped -- couldn't connect to the map data source",
        ]
        self.assertEqual(parse_finished_states(log), ["AL", "AK"])

    def test_a_state_never_reached_is_absent(self):
        # This is what tells a Stopped run's status which planned states it never even got to.
        log = ["[1/3] AL: 2 new companies"]
        self.assertEqual(parse_finished_states(log), ["AL"])

    def test_does_not_duplicate_a_state_seen_across_a_retry_round(self):
        log = [
            "[1/1] CO: skipped -- couldn't connect to the map data source",
            "retry succeeded: CO: 0 new companies",
        ]
        self.assertEqual(parse_finished_states(log), ["CO"])


class RunAbortedEarlyTests(unittest.TestCase):
    def test_none_for_a_normal_run(self):
        log = ["[1/1] CO: 4 new companies", "Done. 4 new rows -> output/leads.db (4 total unique phones)"]
        self.assertIsNone(run_aborted_early(log))

    def test_blocked_when_every_mirror_returned_403(self):
        log = ["[1/4] AL: skipped -- was blocked from reaching the map data source",
               "[2/4] AK: skipped -- was blocked from reaching the map data source",
               "Can't reach the map data service after 2 states in a row -- stopping early instead "
               "of waiting on the rest (blocked from reaching it). Check the connection (or "
               "whatever's blocking it) and try again."]
        self.assertEqual(run_aborted_early(log), "blocked")

    def test_unreachable_when_the_connection_is_refused(self):
        log = ["Can't reach the map data service after 2 states in a row -- stopping early instead "
               "of waiting on the rest (unable to reach it). Check the connection (or whatever's "
               "blocking it) and try again."]
        self.assertEqual(run_aborted_early(log), "unreachable")


class UserFacingLogTests(unittest.TestCase):
    def test_strips_the_cli_only_summary_lines(self):
        log = [
            "[1/1] CO: 4 new companies",
            "Done. 4 new rows -> /opt/render/project/src/output/leads.db (4 total unique phones)",
            "Failed states (rerun to retry): AK",
        ]
        self.assertEqual(user_facing_log(log), ["[1/1] CO: 4 new companies"])


class DeleteLeadTests(unittest.TestCase):
    def test_marks_only_the_matching_row_rejected_rather_than_removing_it(self):
        # The row is kept (not removed) so its phone permanently blocks the scraper from treating
        # it as new again -- see db.is_rejected() and the scraper's use of db.existing_phones().
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            db.insert_if_new(db_path, {"company_name": "Keep Me", "phone": "111"})
            db.insert_if_new(db_path, {"company_name": "Remove Me", "phone": "222"})
            self.assertTrue(delete_lead(db_path, "222"))
            rows = {r["phone"]: r for r in db.all_leads(db_path, include_rejected=True)}
        self.assertEqual(rows["111"]["rejected_at"], "")
        self.assertTrue(rows["222"]["rejected_at"])

    def test_rejected_leads_are_hidden_from_read_leads(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            db.insert_if_new(db_path, {"company_name": "Keep Me", "phone": "111"})
            db.insert_if_new(db_path, {"company_name": "Remove Me", "phone": "222"})
            delete_lead(db_path, "222")
            self.assertEqual([r["company_name"] for r in read_leads(db_path)], ["Keep Me"])

    def test_a_rejected_phone_is_never_treated_as_new_by_a_later_scrape(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            db.insert_if_new(db_path, {"company_name": "Junk Removal Co", "phone": "222"})
            delete_lead(db_path, "222")
            self.assertIn("222", db.existing_phones(db_path))

    def test_returns_false_for_an_unknown_phone(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            db.insert_if_new(db_path, {"company_name": "A", "phone": "111"})
            self.assertFalse(delete_lead(db_path, "999"))

    def test_returns_false_when_the_database_does_not_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(delete_lead(Path(tmp) / "missing.db", "111"))


class UndeleteLeadTests(unittest.TestCase):
    def test_clears_rejected_at_and_the_lead_reappears(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            db.insert_if_new(db_path, {"company_name": "Oops Not Junk After All", "phone": "222"})
            delete_lead(db_path, "222")
            self.assertEqual(read_leads(db_path), [])
            self.assertTrue(undelete_lead(db_path, "222"))
            self.assertEqual([r["company_name"] for r in read_leads(db_path)], ["Oops Not Junk After All"])

    def test_returns_false_for_a_lead_that_was_never_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            db.insert_if_new(db_path, {"company_name": "A", "phone": "111"})
            self.assertFalse(undelete_lead(db_path, "111"))

    def test_returns_false_for_an_unknown_phone(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            db.insert_if_new(db_path, {"company_name": "A", "phone": "111"})
            self.assertFalse(undelete_lead(db_path, "999"))


class UpdateLeadTests(unittest.TestCase):
    def test_sets_only_the_given_fields_on_the_matching_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            db.insert_if_new(db_path, {"company_name": "A", "phone": "111"})
            self.assertTrue(update_lead(db_path, "111", {"status": "Interested"}))
            row = db.all_leads(db_path)[0]
        self.assertEqual(row["status"], "Interested")
        self.assertEqual(row["notes"], "")

    def test_returns_false_for_an_unknown_phone(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            db.insert_if_new(db_path, {"company_name": "A", "phone": "111"})
            self.assertFalse(update_lead(db_path, "999", {"status": "Interested"}))


class PumpOutputTests(unittest.TestCase):
    """A backup failure/success during a scrape (see lead_scraper._attempt_state's BACKUP_ERROR/
    BACKUP_OK marker lines) must reach the same backup_state an edit-triggered backup uses, instead
    of only ever showing up as a raw line in the plain-language run log with no banner for it."""

    def setUp(self):
        app.job["log"] = []

    def tearDown(self):
        app.backup_state["error"] = None
        app.backup_state["lastSuccessAt"] = None
        app.job["log"] = []

    def test_a_backup_error_marker_sets_backup_state_and_is_not_logged(self):
        proc = SimpleNamespace(stdout=["[1/1] CO: 0 new companies\n", "BACKUP_ERROR: GitHub push failed: HTTP 409\n"],
                                wait=lambda: None)
        app.pump_output(proc)
        self.assertEqual(app.backup_state["error"], "GitHub push failed: HTTP 409")
        self.assertEqual(app.job["log"], ["[1/1] CO: 0 new companies"])

    def test_a_later_backup_ok_marker_clears_a_prior_error_and_records_when(self):
        proc = SimpleNamespace(stdout=["BACKUP_ERROR: boom\n", "BACKUP_OK\n"], wait=lambda: None)
        app.pump_output(proc)
        self.assertIsNone(app.backup_state["error"])
        self.assertIsNotNone(app.backup_state["lastSuccessAt"])
        self.assertEqual(app.job["log"], [])


class BackupSchedulingTests(unittest.TestCase):
    """schedule_sync() defers the actual push to a background Timer instead of blocking the
    request, so these call app._run_sync() directly rather than waiting out the real delay."""

    def tearDown(self):
        with app.lock:
            if app._sync_timer is not None:
                app._sync_timer.cancel()
                app._sync_timer = None
        app.backup_state["error"] = None
        app.backup_state["lastSuccessAt"] = None

    @patch("app.db.sync_backup", return_value=None)
    def test_a_successful_sync_clears_any_previous_error_and_records_when(self, mock_sync):
        app.backup_state["error"] = "old failure"
        app._run_sync()
        self.assertIsNone(app.backup_state["error"])
        self.assertIsNotNone(app.backup_state["lastSuccessAt"])
        mock_sync.assert_called_once_with(app.DB_PATH)

    @patch("app.db.sync_backup", return_value="Supabase push failed: HTTP 500")
    def test_a_failed_sync_is_recorded_and_schedules_a_retry(self, mock_sync):
        app._run_sync()
        self.assertEqual(app.backup_state["error"], "Supabase push failed: HTTP 500")
        self.assertIsNone(app.backup_state["lastSuccessAt"])
        with app.lock:
            self.assertIsNotNone(app._sync_timer)

    def test_schedule_sync_sets_a_pending_timer(self):
        app.schedule_sync()
        with app.lock:
            self.assertIsNotNone(app._sync_timer)

    def test_a_new_edit_supersedes_a_pending_retry_instead_of_stacking(self):
        with patch("app.db.sync_backup", return_value="boom"):
            app._run_sync()
        with app.lock:
            first_timer = app._sync_timer
        app.schedule_sync()
        with app.lock:
            self.assertIsNot(app._sync_timer, first_timer)
        # cancel() just signals the timer's thread to stop; give it a moment to actually exit before
        # checking, rather than racing its teardown immediately after cancel() returns.
        first_timer.join(timeout=1)
        self.assertFalse(first_timer.is_alive())  # the stale retry was cancelled, not left running


if __name__ == "__main__":
    unittest.main()
