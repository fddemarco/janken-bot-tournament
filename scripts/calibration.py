"""Are the title floors and norm thresholds calibrated? Read-only.

    uv run python scripts/calibration.py [--db janken.db]

Every threshold in `rating.py` is expressed, in its comments, as an implied *match*
win rate: 1600 is "65% against an average bot", 2100 is "98%". That reading comes
from `expected()` — a logistic on `glicko.SCALE` — not from anything observed, so
if the curve does not match how bots actually beat each other, the floors are
calibrated against a fiction and the titles mean something other than what they
claim.

One thing report 1 cannot tell you: `SCALE` is not the knob for a bad fit. It only
relabels the axis — the ratings and the gaps between them are computed in internal
units, and stretching them to print changes both sides of the comparison equally.
A curve that is too steep in the middle and too flat at the ends is the *field*
being heterogeneous (a cluster of near-equal strong bots, plus weak ones anybody
farms), which one logistic cannot fit at any scale.

Four reports, in decreasing order of how much they can invalidate:

1. Is the curve right? Bucket recorded matches by the rating gap the two bots
   brought in, and compare `expected()` against what happened.
2. What did each floor actually select for — how many bots ever cleared it, and
   what they really scored.
3. Could a norm ever have been earned? Replayed against every period's field.
4. How the titles are distributed, against the intent that the top ones stay rare.

Nothing here writes. It reads the same `matches` and `rating_history` the dashboard
does, and prints counts beside every rate so a thin bucket is visible rather than
quietly persuasive — with a field this size the widest gaps are the thinnest data,
and they are exactly the gaps the top two titles depend on.
"""

import argparse
import collections
import math
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from janken import norms
from janken import rating as elo
from janken.registry import Registry, _score_a

# Gap buckets for report 1. Narrow where the data is thick and the curve is nearly
# linear; wide at the top, where every title above `master` lives and where a
# 19-bot field yields the fewest matches.
GAP_EDGES = (0, 50, 100, 150, 200, 300, 400, 600, 10_000)


def _bucket(gap):
    for lo, hi in zip(GAP_EDGES, GAP_EDGES[1:]):
        if lo <= gap < hi:
            return (lo, hi)
    return (GAP_EDGES[-2], GAP_EDGES[-1])


def _label(lo, hi):
    return "%d+" % lo if hi >= 10_000 else "%d-%d" % (lo, hi)


def _wilson(hits, n):
    """95% interval on a proportion. Wald breaks down on the thin top buckets —
    at 12 matches it happily reports a bound below zero — so Wilson, which does
    not, and which is what makes a small bucket look small."""
    if not n:
        return (0.0, 1.0)
    z = 1.96
    p = hits / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def load_matches(conn):
    """Every rated match, paired with the ratings its two bots brought in.

    The pre-match rating is the *previous* period's snapshot, not the current
    one: `_store_current_rating_snapshot` runs after `record_tournament` has
    applied the deltas, so the row stamped with a period is what the period
    produced, not what it started from. Matches in the earliest period have no
    previous snapshot and are skipped rather than assumed — the count is reported.
    """
    periods = [r[0] for r in conn.execute(
        "SELECT DISTINCT played_at FROM rating_history ORDER BY played_at")]
    previous = {this: prev for prev, this in zip(periods, periods[1:])}

    ratings = collections.defaultdict(dict)     # period -> bot_id -> rating
    for played_at, bot_id, rating in conn.execute(
            "SELECT played_at, bot_id, rating FROM rating_history"):
        ratings[played_at][bot_id] = rating

    rows, skipped = [], 0
    for m in conn.execute(
            """SELECT played_at, bot_a, bot_b, wins_a, wins_b, draws,
                      forfeit_a, forfeit_b
               FROM matches WHERE kind = 'tournament'"""):
        played_at, a, b = m[0], m[1], m[2]
        prior = previous.get(played_at)
        snap = ratings.get(prior, {}) if prior else {}
        if a not in snap or b not in snap:
            skipped += 1
            continue
        rows.append((snap[a], snap[b], _score_a(_Row(m))))
    return rows, skipped


class _Row:
    """`_score_a` reads attributes; sqlite hands back a tuple."""

    def __init__(self, m):
        (_played_at, _a, _b, self.wins_a, self.wins_b, self.draws,
         self.forfeit_a, self.forfeit_b) = m


