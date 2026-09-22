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
- **Progress is saved after every state.** A failed state is reported and skipped and doesn't stop the run. Rerun to retry it.
- `state` is the state searched, so it is always filled in. `city` comes from the OSM address and is blank when the mapper didn't add one.
- **Complete vs partial.** A lead with a company name, phone, email and timezone is "complete". Leads missing any of those are kept but shown separately as "partial" (with what is missing). The page has a tab for each, and Download CSV exports the tab you are viewing.
- **The company name must contain the word "waste"** (for example "Acme Waste Services"). Names like "Acme Dumpsters" or "Southwest Sanitation" are skipped.
- Water and sewer utilities are filtered out by name. Some non-hauler noise (for example an appliance shop with "disposal" in its name) can still slip through, so review before use.

## Tests

```bash
python -m unittest discover -s tests -t .
```

## Notes

- Keep API keys, if any are added later, in `.env`. It is already in `.gitignore`.
- `output/` is git-ignored so the generated lead list isn't committed.
