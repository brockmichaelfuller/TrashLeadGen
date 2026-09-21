"""Collect waste/garbage/dumpster companies from Overture Maps places in ONE pass.

Free and keyless: DuckDB reads Overture's public parquet files directly. Pass a cities
CSV (columns: city,state) to limit the scrape to those cities, or omit it for the whole U.S.
Rows are appended to the CSV, deduped by phone number.
"""
import argparse
import csv
import re
import sys
from datetime import date
from pathlib import Path

import duckdb
import requests

from lead_scraper import COLUMNS, EXCLUDE_NAME, ensure_columns, load_existing_phones, normalize_phone, timezone_label

CATEGORIES = ("waste_management", "junk_removal_and_hauling", "dumpster_rental")
# Strong pickup terms only; the hauler filter in lead_scraper.EXCLUDE_NAME removes the rest of the noise.
NAME_REGEX = r"garbage|trash|rubbish|refuse|dumpster|roll.?off|junk (removal|hauling)|waste (collection|removal|management|services|hauling)"


def latest_release():
    catalog = requests.get("https://stac.overturemaps.org/catalog.json", timeout=30).json()
    return catalog["latest"]


def build_query(release, has_cities):
    source = (f"read_parquet('s3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*', "
              "hive_partitioning=1)")
    city_filter = ("AND (lower(addresses[1].locality), addresses[1].region) IN (SELECT lower(city), state FROM wanted)"
                   if has_cities else "")
    return f"""
        SELECT names.primary AS name, phones[1] AS phone, addresses[1].locality AS city,
               addresses[1].region AS state, id, emails[1] AS email,
               websites[1] AS website, addresses[1].freeform AS address,
               (bbox.xmin + bbox.xmax) / 2 AS lon, (bbox.ymin + bbox.ymax) / 2 AS lat
        FROM {source}
        WHERE addresses[1].country = 'US' AND phones[1] IS NOT NULL AND names.primary IS NOT NULL
          AND (list_contains({list(CATEGORIES)!r}, categories.primary)
               OR regexp_matches(names.primary, '{NAME_REGEX}', 'i'))
          {city_filter}
    """


def run(cities_path, output_path):
    cities = []
    if cities_path and cities_path.exists():
        with cities_path.open(newline="", encoding="utf-8") as f:
            cities = [(c["city"].strip(), c["state"].strip().upper()) for c in csv.DictReader(f)]

    release = latest_release()
    print(f"Overture release {release}; scope: {len(cities)} cities" if cities else
          f"Overture release {release}; scope: entire U.S.", flush=True)
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
    if cities:
        con.execute("CREATE TABLE wanted(city VARCHAR, state VARCHAR)")
        con.executemany("INSERT INTO wanted VALUES (?, ?)", cities)

    print("Scanning places (this reads a few GB and can take several minutes)...", flush=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ensure_columns(output_path)
    seen = load_existing_phones(output_path)
    write_header = not output_path.exists() or output_path.stat().st_size == 0
    today = date.today().isoformat()
    new = 0
    con.execute(build_query(release, bool(cities)))
    with output_path.open("a", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=COLUMNS)
        if write_header:
            writer.writeheader()
            out.flush()
        # Stream results in small batches and flush every lead, so the UI shows them one by one.
        while True:
            batch = con.fetchmany(100)
            if not batch:
                break
            for name, raw_phone, city, state, place_id, email, website, address, lon, lat in batch:
                phone = normalize_phone(raw_phone)
                if not phone or phone in seen or EXCLUDE_NAME.search(name):
                    continue
                seen.add(phone)
                writer.writerow({"company_name": name.strip(), "phone": phone, "email": (email or "").strip().lower(),
                                 "website": website or "", "address": address or "", "city": city or "",
                                 "state": state or "", "timezone": timezone_label(state, lat, lon),
                                 "source": f"overture:{release}/{place_id}",
                                 "date_collected": today})
                out.flush()
                new += 1
                if new % 25 == 0:
                    print(f"...{new} leads so far", flush=True)
    print(f"Done. {new} new rows -> {output_path} ({len(seen)} total unique phones)", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cities", type=Path, default=None, help="CSV with city,state; omit for the whole U.S.")
    parser.add_argument("--output", type=Path, default=Path("output/leads.csv"))
    args = parser.parse_args()
    try:
        run(args.cities, args.output)
    except Exception as error:
        print(f"FAILED: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
