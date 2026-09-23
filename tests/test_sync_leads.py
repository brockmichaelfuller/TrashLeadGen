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
    def test_pull_writes_decoded_content(self, mock_requests):
        import base64
        mock_requests.get.return_value = MagicMock(
            status_code=200, json=lambda: {"content": base64.b64encode(b"company_name,phone\nA,555\n").decode()})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "leads.csv"
            sync_leads.pull_github(path)
            self.assertEqual(path.read_text(), "company_name,phone\nA,555\n")

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
    def test_push_sends_parsed_csv_rows_and_clears_first(self, mock_session_fn):
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
        self.assertIn("A1:clear", mock_session.post.call_args.args[0])
        sent_values = mock_session.put.call_args.kwargs["json"]["values"]
        self.assertEqual(sent_values, [["company_name", "phone"], ["Acme Waste", "(555) 123-4567"]])

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
