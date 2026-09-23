import unittest

from app import parse_failed_states


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


if __name__ == "__main__":
    unittest.main()
