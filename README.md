# TrashLeadGen

Finds waste, garbage and dumpster companies across the U.S. and saves their **name and phone number** to a CSV. This is a research list for outreach prep. Nobody should be called or texted from it before Thomas reviews it (TCPA and do-not-call rules apply once numbers are dialed).

## Data source

[OpenStreetMap](https://www.openstreetmap.org) via the free [Overpass API](https://overpass-api.de). No API key or account is needed. Only business names and publicly listed business phone numbers are collected. OSM data is licensed under [ODbL](https://www.openstreetmap.org/copyright), so credit OpenStreetMap contributors if the list is published.

Coverage is limited by what mappers have added: many businesses have no phone number in OSM, so expect far fewer rows than Google or Yelp would give. The code is structured so more sources can be added later.

## Run it

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python lead_scraper.py
```

One run covers the entire U.S. There is no city list. The free Overpass server can't answer a single nationwide query (it times out), so the script searches 50 states + DC one after another and merges everything into one CSV. Expect the full run to take a while; progress prints per state.

Options:

| Flag | Default | Meaning |
|---|---|---|
| `--output` | `output/leads.csv` | Where results are written |
| `--states` | all 50 + DC | Limit the run, e.g. `--states CO WA` |

## Output columns

`company_name`, `phone` (format `(555) 123-4567`), `city`, `state`, `source` (the OSM object, e.g. `openstreetmap:node/123`), `date_collected`.

- **Deduped by phone number.** Rerunning appends only new phones, so the script is safe to rerun or resume.
- **Progress is saved after every state.** A failed state doesn't stop the run. Since most failures are brief Overpass hiccups, any state still failing after its normal retries gets one more automatic pass (after a short cooldown) before being reported — only a state that fails that too shows up as failed, with a Retry button on the page for whatever's left.
- `state` is the state searched, so it is always filled in. `city` comes from the OSM address and is blank when the mapper didn't add one.
- **Complete vs partial.** A lead with a company name, phone, email and timezone is "complete". Leads missing any of those are kept but shown separately as "partial" (with what is missing). The page has a tab for each, and Download CSV exports the tab you are viewing.
- **Who counts as a lead:** a residential curb-pickup trash hauler doing normal weekly service — not bulk/large-item-only pickup. The company name must either contain a hauling-related word — waste, garbage, trash, refuse, disposal, or rubbish (e.g. "Acme Waste Services", "Downtown Trash Removal") — **or** match `KNOWN_BRANDS` in `lead_scraper.py`, a list of haulers (major names like Republic Services and Waste Management, plus specific smaller ones confirmed by hand, like Curbie Sanitation) whose listings are often just the brand name with no generic keyword in it. "Sanitation" alone isn't a qualifying word — found live that it's just as often a portable-toilet/porta-potty rental company (e.g. "Southwest Sanitation") as a trash hauler, with nothing in the name to tell them apart. Either way, `EXCLUDE_NAME` then drops anything reading as: a utility, medical/hazardous waste, landfill/transfer/recovery/convenience facility, scrap/recycling yard, government department, dumpster rental/roll-off/junk/bulk hauling, construction debris, a corporate HQ, a retail/thrift store or cafe, a moving company, a pet-waste scooper, or a tire/textile recycler — and any listing with a `.gov` website is dropped regardless of what its name says.
- **Website check.** A candidate that clears the name filters and has a website gets that site's own text checked (free, no AI/API cost) for language distinguishing a normal residential route ("weekly curbside," "residential pickup") from a dumpster-rental, junk-removal, or portable-toilet business ("dumpster rental," "junk removal," "porta potty rental") — this is what catches a company like "Horizon Disposal Services," whose name gives no hint it's actually a dumpster-rental business. A site that mentions both (common — plenty of real haulers also rent dumpsters) is kept; an unreachable site or one with neither signal is kept unverified rather than dropped, so a network hiccup never costs a real lead. It's still a heuristic, not a guarantee — review the list before outreach, and extend `DISQUALIFYING_SERVICE_PHRASES`/`QUALIFYING_SERVICE_PHRASES` (or `KNOWN_BRANDS`/`EXCLUDE_NAME`) as gaps turn up. This check adds real time to a scrape (one fetch per new candidate with a website, ~10s timeout each).
- **`status` and `notes`** start blank and aren't set by the scraper. Once Thomas has reviewed the list and outreach begins, use the "Interested?" dropdown and Notes field on each row (right next to the phone number) to record the outcome of a call — they save as you type, each field saves independently so two people editing the same lead at once can't clobber each other's work, and both columns are included in the CSV export.
- **"Not interested" and "Do not contact"** leads stay visible in the table (for the record) but are automatically left out of Copy phones, Copy emails, and Download CSV.

## Making data permanent on Render

Render's free plan has no persistent disk: every redeploy, and every time the service spins back up after ~15 minutes idle, starts from an empty filesystem and loses whatever was scraped. `sync_leads.py` backs the CSV up externally so that doesn't lose data — it's a no-op with nothing configured, and each backend below is independently optional:

- **GitHub** — commits `output/leads.csv` to this repo after every state and every edit, and restores the latest commit when the app starts on a fresh host. Set `GITHUB_TOKEN` (a personal access token with Contents read/write on this repo) and `GITHUB_REPO` (`owner/name`).
- **Google Sheets** — overwrites a sheet with the current CSV after every state and every edit, via a Google service account. Set `GOOGLE_SERVICE_ACCOUNT_JSON` (the full service-account key JSON, as one string) and `GOOGLE_SHEET_ID` (from the sheet's URL, between `/d/` and `/edit`). Share the target sheet with the service account's `client_email` as an Editor first, or the writes will fail (see the app's log). The sheet (only the sheet — the site's own table order is untouched) is grouped into sections by the Interested? status, top to bottom: no status yet, Interested, Not interested, Do not contact, each separated by a blank row; within each section, complete leads (see above) come first.

Both can be set at once — on startup the app tries GitHub first and falls back to Sheets if GitHub has nothing (so either one alone is enough to survive a restart). Neither is required for local use.

## Tests

```bash
python -m unittest discover -s tests -t .
```

## Notes

- Keep API keys, if any are added later, in `.env`. It is already in `.gitignore`.
- `output/` is git-ignored so the generated lead list isn't committed.