def report_curve(rows, skipped):
    print("1. IS THE CURVE RIGHT?")
    print("   Every match, bucketed by the rating gap the two bots brought in.")
    print("   'Predicted' is what `expected()` said; 'observed' is what happened.")
    print()
    buckets = collections.defaultdict(lambda: [0.0, 0.0, 0])   # exp, obs, n
    for ra, rb, score in rows:
        # Orient each match so the favourite is side A, or a symmetric pairing
        # would cancel its own error out and every bucket would read 50%.
        if rb > ra:
            ra, rb, score = rb, ra, 1.0 - score
        slot = buckets[_bucket(ra - rb)]
        slot[0] += elo.expected(ra, rb)
        slot[1] += score
        slot[2] += 1

    print("   %-10s %10s %10s %18s %8s" % ("gap", "predicted", "observed",
                                           "95% interval", "matches"))
    print("   " + "-" * 60)
    for lo, hi in zip(GAP_EDGES, GAP_EDGES[1:]):
        exp, obs, n = buckets.get((lo, hi), [0, 0, 0])
        if not n:
            continue
        lo_ci, hi_ci = _wilson(obs, n)
        flag = "" if lo_ci <= exp / n <= hi_ci else "  <-- outside"
        interval = "%.1f-%.1f%%" % (100 * lo_ci, 100 * hi_ci)
        print("   %-10s %9.1f%% %9.1f%% %18s %8d%s"
              % (_label(lo, hi), 100 * exp / n, 100 * obs / n,
                 interval, n, flag))
    if skipped:
        print()
        print("   (%d matches skipped: no prior snapshot to read their ratings from,"
              % skipped)
        print("    which is the earliest period and any bot's first appearance.)")
    print()


def report_floors(conn, rows):
    print("2. WHAT DID EACH FLOOR SELECT FOR?")
    print("   The comments in rating.py read each floor as a win rate against an")
    print("   average bot. This is that claim, measured.")
    print()
    print("   %-22s %8s %12s %12s" % ("title", "floor", "claimed", "observed"))
    print("   " + "-" * 58)
    for title, floor in reversed(elo.RATING_TITLES):
        claimed = elo.expected(floor, elo.START_RATING)
        # Every match played by a bot rated within 25 points of the floor, which is
        # the only direct evidence about what that rating is worth.
        near = [(ra, rb, s) for ra, rb, s in rows if abs(ra - floor) <= 25]
        near += [(rb, ra, 1.0 - s) for ra, rb, s in rows if abs(rb - floor) <= 25]
        if near:
            observed = sum(s for _a, _b, s in near) / len(near)
            # ...but those opponents were not all average, so also report what the
            # curve expected of the same matches. The gap between the two is the
            # honest reading.
            predicted = sum(elo.expected(a, b) for a, b, _s in near) / len(near)
            print("   %-22s %8.0f %11.1f%% %11.1f%%   (vs %.1f%% predicted for the "
                  "same opponents, %d matches)"
                  % (elo.name(title), floor, 100 * claimed, 100 * observed,
                     100 * predicted, len(near)))
        else:
            print("   %-22s %8.0f %11.1f%% %12s"
                  % (elo.name(title), floor, 100 * claimed, "no data"))
    print()


