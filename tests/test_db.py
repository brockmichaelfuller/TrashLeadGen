import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import db
from tests.helpers import clear_leads_table, requires_db


@requires_db
class InsertIfNewTests(unittest.TestCase):
    def setUp(self):
        clear_leads_table()

    def test_inserts_a_new_phone_and_returns_true(self):
        row = {"company_name": "Acme Waste", "phone": "111", "email": "a@b.com"}
        self.assertTrue(db.insert_if_new(row))
        leads = db.all_leads()
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]["company_name"], "Acme Waste")
        self.assertEqual(leads[0]["phone"], "111")

    def test_a_duplicate_phone_is_ignored_and_returns_false(self):
        db.insert_if_new({"company_name": "First", "phone": "111"})
        self.assertFalse(db.insert_if_new({"company_name": "Second", "phone": "111"}))
        leads = db.all_leads()
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]["company_name"], "First")  # first write wins

    def test_missing_fields_default_to_empty_string_not_null(self):
        db.insert_if_new({"phone": "111"})
        lead = db.all_leads()[0]
        for col in db.COLUMNS:
            if col in ("phone", "updated_at", "created_at"):
                continue
            self.assertEqual(lead[col], "")
        self.assertEqual(lead["phone"], "111")

    def test_insert_stamps_updated_at_and_created_at(self):
        db.insert_if_new({"phone": "111"})
        lead = db.all_leads()[0]
        self.assertTrue(lead["updated_at"])
        self.assertTrue(lead["created_at"])

    def test_all_leads_orders_oldest_inserted_first(self):
        # The page reverses this to show newest first by default -- see static/index.html's
        # renderLeads(). created_at (not updated_at, which changes on every edit) is what has to
        # drive this, or editing an old lead would make it jump to look newly-scraped.
        db.insert_if_new({"phone": "1", "company_name": "First"})
        db.insert_if_new({"phone": "2", "company_name": "Second"})
        db.insert_if_new({"phone": "3", "company_name": "Third"})
        db.update_fields("1", {"status": "Interested"})  # editing the oldest must not reorder it
        self.assertEqual([l["company_name"] for l in db.all_leads()], ["First", "Second", "Third"])


@requires_db
class ExistingPhonesTests(unittest.TestCase):
    def setUp(self):
        clear_leads_table()

    def test_returns_every_phone_in_the_database(self):
        db.insert_if_new({"phone": "111"})
        db.insert_if_new({"phone": "222"})
        self.assertEqual(db.existing_phones(), {"111", "222"})

    def test_empty_for_a_brand_new_database(self):
        self.assertEqual(db.existing_phones(), set())


@requires_db
class UpdateFieldsTests(unittest.TestCase):
    def setUp(self):
        clear_leads_table()

    def test_updates_only_the_matching_row(self):
        db.insert_if_new({"phone": "111", "status": ""})
        db.insert_if_new({"phone": "222", "status": ""})
        self.assertTrue(db.update_fields("111", {"status": "Interested"}))
        leads = {l["phone"]: l for l in db.all_leads()}
        self.assertEqual(leads["111"]["status"], "Interested")
        self.assertEqual(leads["222"]["status"], "")

    def test_returns_false_for_an_unknown_phone(self):
        self.assertFalse(db.update_fields("999", {"status": "Interested"}))

    def test_returns_false_for_empty_updates(self):
        db.insert_if_new({"phone": "111"})
        self.assertFalse(db.update_fields("111", {}))

    def test_updating_a_field_stamps_updated_at(self):
        # push_supabase (back when this was a sync target rather than the only storage) used this to
        # tell a later edit apart from an older, in-flight snapshot of the same row -- kept now as a
        # general "when did this last change" fact other tooling may still want.
        db.insert_if_new({"phone": "111"})
        before = db.all_leads()[0]["updated_at"]
        time.sleep(0.01)
        db.update_fields("111", {"status": "Interested"})
        after = db.all_leads()[0]["updated_at"]
        self.assertNotEqual(before, after)
        self.assertGreater(after, before)


