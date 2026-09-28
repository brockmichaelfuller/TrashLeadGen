"""Collect waste/garbage/dumpster company names and phone numbers from OpenStreetMap.

Uses the free Overpass API (no key needed). One run covers the whole U.S.: the free
Overpass server can't answer a single nationwide query (it times out), so the search is
split into 50 states + DC internally. Businesses that publish a phone number are saved to
a SQLite database (db.py), deduped by phone number (the database's primary key).
"""
import argparse
import concurrent.futures
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

import db

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
    r"pest|plumb|landfill|\btransfer\b|material recovery|\bsolid waste\b|yard waste|"
    r"recycl(e|ing) (center|facility|depot)|scrap|salvage|metal|mattress|"
    r"waste paper|waste material|waste[- ]to[- ]energy|\bvehicle|liquidat|"
    r"e-?waste|electronic|shred|portable|porta[- ]?(potty|john)|toilet|restroom|cleaning|janitor|"
    r"\b(city|town|village|county|township|state) of\b|\b(department|dept|division|bureau|commission|agency|"
    r"authority|public works|municipal|school|hospital|clinic|dental|veterinary)\b|\bfacility\b|"
    r"drop[- ]?off|collection center|convenience center|"
    r"dumpster|roll[- ]?off|\bjunk\b|\bbulk\b|construction|demolition|debris|industrial|"
    r"campus|sustainability|headquarters|corporate office|\bstore\b|\bshop\b|\bmarket\b|"
    r"treasures|antique|consignment|thrift|vintage|furnishings|clothing|apparel|needlepoint|\bzero waste\b|"
    r"\bmov(ing|ers)\b|relocation|"
    r"pet waste|dog waste|\bk-?9\b|pooper|\bpoop\b|\bscoop|"
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


def is_rejected(row):
    """True once a lead has been deleted as the wrong business type (see app.py's delete_lead).
    The row is kept rather than removed so its phone stays in db.existing_phones()'s result forever,
    instead of being treated as new and re-added on a later scrape."""
    return bool((row.get("rejected_at") or "").strip())

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
# Bare "trash pickup" deliberately isn't here: found live on a junk-removal site ("Breezeway
# Disposal", "get rid of junk and trash") -- it's too generic to vouch for a weekly route.
QUALIFYING_SERVICE_PHRASES = [
    "weekly curbside", "curbside pickup", "curbside collection", "curbside service",
    "residential pickup", "residential trash", "residential garbage", "residential service",
    "weekly collection", "weekly trash pickup", "weekly pick-up", "weekly pickup",
]
WEBSITE_CHECK_TIMEOUT_SECONDS = 10
WEBSITE_CHECK_MAX_BYTES = 300_000  # plenty for a marketing homepage; keeps a huge page from stalling the regex
WEBSITE_CHECK_CONCURRENCY = 5  # candidates with a website are checked this many at a time, not one by one


def website_offers_residential_pickup(website):
    """Best-effort, free check of a candidate's own website for whether it actually offers normal
    weekly residential pickup, catching the class of false positive that a name alone won't reveal.
    Deliberately fails open: no website, an unreachable site, or ambiguous wording all keep the lead
    rather than drop it -- this is one more signal on top of the name filters, not a replacement for
    reviewing the list, and a network hiccup here should never cost a real lead."""
    if not website:
        return True
    if not re.match(r"^https?://", website, re.I):
        # A bare "example.com" (no scheme) used to raise requests' MissingSchema, which the broad
        # except below quietly turned into "kept unverified" -- so these were never actually checked.
        website = f"https://{website}"
    try:
        # Streamed and capped instead of pulling the whole response into memory first -- a large
        # page (some CMS homepages run several MB) no longer has to fully download before the first
        # WEBSITE_CHECK_MAX_BYTES are all this ever looks at.
        response = requests.get(website, timeout=WEBSITE_CHECK_TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT},
                                 stream=True)
        raw = b""
        for chunk in response.iter_content(chunk_size=8192):
            raw += chunk
            if len(raw) >= WEBSITE_CHECK_MAX_BYTES:
                break
        response.close()
        text = re.sub(r"<[^>]+>", " ", raw[:WEBSITE_CHECK_MAX_BYTES].decode(response.encoding or "utf-8", "ignore")).lower()
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


UNREACHABLE_ERROR_MESSAGE = "couldn't connect to the map data source"


def _friendly_error(error):
    """A short, non-technical description of why a state's request failed, for the UI log --
    nobody using the web page needs to see a Python traceback or a raw HTTP error."""
    text = str(error).lower()
    if "timeout" in text or "timed out" in text:
        return "the map data source took too long to respond"
    if "runtime error" in text:
        return "the map data source was too busy to finish this search"
    if any(s in text for s in ("network is unreachable", "connection refused", "newconnectionerror", "connectionerror")):
        return UNREACHABLE_ERROR_MESSAGE
    # A permanent block (e.g. a proxy/WAF) reads very differently from a busy server -- lumping it
    # in with "temporary problem" told the user to just try again, when that's unlikely to help.
    if "403" in text or "forbidden" in text:
        return "was blocked from reaching the map data source"
    if "429" in text or "too many requests" in text:
        return "the map data source is rate-limiting requests right now"
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
# typically clear within a minute or two. Automatically give the whole batch of failures more
# passes after a cooldown, instead of leaving that to a person clicking Retry every time.
#
# Hit live: a 51-state run caught overpass-api.de refusing every connection, kumi.systems returning
# 504s, and the .fr mirror throwing its own server-side data error, all three at once, for a stretch
# of over 30 minutes -- a single 30s cooldown and one extra pass wasn't enough for 4 of 51 states to
# land in a good window. Longer cooldown, one more pass.
RETRY_ROUNDS = 3  # total passes over a state before giving up on it, including the initial one
RETRY_ROUND_DELAY_SECONDS = 90

