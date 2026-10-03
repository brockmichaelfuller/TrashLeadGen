import unittest
from types import SimpleNamespace

import app
import db
from app import delete_lead, parse_failed_states, parse_finished_states, read_leads, run_aborted_early, \
    undelete_lead, update_lead, user_facing_log
from tests.helpers import clear_leads_table, requires_db


class ParseFailedStatesTests(unittest.TestCase):
    def test_reads_states_from_a_completed_runs_summary(self):
        log = [
            "[1/3] AL: 2 new companies",
            "[2/3] AK: skipped -- couldn't connect to the map data source",
            "[3/3] AZ: 1 new companies",
            "Done. 3 new rows -> Supabase (3 total unique phones)",
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
        log = ["[1/1] CO: 4 new companies", "Done. 4 new rows -> Supabase (4 total unique phones)"]
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
            "Done. 1 new rows -> Supabase (2 total unique phones)",
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
        log = ["[1/1] CO: 4 new companies", "Done. 4 new rows -> Supabase (4 total unique phones)"]
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
            "Done. 4 new rows -> Supabase (4 total unique phones)",
            "Failed states (rerun to retry): AK",
        ]
        self.assertEqual(user_facing_log(log), ["[1/1] CO: 4 new companies"])


@requires_db
class DeleteLeadTests(unittest.TestCase):
    def setUp(self):
        clear_leads_table()

    def test_marks_only_the_matching_row_rejected_rather_than_removing_it(self):
        # The row is kept (not removed) so its phone permanently blocks the scraper from treating
        # it as new again -- see db.is_rejected() and the scraper's use of db.existing_phones().
        db.insert_if_new({"company_name": "Keep Me", "phone": "111"})
        db.insert_if_new({"company_name": "Remove Me", "phone": "222"})
        self.assertTrue(delete_lead("222"))
        rows = {r["phone"]: r for r in db.all_leads(include_rejected=True)}
        self.assertEqual(rows["111"]["rejected_at"], "")
        self.assertTrue(rows["222"]["rejected_at"])

    def test_rejected_leads_are_hidden_from_read_leads(self):
        db.insert_if_new({"company_name": "Keep Me", "phone": "111"})
        db.insert_if_new({"company_name": "Remove Me", "phone": "222"})
        delete_lead("222")
        self.assertEqual([r["company_name"] for r in read_leads()], ["Keep Me"])

    def test_a_rejected_phone_is_never_treated_as_new_by_a_later_scrape(self):
        db.insert_if_new({"company_name": "Junk Removal Co", "phone": "222"})
        delete_lead("222")
        self.assertIn("222", db.existing_phones())

    def test_returns_false_for_an_unknown_phone(self):
        db.insert_if_new({"company_name": "A", "phone": "111"})
        self.assertFalse(delete_lead("999"))


@requires_db
class UndeleteLeadTests(unittest.TestCase):
    def setUp(self):
        clear_leads_table()

    def test_clears_rejected_at_and_the_lead_reappears(self):
        db.insert_if_new({"company_name": "Oops Not Junk After All", "phone": "222"})
        delete_lead("222")
        self.assertEqual(read_leads(), [])
        self.assertTrue(undelete_lead("222"))
        self.assertEqual([r["company_name"] for r in read_leads()], ["Oops Not Junk After All"])

    def test_returns_false_for_a_lead_that_was_never_rejected(self):
        db.insert_if_new({"company_name": "A", "phone": "111"})
        self.assertFalse(undelete_lead("111"))

    def test_returns_false_for_an_unknown_phone(self):
        db.insert_if_new({"company_name": "A", "phone": "111"})
        self.assertFalse(undelete_lead("999"))


@requires_db
class UpdateLeadTests(unittest.TestCase):
    def setUp(self):
        clear_leads_table()

    def test_sets_only_the_given_fields_on_the_matching_row(self):
        db.insert_if_new({"company_name": "A", "phone": "111"})
        self.assertTrue(update_lead("111", {"status": "Interested"}))
        row = db.all_leads()[0]
        self.assertEqual(row["status"], "Interested")
        self.assertEqual(row["notes"], "")

    def test_returns_false_for_an_unknown_phone(self):
        db.insert_if_new({"company_name": "A", "phone": "111"})
        self.assertFalse(update_lead("999", {"status": "Interested"}))


class PumpOutputTests(unittest.TestCase):
    def setUp(self):
        app.job["log"] = []

    def tearDown(self):
        app.job["log"] = []

    def test_appends_every_subprocess_line_to_the_run_log(self):
        proc = SimpleNamespace(stdout=["[1/1] CO: 0 new companies\n", "[1/1] CO: 0 new companies\n"],
                                wait=lambda: None)
        app.pump_output(proc)
        self.assertEqual(app.job["log"], ["[1/1] CO: 0 new companies"] * 2)

    def test_trims_to_the_most_recent_max_log_lines(self):
        proc = SimpleNamespace(stdout=[f"line {i}\n" for i in range(app.MAX_LOG_LINES + 10)], wait=lambda: None)
        app.pump_output(proc)
        self.assertEqual(len(app.job["log"]), app.MAX_LOG_LINES)
        self.assertEqual(app.job["log"][-1], f"line {app.MAX_LOG_LINES + 9}")


if __name__ == "__main__":
    unittest.main()