def report_norm_reachability(conn):
    print("3. COULD A NORM EVER HAVE BEEN EARNED?")
    print("   Replaying every period: the field a norm event would have selected,")
    print("   and whether its average cleared each level's floor.")
    print()
    with Registry(conn_path(conn)) as reg:
        field = reg.eligible_bots()
    chosen, shape = norms.select_field(field)
    print("   Today: %s" % (shape.why() if chosen else "no qualifying field"))
    if chosen:
        for title in ("im", "gm"):
            needed, perf, avg = elo.NORM_REQUIREMENTS[title]
            room = shape.average_rating - avg
            print("     %-22s event floor %d, field %d -> %s"
                  % (elo.name(title), avg, round(shape.average_rating),
                     "reachable (%+d)" % room if room >= 0
                     else "OUT OF REACH (%+d)" % room))
            if room >= 0:
                ratings = sorted((b.rating for b in chosen), reverse=True)
                opponents = ratings[1:]
                score = elo.score_for_performance(perf, opponents)
                if score is None:
                    print("       ...but the performance floor is unreachable at "
                          "any score against this field")
                else:
                    print("       top seed would need %.1f of %d games (%.0f%%)"
                          % (score, len(opponents), 100 * score / len(opponents)))
    print()
    awarded = conn.execute("SELECT COUNT(*) FROM norms").fetchone()[0]
    events = conn.execute(
        "SELECT COUNT(DISTINCT played_at) FROM matches WHERE kind = 'norm'"
    ).fetchone()[0]
    print("   Historically: %d norm event(s) held, %d norm(s) awarded."
          % (events, awarded))

    best = replay_best_field(conn)
    if best:
        when, average, size = best
        print("   Best field a norm event could ever have drawn: %d bots "
              "averaging %d" % (size, round(average)))
        print("   (%s)" % when[:16].replace("T", " "))
        for title in ("im", "gm"):
            _needed, _perf, avg = elo.NORM_REQUIREMENTS[title]
            verdict = ("was reachable" if average >= avg
                       else "never reachable, best was %+d short" % (average - avg))
            print("     %-22s floor %d -> %s" % (elo.name(title), avg, verdict))
    print()


def replay_best_field(conn):
    """The strongest field a norm event could have selected, over all history.

    Answers whether a level is unreachable *by construction* or merely unreachable
    today. Approximate in one way worth naming: a bot's title at the time is
    derived from the peak rating in that snapshot, because `rating_history` does not
    store the title and today's is wrong for a past period. That only affects the
    titled-share check, not the field average the floors are compared against.
    """
    owners = {bot_id: submitter for bot_id, submitter in
              conn.execute("SELECT id, submitter FROM bots")}
    names = {bot_id: name for bot_id, name in
             conn.execute("SELECT id, name FROM bots")}
    periods = collections.defaultdict(list)
    for played_at, bot_id, rating, peak in conn.execute(
            "SELECT played_at, bot_id, rating, peak_rating FROM rating_history"):
        if bot_id not in owners:
            continue                      # removed since; nothing to field
        periods[played_at].append(_Stub(
            bot_id, names[bot_id], rating, owners[bot_id],
            elo.title_for_rating(peak)))

    best = None
    for played_at, field in periods.items():
        chosen, shape = norms.select_field(field)
        if chosen and (best is None or shape.average_rating > best[1]):
            best = (played_at, shape.average_rating, len(chosen))
    return best


class _Stub:
    """A bot as `norms.select_field` needs it, at a past moment."""

    def __init__(self, bot_id, name, rating, submitter, league):
        self.id = bot_id
        self.name = name
        self.rating = rating
        self.submitter = submitter
        self.league = league


def conn_path(conn):
    for _seq, name, path in conn.execute("PRAGMA database_list"):
        if name == "main":
            return path
    raise RuntimeError("no main database")


def report_distribution(conn):
    print("4. HOW ARE THE TITLES DISTRIBUTED?")
    print("   The top rungs are meant to stay rare. FIDE's own GM share of rated")
    print("   players is around 1%; a board where half the field is titled is a")
    print("   board where the title is not saying much.")
    print()
    rows = conn.execute(
        """SELECT league, COUNT(*) FROM bots
           WHERE banned = 0 AND held = 0 GROUP BY league""").fetchall()
    counts = dict(rows)
    total = sum(counts.values()) or 1
    for title in reversed(elo.TITLES):
        n = counts.get(title, 0)
        bar = "#" * int(round(40 * n / total))
        print("   %-22s %3d  %5.1f%%  %s" % (elo.name(title), n,
                                             100 * n / total, bar))
    print("   %-22s %3d" % ("in the field", total))
    print()


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", default=os.environ.get("JANKEN_DB", "janken.db"))
    args = p.parse_args()
    if not os.path.isfile(args.db):
        raise SystemExit("no such registry: %s" % args.db)

    conn = sqlite3.connect(args.db)
    try:
        rows, skipped = load_matches(conn)
        print()
        print("=" * 72)
        print("THRESHOLD CALIBRATION — %s  (%d rated matches usable)"
              % (os.path.basename(args.db), len(rows)))
        print("=" * 72)
        print()
        if not rows:
            print("No rated matches with a prior snapshot — run some tournaments.")
            return
        report_curve(rows, skipped)
        report_floors(conn, rows)
        report_norm_reachability(conn)
        report_distribution(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
