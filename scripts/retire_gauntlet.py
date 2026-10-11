#!/usr/bin/env python3
"""One-shot cleanup for a registry written before the gauntlet was retired.

The code change needs no migration: a pre-gauntlet database opens and runs
unchanged, because the dead `trial_wins` / `trial_beaten` columns are simply
never read again. This script is for the two cosmetic leftovers, and it is
optional — run it once, or not at all.

  --titles   Realign every title with the peak rating that would earn it today.
             Titles used to have a second door: clear the gauntlet and you were
             a Candidate Master at any rating. Those titles are now unbacked, and
             a CM sitting below the 1600 floor recreates the exact confusion the
             floor was added to fix — a higher-rated bot reading `Novice` beneath
             it. This DEMOTES such bots, the only place in the system that ever
             takes a title away, which is why it is opt-in.

  --matches  Delete the `kind = 'trial'` rows. Their `bot_b` is a pseudo-id
             (`gauntlet:rocky`) that matches no bot, so they can never join
             anything; they only linger as a "trial" option in the dashboard's
             match-log filter.

Neither flag touches a bot's identity, submitter, path, rating, career record or
ban. Prints what it would do and changes nothing without --apply.

  python3 scripts/retire_gauntlet.py --db ./janken.db --titles --matches
  python3 scripts/retire_gauntlet.py --db ./janken.db --titles --matches --apply
"""

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from janken import rating as elo


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="janken.db", help="registry database file")
    p.add_argument("--titles", action="store_true",
                   help="realign titles with peak rating (may demote)")
    p.add_argument("--matches", action="store_true",
                   help="delete old kind='trial' match rows")
    p.add_argument("--apply", action="store_true",
                   help="actually write; without it this is a dry run")
    args = p.parse_args()

    if not os.path.isfile(args.db):
        raise SystemExit("no such database: %s" % args.db)
    if not (args.titles or args.matches):
        raise SystemExit("nothing to do — pass --titles and/or --matches")

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    try:
        if args.titles:
            rows = con.execute(
                "SELECT id, name, league, peak_rating FROM bots ORDER BY peak_rating DESC"
            ).fetchall()
            changes = [
                (r, elo.title_for_rating(r["peak_rating"])) for r in rows
                if elo.title_for_rating(r["peak_rating"]) != r["league"]
            ]
            print("Titles: %d of %d bot(s) disagree with their peak rating."
                  % (len(changes), len(rows)))
            for r, now in changes:
                arrow = "DEMOTE" if elo.TITLE_RANK[now] < elo.TITLE_RANK[r["league"]] else "promote"
                print("  %-7s %-20s peak %6.1f   %s -> %s"
                      % (arrow, r["name"], r["peak_rating"],
                         elo.abbr(r["league"]), elo.abbr(now)))
            if args.apply and changes:
                with con:
                    con.executemany("UPDATE bots SET league = ? WHERE id = ?",
                                    [(now, r["id"]) for r, now in changes])

        if args.matches:
            n = con.execute(
                "SELECT COUNT(*) FROM matches WHERE kind = 'trial'").fetchone()[0]
            print("Matches: %d row(s) with kind = 'trial'." % n)
            if args.apply and n:
                with con:
                    con.execute("DELETE FROM matches WHERE kind = 'trial'")
    finally:
        con.close()

    print("\n%s" % ("Applied." if args.apply
                    else "Dry run — nothing written. Re-run with --apply."))


if __name__ == "__main__":
    main()
