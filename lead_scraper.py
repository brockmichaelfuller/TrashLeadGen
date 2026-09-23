"""Collect waste/garbage/dumpster company names and phone numbers from OpenStreetMap.

Uses the free Overpass API (no key needed). One run covers the whole U.S.: the free
Overpass server can't answer a single nationwide query (it times out), so the search is
split into 50 states + DC internally. Businesses that publish a phone number are appended
to a single CSV, deduped by phone number.
"""
import argparse
import csv
import re
import sys
import time
import traceback
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import socket
import urllib3.util.connection as urllib3_connection

import sync_leads

# Force IPv4: Render's containers (and some other small hosts) have no outbound IPv6 route, but
# these Overpass mirrors publish IPv6 addresses too. Left to its own devices, Python sometimes tries
# the IPv6 address, fails with "Network is unreachable", and gives up instead of falling back to
# IPv4 -- forcing AF_INET here skips straight to an address that's actually reachable.
urllib3_connection.allowed_gai_family = lambda: socket.AF_INET

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.fr/api/interpreter",
]
USER_AGENT = "TrashLeadGen/0.1 (+https://github.com/brockmichaelfuller/TrashLeadGen)"
REQUEST_DELAY_SECONDS = 5
MAX_ATTEMPTS = 4  # cycles through all 3 mirrors at least once, then retries the first again
QUERY_TIMEOUT_SECONDS = 300

COLUMNS = ["company_name", "phone", "email", "website", "address", "city", "state", "timezone", "source",
           "date_collected", "status", "notes"]

NAME_KEYWORDS = ["waste", "garbage", "trash", "refuse", "disposal", "rubbish"]
NAME_REGEX = "|".join(NAME_KEYWORDS)
# The name must contain one of these as a whole word (plurals allowed), so "Wasted Ink" or "Unwaste"
# don't match. The point isn't the word "waste" specifically -- it's that the business reads as a
# residential trash hauler by name; EXCLUDE_NAME below is what actually narrows that down.
# "Sanitation" alone deliberately isn't in this list: found live that plenty of real businesses
# using just that word are portable-toilet/porta-potty rental companies, not trash haulers, and
# nothing in the name itself distinguishes the two.
KEYWORD_NAME = re.compile(rf"\b({NAME_REGEX})s?\b", re.I)
# Haulers whose listings are just the brand name, with none of NAME_KEYWORDS in it (e.g. "Rumpke",
# "Republic Services") -- without this, they're invisible to both the Overpass query and
# KEYWORD_NAME. Mostly major national/regional names, but also specific smaller haulers confirmed
# by hand (e.g. "Curbie Sanitation" -- real residential curbside service, verified via their own
# site, that "sanitation" alone no longer catches since that word alone is too often a porta-potty
# company). Not exhaustive; extend as gaps are found.
KNOWN_BRANDS = [
    "Republic Services", "Waste Management", "Waste Connections", "Rumpke", "Recology", "Burrtec",
    "Athens Services", "GFL Environmental", "Casella Waste", "Waste Pro", "WCA Waste",
    "Advanced Disposal", "County Waste", "Curbie Sanitation",
]
BRAND_REGEX = "|".join(re.escape(b) for b in KNOWN_BRANDS)
BRAND_NAME = re.compile(BRAND_REGEX, re.I)
# Keep only private garbage/waste pickup companies: drop utilities, medical/hazardous waste, marine and
# portable sanitation, landfills/transfer stations, scrap and recycling yards, equipment sellers, and
# public agencies. Used by both scrapers and by clean_existing().
# "Residential curb pickup" means normal weekly garbage-truck service, not: dumpster rental /
# roll-off / junk hauling / bulk-item pickup, construction & demolition debris, corporate offices /
# sustainability campuses, retail stores selling zero-waste products, moving companies, or
# pet-waste scoopers.
EXCLUDE_NAME = re.compile(
    r"water|sewer|sewage|septic|medical|biohazard|hazardous|hazmat|marine|boat|\bsupply\b|supplies|equipment|"
    r"pest|plumb|landfill|transfer station|waste transfer|material recovery|"
    r"recycling (center|facility|depot)|scrap|salvage|metal|mattress|"
    r"e-?waste|electronic|shred|portable|porta[- ]?(potty|john)|toilet|restroom|cleaning|janitor|"
    r"\b(city|town|village|county|township|state) of\b|\b(department|dept|division|bureau|commission|agency|"
    r"authority|public works|municipal|school|hospital|clinic|dental|veterinary)\b|\bfacility\b|"
    r"drop[- ]?off|collection center|convenience center|"
    r"dumpster|roll[- ]?off|\bjunk\b|\bbulk\b|construction|demolition|debris|industrial|"
    r"campus|sustainability|headquarters|corporate office|\bstore\b|\bshop\b|\bmarket\b|"
    r"treasures|antique|consignment|thrift|vintage|"
    r"\bmov(ing|ers)\b|relocation|"
    r"pet waste|dog waste|pooper|\bpoop\b|\bscoop|"
    r"\btires?\b|textile|"
    r"\bcafe\b|\bcoffee\b|\broastery\b|\brestaurant\b|\bbakery\b",
    re.I,
)
EXCLUDE_MAN_MADE = {"wastewater_plant", "water_works", "water_tower", "storage_tank", "pumping_station"}
STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS",
    "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC",
    "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
]
# Split into 4 chunks of ~13 states so a run finishes well within Render's free-plan idle window,
# instead of one long run that dies if the tab loses focus partway through the country.
STATE_GROUPS = [STATES[i:i + 13] for i in range(0, len(STATES), 13)]
PHONE_KEYS = ("phone", "contact:phone")
REQUIRED_FIELDS = ("company_name", "phone", "email", "timezone")  # a lead with all four is "complete"
EMAIL_RE = re.compile(r"^[^@\s;,]+@[^@\s;,]+\.[A-Za-z]{2,}$")


