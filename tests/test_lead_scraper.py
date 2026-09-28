import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import db
import lead_scraper
from lead_scraper import (STATE_GROUPS, STATES, UNREACHABLE_ERROR_MESSAGE, _friendly_error, clean_email,
                           element_to_row, is_complete, is_rejected, missing_fields, normalize_phone, run,
                           website_offers_residential_pickup)


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
        # The word doesn't have to be "waste" specifically -- garbage/trash/refuse/disposal/rubbish
        # all count, since the point is "residential trash hauler," not the word.
        for name in ("Downtown Trash Removal", "Acme Garbage Co", "City Refuse Pickup",
                     "Acme Disposal Services", "Olde Towne Rubbish Co"):
            tags = {"name": name, "phone": "303-343-7096"}
            self.assertIsNotNone(element_to_row({"type": "node", "id": 1, "tags": tags}, "CO", "x"), name)

    def test_sanitation_alone_does_not_qualify(self):
        # Found live: plenty of real "___ Sanitation" businesses are portable-toilet/porta-potty
        # rental companies, not residential trash haulers, with nothing in the name to tell them
        # apart. So unlike waste/garbage/trash/refuse/disposal/rubbish, "sanitation" alone isn't
        # enough -- it has to also carry one of those other words, or match a KNOWN_BRANDS entry.
        for name in ("Southwest Sanitation", "Acme Sanitation Co", "Downtown Sanitation Services"):
            tags = {"name": name, "phone": "303-343-7096"}
            self.assertIsNone(element_to_row({"type": "node", "id": 1, "tags": tags}, "CO", "x"), name)

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
        for name in ("Rumpke", "Recology", "Republic Services", "Burrtec", "Athens Services", "GFL Environmental",
                     "Curbie Sanitation"):
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
        for name in ("Jefferson County Solid Waste Division", "Metro Solid Waste Bureau", "City Sanitation Commission",
                     # Caught live: county-run programs that end right at "Solid Waste" -- no "of",
                     # "department", "division" etc. to catch them, just the bare bureaucratic phrase.
                     "Hertford County Solid Waste", "Avery County Solid Waste MRS"):
            tags = {"name": name, "phone": "303-343-7096"}
            self.assertIsNone(element_to_row({"type": "node", "id": 1, "tags": tags}, "CO", "x"), name)

    def test_bulk_trash_pickup_is_excluded(self):
        # The site is for normal weekly residential service, not one-off bulk/large-item pickup.
        for name in ("Acme Bulk Trash Pickup", "Metro Bulk Waste Removal", "City Bulk Item Collection"):
            tags = {"name": name, "phone": "303-343-7096"}
            self.assertIsNone(element_to_row({"type": "node", "id": 1, "tags": tags}, "CO", "x"), name)

    def test_cafes_and_transfer_or_recovery_facilities_are_excluded(self):
        # All caught live: a "No Waste" cafe/roastery, and a public materials-recovery/transfer
        # facility whose OSM name didn't happen to say "facility" or "transfer station".
        for name in ("No Waste Cafe & Roastery", "Downtown Waste Coffee Roasters",
                     "Metro Solid Waste Transfer", "City Material Recovery & Solid Waste",
                     "Neighborhood Recycling Convenience Center"):
            tags = {"name": name, "phone": "303-343-7096"}
            self.assertIsNone(element_to_row({"type": "node", "id": 1, "tags": tags}, "CO", "x"), name)

    def test_non_hauler_facilities_and_businesses_caught_in_audit_are_excluded(self):
        # All caught live in a lead-quality audit: transfer facilities whose names drop "station",
        # public yard-waste drop sites, a recycle depot, and businesses that merely use a hauling
        # word (paper/metal recyclers, a waste-to-energy vendor, a fleet liquidator, a K-9 poop
        # service, and shops selling clothing, furniture or needlepoint).
        for name in ("Rumpke Circleville Transfer", "Waste Management LaPorte Transfer",
                     "Republic Services Akron Transfer & Recycling", "Midway Yard Waste Site",
                     "Republic Services Corvallis Recycle Depot", "Hub City Waste Paper, LLC",
                     "Central Waste Material Co", "Waste To Energy Systems LLC",
                     "Fleet Vehicle Disposal & Commercial Liquidations", "Monarch K-9 Waste Removal",
                     "Zero Waste Daniel", "Waste Knot Needlepoint", "White Trash Furnishings",
                     "Trash Clothing Co"):
            tags = {"name": name, "phone": "303-343-7096"}
            self.assertIsNone(element_to_row({"type": "node", "id": 1, "tags": tags}, "CO", "x"), name)

    def test_a_dot_gov_website_is_excluded_regardless_of_name(self):
        # Caught live: "Fayetteville Recycling & Trash Collection" (fayetteville-ar.gov) and "McKay
        # Bay Scale House Waste Disposal" (tampa.gov) -- both municipal facilities whose plain name
        # gave no indication they were government-run.
        tags = {"name": "Acme Waste Collection", "phone": "303-343-7096", "website": "https://www.fayetteville-ar.gov/531/Recycling-Trash-Service"}
        self.assertIsNone(element_to_row({"type": "node", "id": 1, "tags": tags}, "CO", "x"))
        tags = {"name": "Acme Waste Services", "phone": "303-343-7096", "website": "https://acmewaste.com"}
        self.assertIsNotNone(element_to_row({"type": "node", "id": 1, "tags": tags}, "CO", "x"))

    def test_a_state_locality_dot_us_website_is_excluded_like_dot_gov(self):
        # Caught live in the audit: "Dennis Town Disposal Area" (town.dennis.ma.us), a town-run
        # facility on the .us locality namespace many towns use instead of .gov. A company's own
        # plain .us domain is still fine.
        for website in ("https://www.town.dennis.ma.us/343/Solid-Waste-Recycling-Division", "http://ci.fresno.ca.us"):
            tags = {"name": "Acme Waste Collection", "phone": "303-343-7096", "website": website}
            self.assertIsNone(element_to_row({"type": "node", "id": 1, "tags": tags}, "CO", "x"), website)
        tags = {"name": "Dennis Town Disposal Area", "phone": "303-343-7096"}
        self.assertIsNone(element_to_row({"type": "node", "id": 1, "tags": tags}, "CO", "x"))
        tags = {"name": "Acme Waste Services", "phone": "303-343-7096", "website": "https://acmewaste.us"}
        self.assertIsNotNone(element_to_row({"type": "node", "id": 1, "tags": tags}, "CO", "x"))


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

    def test_is_rejected(self):
        self.assertFalse(is_rejected({"rejected_at": ""}))
        self.assertFalse(is_rejected({}))
        self.assertTrue(is_rejected({"rejected_at": "2026-09-24"}))

    def test_clean_email(self):
        self.assertEqual(clean_email("Info@Acme.com; other@acme.com"), "info@acme.com")
        self.assertEqual(clean_email("mailto:hi@acme.com"), "hi@acme.com")
        self.assertEqual(clean_email("not an email"), "")
        self.assertEqual(clean_email(None), "")


