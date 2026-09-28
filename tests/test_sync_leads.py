import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import sync_leads


class GitHubDisabledTests(unittest.TestCase):
    """With no GITHUB_TOKEN/GITHUB_REPO set, both functions must be silent no-ops."""

    @patch.dict("os.environ", {}, clear=True)
    @patch("sync_leads.requests")
    def test_pull_and_push_do_nothing_without_config(self, mock_requests):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leads.csv"
            path.write_text("company_name,phone\nA,555\n")
            sync_leads.pull_github(path)
            sync_leads.push_github(path)
        mock_requests.get.assert_not_called()
        mock_requests.put.assert_not_called()


class GitHubEnabledTests(unittest.TestCase):
    @patch.dict("os.environ", {"GITHUB_TOKEN": "t", "GITHUB_REPO": "me/repo"}, clear=True)
    @patch("sync_leads.requests")
    def test_pull_writes_raw_content(self, mock_requests):
        mock_requests.get.return_value = MagicMock(status_code=200, content=b"company_name,phone\nA,555\n")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "leads.csv"
            sync_leads.pull_github(path)
            self.assertEqual(path.read_text(), "company_name,phone\nA,555\n")

    @patch.dict("os.environ", {"GITHUB_TOKEN": "t", "GITHUB_REPO": "me/repo"}, clear=True)
    @patch("sync_leads.requests")
    def test_pull_requests_the_raw_media_type_so_files_over_1mb_still_restore(self, mock_requests):
        # The default JSON envelope's base64 "content" field comes back empty for any file over
        # 1MB -- the "raw" media type returns the actual bytes directly instead, at any size.
        big_content = b"company_name,phone\n" + b"A,555\n" * 100_000  # well over 1MB
        mock_requests.get.return_value = MagicMock(status_code=200, content=big_content)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leads.csv"
            sync_leads.pull_github(path)
            self.assertEqual(path.read_bytes(), big_content)
        self.assertEqual(mock_requests.get.call_args.kwargs["headers"]["Accept"], "application/vnd.github.raw+json")

    @patch.dict("os.environ", {"GITHUB_TOKEN": "t", "GITHUB_REPO": "me/repo"}, clear=True)
    @patch("sync_leads.requests")
    def test_pull_leaves_local_file_alone_on_404(self, mock_requests):
        mock_requests.get.return_value = MagicMock(status_code=404)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leads.csv"
            path.write_text("original")
            sync_leads.pull_github(path)
            self.assertEqual(path.read_text(), "original")

    @patch.dict("os.environ", {"GITHUB_TOKEN": "t", "GITHUB_REPO": "me/repo"}, clear=True)
    @patch("sync_leads.requests")
    def test_push_includes_sha_when_file_already_exists(self, mock_requests):
        mock_requests.get.return_value = MagicMock(status_code=200, json=lambda: {"sha": "abc123"})
        mock_requests.put.return_value = MagicMock(status_code=200)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leads.csv"
            path.write_text("company_name,phone\nA,555\n")
            sync_leads.push_github(path)
        self.assertEqual(mock_requests.put.call_args.kwargs["json"]["sha"], "abc123")

    @patch.dict("os.environ", {"GITHUB_TOKEN": "t", "GITHUB_REPO": "me/repo"}, clear=True)
    @patch("sync_leads.requests")
    def test_push_omits_sha_for_a_new_file(self, mock_requests):
        mock_requests.get.return_value = MagicMock(status_code=404)
        mock_requests.put.return_value = MagicMock(status_code=201)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leads.csv"
            path.write_text("company_name,phone\nA,555\n")
            sync_leads.push_github(path)
        self.assertNotIn("sha", mock_requests.put.call_args.kwargs["json"])

    @patch.dict("os.environ", {"GITHUB_TOKEN": "t", "GITHUB_REPO": "me/repo"}, clear=True)
    @patch("sync_leads.requests")
    def test_push_retries_once_on_a_409_sha_conflict(self, mock_requests):
        # The scraper subprocess and the web app's debounce timer can each push independently --
        # whichever loses that race gets a 409 (its sha is now stale) purely from bad timing, not
        # because its content was actually invalid. Re-reading the sha and retrying once covers it.
        mock_requests.get.side_effect = [
            MagicMock(status_code=200, json=lambda: {"sha": "stale"}),
            MagicMock(status_code=200, json=lambda: {"sha": "fresh"}),
        ]
        mock_requests.put.side_effect = [MagicMock(status_code=409, text="conflict"), MagicMock(status_code=200)]
        sync_leads.last_backup_error = None  # isolate from whatever an earlier test in the run left behind
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leads.csv"
            path.write_text("company_name,phone\nA,555\n")
            sync_leads.push_github(path)
        self.assertEqual(mock_requests.put.call_count, 2)
        self.assertEqual(mock_requests.put.call_args.kwargs["json"]["sha"], "fresh")
        self.assertIsNone(sync_leads.last_backup_error)

    @patch.dict("os.environ", {"GITHUB_TOKEN": "t", "GITHUB_REPO": "me/repo"}, clear=True)
    @patch("sync_leads.requests")
    def test_push_reports_a_409_that_does_not_clear_on_retry(self, mock_requests):
        mock_requests.get.return_value = MagicMock(status_code=200, json=lambda: {"sha": "stale"})
        mock_requests.put.return_value = MagicMock(status_code=409, text="still conflicting")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leads.csv"
            path.write_text("company_name,phone\nA,555\n")
            sync_leads.push_github(path)
        self.assertEqual(mock_requests.put.call_count, 2)  # one retry, then gives up
        self.assertIn("409", sync_leads.last_backup_error)

    @patch.dict("os.environ", {"GITHUB_TOKEN": "t", "GITHUB_REPO": "me/repo"}, clear=True)
    @patch("sync_leads.requests")
    def test_push_skips_a_missing_file(self, mock_requests):
        sync_leads.push_github(Path("/nonexistent/leads.csv"))
        mock_requests.get.assert_not_called()

    @patch.dict("os.environ", {"GITHUB_TOKEN": "t", "GITHUB_REPO": "me/repo"}, clear=True)
    @patch("sync_leads.requests")
    def test_push_swallows_network_errors(self, mock_requests):
        mock_requests.RequestException = Exception
        mock_requests.get.side_effect = Exception("boom")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leads.csv"
            path.write_text("company_name,phone\nA,555\n")
            sync_leads.push_github(path)  # must not raise