def clean_email(raw):
    """First valid email in the value (OSM may list several separated by ';'), lowercased, or ''."""
    for candidate in re.split(r"[;,\s]+", raw or ""):
        candidate = candidate.strip().lower().removeprefix("mailto:")
        if EMAIL_RE.match(candidate):
            return candidate
    return ""


def missing_fields(row):
    """Which of name/phone/email/timezone this lead lacks (empty list means complete)."""
    return [f for f in REQUIRED_FIELDS if not (row.get(f) or "").strip()]


def is_complete(row):
    return not missing_fields(row)

# Fallback when a lead has no coordinates: the state's main timezone (split states use where most people live).
STATE_TIMEZONES = {
    **dict.fromkeys("CT DE DC FL GA IN KY ME MD MA MI NH NJ NY NC OH PA RI SC VT VA WV".split(), "Eastern"),
    **dict.fromkeys("AL AR IL IA KS LA MN MS MO NE ND OK SD TN TX WI".split(), "Central"),
    **dict.fromkeys("AZ CO ID MT NM UT WY".split(), "Mountain"),
    **dict.fromkeys("CA NV OR WA".split(), "Pacific"),
    "AK": "Alaska", "HI": "Hawaii",
}
STANDARD_OFFSET_LABELS = {-5: "Eastern", -6: "Central", -7: "Mountain", -8: "Pacific", -9: "Alaska", -10: "Hawaii"}
_finder = None


def timezone_label(state, lat=None, lon=None):
    """Timezone name like 'Central'. Uses coordinates when given (handles split states), else the state."""
    global _finder
    if lat is not None and lon is not None:
        try:
            if _finder is None:
                from timezonefinder import TimezoneFinder
                _finder = TimezoneFinder()
            zone = _finder.timezone_at(lat=lat, lng=lon)
            if zone:
                offset = datetime(2026, 1, 15, tzinfo=ZoneInfo(zone)).utcoffset().total_seconds() / 3600
                if offset in STANDARD_OFFSET_LABELS:  # standard-time offset, so Arizona counts as Mountain
                    return STANDARD_OFFSET_LABELS[offset]
        except Exception:
            pass  # fall back to the state below
    return STATE_TIMEZONES.get((state or "").upper(), "")


def build_query(state_code):
    # Filtering on the indexed "phone" key first keeps this fast; scanning names alone or
    # adding "contact:phone" made Overpass time out. Phones are re-validated in Python.
    # The name filter also needs to include KNOWN_BRANDS, or a branch listed under just its brand
    # name (e.g. "Rumpke", with no generic keyword) never gets fetched from Overpass at all.
    query_name_regex = f"{NAME_REGEX}|{BRAND_REGEX}"
    return (
        f"[out:json][timeout:{QUERY_TIMEOUT_SECONDS}];"
        f'area["ISO3166-2"="US-{state_code}"]->.state;'
        f'nwr(area.state)["phone"]["name"~"{query_name_regex}",i];out center tags;'
    )


def normalize_phone(raw):
    """Return a US phone as '(555) 123-4567', or None if it isn't a valid 10-digit number."""
    for candidate in re.split(r"[;,/]", raw or ""):
        digits = re.sub(r"\D", "", candidate)
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) == 10 and digits[0] in "23456789":
            return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return None


