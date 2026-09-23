import csv
import tempfile
import unittest
from pathlib import Path

from app import delete_lead, parse_failed_states


class ParseFailedStatesTests(unittest.TestCase):
    def test_reads_states_from_a_completed_runs_summary(self):
        log = [
            "[1/3] AL: 2 new companies",
            "[2/3] AK: skipped -- couldn't connect to the map data source",
            "[3/3] AZ: 1 new companies",
            "Done. 3 new rows -> output/leads.csv (3 total unique phones)",
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
        log = ["[1/1] CO: 4 new companies", "Done. 4 new rows -> output/leads.csv (4 total unique phones)"]
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
            "Done. 1 new rows -> output/leads.csv (2 total unique phones)",
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


class DeleteLeadTests(unittest.TestCase):
    def test_removes_only_the_matching_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leads.csv"
            with path.open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["company_name", "phone"])
                writer.writeheader()
                writer.writerow({"company_name": "Keep Me", "phone": "111"})
                writer.writerow({"company_name": "Remove Me", "phone": "222"})
            self.assertTrue(delete_lead(path, "222"))
            with path.open() as f:
                rows = list(csv.DictReader(f))
        self.assertEqual([r["company_name"] for r in rows], ["Keep Me"])

    def test_returns_false_for_an_unknown_phone(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leads.csv"
            with path.open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["company_name", "phone"])
                writer.writeheader()
                writer.writerow({"company_name": "A", "phone": "111"})
            self.assertFalse(delete_lead(path, "999"))

    def test_returns_false_when_the_file_does_not_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(delete_lead(Path(tmp) / "missing.csv", "111"))


if __name__ == "__main__":
    unittest.main()
