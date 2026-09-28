"""A stand-in for lead_scraper.py used only by the browser tests (test_browser.py), so a real Run/
Stop/Retry click against the real app.py server never actually hits Overpass over the network.
Speaks the same CLI (--output/--states) and prints the same log-line shapes app.py's own parsing
(parse_failed_states, parse_finished_states, run_aborted_early) already knows how to read, so the
page behaves exactly as it would against the real scraper for these scenarios -- driven entirely by
special state codes rather than real network conditions:

  SLOW<n>     sleeps n tenths of a second before succeeding (default 3) -- gives a test time to
              click Stop mid-run.
  FAIL        always reports as skipped, for testing the ordinary Retry-button path.
  BLOCKED*    (e.g. BLOCKED1, BLOCKED2 -- any two distinct codes with this prefix, since app.py's
              log parsing keys on the exact state string and two real runs never repeat one) report
              as blocked, then print the same early-abort line lead_scraper.py's real
              CONSECUTIVE_PERSISTENT_FAILURE_ABORT_THRESHOLD path prints once two in a row have,
              and stop -- for testing the aborted-early headline.
  anything else inserts one fake lead for that state code and reports success.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import db  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--states", nargs="+", default=[])
    args = parser.parse_args()

    blocked_run = 0
    for i, state in enumerate(args.states):
        label = f"[{i + 1}/{len(args.states)}]"
        if state.startswith("BLOCKED"):
            blocked_run += 1
            print(f"{label} {state}: skipped -- was blocked from reaching the map data source", file=sys.stderr)
            if blocked_run >= 2:
                print("Can't reach the map data service after 2 states in a row -- stopping early "
                      "instead of waiting on the rest (blocked from reaching it). Check the "
                      "connection (or whatever's blocking it) and try again.", file=sys.stderr)
                break
            continue
        blocked_run = 0
        if state == "FAIL":
            print(f"{label} {state}: skipped -- the map data source had a temporary problem", file=sys.stderr)
            continue
        if state.startswith("SLOW"):
            time.sleep(int(state[4:] or 3) / 10)
        else:
            time.sleep(0.05)
        db.insert_if_new(args.output, {"phone": f"(555) 000-{i:04d}", "company_name": f"Fake Co {state}",
                                        "state": state, "email": f"fake{i}@example.com"})
        print(f"{label} {state}: 1 new companies")

    print(f"Done. 0 new rows -> {args.output} (0 total unique phones)")


if __name__ == "__main__":
    main()