def element_to_row(element, state, today):
    tags = element.get("tags", {})
    name = (tags.get("name") or "").strip()
    is_named_match = KEYWORD_NAME.search(name) or BRAND_NAME.search(name)
    if not name or not is_named_match or EXCLUDE_NAME.search(name) or tags.get("man_made") in EXCLUDE_MAN_MADE:
        return None
    website = (tags.get("website") or tags.get("contact:website") or "").strip()
    if re.search(r"\.gov(/|$)", website, re.I):  # a government site regardless of what the name says
        return None
    phone = next((normalize_phone(tags[k]) for k in PHONE_KEYS if k in tags), None)
    if not phone:
        return None
    point = element if "lat" in element else element.get("center", {})
    row = {
        "company_name": name,
        "phone": phone,
        "email": clean_email(tags.get("email") or tags.get("contact:email")),
        "website": website,
        "address": " ".join(filter(None, [tags.get("addr:housenumber"), tags.get("addr:street")])),
        "city": (tags.get("addr:city") or "").strip(),
        "state": state,
        "timezone": timezone_label(state, point.get("lat"), point.get("lon")),
        "source": f"openstreetmap:{element['type']}/{element['id']}",
        "date_collected": today,
    }
    return row


# Phrases that describe the business's actual service, found on a live scrape: several companies
# passed the name-based filters (nothing in "Horizon Disposal Services" or "Precision Disposal"
# says "dumpster") but turned out, on their own website, to be dumpster rental, junk removal, or
# portable-toilet/septic companies -- not a normal weekly residential trash route.
DISQUALIFYING_SERVICE_PHRASES = [
    "dumpster rental", "roll-off rental", "roll off rental", "roll-off dumpster", "roll off dumpster",
    "junk removal", "junk hauling", "property cleanout", "home cleanout", "estate cleanout",
    "portable toilet", "porta potty rental", "porta-potty rental", "septic pumping", "septic tank",
    "construction debris removal", "demolition debris",
]
# If a site also uses ordinary residential-service language, it's kept regardless of the above --
# plenty of real haulers mention roll-off/bulk/commercial service alongside their core weekly route
# (e.g. RAM Waste Systems, Waste Pro), and that combination shouldn't cost them the lead.
QUALIFYING_SERVICE_PHRASES = [
    "weekly curbside", "curbside pickup", "curbside collection", "curbside service",
    "residential pickup", "residential trash", "residential garbage", "residential service",
    "weekly collection", "weekly trash pickup", "weekly pick-up", "weekly pickup", "trash pickup",
]
WEBSITE_CHECK_TIMEOUT_SECONDS = 10
WEBSITE_CHECK_MAX_CHARS = 300_000  # plenty for a marketing homepage; keeps a huge page from stalling the regex


def website_offers_residential_pickup(website):
    """Best-effort, free check of a candidate's own website for whether it actually offers normal
    weekly residential pickup, catching the class of false positive that a name alone won't reveal.
    Deliberately fails open: no website, an unreachable site, or ambiguous wording all keep the lead
    rather than drop it -- this is one more signal on top of the name filters, not a replacement for
    reviewing the list, and a network hiccup here should never cost a real lead."""
    if not website:
        return True
    try:
        response = requests.get(website, timeout=WEBSITE_CHECK_TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT})
        text = re.sub(r"<[^>]+>", " ", response.text[:WEBSITE_CHECK_MAX_CHARS]).lower()
    except requests.RequestException:
        return True
    if any(phrase in text for phrase in QUALIFYING_SERVICE_PHRASES):
        return True
    return not any(phrase in text for phrase in DISQUALIFYING_SERVICE_PHRASES)


def fetch_elements(state_code):
    query = build_query(state_code)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    errors = []  # one per attempt, so a failure shows what every mirror actually did, not just the last
    for attempt in range(MAX_ATTEMPTS):
        url = OVERPASS_URLS[attempt % len(OVERPASS_URLS)]
        try:
            response = requests.post(url, data={"data": query}, headers=headers, timeout=QUERY_TIMEOUT_SECONDS + 30)
            response.raise_for_status()
            data = response.json()
            if "runtime error" in data.get("remark", ""):  # Overpass reports timeouts with HTTP 200
                raise RuntimeError(data["remark"])
            return data.get("elements", [])
        except (requests.RequestException, ValueError, RuntimeError) as error:
            errors.append(f"{url}: {type(error).__name__}: {error}")
            time.sleep(REQUEST_DELAY_SECONDS * (attempt + 1))
    raise RuntimeError(f"Overpass failed after {MAX_ATTEMPTS} attempts:\n" + "\n".join(errors))


def load_existing_phones(path):
    """Phones already saved in the CSV, normalized so old rows in other formats still match."""
    if not path.exists():
        return set()
    with path.open(newline="", encoding="utf-8") as f:
        return {normalize_phone(row.get("phone")) or row.get("phone") for row in csv.DictReader(f)}


def ensure_columns(path):
    """Upgrade an older CSV in place so its header matches COLUMNS (missing fields left blank)."""
    if not path.exists() or path.stat().st_size == 0:
        return
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames == COLUMNS:
            return
        rows = list(reader)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore", restval="")
        writer.writeheader()
        writer.writerows(rows)


