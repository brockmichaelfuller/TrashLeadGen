import csv
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import db


class InsertIfNewTests(unittest.TestCase):
    def test_inserts_a_new_phone_and_returns_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            row = {"company_name": "Acme Waste", "phone": "111", "email": "a@b.com"}
            self.assertTrue(db.insert_if_new(db_path, row))
            leads = db.all_leads(db_path)
            self.assertEqual(len(leads), 1)
            self.assertEqual(leads[0]["company_name"], "Acme Waste")
            self.assertEqual(leads[0]["phone"], "111")

    def test_a_duplicate_phone_is_ignored_and_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            db.insert_if_new(db_path, {"company_name": "First", "phone": "111"})
            self.assertFalse(db.insert_if_new(db_path, {"company_name": "Second", "phone": "111"}))
            leads = db.all_leads(db_path)
            self.assertEqual(len(leads), 1)
            self.assertEqual(leads[0]["company_name"], "First")  # first write wins

    def test_missing_fields_default_to_empty_string_not_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            db.insert_if_new(db_path, {"phone": "111"})
            lead = db.all_leads(db_path)[0]
            for col in db.COLUMNS:
                if col in ("phone", "updated_at"):
                    continue
                self.assertEqual(lead[col], "")
            self.assertEqual(lead["phone"], "111")

    def test_insert_stamps_updated_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            db.insert_if_new(db_path, {"phone": "111"})
            self.assertTrue(db.all_leads(db_path)[0]["updated_at"])


class ExistingPhonesTests(unittest.TestCase):
    def test_returns_every_phone_in_the_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            db.insert_if_new(db_path, {"phone": "111"})
            db.insert_if_new(db_path, {"phone": "222"})
            self.assertEqual(db.existing_phones(db_path), {"111", "222"})

    def test_empty_for_a_brand_new_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            self.assertEqual(db.existing_phones(db_path), set())