class SheetsDisabledTests(unittest.TestCase):
    @patch.dict("os.environ", {}, clear=True)
    def test_push_sheets_does_nothing_without_config(self):
        sync_leads._sheets_session_tried = False
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leads.csv"
            path.write_text("company_name,phone\nA,555\n")
            sync_leads.push_sheets(path)  # must not raise, and there's nothing to assert beyond that


class SheetsEnabledTests(unittest.TestCase):
    def setUp(self):
        sync_leads._sheets_session = None
        sync_leads._sheets_session_tried = False

    def tearDown(self):
        sync_leads._sheets_session = None
        sync_leads._sheets_session_tried = False

    @patch.dict("os.environ", {"GOOGLE_SHEET_ID": "sheet123", "GOOGLE_SERVICE_ACCOUNT_JSON": '{"bad": "key"}'}, clear=True)
    def test_bad_service_account_json_is_a_silent_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leads.csv"
            path.write_text("company_name,phone\nA,555\n")
            sync_leads.push_sheets(path)  # must not raise despite an unusable key

    @patch.dict("os.environ", {"GOOGLE_SHEET_ID": "sheet123", "GOOGLE_SERVICE_ACCOUNT_JSON": "x"}, clear=True)
    @patch("sync_leads._sheets_session_or_none")
    def test_push_sends_parsed_csv_rows_and_clears_the_tail_after(self, mock_session_fn):
        mock_session = MagicMock()
        mock_session.put.return_value = MagicMock(status_code=200)
        mock_session_fn.return_value = mock_session
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leads.csv"
            with path.open("w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["company_name", "phone"])
                writer.writerow(["Acme Waste", "(555) 123-4567"])
            sync_leads.push_sheets(path)
        mock_session.post.assert_called_once()
        # Clears everything past the 2 rows just written, not just cell A1 -- clearing only A1
        # previously left stale rows behind whenever a push had fewer rows than the sheet's
        # previous content (e.g. after a deletion), so the sheet kept accumulating leftovers.
        self.assertIn("A3:Z100000:clear", mock_session.post.call_args.args[0])
        sent_values = mock_session.put.call_args.kwargs["json"]["values"]
        self.assertEqual(sent_values, [["company_name", "phone"], ["Acme Waste", "(555) 123-4567"]])

    @patch.dict("os.environ", {"GOOGLE_SHEET_ID": "sheet123", "GOOGLE_SERVICE_ACCOUNT_JSON": "x"}, clear=True)
    @patch("sync_leads._sheets_session_or_none")
    def test_a_failed_write_never_clears_the_sheet(self, mock_session_fn):
        # Hit live risk: the old clear-then-write order meant a write that failed right after a
        # successful clear left the sheet holding nothing -- and since this sheet is the only
        # backup once GitHub is unconfigured, a cold start right after would restore an empty
        # database from it. The write must happen first, so a failed write leaves prior content
        # (whatever's already in the sheet, which this test never touches) completely alone.
        mock_session = MagicMock()
        mock_session.put.return_value = MagicMock(status_code=500, text="server error")
        mock_session_fn.return_value = mock_session
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leads.csv"
            with path.open("w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["company_name", "phone"])
                writer.writerow(["Acme Waste", "(555) 123-4567"])
            sync_leads.push_sheets(path)
        mock_session.post.assert_not_called()
        self.assertEqual(sync_leads.last_backup_error, "Google Sheets push failed: HTTP 500: server error")

    @patch.dict("os.environ", {"GOOGLE_SHEET_ID": "sheet123", "GOOGLE_SERVICE_ACCOUNT_JSON": "x"}, clear=True)
    @patch("sync_leads._sheets_session_or_none")
    def test_a_failed_tail_clear_does_not_report_the_push_as_failed(self, mock_session_fn):
        # The write already landed at this point -- a stale trailing row or two left behind by a
        # failed clear self-heals on the next successful push, so this must not surface as an error.
        mock_session = MagicMock()
        mock_session.put.return_value = MagicMock(status_code=200)
        mock_session.post.side_effect = sync_leads.requests.RequestException("boom")
        mock_session_fn.return_value = mock_session
        sync_leads.last_backup_error = None  # isolate from whatever an earlier test in the run left behind
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leads.csv"
            with path.open("w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["company_name", "phone"])
                writer.writerow(["Acme Waste", "(555) 123-4567"])
            sync_leads.push_sheets(path)  # must not raise
        self.assertIsNone(sync_leads.last_backup_error)

    @patch.dict("os.environ", {"GOOGLE_SHEET_ID": "sheet123", "GOOGLE_SERVICE_ACCOUNT_JSON": "x"}, clear=True)
    @patch("sync_leads._sheets_session_or_none")
    def test_push_puts_rejected_leads_in_their_own_final_section_not_dropped(self, mock_session_fn):
        # Rejected rows must still be *in* the pushed sheet somewhere, not dropped -- restoring from
        # Sheets is this app's only backup once GitHub is unconfigured, and a rejected lead pushed
        # nowhere would silently come back as new-and-unrejected after the next cold start (see
        # RestoreRoundTripTests below). They get their own section, out of the way, instead of
        # mixing in with real leads.
        mock_session = MagicMock()
        mock_session.put.return_value = MagicMock(status_code=200)
        mock_session_fn.return_value = mock_session
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leads.csv"
            with path.open("w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["company_name", "phone", "email", "timezone", "rejected_at"])
                writer.writerow(["Keep Co", "1", "a@b.com", "CST", ""])
                writer.writerow(["Junk Removal Co", "2", "c@d.com", "CST", "2026-09-24"])
            sync_leads.push_sheets(path)
        sent_values = mock_session.put.call_args.kwargs["json"]["values"]
        self.assertEqual(sent_values, [
            ["company_name", "phone", "email", "timezone", "rejected_at"],
            ["Keep Co", "1", "a@b.com", "CST", ""],
            [],
            ["Junk Removal Co", "2", "c@d.com", "CST", "2026-09-24"],
        ])

    @patch.dict("os.environ", {"GOOGLE_SHEET_ID": "sheet123", "GOOGLE_SERVICE_ACCOUNT_JSON": "x"}, clear=True)
    @patch("sync_leads._sheets_session_or_none")
    def test_push_groups_complete_leads_at_the_top(self, mock_session_fn):
        mock_session = MagicMock()
        mock_session.put.return_value = MagicMock(status_code=200)
        mock_session_fn.return_value = mock_session
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leads.csv"
            with path.open("w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["company_name", "phone", "email", "timezone"])
                writer.writerow(["No Email Co", "555", "", "CST"])
                writer.writerow(["Complete Co", "555", "a@b.com", "CST"])
                writer.writerow(["No Timezone Co", "555", "a@b.com", ""])
                writer.writerow(["Also Complete Co", "555", "c@d.com", "EST"])
            sync_leads.push_sheets(path)
        sent_values = mock_session.put.call_args.kwargs["json"]["values"]
        self.assertEqual(sent_values, [
            ["company_name", "phone", "email", "timezone"],
            ["Complete Co", "555", "a@b.com", "CST"],
            ["Also Complete Co", "555", "c@d.com", "EST"],
            ["No Email Co", "555", "", "CST"],
            ["No Timezone Co", "555", "a@b.com", ""],
        ])

    @patch.dict("os.environ", {"GOOGLE_SHEET_ID": "sheet123", "GOOGLE_SERVICE_ACCOUNT_JSON": "x"}, clear=True)
    @patch("sync_leads._sheets_session_or_none")
    def test_push_sections_by_status_with_blank_rows_between(self, mock_session_fn):
        mock_session = MagicMock()
        mock_session.put.return_value = MagicMock(status_code=200)
        mock_session_fn.return_value = mock_session
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leads.csv"
            with path.open("w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["company_name", "phone", "email", "timezone", "status"])
                writer.writerow(["Not Interested Co", "1", "a@b.com", "CST", "Not interested"])
                writer.writerow(["No Status Co", "2", "a@b.com", "CST", ""])
                writer.writerow(["Do Not Contact Co", "3", "a@b.com", "CST", "Do not contact"])
                writer.writerow(["Interested Co", "4", "a@b.com", "CST", "Interested"])
            sync_leads.push_sheets(path)
        sent_values = mock_session.put.call_args.kwargs["json"]["values"]
        self.assertEqual(sent_values, [
            ["company_name", "phone", "email", "timezone", "status"],
            ["No Status Co", "2", "a@b.com", "CST", ""],
            [],
            ["Interested Co", "4", "a@b.com", "CST", "Interested"],
            [],
            ["Not Interested Co", "1", "a@b.com", "CST", "Not interested"],
            [],
            ["Do Not Contact Co", "3", "a@b.com", "CST", "Do not contact"],
        ])

    @patch.dict("os.environ", {"GOOGLE_SHEET_ID": "sheet123", "GOOGLE_SERVICE_ACCOUNT_JSON": "x"}, clear=True)
    @patch("sync_leads._sheets_session_or_none")
    def test_pull_writes_rows_and_pads_ragged_ones(self, mock_session_fn):
        mock_session = MagicMock()
        mock_session.get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"values": [["company_name", "phone", "notes"], ["Acme", "555"]]})  # Sheets drops trailing blanks
        mock_session_fn.return_value = mock_session
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leads.csv"
            sync_leads.pull_sheets(path)
            with path.open() as f:
                rows = list(csv.reader(f))
        self.assertEqual(rows, [["company_name", "phone", "notes"], ["Acme", "555", ""]])

    @patch.dict("os.environ", {"GOOGLE_SHEET_ID": "sheet123", "GOOGLE_SERVICE_ACCOUNT_JSON": "x"}, clear=True)
    @patch("sync_leads._sheets_session_or_none")
    def test_pull_drops_blank_separator_rows(self, mock_session_fn):
        # push_sheets inserts a wholly-blank row between status sections (see _group_and_sort);
        # pulling that back must not resurrect it as a lead with every field empty.
        mock_session = MagicMock()
        mock_session.get.return_value = MagicMock(status_code=200, json=lambda: {"values": [
            ["company_name", "phone", "email", "timezone", "status"],
            ["A", "1", "a@b.com", "CST", ""],
            [],
            ["B", "2", "b@c.com", "EST", "Interested"],
        ]})
        mock_session_fn.return_value = mock_session
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leads.csv"
            sync_leads.pull_sheets(path)
            with path.open() as f:
                rows = list(csv.DictReader(f))
        self.assertEqual([r["company_name"] for r in rows], ["A", "B"])

    def test_push_then_pull_preserves_a_rejected_lead(self):
        # End-to-end proof of the actual bug: a rejected lead must survive a full push-then-pull
        # round trip through Sheets, since that's exactly what a cold Render restart does when
        # GitHub backup is unconfigured. If it didn't, the lead would come back looking brand new
        # to the scraper and get re-added -- silently undoing the delete.
        with patch.dict("os.environ", {"GOOGLE_SHEET_ID": "sheet123", "GOOGLE_SERVICE_ACCOUNT_JSON": "x"}, clear=True), \
             patch("sync_leads._sheets_session_or_none") as mock_session_fn:
            mock_session = MagicMock()
            mock_session.put.return_value = MagicMock(status_code=200)
            mock_session_fn.return_value = mock_session
            with tempfile.TemporaryDirectory() as tmp:
                push_path = Path(tmp) / "leads.csv"
                with push_path.open("w", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(["company_name", "phone", "email", "timezone", "rejected_at"])
                    writer.writerow(["Keep Co", "1", "a@b.com", "CST", ""])
                    writer.writerow(["Junk Removal Co", "2", "c@d.com", "CST", "2026-09-24"])
                sync_leads.push_sheets(push_path)
            pushed_values = mock_session.put.call_args.kwargs["json"]["values"]

        with patch.dict("os.environ", {"GOOGLE_SHEET_ID": "sheet123", "GOOGLE_SERVICE_ACCOUNT_JSON": "x"}, clear=True), \
             patch("sync_leads._sheets_session_or_none") as mock_session_fn2:
            mock_session2 = MagicMock()
            mock_session2.get.return_value = MagicMock(status_code=200, json=lambda: {"values": pushed_values})
            mock_session_fn2.return_value = mock_session2
            with tempfile.TemporaryDirectory() as tmp:
                pull_path = Path(tmp) / "leads.csv"
                sync_leads.pull_sheets(pull_path)
                with pull_path.open() as f:
                    rows = list(csv.DictReader(f))

        by_phone = {r["phone"]: r for r in rows}
        self.assertEqual(set(by_phone), {"1", "2"})
        self.assertEqual(by_phone["2"]["rejected_at"], "2026-09-24")


class RestoreTests(unittest.TestCase):
    def setUp(self):
        sync_leads._sheets_session = None
        sync_leads._sheets_session_tried = False

    def tearDown(self):
        sync_leads._sheets_session = None
        sync_leads._sheets_session_tried = False

    @patch("sync_leads.pull_sheets")
    @patch("sync_leads.pull_github")
    def test_falls_back_to_sheets_when_github_yields_nothing(self, mock_pull_github, mock_pull_sheets):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leads.csv"  # pull_github is mocked to a no-op, so this stays missing
            sync_leads.restore(path)
        mock_pull_github.assert_called_once_with(path)
        mock_pull_sheets.assert_called_once_with(path)

    @patch("sync_leads.pull_sheets")
    @patch("sync_leads.pull_github")
    def test_skips_sheets_when_github_already_restored_data(self, mock_pull_github, mock_pull_sheets):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leads.csv"
            mock_pull_github.side_effect = lambda p: p.write_text("company_name,phone\nA,555\n")
            sync_leads.restore(path)
        mock_pull_sheets.assert_not_called()


if __name__ == "__main__":
    unittest.main()
