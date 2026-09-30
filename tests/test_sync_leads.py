import csv
import sys
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


def _fake_psycopg2_module():
    """A MagicMock standing in for the `psycopg2`/`psycopg2.extras` modules, wired so the
    `with psycopg2.connect(...) as conn: with conn.cursor() as cur:` pattern in push/pull_supabase
    works, and `except psycopg2.Error` catches a real exception (mock.Error is set to the real
    Exception class, not a MagicMock, since `except` needs an actual exception type)."""
    mock_psycopg2 = MagicMock()
    mock_psycopg2.Error = Exception
    mock_conn = mock_psycopg2.connect.return_value.__enter__.return_value
    mock_cur = mock_conn.cursor.return_value.__enter__.return_value
    mock_extras = MagicMock()
    return mock_psycopg2, mock_cur, mock_extras


class SupabaseDisabledTests(unittest.TestCase):
    @patch.dict("os.environ", {}, clear=True)
    def test_push_and_pull_do_nothing_without_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leads.csv"
            path.write_text("company_name,phone\nA,555\n")
            sync_leads.push_supabase(path)  # must not raise
            sync_leads.pull_supabase(path)  # must not raise
            self.assertEqual(path.read_text(), "company_name,phone\nA,555\n")  # pull left it untouched


class SupabaseEnabledTests(unittest.TestCase):
    @patch.dict("os.environ", {"SUPABASE_DB_URL": "postgresql://x"}, clear=True)
    def test_push_upserts_every_row_keyed_on_phone(self):
        mock_psycopg2, mock_cur, mock_extras = _fake_psycopg2_module()
        with patch.dict(sys.modules, {"psycopg2": mock_psycopg2, "psycopg2.extras": mock_extras}):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "leads.csv"
                with path.open("w", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(["company_name", "phone"])
                    writer.writerow(["Acme Waste", "(555) 123-4567"])
                sync_leads.push_supabase(path)
        mock_extras.execute_values.assert_called_once()
        cur_arg, query, values = mock_extras.execute_values.call_args.args
        self.assertIs(cur_arg, mock_cur)
        self.assertIn("ON CONFLICT (phone) DO UPDATE SET", query)
        self.assertIn('INSERT INTO leads ("company_name", "phone")', query)
        self.assertEqual(values, [("Acme Waste", "(555) 123-4567")])

    @patch.dict("os.environ", {"SUPABASE_DB_URL": "postgresql://x"}, clear=True)
    def test_push_skips_rows_with_no_phone(self):
        # A stray blank separator row (or any row with no phone) must never reach the upsert --
        # phone is the primary key, so an empty one would collide with every other empty one.
        mock_psycopg2, mock_cur, mock_extras = _fake_psycopg2_module()
        with patch.dict(sys.modules, {"psycopg2": mock_psycopg2, "psycopg2.extras": mock_extras}):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "leads.csv"
                with path.open("w", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(["company_name", "phone"])
                    writer.writerow(["Acme Waste", "111"])
                    writer.writerow(["", ""])
                sync_leads.push_supabase(path)
        _, _, values = mock_extras.execute_values.call_args.args
        self.assertEqual(values, [("Acme Waste", "111")])

    @patch.dict("os.environ", {"SUPABASE_DB_URL": "postgresql://x"}, clear=True)
    def test_push_does_nothing_for_an_empty_csv(self):
        mock_psycopg2, mock_cur, mock_extras = _fake_psycopg2_module()
        with patch.dict(sys.modules, {"psycopg2": mock_psycopg2, "psycopg2.extras": mock_extras}):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "leads.csv"
                path.write_text("company_name,phone\n")
                sync_leads.push_supabase(path)
        mock_extras.execute_values.assert_not_called()

    @patch.dict("os.environ", {"SUPABASE_DB_URL": "postgresql://x"}, clear=True)
    def test_push_skips_a_missing_file(self):
        mock_psycopg2, mock_cur, mock_extras = _fake_psycopg2_module()
        with patch.dict(sys.modules, {"psycopg2": mock_psycopg2, "psycopg2.extras": mock_extras}):
            sync_leads.push_supabase(Path("/nonexistent/leads.csv"))
        mock_psycopg2.connect.assert_not_called()

    @patch.dict("os.environ", {"SUPABASE_DB_URL": "postgresql://x"}, clear=True)
    def test_push_swallows_a_database_error(self):
        mock_psycopg2, mock_cur, mock_extras = _fake_psycopg2_module()
        mock_psycopg2.connect.side_effect = Exception("could not connect to server")
        sync_leads.last_backup_error = None
        with patch.dict(sys.modules, {"psycopg2": mock_psycopg2, "psycopg2.extras": mock_extras}):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "leads.csv"
                path.write_text("company_name,phone\nA,111\n")
                sync_leads.push_supabase(path)  # must not raise
        self.assertIn("Supabase push failed", sync_leads.last_backup_error)

    @patch.dict("os.environ", {"SUPABASE_DB_URL": "postgresql://x"}, clear=True)
    def test_pull_writes_every_row_from_the_table(self):
        mock_psycopg2, mock_cur, mock_extras = _fake_psycopg2_module()
        mock_cur.fetchall.return_value = [("Acme Waste", "111", "2026-09-28")]
        mock_cur.description = [MagicMock(), MagicMock(), MagicMock()]
        for col, m in zip(["company_name", "phone", "rejected_at"], mock_cur.description):
            m.name = col  # MagicMock(name=...) in the constructor sets repr, not the .name attribute
        with patch.dict(sys.modules, {"psycopg2": mock_psycopg2, "psycopg2.extras": mock_extras}):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "leads.csv"
                sync_leads.pull_supabase(path)
                with path.open() as f:
                    rows = list(csv.DictReader(f))
        self.assertEqual(rows, [{"company_name": "Acme Waste", "phone": "111", "rejected_at": "2026-09-28"}])

    @patch.dict("os.environ", {"SUPABASE_DB_URL": "postgresql://x"}, clear=True)
    def test_pull_leaves_local_file_untouched_when_the_table_is_empty(self):
        mock_psycopg2, mock_cur, mock_extras = _fake_psycopg2_module()
        mock_cur.fetchall.return_value = []
        mock_cur.description = []
        with patch.dict(sys.modules, {"psycopg2": mock_psycopg2, "psycopg2.extras": mock_extras}):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "leads.csv"
                path.write_text("original")
                sync_leads.pull_supabase(path)
                self.assertEqual(path.read_text(), "original")

    @patch.dict("os.environ", {"SUPABASE_DB_URL": "postgresql://x"}, clear=True)
    def test_pull_swallows_a_database_error(self):
        mock_psycopg2, mock_cur, mock_extras = _fake_psycopg2_module()
        mock_psycopg2.connect.side_effect = Exception("timeout")
        sync_leads.last_backup_error = None
        with patch.dict(sys.modules, {"psycopg2": mock_psycopg2, "psycopg2.extras": mock_extras}):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "leads.csv"
                sync_leads.pull_supabase(path)  # must not raise
        self.assertIn("Supabase pull failed", sync_leads.last_backup_error)

    def test_push_then_pull_preserves_a_rejected_lead(self):
        # End-to-end proof against a real bug class: a rejected lead must survive a full
        # push-then-pull round trip through Supabase, since that's exactly what a cold Render
        # restart does when GitHub backup is unconfigured. If it didn't, the lead would come back
        # looking brand new to the scraper and get re-added -- silently undoing the delete.
        stored_rows = {}

        def fake_execute_values(cur, query, values):
            for row in values:
                stored_rows[row[1]] = row  # phone is column index 1 here

        mock_psycopg2, mock_cur, mock_extras = _fake_psycopg2_module()
        mock_extras.execute_values.side_effect = fake_execute_values
        with patch.dict("os.environ", {"SUPABASE_DB_URL": "postgresql://x"}, clear=True), \
             patch.dict(sys.modules, {"psycopg2": mock_psycopg2, "psycopg2.extras": mock_extras}):
            with tempfile.TemporaryDirectory() as tmp:
                push_path = Path(tmp) / "leads.csv"
                with push_path.open("w", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(["company_name", "phone", "rejected_at"])
                    writer.writerow(["Keep Co", "1", ""])
                    writer.writerow(["Junk Removal Co", "2", "2026-09-24"])
                sync_leads.push_supabase(push_path)

            mock_cur.description = [MagicMock(), MagicMock(), MagicMock()]
            for col, m in zip(["company_name", "phone", "rejected_at"], mock_cur.description):
                m.name = col
            mock_cur.fetchall.return_value = list(stored_rows.values())
            with tempfile.TemporaryDirectory() as tmp:
                pull_path = Path(tmp) / "leads.csv"
                sync_leads.pull_supabase(pull_path)
                with pull_path.open() as f:
                    rows = list(csv.DictReader(f))

        by_phone = {r["phone"]: r for r in rows}
        self.assertEqual(set(by_phone), {"1", "2"})
        self.assertEqual(by_phone["2"]["rejected_at"], "2026-09-24")


class RestoreTests(unittest.TestCase):
    @patch("sync_leads.pull_supabase")
    @patch("sync_leads.pull_github")
    def test_falls_back_to_supabase_when_github_yields_nothing(self, mock_pull_github, mock_pull_supabase):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leads.csv"  # pull_github is mocked to a no-op, so this stays missing
            sync_leads.restore(path)
        mock_pull_github.assert_called_once_with(path)
        mock_pull_supabase.assert_called_once_with(path)

    @patch("sync_leads.pull_supabase")
    @patch("sync_leads.pull_github")
    def test_skips_supabase_when_github_already_restored_data(self, mock_pull_github, mock_pull_supabase):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leads.csv"
            mock_pull_github.side_effect = lambda p: p.write_text("company_name,phone\nA,555\n")
            sync_leads.restore(path)
        mock_pull_supabase.assert_not_called()


if __name__ == "__main__":
    unittest.main()