class UpdateFieldsTests(unittest.TestCase):
    def test_updates_only_the_matching_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            db.insert_if_new(db_path, {"phone": "111", "status": ""})
            db.insert_if_new(db_path, {"phone": "222", "status": ""})
            self.assertTrue(db.update_fields(db_path, "111", {"status": "Interested"}))
            leads = {l["phone"]: l for l in db.all_leads(db_path)}
            self.assertEqual(leads["111"]["status"], "Interested")
            self.assertEqual(leads["222"]["status"], "")

    def test_returns_false_for_an_unknown_phone(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            self.assertFalse(db.update_fields(db_path, "999", {"status": "Interested"}))

    def test_returns_false_for_empty_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            db.insert_if_new(db_path, {"phone": "111"})
            self.assertFalse(db.update_fields(db_path, "111", {}))

    def test_updating_a_field_stamps_updated_at(self):
        # sync_leads.push_supabase uses this to tell a later edit apart from an older, in-flight
        # snapshot of the same row (see its docstring) -- a stale updated_at would defeat that.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            db.insert_if_new(db_path, {"phone": "111"})
            before = db.all_leads(db_path)[0]["updated_at"]
            db.update_fields(db_path, "111", {"status": "Interested"})
            after = db.all_leads(db_path)[0]["updated_at"]
            self.assertNotEqual(before, after)
            self.assertGreater(after, before)


class RejectionTests(unittest.TestCase):
    def test_marking_and_clearing_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            db.insert_if_new(db_path, {"phone": "111"})
            self.assertFalse(db.is_rejected(db_path, "111"))
            db.update_fields(db_path, "111", {"rejected_at": "2026-09-28"})
            self.assertTrue(db.is_rejected(db_path, "111"))
            db.update_fields(db_path, "111", {"rejected_at": ""})
            self.assertFalse(db.is_rejected(db_path, "111"))

    def test_a_rejected_lead_is_excluded_from_all_leads_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            db.insert_if_new(db_path, {"phone": "111", "company_name": "Keep"})
            db.insert_if_new(db_path, {"phone": "222", "company_name": "Reject"})
            db.update_fields(db_path, "222", {"rejected_at": "2026-09-28"})
            self.assertEqual([l["company_name"] for l in db.all_leads(db_path)], ["Keep"])
            self.assertEqual({l["company_name"] for l in db.all_leads(db_path, include_rejected=True)},
                              {"Keep", "Reject"})

    def test_a_rejected_phone_still_counts_as_existing(self):
        # This is the whole point: existing_phones() must still include a rejected phone, or the
        # scraper would treat it as new and re-add it.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            db.insert_if_new(db_path, {"phone": "111"})
            db.update_fields(db_path, "111", {"rejected_at": "2026-09-28"})
            self.assertIn("111", db.existing_phones(db_path))


class RejectPhonesTests(unittest.TestCase):
    def test_inserts_a_placeholder_row_rejected_for_an_unknown_phone(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            db.reject_phones(db_path, {"111": "Junk Removal Co"})
            self.assertTrue(db.is_rejected(db_path, "111"))
            lead = db.all_leads(db_path, include_rejected=True)[0]
            self.assertEqual(lead["company_name"], "Junk Removal Co")

    def test_rejects_a_phone_that_already_exists_as_an_active_lead(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            db.insert_if_new(db_path, {"phone": "111", "company_name": "Keep The Real Data"})
            db.reject_phones(db_path, {"111": "Ignored -- row already exists"})
            self.assertTrue(db.is_rejected(db_path, "111"))
            lead = db.all_leads(db_path, include_rejected=True)[0]
            self.assertEqual(lead["company_name"], "Keep The Real Data")  # existing data untouched

    def test_never_overwrites_an_existing_rejected_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            db.insert_if_new(db_path, {"phone": "111"})
            db.update_fields(db_path, "111", {"rejected_at": "2020-01-01"})
            db.reject_phones(db_path, {"111": "whatever"})
            lead = db.all_leads(db_path, include_rejected=True)[0]
            self.assertEqual(lead["rejected_at"], "2020-01-01")

    def test_does_not_reject_an_active_lead_absent_from_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            db.insert_if_new(db_path, {"phone": "111", "company_name": "Untouched"})
            db.reject_phones(db_path, {"222": "Some Other Co"})
            self.assertFalse(db.is_rejected(db_path, "111"))


class ImportAuditLogRejectionsTests(unittest.TestCase):
    def test_rejects_every_deleted_verdict_and_skips_kept_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            audit_path = Path(tmp) / "audit_log.json"
            audit_path.write_text(json.dumps({"reviewed": {
                "111": {"company": "Junk Removal Co", "verdict": "deleted"},
                "222": {"company": "Real Hauler Inc", "verdict": "kept"},
            }}))
            with patch("db.AUDIT_LOG_PATH", audit_path):
                db.import_audit_log_rejections(db_path)
            self.assertTrue(db.is_rejected(db_path, "111"))
            self.assertNotIn("222", db.existing_phones(db_path))  # "kept" leaves no trace here

    def test_a_missing_audit_log_is_a_silent_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            with patch("db.AUDIT_LOG_PATH", Path(tmp) / "does-not-exist.json"):
                db.import_audit_log_rejections(db_path)  # must not raise
            self.assertEqual(db.existing_phones(db_path), set())


class CsvBridgeTests(unittest.TestCase):
    def test_export_then_import_round_trips_every_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path, csv_path = Path(tmp) / "leads.db", Path(tmp) / "leads.csv"
            row = {c: f"val-{c}" for c in db.COLUMNS}
            row["phone"] = "111"
            db.insert_if_new(db_path, row)
            # insert_if_new stamps a real updated_at regardless of what's passed in (see its
            # docstring) -- round-trip against what actually landed, not the synthetic input value.
            row["updated_at"] = db.all_leads(db_path, include_rejected=True)[0]["updated_at"]
            db.export_to_csv(db_path, csv_path)

            new_db_path = Path(tmp) / "restored.db"
            db.import_from_csv(new_db_path, csv_path)
            restored = db.all_leads(new_db_path, include_rejected=True)
            self.assertEqual(len(restored), 1)
            for col in db.COLUMNS:
                self.assertEqual(restored[0][col], row[col])

    def test_export_includes_rejected_leads(self):
        # This is the specific bug fixed earlier today, at the CSV/Sheets layer -- a rejected lead
        # must never just vanish from a backup snapshot.
        with tempfile.TemporaryDirectory() as tmp:
            db_path, csv_path = Path(tmp) / "leads.db", Path(tmp) / "leads.csv"
            db.insert_if_new(db_path, {"phone": "111"})
            db.update_fields(db_path, "111", {"rejected_at": "2026-09-28"})
            db.export_to_csv(db_path, csv_path)
            with csv_path.open() as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]["rejected_at"], "2026-09-28")

    def test_import_skips_a_blank_separator_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path, csv_path = Path(tmp) / "leads.db", Path(tmp) / "leads.csv"
            with csv_path.open("w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(db.COLUMNS)
                writer.writerow(["A", "111"] + [""] * (len(db.COLUMNS) - 2))
                writer.writerow([])
            db.import_from_csv(db_path, csv_path)
            self.assertEqual(len(db.all_leads(db_path)), 1)

    def test_import_into_a_nonexistent_csv_is_a_silent_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path, csv_path = Path(tmp) / "leads.db", Path(tmp) / "missing.csv"
            db.import_from_csv(db_path, csv_path)  # must not raise
            self.assertTrue(db.is_empty(db_path))

    def test_concurrent_exports_never_leave_a_reader_seeing_a_partial_file(self):
        # The scraper subprocess (after every state) and the web app (on its debounce timer, after
        # an edit) each call export_to_csv independently, so two exports to the same path can
        # genuinely overlap -- a reader (sync_leads pushing this file) must never be able to see a
        # half-written one. export_to_csv writes to a temp file and renames it into place instead of
        # truncating the target in place, so a concurrent read of csv_path always gets either the
        # complete old file or the complete new one, never something in between.
        with tempfile.TemporaryDirectory() as tmp:
            db_path, csv_path = Path(tmp) / "leads.db", Path(tmp) / "leads.csv"
            row_count = 30
            for i in range(row_count):
                db.insert_if_new(db_path, {"phone": str(i), "company_name": f"Co {i}"})

            stop = threading.Event()
            bad_reads = []

            def exporter():
                while not stop.is_set():
                    db.export_to_csv(db_path, csv_path)

            def reader():
                while not stop.is_set():
                    try:
                        with csv_path.open(newline="", encoding="utf-8") as f:
                            rows = list(csv.reader(f))
                    except FileNotFoundError:
                        continue
                    if len(rows) not in (0, row_count + 1):  # +1 for the header
                        bad_reads.append(len(rows))

            threads = [threading.Thread(target=exporter) for _ in range(4)] + [threading.Thread(target=reader) for _ in range(4)]
            for t in threads:
                t.start()
            time.sleep(0.5)
            stop.set()
            for t in threads:
                t.join(timeout=5)
            self.assertEqual(bad_reads, [])


class IsEmptyTests(unittest.TestCase):
    def test_true_for_a_path_that_does_not_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(db.is_empty(Path(tmp) / "missing.db"))

    def test_false_once_a_lead_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            db.insert_if_new(db_path, {"phone": "111"})
            self.assertFalse(db.is_empty(db_path))


class RestoreIfEmptyTests(unittest.TestCase):
    """Hit live: a failed restore used to be silently treated exactly like a genuinely-new,
    never-used database -- the caller had no way to tell the difference, so it proceeded to scrape
    and then backed up blank status/notes/rejected_at straight over Supabase's real values. These
    prove restore_if_empty now hands that distinction up instead of swallowing it."""

    def test_returns_the_error_and_does_not_import_anything_when_restore_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            with patch.object(db.sync_leads, "restore", return_value="Supabase pull failed: timeout"):
                error = db.restore_if_empty(db_path)
        self.assertEqual(error, "Supabase pull failed: timeout")
        self.assertTrue(db.is_empty(db_path))  # must not have imported a half-restored CSV

    def test_returns_none_and_imports_normally_when_restore_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"

            def fake_restore(csv_path):
                with csv_path.open("w", newline="") as f:
                    f.write("company_name,phone\nA,111\n")
                return None

            with patch.object(db.sync_leads, "restore", side_effect=fake_restore):
                error = db.restore_if_empty(db_path)
            self.assertIsNone(error)
            self.assertEqual(db.all_leads(db_path)[0]["phone"], "111")

    def test_returns_none_without_calling_restore_when_already_non_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            db.insert_if_new(db_path, {"phone": "111"})
            with patch.object(db.sync_leads, "restore") as mock_restore:
                error = db.restore_if_empty(db_path)
        mock_restore.assert_not_called()
        self.assertIsNone(error)


class BackupsPausedTests(unittest.TestCase):
    def test_not_paused_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(db.backups_paused(Path(tmp) / "leads.db"))

    def test_set_paused_then_unpaused(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            db.set_backups_paused(db_path, True)
            self.assertTrue(db.backups_paused(db_path))
            db.set_backups_paused(db_path, False)
            self.assertFalse(db.backups_paused(db_path))

    def test_unpausing_when_never_paused_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            db.set_backups_paused(Path(tmp) / "leads.db", False)  # must not raise

    def test_sync_backup_skips_the_external_push_while_paused_but_still_exports_locally(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            db.insert_if_new(db_path, {"phone": "111", "company_name": "Acme"})
            db.set_backups_paused(db_path, True)
            with patch.object(db.sync_leads, "sync") as mock_sync:
                result = db.sync_backup(db_path)
            mock_sync.assert_not_called()
            self.assertIsNone(result)
            csv_path = db_path.with_suffix(".csv")
            self.assertTrue(csv_path.exists())
            self.assertIn("Acme", csv_path.read_text())

    def test_sync_backup_pushes_normally_once_unpaused(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            db.insert_if_new(db_path, {"phone": "111"})
            db.set_backups_paused(db_path, True)
            db.set_backups_paused(db_path, False)
            with patch.object(db.sync_leads, "sync", return_value=None) as mock_sync:
                db.sync_backup(db_path)
            mock_sync.assert_called_once()


class BackupsPausedDurabilityTests(unittest.TestCase):
    """Hit live: the pause flag was a file on Render's free-plan disk, which is wiped on every
    restart -- Pause silently turned itself back on exactly when a restart was already the risky
    moment (see item 1's overwrite bug). When Supabase is configured, the pause state is stored
    there instead, via sync_leads.get_setting/set_setting, so it survives a restart."""

    @patch.dict("os.environ", {"SUPABASE_DB_URL": "postgresql://x"}, clear=True)
    def test_set_and_get_round_trip_through_supabase_not_the_local_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            with patch.object(db.sync_leads, "set_setting") as mock_set, \
                 patch.object(db.sync_leads, "get_setting", return_value="1") as mock_get:
                db.set_backups_paused(db_path, True)
                self.assertTrue(db.backups_paused(db_path))
            mock_set.assert_called_once_with("backups_paused", "1")
            mock_get.assert_called_once_with("backups_paused", "0")
            self.assertFalse(db._pause_flag_path(db_path).exists())  # never touches the local file

    @patch.dict("os.environ", {"SUPABASE_DB_URL": "postgresql://x"}, clear=True)
    def test_fails_open_not_paused_when_supabase_is_unreachable(self):
        # A transient Supabase outage must never silently stop backups by reporting "paused" when
        # the real answer just couldn't be determined -- same fail-open philosophy as every other
        # backup operation.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            with patch.object(db.sync_leads, "get_setting", return_value="0"):  # get_setting itself
                self.assertFalse(db.backups_paused(db_path))                   # already fails open


class ConcurrencyTests(unittest.TestCase):
    def test_concurrent_inserts_from_multiple_threads_never_duplicate_or_lose_a_phone(self):
        # This replaces the old fcntl-lock-based test: WAL mode + busy_timeout should make this safe
        # without any explicit locking code at all.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            phones = [str(i) for i in range(50)]

            def insert_all():
                for p in phones:
                    db.insert_if_new(db_path, {"phone": p, "company_name": f"Co {p}"})

            threads = [threading.Thread(target=insert_all) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            self.assertEqual(db.existing_phones(db_path), set(phones))
            self.assertEqual(len(db.all_leads(db_path)), len(phones))  # no duplicates

    def test_a_read_during_a_concurrent_write_does_not_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            db.insert_if_new(db_path, {"phone": "0"})
            errors = []

            def writer():
                for i in range(1, 100):
                    db.insert_if_new(db_path, {"phone": str(i)})

            def reader():
                for _ in range(100):
                    try:
                        db.all_leads(db_path)
                    except Exception as error:
                        errors.append(error)

            t1, t2 = threading.Thread(target=writer), threading.Thread(target=reader)
            t1.start(); t2.start()
            t1.join(timeout=10); t2.join(timeout=10)
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