def _friendly_error(error):
    """A short, non-technical description of why a state's request failed, for the UI log --
    nobody using the web page needs to see a Python traceback or a raw HTTP error."""
    text = str(error).lower()
    if "timeout" in text or "timed out" in text:
        return "the map data source took too long to respond"
    if "runtime error" in text:
        return "the map data source was too busy to finish this search"
    if any(s in text for s in ("network is unreachable", "connection refused", "newconnectionerror", "connectionerror")):
        return "couldn't connect to the map data source"
    return "the map data source had a temporary problem"


def log_debug_detail(output_path, state, error):
    """Append the real exception (with traceback) to debug.log, next to the CSV. The UI's log only
    ever shows _friendly_error()'s plain-English version; this is the raw detail for actually
    diagnosing a recurring failure, fetched separately via GET /api/debug.log."""
    debug_path = output_path.parent / "debug.log"
    try:
        with debug_path.open("a", encoding="utf-8") as f:
            f.write(f"\n--- {datetime.now().isoformat(timespec='seconds')} {state} ---\n")
            f.write(f"{type(error).__name__}: {error}\n")
            traceback.print_exc(file=f)
    except OSError:
        pass  # debug logging must never itself break a run


# A state that still fails after MAX_ATTEMPTS mirror cycles usually isn't broken -- Overpass is a
# free, best-effort public service and individual mirrors have brief outages/rate limits that
# typically clear within a minute or two. Automatically give the whole batch of failures one more
# pass after a cooldown, instead of leaving that to a person clicking Retry every time.
RETRY_ROUNDS = 2  # the initial pass, plus this many additional automatic passes over failures
RETRY_ROUND_DELAY_SECONDS = 30


def _attempt_state(state, writer, out, output_path, seen, today):
    """Fetch, write, and back up one state. Returns (new_count, error); error is None on success.
    Everything is inside one try, so a failure anywhere in it -- not just the network call -- can
    never kill the whole run; worst case this one state comes back as a failure to retry."""
    try:
        seen.update(load_existing_phones(output_path))  # pick up rows another scraper added meanwhile
        elements = fetch_elements(state)
        new = 0
        for element in elements:
            row = element_to_row(element, state, today)
            if row and row["phone"] not in seen and website_offers_residential_pickup(row["website"]):
                seen.add(row["phone"])
                writer.writerow(row)
                new += 1
        out.flush()  # keep progress if the run is interrupted later
        sync_leads.sync(output_path)  # and back it up, since a restart wipes the local disk
        return new, None
    except Exception as error:  # one bad state must not stop the run
        return 0, error


def run(output_path, states):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sync_leads.restore(output_path)  # recover prior runs' data on a fresh (empty) host
    ensure_columns(output_path)
    seen = load_existing_phones(output_path)
    write_header = not output_path.exists() or output_path.stat().st_size == 0
    today = date.today().isoformat()
    total_new = 0

    with output_path.open("a", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=COLUMNS)
        if write_header:
            writer.writeheader()

        remaining = list(states)
        for round_num in range(1, RETRY_ROUNDS + 1):
            still_failing = []
            for i, state in enumerate(remaining):
                new, error = _attempt_state(state, writer, out, output_path, seen, today)
                if error is None:
                    total_new += new
                    label = f"[{i + 1}/{len(remaining)}]" if round_num == 1 else "retry succeeded:"
                    print(f"{label} {state}: {new} new companies")
                else:
                    message = _friendly_error(error)
                    label = f"[{i + 1}/{len(remaining)}]" if round_num == 1 else "still failing after retry:"
                    print(f"{label} {state}: skipped -- {message}", file=sys.stderr)
                    log_debug_detail(output_path, state, error)  # the real exception, for /api/debug.log
                    still_failing.append(state)                  # -- never shown in the user-facing log
                time.sleep(REQUEST_DELAY_SECONDS)
            remaining = still_failing
            if not remaining or round_num == RETRY_ROUNDS:
                break
            print(f"{len(remaining)} state(s) had a temporary problem -- retrying automatically "
                  f"in {RETRY_ROUND_DELAY_SECONDS}s: {', '.join(remaining)}")
            time.sleep(RETRY_ROUND_DELAY_SECONDS)

    print(f"Done. {total_new} new rows -> {output_path} ({len(seen)} total unique phones)")
    if remaining:
        print(f"Failed states (rerun to retry): {', '.join(remaining)}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("output/leads.csv"))
    parser.add_argument("--states", nargs="+", default=STATES, metavar="ST",
                        help="limit the run to these state codes, e.g. --states CO WA")
    args = parser.parse_args()
    run(args.output, [state.upper() for state in args.states])


if __name__ == "__main__":
    main()