class FriendlyErrorTests(unittest.TestCase):
    def test_connection_refused_is_unreachable(self):
        self.assertEqual(_friendly_error(ConnectionError("Connection refused")), UNREACHABLE_ERROR_MESSAGE)

    def test_timeout_is_distinguished_from_unreachable(self):
        self.assertEqual(_friendly_error(TimeoutError("timed out")), "the map data source took too long to respond")

    def test_a_403_reads_as_blocked_not_a_generic_temporary_problem(self):
        # Caught in review: a permanent block (e.g. a proxy/WAF) was described the same as any other
        # unclassified error ("a temporary problem"), which tells the user to just try again when
        # that's unlikely to help.
        error = Exception("403 Client Error: Forbidden for url: https://overpass-api.de/api/interpreter")
        self.assertEqual(_friendly_error(error), "was blocked from reaching the map data source")

    def test_a_429_reads_as_rate_limited(self):
        error = Exception("429 Client Error: Too Many Requests")
        self.assertEqual(_friendly_error(error), "the map data source is rate-limiting requests right now")

    def test_unrecognized_errors_fall_back_to_the_generic_message(self):
        self.assertEqual(_friendly_error(Exception("something odd")), "the map data source had a temporary problem")


class RunResilienceTests(unittest.TestCase):
    """Hit live: a failure past the network fetch (in this case, the backup sync step) crashed the
    whole run instead of just skipping that one state, because only fetch_elements() was wrapped in
    a try/except. The whole per-state body is wrapped now -- this proves a state that raises partway
    through doesn't stop the next one from being processed.

    sync_leads is imported by db.py (which lead_scraper.py's run()/_attempt_state now delegate backup
    to), not by lead_scraper.py itself -- so these patch db.sync_leads, not lead_scraper.sync_leads."""

    def test_a_failure_after_a_successful_fetch_does_not_abort_the_run(self):
        elements = {
            "CO": [{"type": "node", "id": 1, "tags": {"name": "Acme Waste", "phone": "303-343-7096"}}],
            "WY": [{"type": "node", "id": 2, "tags": {"name": "Rocky Mountain Waste", "phone": "307-555-0100"}}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            # CO's backup sync blows up on round 1, then succeeds on the automatic retry round; WY
            # is fine throughout. insert_if_new already landed the row before sync_backup is called,
            # so a sync failure doesn't lose the row -- it just gets logged as this state failing.
            with patch("lead_scraper.fetch_elements", side_effect=lambda state: elements[state]), \
                 patch.object(db.sync_leads, "restore"), \
                 patch.object(db.sync_leads, "sync",
                               side_effect=[RuntimeError("simulated backup failure"), None, None]), \
                 patch("lead_scraper.time.sleep"):
                run(db_path, ["CO", "WY"])  # must not raise
            rows = db.all_leads(db_path)
        self.assertEqual({r["company_name"] for r in rows}, {"Acme Waste", "Rocky Mountain Waste"})

    def test_a_state_that_fails_every_round_ends_up_in_the_final_failed_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            with patch("lead_scraper.fetch_elements", side_effect=RuntimeError("still down")), \
                 patch.object(db.sync_leads, "restore"), \
                 patch.object(db.sync_leads, "sync"), \
                 patch("lead_scraper.time.sleep"), \
                 patch("builtins.print") as mock_print:
                run(db_path, ["CO"])
        summary_lines = [c.args[0] for c in mock_print.call_args_list if c.args]
        self.assertTrue(any("Failed states (rerun to retry): CO" in line for line in summary_lines), summary_lines)

    def test_aborts_early_after_consecutive_states_cannot_connect_at_all(self):
        # Reproduced live: with the data source fully unreachable, a 13-state run spent ~25 minutes
        # grinding through every state one at a time to report the same "can't connect" outcome 13
        # times. A handful of connection-refused states in a row means the service is unreachable
        # from here entirely, not just having a rough moment for one state -- stop early instead.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            with patch("lead_scraper.fetch_elements", side_effect=ConnectionError("Connection refused")), \
                 patch.object(db.sync_leads, "restore"), \
                 patch.object(db.sync_leads, "sync"), \
                 patch("lead_scraper.time.sleep"), \
                 patch("builtins.print") as mock_print:
                run(db_path, ["AL", "AK", "AZ", "AR"])
        printed = [c.args[0] for c in mock_print.call_args_list if c.args]
        state_re = re.compile(r"^(?:\[\d+/\d+\]|still failing after retry:) (\S+): skipped")
        attempted = {m.group(1) for line in printed for m in [state_re.match(line)] if m}
        self.assertEqual(attempted, {"AL", "AK"})  # AZ and AR never attempted -- aborted after 2
        self.assertTrue(any("stopping early" in line for line in printed), printed)

    def test_does_not_abort_early_when_failures_are_not_connection_level(self):
        # A busy/blocked/slow response is a different situation from "unreachable" -- these must not
        # trip the same early-abort, or a run would give up after any two ordinary failures.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            with patch("lead_scraper.fetch_elements", side_effect=TimeoutError("timed out")), \
                 patch.object(db.sync_leads, "restore"), \
                 patch.object(db.sync_leads, "sync"), \
                 patch("lead_scraper.time.sleep"), \
                 patch("builtins.print") as mock_print:
                run(db_path, ["AL", "AK", "AZ", "AR"])
        printed = [c.args[0] for c in mock_print.call_args_list if c.args]
        state_re = re.compile(r"^(?:\[\d+/\d+\]|still failing after retry:) (\S+): skipped")
        attempted = {m.group(1) for line in printed for m in [state_re.match(line)] if m}
        self.assertEqual(attempted, {"AL", "AK", "AZ", "AR"})  # all four still get attempted

    def test_a_failure_in_the_logging_around_attempt_state_does_not_abort_the_run(self):
        # Hit live: the per-state loop's own print()/log_debug_detail()/sleep() calls sat outside
        # any try/except (only _attempt_state's internals were covered), so a failure there crashed
        # the whole run after only 2 of 13 states with no explanation. This reproduces that failure
        # mode directly -- log_debug_detail raising for AL -- and proves AK still gets processed.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "leads.db"
            calls = []

            def flaky_log_debug_detail(*args):
                calls.append(args)
                if len(calls) == 1:
                    raise OSError("disk hiccup")

            with patch("lead_scraper.fetch_elements", side_effect=RuntimeError("boom")), \
                 patch("lead_scraper.log_debug_detail", side_effect=flaky_log_debug_detail), \
                 patch.object(db.sync_leads, "restore"), \
                 patch.object(db.sync_leads, "sync"), \
                 patch("lead_scraper.time.sleep"), \
                 patch("builtins.print") as mock_print:
                run(db_path, ["AL", "AK"])  # must not raise despite AL's logging call blowing up
        printed_states = {c.args[0].split(":")[0].split()[-1] for c in mock_print.call_args_list
                          if c.args and "skipped" in c.args[0]}
        self.assertIn("AK", printed_states)  # AK was still reached after AL's crash


def _mock_html_response(html):
    """A requests.Response stand-in for the streamed, chunked read website_offers_residential_pickup
    now does, instead of the simpler (but fully-buffering) .text property."""
    body = html.encode()
    return MagicMock(encoding="utf-8", iter_content=lambda chunk_size: [body])


class WebsiteResidentialPickupCheckTests(unittest.TestCase):
    def test_no_website_is_kept_unverified(self):
        self.assertTrue(website_offers_residential_pickup(""))

    def test_unreachable_site_fails_open(self):
        with patch("lead_scraper.requests.get", side_effect=lead_scraper.requests.RequestException("timeout")):
            self.assertTrue(website_offers_residential_pickup("https://example.com"))

    def test_a_scheme_less_url_is_actually_checked_not_silently_skipped(self):
        # requests raises MissingSchema for a bare "example.com" with no http(s):// -- caught by the
        # same broad except as a real network failure, so these silently never got checked at all.
        html = "<html><body><h1>Roll-Off Dumpster Rental</h1><p>Dumpster rental for construction debris.</p></body></html>"
        with patch("lead_scraper.requests.get", return_value=_mock_html_response(html)) as mock_get:
            self.assertFalse(website_offers_residential_pickup("example.com"))
        self.assertTrue(mock_get.call_args.args[0].startswith("https://"), mock_get.call_args.args[0])

    def test_pure_dumpster_rental_site_is_rejected(self):
        # Modeled on "Horizon Disposal Services": all roll-off/dumpster language, nothing about a
        # normal residential route.
        html = "<html><body><h1>Roll-Off Dumpster Rental</h1><p>Fast, affordable dumpster rental for home cleanouts and construction debris removal.</p></body></html>"
        with patch("lead_scraper.requests.get", return_value=_mock_html_response(html)):
            self.assertFalse(website_offers_residential_pickup("https://example.com"))

    def test_a_site_that_also_mentions_dumpsters_is_still_kept_if_it_offers_curbside_service(self):
        # Modeled on "RAM Waste Systems": real weekly residential pickup that also happens to
        # mention roll-off rentals as one of several services -- must not be penalized for that.
        html = "<html><body><p>Weekly curbside pickup for residential customers, plus temporary dumpster rental for projects.</p></body></html>"
        with patch("lead_scraper.requests.get", return_value=_mock_html_response(html)):
            self.assertTrue(website_offers_residential_pickup("https://example.com"))

    def test_generic_trash_pickup_wording_does_not_rescue_a_junk_removal_site(self):
        # Modeled on "Breezeway Disposal": a junk-removal company whose copy says "trash pickup"
        # in passing -- that alone must not count as a weekly residential route.
        html = "<html><body><h1>Junk Removal</h1><p>Fast junk and trash pickup, estate cleanout and property cleanout.</p></body></html>"
        with patch("lead_scraper.requests.get", return_value=_mock_html_response(html)):
            self.assertFalse(website_offers_residential_pickup("https://example.com"))

    def test_a_site_with_neither_signal_is_kept_unverified(self):
        html = "<html><body><p>Welcome to our company. Call us for a quote.</p></body></html>"
        with patch("lead_scraper.requests.get", return_value=_mock_html_response(html)):
            self.assertTrue(website_offers_residential_pickup("https://example.com"))

    def test_a_huge_page_is_truncated_instead_of_fully_buffered(self):
        # The old implementation read response.text (the whole body) before truncating; a genuinely
        # huge page should still resolve correctly (and not hang) now that it's read in chunks.
        huge = ("x " * 500_000) + "weekly curbside pickup"  # qualifying phrase past the byte cap
        with patch("lead_scraper.requests.get", return_value=_mock_html_response(huge)):
            # Kept unverified is fine here -- the point is it returns promptly, not which way it goes.
            website_offers_residential_pickup("https://example.com")


if __name__ == "__main__":
    unittest.main()
