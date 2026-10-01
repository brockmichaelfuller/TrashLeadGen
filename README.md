# TrashLeadGen

Finds residential curbside trash-hauling companies across the U.S. and saves their **name and phone number** to a local SQLite database, with a small web page (`app.py`) to run scrapes and record outreach outcomes. This is a research list for outreach prep. Nobody should be called or texted from it before Thomas reviews it (TCPA and do-not-call rules apply once numbers are dialed).

## Data source

[OpenStreetMap](https://www.openstreetmap.org) via the free [Overpass API](https://overpass-api.de). No API key or account is needed. Only business names and publicly listed business phone numbers are collected. OSM data is licensed under [ODbL](https://www.openstreetmap.org/copyright), so credit OpenStreetMap contributors if the list is published.

Coverage is limited by what mappers have added: many businesses have no phone number in OSM, so expect far fewer rows than Google or Yelp would give. The code is structured so more sources can be added later.

## Running the web page (the actual product)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py            # then open http://127.0.0.1:8000
```

This is what Render deploys (`python app.py`, per `render.yaml`/the service's start command) and what an owner actually uses day to day: click Run scrape, watch progress, mark each lead Interested/Not interested/Do not contact, add notes, and remove a lead entirely if it's the wrong business type. Environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8000` | Port to listen on |
| `HOST` | `127.0.0.1` (localhost only) | Set to `0.0.0.0` to accept connections from outside the machine (needs `APP_PASSWORD` — the app refuses to start on a public host without one) |
| `APP_PASSWORD` | none (open access) | HTTP Basic Auth password. Required once `HOST` isn't localhost |
| `APP_USERNAME` | any username accepted | Optional comma-separated allowlist of usernames, checked alongside `APP_PASSWORD` |
| `GITHUB_TOKEN`, `GITHUB_REPO`, `SUPABASE_DB_URL` | unset (disabled) | See "Making data permanent on Render" below |

A few things worth knowing about the page itself:

- **Scope.** "Entire U.S." runs all 50 states + DC in one subprocess; the four "Group" options split that into smaller chunks (useful on a host that can't finish the whole country in one sitting, like Render's free plan).
- **Stop and Retry.** Stop ends the run cleanly (reported as "Stopped," not an error) and offers to continue with whatever states weren't reached. If some states fail outright (or the map data source is unreachable and the run gives up on it early — see below), a Retry button appears for just those.
- **One list.** Every lead with a name and phone (everything actually needed to call) is on the page. Email, when OpenStreetMap has it, is a column and an optional "Has email" filter -- not a separate tab -- so Copy phones/emails and Download CSV always cover everyone, not just whichever view happens to be open.
- **A backup warning banner** appears if the last save to GitHub/Supabase failed (see below) — it keeps retrying in the background on its own; the banner is just so a stuck backup isn't silent.
- **`/api/debug.log`** has the raw exception + traceback behind whatever plain-language message the page shows for a failed state — useful for diagnosing a *recurring* failure; not meant for the owner.

## Running the scraper directly (no web page)

```bash
python lead_scraper.py
```

Same scraper `app.py` runs as a subprocess, invoked directly instead. Useful for local debugging or a one-off run without starting the server.

Options:

| Flag | Default | Meaning |
|---|---|---|
| `--output` | `output/leads.db` | Where the SQLite database is written |
| `--states` | all 50 + DC | Limit the run, e.g. `--states CO WA` |

## Storage

Leads live in a SQLite database (`db.py`), with `phone` as the primary key -- that's what actually guarantees no duplicate, not application code. Both the web app and the scraper subprocess read and write it directly (SQLite's WAL journal mode plus a busy-timeout give real concurrent access; nothing needs its own file lock). GitHub backup only ever speaks CSV, so `db.py` exports the database to a companion `output/leads.csv` right before every backup push, and imports that same file into a fresh database on restore. Supabase backup speaks rows directly instead (a plain per-lead upsert, not a CSV blob), reading/writing that same companion CSV as the bridge between it and the database -- either way, the backup logic itself (`sync_leads.py`) is what does the actual talking to GitHub/Supabase, never `db.py` directly.

## Output columns

`company_name`, `phone` (format `(555) 123-4567`), `email`, `website`, `address`, `city`, `state`, `timezone`, `source` (the OSM object, e.g. `openstreetmap:node/123`), `date_collected`, `status` and `notes` (set from the page once outreach begins — see below), `rejected_at` (set when a lead is removed as not a real hauler — see below).