@requires_db
class RejectionTests(unittest.TestCase):
    def setUp(self):
        clear_leads_table()

    def test_marking_and_clearing_rejected(self):
        db.insert_if_new({"phone": "111"})
        self.assertFalse(db.is_rejected("111"))
        db.update_fields("111", {"rejected_at": "2026-09-28"})
        self.assertTrue(db.is_rejected("111"))
        db.update_fields("111", {"rejected_at": ""})
        self.assertFalse(db.is_rejected("111"))

    def test_a_rejected_lead_is_excluded_from_all_leads_by_default(self):
        db.insert_if_new({"phone": "111", "company_name": "Keep"})
        db.insert_if_new({"phone": "222", "company_name": "Reject"})
        db.update_fields("222", {"rejected_at": "2026-09-28"})
        self.assertEqual([l["company_name"] for l in db.all_leads()], ["Keep"])
        self.assertEqual({l["company_name"] for l in db.all_leads(include_rejected=True)},
                          {"Keep", "Reject"})

    def test_a_rejected_phone_still_counts_as_existing(self):
        # This is the whole point: existing_phones() must still include a rejected phone, or the
        # scraper would treat it as new and re-add it.
        db.insert_if_new({"phone": "111"})
        db.update_fields("111", {"rejected_at": "2026-09-28"})
        self.assertIn("111", db.existing_phones())


@requires_db
class RejectPhonesTests(unittest.TestCase):
    def setUp(self):
        clear_leads_table()

    def test_inserts_a_placeholder_row_rejected_for_an_unknown_phone(self):
        db.reject_phones({"111": "Junk Removal Co"})
        self.assertTrue(db.is_rejected("111"))
        lead = db.all_leads(include_rejected=True)[0]
        self.assertEqual(lead["company_name"], "Junk Removal Co")

    def test_rejects_a_phone_that_already_exists_as_an_active_lead(self):
        db.insert_if_new({"phone": "111", "company_name": "Keep The Real Data"})
        db.reject_phones({"111": "Ignored -- row already exists"})
        self.assertTrue(db.is_rejected("111"))
        lead = db.all_leads(include_rejected=True)[0]
        self.assertEqual(lead["company_name"], "Keep The Real Data")  # existing data untouched

    def test_never_overwrites_an_existing_rejected_at(self):
        db.insert_if_new({"phone": "111"})
        db.update_fields("111", {"rejected_at": "2020-01-01"})
        db.reject_phones({"111": "whatever"})
        lead = db.all_leads(include_rejected=True)[0]
        self.assertEqual(lead["rejected_at"], "2020-01-01")

    def test_does_not_reject_an_active_lead_absent_from_entries(self):
        db.insert_if_new({"phone": "111", "company_name": "Untouched"})
        db.reject_phones({"222": "Some Other Co"})
        self.assertFalse(db.is_rejected("111"))


@requires_db
class ImportAuditLogRejectionsTests(unittest.TestCase):
    def setUp(self):
        clear_leads_table()

    def test_rejects_every_deleted_verdict_and_skips_kept_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "audit_log.json"
            audit_path.write_text(json.dumps({"reviewed": {
                "111": {"company": "Junk Removal Co", "verdict": "deleted"},
                "222": {"company": "Real Hauler Inc", "verdict": "kept"},
            }}))
            with patch("db.AUDIT_LOG_PATH", audit_path):
                db.import_audit_log_rejections()
        self.assertTrue(db.is_rejected("111"))
        self.assertNotIn("222", db.existing_phones())  # "kept" leaves no trace here

    def test_a_missing_audit_log_is_a_silent_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("db.AUDIT_LOG_PATH", Path(tmp) / "does-not-exist.json"):
                db.import_audit_log_rejections()  # must not raise
        self.assertEqual(db.existing_phones(), set())


@requires_db
class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        clear_leads_table()

    def test_concurrent_inserts_from_multiple_threads_never_duplicate_or_lose_a_phone(self):
        # Each insert_if_new opens its own connection -- this proves Postgres's own transactional
        # guarantees are enough on their own, with no extra locking code needed on this side.
        phones = [str(i) for i in range(50)]

        def insert_all():
            for p in phones:
                db.insert_if_new({"phone": p, "company_name": f"Co {p}"})

        threads = [threading.Thread(target=insert_all) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        self.assertEqual(db.existing_phones(), set(phones))
        self.assertEqual(len(db.all_leads()), len(phones))  # no duplicates

    def test_a_read_during_a_concurrent_write_does_not_error(self):
        db.insert_if_new({"phone": "0"})
        errors = []

        def writer():
            for i in range(1, 50):
                db.insert_if_new({"phone": str(i)})

        def reader():
            for _ in range(50):
                try:
                    db.all_leads()
                except Exception as error:
                    errors.append(error)

        t1, t2 = threading.Thread(target=writer), threading.Thread(target=reader)
        t1.start(); t2.start()
        t1.join(timeout=15); t2.join(timeout=15)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
