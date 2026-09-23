import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import lead_scraper
from lead_scraper import STATE_GROUPS, STATES, clean_email, element_to_row, is_complete, missing_fields, load_existing_phones, normalize_phone, run


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

    def test_known_brands_are_kept_even_without_a_keyword(self):
        # These are real national/regional haulers whose branch listings are often just the brand
        # name -- e.g. "Rumpke" has none of NAME_KEYWORDS in it, so without an explicit allowlist
        # they'd be silently invisible even though they're exactly who this tool should find.
        for name in ("Rumpke", "Recology", "Republic Services", "Burrtec", "Athens Services", "GFL Environmental"):
            tags = {"name": name, "phone": "303-343-7096"}
            self.assertIsNotNone(element_to_row({"type": "node", "id": 1, "tags": tags}, "CO", "x"), name)

    def test_moving_companies_and_pet_waste_scoopers_are_excluded(self):
        for name in ("Acme Movers & Waste Hauling", "XYZ Moving & Trash Removal", "Speedy Relocation Waste Services",
                     "Scoopy Doo Pet Waste Removal", "Doody Duty Dog Waste Service", "Pooper Scooper Waste Co"):
            tags = {"name": name, "phone": "303-343-7096"}
            self.assertIsNone(element_to_row({"type": "node", "id": 1, "tags": tags}, "CO", "x"), name)

    def test_tire_and_textile_recyclers_are_excluded(self):
        # Caught live on a real scrape: both matched on "waste" but are tire/rag recyclers, not
        # residential curbside haulers.
        for name in ("American Waste & Textile, LLC", "Paracha Brothers-Tire Waste Management"):
            tags = {"name": name, "phone": "303-343-7096"}
            self.assertIsNone(element_to_row({"type": "node", "id": 1, "tags": tags}, "CO", "x"), name)

    def test_thrift_and_antique_stores_using_trash_in_the_name_are_excluded(self):
        for name in ("Trash & Treasures", "Trash to Treasure Antiques", "Vintage Trash Consignment"):
            tags = {"name": name, "phone": "303-343-7096"}
            self.assertIsNone(element_to_row({"type": "node", "id": 1, "tags": tags}, "CO", "x"), name)

    def test_municipal_solid_waste_divisions_are_excluded(self):
        for name in ("Jefferson County Solid Waste Division", "Metro Solid Waste Bureau", "City Sanitation Commission"):
            tags = {"name": name, "phone": "303-343-7096"}
            self.assertIsNone(element_to_row({"type": "node", "id": 1, "tags": tags}, "CO", "x"), name)


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


class RunResilienceTests(unittest.TestCase):
    """Hit live: a failure past the network fetch (in this case, the backup sync step) crashed the
    whole run instead of just skipping that one state, because only fetch_elements() was wrapped in
    a try/except. The whole per-state body is wrapped now -- this proves a state that raises partway
    through doesn't stop the next one from being processed."""

    def test_a_failure_after_a_successful_fetch_does_not_abort_the_run(self):
        elements = {
            "CO": [{"type": "node", "id": 1, "tags": {"name": "Acme Waste", "phone": "303-343-7096"}}],
            "WY": [{"type": "node", "id": 2, "tags": {"name": "Rocky Mountain Waste", "phone": "307-555-0100"}}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "leads.csv"
            with patch("lead_scraper.fetch_elements", side_effect=lambda state: elements[state]), \
                 patch.object(lead_scraper.sync_leads, "restore"), \
                 patch.object(lead_scraper.sync_leads, "sync", side_effect=[RuntimeError("simulated backup failure"), None]), \
                 patch("lead_scraper.time.sleep"):
                run(output_path, ["CO", "WY"])  # must not raise, even though CO's backup sync blows up
            with output_path.open() as f:
                rows = list(csv.DictReader(f))
        self.assertEqual({r["company_name"] for r in rows}, {"Acme Waste", "Rocky Mountain Waste"})


if __name__ == "__main__":
    unittest.main()
