import csv
import tempfile
import unittest
from pathlib import Path

from lead_scraper import STATE_GROUPS, STATES, clean_email, element_to_row, is_complete, missing_fields, load_existing_phones, normalize_phone


class NormalizePhoneTests(unittest.TestCase):
    def test_formats_common_variants(self):
        self.assertEqual(normalize_phone("+1-303-343-7096"), "(303) 343-7096")
        self.assertEqual(normalize_phone("303.343.7096"), "(303) 343-7096")
        self.assertEqual(normalize_phone("1 (303) 343 7096"), "(303) 343-7096")

    def test_takes_first_valid_of_multiple(self):
        self.assertEqual(normalize_phone("n/a; 303-343-7096"), "(303) 343-7096")

    def test_rejects_invalid(self):
        self.assertIsNone(normalize_phone("123-4567"))
        self.assertIsNone(normalize_phone("000-000-0000"))
        self.assertIsNone(normalize_phone(""))
        self.assertIsNone(normalize_phone(None))


class ElementToRowTests(unittest.TestCase):
    def element(self, **tags):
        return {"type": "node", "id": 1, "tags": tags}

    def test_builds_row_with_search_state(self):
        row = element_to_row(self.element(name="Acme Waste", phone="303-343-7096", email="info@acme.com", **{"addr:city": "Denver"}), "CO", "2026-01-01")
        self.assertEqual(row["city"], "Denver")
        self.assertEqual(row["state"], "CO")
        self.assertEqual(row["source"], "openstreetmap:node/1")

    def test_skips_water_utilities_and_missing_phone(self):
        self.assertIsNone(element_to_row(self.element(name="Metro Water & Sanitation", phone="303-343-7096"), "CO", "x"))
        self.assertIsNone(element_to_row(self.element(name="Acme Waste"), "CO", "x"))


class MoreTests(unittest.TestCase):
    def test_covers_all_states_and_dc(self):
        self.assertEqual(len(STATES), 51)
        self.assertEqual(len(set(STATES)), 51)

    def test_state_groups_cover_every_state_once(self):
        self.assertEqual(len(STATE_GROUPS), 4)
        flattened = [s for g in STATE_GROUPS for s in g]
        self.assertEqual(flattened, STATES)
        for g in STATE_GROUPS:
            self.assertIn(len(g), (12, 13))

    def test_keyword_must_be_a_whole_word(self):
        for name in ("Wasted Ink Zine Distro", "The Unwaste Shop"):
            tags = {"name": name, "phone": "303-343-7096"}
            self.assertIsNone(element_to_row({"type": "node", "id": 1, "tags": tags}, "CO", "x"))
        tags = {"name": "Acme Waste Services", "phone": "303-343-7096", "email": "info@acme.com"}
        self.assertIsNotNone(element_to_row({"type": "node", "id": 1, "tags": tags}, "CO", "x"))

    def test_name_must_contain_a_hauling_keyword(self):
        # "Dumpsters" and "Hauling" alone are too generic (dumpster rental, moving companies, etc.)
        # and aren't in the keyword list, so these are skipped even though they're plausible names.
        for name in ("Acme Dumpsters", "Toyland Hauling"):
            tags = {"name": name, "phone": "303-343-7096"}
            self.assertIsNone(element_to_row({"type": "node", "id": 1, "tags": tags}, "CO", "x"), name)
        # The word doesn't have to be "waste" specifically -- garbage/trash/sanitation/refuse/
        # disposal/rubbish all count, since the point is "residential trash hauler," not the word.
        for name in ("Southwest Sanitation", "Downtown Trash Removal", "Acme Garbage Co",
                     "City Refuse Pickup", "Acme Disposal Services", "Olde Towne Rubbish Co"):
            tags = {"name": name, "phone": "303-343-7096"}
            self.assertIsNotNone(element_to_row({"type": "node", "id": 1, "tags": tags}, "CO", "x"), name)

    def test_only_garbage_pickup_companies_are_kept(self):
        for name in ("Colorado Medical Waste", "Marine Sanitation & Supply", "Sunset Landfill Waste",
                     "City of Dallas Sanitation Department", "Acme Portable Toilet Waste"):
            tags = {"name": name, "phone": "303-343-7096"}
            self.assertIsNone(element_to_row({"type": "node", "id": 1, "tags": tags}, "CO", "x"), name)
        for name in ("ABC Waste Services", "Tri-County Waste Disposal"):
            tags = {"name": name, "phone": "303-343-7096", "email": "a@b.com"}
            self.assertIsNotNone(element_to_row({"type": "node", "id": 1, "tags": tags}, "CO", "x"), name)

    def test_only_residential_curb_pickup_is_kept(self):
        for name in ("Metro Roll-Off Waste Hauling", "ABC Waste Dumpster Rental",
                     "Waste Connections Sustainability Campus", "Zero Waste Market", "My Zero Waste Store",
                     "Acme Waste Junk Removal", "Waste Management Corporate Headquarters",
                     "XYZ Construction & Waste Debris Removal"):
            tags = {"name": name, "phone": "303-343-7096"}
            self.assertIsNone(element_to_row({"type": "node", "id": 1, "tags": tags}, "CO", "x"), name)
        for name in ("Waste Connections", "ABC Waste Services", "Republic Waste Pickup"):
            tags = {"name": name, "phone": "303-343-7096"}
            self.assertIsNotNone(element_to_row({"type": "node", "id": 1, "tags": tags}, "CO", "x"), name)


class ExistingPhonesTests(unittest.TestCase):
    def test_existing_rows_match_regardless_of_phone_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leads.csv"
            with path.open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["company_name", "phone"])
                writer.writeheader()
                writer.writerow({"company_name": "A", "phone": "303-343-7096"})
                writer.writerow({"company_name": "B", "phone": "(480) 400-3393"})
            phones = load_existing_phones(path)
            self.assertIn("(303) 343-7096", phones)
            self.assertIn("(480) 400-3393", phones)
            self.assertEqual(load_existing_phones(Path(tmp) / "missing.csv"), set())


class RequiredFieldsTests(unittest.TestCase):
    def test_lead_needs_name_phone_email_and_timezone(self):
        full = {"company_name": "A Waste", "phone": "(303) 343-7096", "email": "a@b.com", "timezone": "Mountain"}
        self.assertTrue(is_complete(full))
        for missing in full:
            self.assertFalse(is_complete({**full, missing: ""}), missing)

    def test_scraped_row_without_email_is_kept_as_partial(self):
        tags = {"name": "Acme Waste", "phone": "303-343-7096"}
        row = element_to_row({"type": "node", "id": 1, "tags": tags}, "CO", "x")
        self.assertEqual(missing_fields(row), ["email"])
        self.assertFalse(is_complete(row))

    def test_clean_email(self):
        self.assertEqual(clean_email("Info@Acme.com; other@acme.com"), "info@acme.com")
        self.assertEqual(clean_email("mailto:hi@acme.com"), "hi@acme.com")
        self.assertEqual(clean_email("not an email"), "")
        self.assertEqual(clean_email(None), "")


if __name__ == "__main__":
    unittest.main()