- **Deduped by phone number.** Rerunning only adds new phones (the database's primary key rejects a duplicate outright), so the script is safe to rerun or resume.
- **Progress is saved after every state.** A failed state doesn't stop the run. Since most failures are brief Overpass hiccups, any state still failing after its normal retries gets one more automatic pass (after a short cooldown) before being reported — only a state that fails that too shows up as failed, with a Retry button on the page for whatever's left.
- `state` is the state searched, so it is always filled in. `city` comes from the OSM address and is blank when the mapper didn't add one.
- **Complete vs partial.** A lead with a company name, phone, email and timezone is "complete"; the page's Missing column names what any other lead lacks. This doesn't affect what's callable or exportable -- a phone number is all that's actually needed, so a lead missing email or timezone is still on the list, still in Copy phones/emails, and still in Download CSV.
- **Who counts as a lead:** a residential curb-pickup trash hauler doing normal weekly service — not bulk/large-item-only pickup. The company name must either contain a hauling-related word — waste, garbage, trash, refuse, disposal, or rubbish (e.g. "Acme Waste Services", "Downtown Trash Removal") — **or** match `KNOWN_BRANDS` in `lead_scraper.py`, a list of haulers (major names like Republic Services and Waste Management, plus specific smaller ones confirmed by hand, like Curbie Sanitation) whose listings are often just the brand name with no generic keyword in it. "Sanitation" alone isn't a qualifying word — found live that it's just as often a portable-toilet/porta-potty rental company (e.g. "Southwest Sanitation") as a trash hauler, with nothing in the name to tell them apart. Either way, `EXCLUDE_NAME` then drops anything reading as: a utility, medical/hazardous waste, landfill/transfer/recovery/convenience facility, scrap/recycling yard, government department, dumpster rental/roll-off/junk/bulk hauling, construction debris, a corporate HQ, a retail/thrift store or cafe, a moving company, a pet-waste scooper, or a tire/textile recycler — and any listing with a `.gov` website is dropped regardless of what its name says.
- **Website check.** A candidate that clears the name filters and has a website gets that site's own text checked (free, no AI/API cost) for language distinguishing a normal residential route ("weekly curbside," "residential pickup") from a dumpster-rental, junk-removal, or portable-toilet business ("dumpster rental," "junk removal," "porta potty rental") — this is what catches a company like "Horizon Disposal Services," whose name gives no hint it's actually a dumpster-rental business. A site that mentions both (common — plenty of real haulers also rent dumpsters) is kept; an unreachable site or one with neither signal is kept unverified rather than dropped, so a network hiccup never costs a real lead. It's still a heuristic, not a guarantee — review the list before outreach, and extend `DISQUALIFYING_SERVICE_PHRASES`/`QUALIFYING_SERVICE_PHRASES` (or `KNOWN_BRANDS`/`EXCLUDE_NAME`) as gaps turn up. This check adds real time to a scrape (a few new-candidate websites are fetched concurrently at a time, ~10s timeout each, rather than one at a time).
- **`status` and `notes`** start blank and aren't set by the scraper. Once Thomas has reviewed the list and outreach begins, use the "Interested?" dropdown and Notes field on each row (right next to the phone number) to record the outcome of a call — they save as you type, each field saves independently so two people editing the same lead at once can't clobber each other's work, and both columns are included in the CSV export.
- **"Not interested" and "Do not contact"** leads stay visible in the table (for the record) but are automatically left out of Copy phones, Copy emails, and Download CSV.
- **Deleting a lead is permanent.** The Remove button on each row (confirms first, and offers Undo for a few seconds after) doesn't just drop the row — it's kept with a `rejected_at` date so its phone stays in the database forever, which is what stops a later scrape from seeing it as new and adding it right back. A rejected row is hidden everywhere else (the site, exports, the Google Sheet), it just isn't gone from the database.

## Making data permanent on Render

Render's free plan has no persistent disk: every redeploy, and every time the service spins back up after ~15 minutes idle, starts from an empty filesystem and loses whatever was scraped. `sync_leads.py` backs up a CSV export of the database externally so that doesn't lose data — it's a no-op with nothing configured, and each backend below is independently optional:

- **GitHub** — commits `output/leads.csv` to this repo after every state and every edit, and restores the latest commit (into a fresh database) when the app starts with none. Set `GITHUB_TOKEN` (a personal access token with Contents read/write on this repo) and `GITHUB_REPO` (`owner/name`). **If `GITHUB_REPO` is ever set to the same repo Render deploys from**, every backup commit will also trigger a new deploy (auto-deploy watches every push to the branch) — mid-scrape, that restarts the service and kills the run. Point it at a separate repo (or a branch Render doesn't deploy) instead, or turn off auto-deploy for that branch.
- **Supabase** — a free-tier Postgres database. Every lead is upserted as its own row, keyed on `phone`, after every state and every edit — no clear-then-rewrite of anything, so a failed push can't lose what's already there. Set `SUPABASE_DB_URL` to a Postgres connection string (from the Supabase dashboard: **Project Settings → Database → Connection string**, the **Transaction pooler** one specifically — it's on port 6543 and IPv4-compatible, which Render needs; the direct connection is IPv6-only on most regions). The table has to exist first — run this once in Supabase's **SQL Editor**:

  ```sql
  create table leads (
    phone text primary key,
    company_name text not null default '',
    email text not null default '',
    website text not null default '',
    address text not null default '',
    city text not null default '',
    state text not null default '',
    timezone text not null default '',
    source text not null default '',
    date_collected text not null default '',
    status text not null default '',
    notes text not null default '',
    rejected_at text not null default ''
  );
  ```

  A free Supabase project pauses itself after about a week with no activity (a scrape or an edit counts, so an actively-used app won't trigger this) — a paused project is resumed from the Supabase dashboard with one click, and nothing is lost while paused, it just won't back up until you resume it.

Both GitHub and Supabase can be set at once — on startup the app tries GitHub first and falls back to Supabase if GitHub has nothing (so either one alone is enough to survive a restart, into a brand new database). Neither is required for local use.

A save from the web page (a status/notes edit or a delete/undo) writes to disk immediately and returns right away; the backup push happens a few seconds later in the background, so a burst of edits results in one push, not one per keystroke-save. If a push fails, it keeps retrying automatically every 30 seconds until one succeeds (or a new edit reschedules it sooner), and the page shows a banner for as long as the most recent attempt is still failing. A scrape backs up after every state regardless of whether the previous state's push succeeded, so a failure there self-corrects on the very next state.

## Stopping things

| What | How | How fast |
|---|---|---|
| A running scrape | Click **Stop** on the page, or `POST /api/stop` | Immediate — the scraper subprocess is killed outright |
| External backups (GitHub/Supabase) | Click **Pause external backups** in the page header, or `POST /api/backups {"paused": true}` | Immediate, and doesn't restart the service (an environment variable change would). Local saves keep working; nothing pushes until you **Resume external backups** (or `{"paused": false}`) |
| The whole app | Stop the process (Ctrl+C locally; suspend/delete the service on Render) | Immediate, but Render's free plan has no persistent disk — see below before relying on this |

Anyone who can reach the page (i.e. anyone with the `APP_PASSWORD`, once one is set) can do any of these; there's no separate owner role.

The pause itself survives a restart when `SUPABASE_DB_URL` is set -- it's a settings row in Supabase (`app_settings`, created automatically the first time it's needed), not a file on Render's disk. Without Supabase configured, it falls back to a local flag file, which does **not** survive a restart on Render's free plan (it'll quietly resume on the next cold start) -- the same limitation local-only use has for everything else.

## Recurring lead-quality audits

`audit_log.json` tracks which leads have already been reviewed (by a person or by an AI session) for actually being a residential curbside hauler, so a recurring audit only looks at leads it hasn't seen before instead of starting over each time. It's read and written by whatever process runs that audit (a Claude Code session, following its own setup instructions). The app itself also reads it, but only the `"deleted"` verdicts, and only to keep them rejected in the database (see `db.import_audit_log_rejections`, run at startup by both `app.py` and `lead_scraper.py`) — a lead the audit removed with older code, before permanent rejection existed, would otherwise come back as new on a later scrape. Commit `audit_log.json` whenever an audit runs, regardless of which session ran it, so the history carries over.

## Tests

```bash
python -m unittest discover -s tests -t .
```

Runs automatically (along with `pyflakes`) on every push and pull request via `.github/workflows/ci.yml`.

Most of the suite is server-side (the Python modules above). `tests/test_browser.py` is different -- it drives the actual page in a real headless browser against a real running server, since headline wording, Stop/Retry/Continue, the email filter, row-level errors, and the inline Remove confirm are all client-side JS that no server-side test can reach at all. It needs [Playwright](https://playwright.dev/python/) and a browser binary, which the base `requirements.txt` deliberately doesn't pull in (the production app itself needs neither):

```bash
pip install -r requirements-dev.txt
playwright install chromium
```

It runs against `tests/fixtures/fake_scraper.py`, a stand-in for `lead_scraper.py` that speaks the same `--output`/`--states` CLI and understands a few special state codes (`SLOW<n>`, `FAIL`, `BLOCKED*` -- see that file) to trigger a slow/failed/blocked state on demand, so Run/Stop/Retry can be exercised through the real subprocess/log-parsing code path without ever hitting Overpass over the network. If `test_browser.py`'s tests are missing from a local run, Playwright isn't installed -- `unittest` skips the whole file rather than failing, but CI always has it.

## Notes

- Keep API keys, if any are added later, in `.env`. It is already in `.gitignore`.
- `output/` is git-ignored so the generated lead list isn't committed.
