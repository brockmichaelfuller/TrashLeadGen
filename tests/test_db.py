import csv
import tempfile
import threading
import unittest
from pathlib import Path

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
                self.assertEqual(lead[col], "" if col != "phone" else "111")


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


class CsvBridgeTests(unittest.TestCase):
    def test_export_then_import_round_trips_every_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path, csv_path = Path(tmp) / "leads.db", Path(tmp) / "leads.csv"
            row = {c: f"val-{c}" for c in db.COLUMNS}
            row["phone"] = "111"
            db.insert_if_new(db_path, row)
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


class IsEmptyTests(unittest.TestCase):
    def test_true_for_a_path_that_does_not_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(db.is_empty(Path(tmp) / "missing.db"))

    def test_false_once_a_lead_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            db.insert_if_new(db_path, {"phone": "111"})
            self.assertFalse(db.is_empty(db_path))


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