# This many states in a row failing to even connect (not just slow/busy/blocked) means the data
# source is unreachable from here entirely, not just having a rough moment -- see its use in run().
CONSECUTIVE_UNREACHABLE_ABORT_THRESHOLD = 2


def _attempt_state(state, db_path, seen, today):
    """Fetch, write, and back up one state. Returns (new_count, error); error is None on success.
    Everything is inside one try, so a failure anywhere in it -- not just the network call -- can
    never kill the whole run; worst case this one state comes back as a failure to retry."""
    try:
        seen.update(db.existing_phones(db_path))  # pick up rows another scraper added meanwhile
        elements = fetch_elements(state)
        candidates, candidate_phones = [], set()
        for element in elements:
            row = element_to_row(element, state, today)
            if row and row["phone"] not in seen and row["phone"] not in candidate_phones:
                candidate_phones.add(row["phone"])
                candidates.append(row)
        # The one network-bound check per candidate used to run one at a time -- for a state with
        # many candidates that have a website, this is what made the check "add real time to a
        # scrape" (see the README). A handful run concurrently instead; ThreadPoolExecutor.map keeps
        # results in the same order as `candidates`, so writes below stay deterministic either way.
        with concurrent.futures.ThreadPoolExecutor(max_workers=WEBSITE_CHECK_CONCURRENCY) as pool:
            keep_flags = pool.map(lambda row: website_offers_residential_pickup(row["website"]), candidates)
        new = 0
        for row, keep in zip(candidates, keep_flags):
            # insert_if_new's primary-key constraint is the real guarantee against a duplicate --
            # not the `seen` set above, which is just there to skip a repeat website check.
            if keep and db.insert_if_new(db_path, row):
                seen.add(row["phone"])
                new += 1
        db.sync_backup(db_path)  # and back it up, since a restart wipes the local disk
        return new, None
    except Exception as error:  # one bad state must not stop the run
        return 0, error


def run(db_path, states):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db.restore_if_empty(db_path)
    seen = db.existing_phones(db_path)
    today = date.today().isoformat()
    total_new = 0

    remaining = list(states)
    consecutive_unreachable = 0
    aborted_early = False
    for round_num in range(1, RETRY_ROUNDS + 1):
        still_failing = []
        for i, state in enumerate(remaining):
            # This whole iteration -- not just _attempt_state's own internals -- must be unable
            # to kill the run. It wasn't: the logging/sleep below used to sit outside any try, so
            # a failure here (hit live) crashed the process after only 2 of 13 states with no
            # explanation, the same class of bug _attempt_state itself was built to prevent.
            try:
                new, error = _attempt_state(state, db_path, seen, today)
                if error is None:
                    total_new += new
                    consecutive_unreachable = 0
                    label = f"[{i + 1}/{len(remaining)}]" if round_num == 1 else "retry succeeded:"
                    print(f"{label} {state}: {new} new companies")
                else:
                    message = _friendly_error(error)
                    label = f"[{i + 1}/{len(remaining)}]" if round_num == 1 else "still failing after retry:"
                    print(f"{label} {state}: skipped -- {message}", file=sys.stderr)
                    log_debug_detail(db_path, state, error)  # the real exception, for /api/debug.log
                    still_failing.append(state)              # -- never shown in the user-facing log
                    consecutive_unreachable = consecutive_unreachable + 1 if message == UNREACHABLE_ERROR_MESSAGE else 0
                time.sleep(REQUEST_DELAY_SECONDS)
            except Exception as error:
                # Same [i/n]-style prefix as the two branches above, so this line matches
                # STATE_OUTCOME_RE too -- without it, a state failing here (the logging/sleep
                # code, not _attempt_state itself) went missing from the retry button entirely,
                # since app.py's parsing only recognized the other two branches' formats.
                label = f"[{i + 1}/{len(remaining)}]" if round_num == 1 else "still failing after retry:"
                print(f"{label} {state}: skipped -- the map data source had a temporary problem", file=sys.stderr)
                log_debug_detail(db_path, state, error)
                still_failing.append(state)
                consecutive_unreachable = 0
            # A handful of states in a row that can't even connect (not just slow/busy/blocked)
            # means the data source itself is unreachable from here -- grinding through every
            # remaining state at several minutes each just to report the same thing N more times
            # helps no one. Whatever's left of this run stays unattempted (shown as "not reached",
            # the same status a Stop click leaves behind) rather than getting the full retry cycle.
            if consecutive_unreachable >= CONSECUTIVE_UNREACHABLE_ABORT_THRESHOLD:
                print(f"Can't reach the map data service after {consecutive_unreachable} states in a row -- "
                      "stopping early instead of waiting on the rest. Check the connection and try again.",
                      file=sys.stderr)
                aborted_early = True
                break
        remaining = still_failing
        if aborted_early or not remaining or round_num == RETRY_ROUNDS:
            break
        try:
            print(f"{len(remaining)} state(s) had a temporary problem -- retrying automatically "
                  f"in {RETRY_ROUND_DELAY_SECONDS}s: {', '.join(remaining)}")
            time.sleep(RETRY_ROUND_DELAY_SECONDS)
        except Exception:
            pass

    print(f"Done. {total_new} new rows -> {db_path} ({len(seen)} total unique phones)")
    if remaining:
        print(f"Failed states (rerun to retry): {', '.join(remaining)}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("output/leads.db"))
    parser.add_argument("--states", nargs="+", default=STATES, metavar="ST",
                        help="limit the run to these state codes, e.g. --states CO WA")
    args = parser.parse_args()
    run(args.output, [state.upper() for state in args.states])


if __name__ == "__main__":
    main()
