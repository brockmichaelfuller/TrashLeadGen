import unittest

from lead_scraper import STATES, element_to_row, normalize_phone


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
        row = element_to_row(self.element(name="Acme Waste", phone="303-343-7096", **{"addr:city": "Denver"}), "CO", "2026-01-01")
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

    def test_keyword_must_be_a_whole_word(self):
        for name in ("Wasted Ink Zine Distro", "The Unwaste Shop"):
            tags = {"name": name, "phone": "303-343-7096"}
            self.assertIsNone(element_to_row({"type": "node", "id": 1, "tags": tags}, "CO", "x"))
        tags = {"name": "Acme Waste Services", "phone": "303-343-7096"}
        self.assertIsNotNone(element_to_row({"type": "node", "id": 1, "tags": tags}, "CO", "x"))

    def test_name_must_contain_waste(self):
        for name in ("Acme Dumpsters", "Southwest Sanitation", "Toyland Hauling"):
            tags = {"name": name, "phone": "303-343-7096"}
            self.assertIsNone(element_to_row({"type": "node", "id": 1, "tags": tags}, "CO", "x"))

    def test_only_garbage_pickup_companies_are_kept(self):
        for name in ("Colorado Medical Waste", "Marine Sanitation & Supply", "Sunset Landfill Waste",
                     "City of Dallas Sanitation Department", "Acme Portable Toilet Waste"):
            tags = {"name": name, "phone": "303-343-7096"}
            self.assertIsNone(element_to_row({"type": "node", "id": 1, "tags": tags}, "CO", "x"), name)
        for name in ("ABC Waste Services", "Metro Roll-Off Waste Hauling", "Tri-County Waste Disposal"):
            tags = {"name": name, "phone": "303-343-7096"}
            self.assertIsNotNone(element_to_row({"type": "node", "id": 1, "tags": tags}, "CO", "x"), name)


if __name__ == "__main__":
    unittest.main()
