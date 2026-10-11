"""Fit Glicko-2's display constants against the real match log. Read-only.

    uv run python scripts/fit_glicko.py [--db janken.db] [--anchor-top 1870]
    uv run python scripts/fit_glicko.py --score round --anchor-top 1870

The match ladder no longer needs this: `glicko.SCALE` is Glickman's published
173.7178, because fitting it produced 158.9 and a worse ladder in every way that
mattered (see the note above `SCALE`). The script stays for two jobs — fitting
`ALT_SCALE` for the round-fraction ladder, which has no published constant to fall
back on, and re-checking the match ladder against the log whenever the field
changes shape enough to be worth asking.

Two numbers have to be chosen before the ladder means anything, and they are not
independent:

`SCALE` maps Glicko-2's internal units onto the numbers people read. Glickman uses
173.7178, which puts a standard Elo scale on top of the internal one. That constant
is wrong here because the score is a round fraction rather than a win or a loss:
round margins in rock-paper-scissors cluster hard around 0.5, so the internal
spread comes out compressed, and at 173.7178 the whole field would fit between
about 1200 and 1625 — every title floor above `cm` unreachable by construction.

`START_RD` says how unsure we are of a bot that has played nothing. It cannot be
picked separately, because `SCALE` converts the deviation too: stretch the scale to
recover a familiar spread and a newcomer's `2 x RD` penalty stretches with it.

`START_RD` is `start_phi x SCALE`, so the two move together and it is tempting to
solve for both at once — pick a target for what a newcomer's `2 x RD` should cost
and bisect. Doing that gives an internally consistent answer that is wrong:
`start_phi = 0.27`, `START_RD = 98`. Measured against this history, a newcomer as
strong as the best bot in the field then needs **nine tournaments** to climb to its
true rating, against **one** at the standard starting deviation. The bisection buys
a tidy penalty by telling the model it already knows roughly where a bot it has
never seen belongs, and the model believes it.

So `start_phi` is not fitted. A bot that has played nothing is unknown, which is
what Glickman's 350/173.7178 = 2.015 means, and only `SCALE` is solved for here —
one replay, since the internal dynamics do not depend on it:

  1. replay every recorded period at unit scale,
  2. measure the internal spread the data produces,
  3. set `SCALE` so that spread lands on `--spread`.

What a newcomer costs is then a property of the answer rather than an input to it,
and the lever for it is `k` in `Rating.conservative` — which is a ranking choice,
not a rating one. The script prints the climb a cold top bot would make at each
candidate `k` so that choice can be made by looking at it.

Nothing is written. The output is two constants to paste into `glicko.py`, and the
ladder they produce so it can be eyeballed before anything depends on it.
"""

import argparse
import collections
import math
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from janken import glicko

# Glickman's own starting deviation, in internal units: 350/173.7178. Not fitted —
# see the module docstring. It is the statement "this bot is unknown", and the
# replay is what showed that weakening it costs eight tournaments of convergence.
START_PHI = 350.0 / 173.7178

# The `k` values whose effect on a newcomer's rank the report walks through.
CANDIDATE_K = (0.0, 1.0, 2.0)


def _forfeit_score(my_forfeit, their_forfeit):
    """A forfeit's flat score, or None if nobody forfeited.

    Flat rather than the fraction of rounds played before the walkout: otherwise a
    bot losing badly could trip the invalid-move limit on purpose and bank the
    better fraction it held over three rounds instead of the worse one it was
    heading for over two hundred.
    """
    if my_forfeit and their_forfeit:
        return 0.5
    if my_forfeit:
        return 0.0
    if their_forfeit:
        return 1.0
    return None


def round_score(my_wins, their_wins, draws, my_forfeit, their_forfeit):
    """This bot's share of the *rounds*, in [0, 1].

    The low-variance measurement of one bot's edge over another, and the one that
    makes a uniform-random bot inert. It is also the one that collapses: among bots
    that are all competent it lives in a band a couple of points wide, because
    nobody can beat a near-equilibrium opponent by much per round.
    """
    forfeit = _forfeit_score(my_forfeit, their_forfeit)
    if forfeit is not None:
        return forfeit
    rounds = my_wins + their_wins + draws
    if not rounds:
        return 0.5
    return (my_wins + 0.5 * draws) / rounds


def match_score(my_wins, their_wins, draws, my_forfeit, their_forfeit):
    """Who won the match: 1, 0.5 or 0.

    Coarser per match than `round_score` — one bit against eight — but a match of
    hundreds of rounds is a sigmoid amplifier, so a round edge too small to see
    turns into a match record that is not. That is what keeps the ladder spread out
    as the field gets stronger.
    """
    forfeit = _forfeit_score(my_forfeit, their_forfeit)
    if forfeit is not None:
        return forfeit
    if my_wins > their_wins:
        return 1.0
    if my_wins < their_wins:
        return 0.0
    return 0.5


SCORERS = {"round": round_score, "match": match_score}


def load_periods(conn, score=round_score):
    """[(played_at, {bot_id: [(opponent_id, score), ...]})], oldest first."""
    periods = collections.OrderedDict()
    for row in conn.execute(
            """SELECT played_at, bot_a, bot_b, wins_a, wins_b, draws,
                      forfeit_a, forfeit_b
               FROM matches WHERE kind IN ('tournament', 'norm')
               ORDER BY played_at, id"""):
        (played_at, a, b, wa, wb, dr, fa, fb) = row
        slot = periods.setdefault(played_at, collections.defaultdict(list))
        slot[a].append((b, score(wa, wb, dr, fa, fb)))
        slot[b].append((a, score(wb, wa, dr, fb, fa)))
    return list(periods.items())


def _days_between(earlier, later):
    import datetime

    def parse(stamp):
        try:
            moment = datetime.datetime.fromisoformat(stamp)
        except ValueError:
            return None
        return moment if moment.tzinfo else moment.replace(
            tzinfo=datetime.timezone.utc)

    a, b = parse(earlier), parse(later)
    if a is None or b is None:
        return 0.0
    return max(0.0, (b - a).total_seconds() / 86400.0)


def replay(periods, start_phi, scale, tau=glicko.TAU):
    """Play the whole history through Glicko-2. Returns {bot_id: Rating}, and the
    deviation a bot that debuted in the final period ended up with."""
    start_rd = start_phi * scale
    ratings = {}
    last_played = {}
    debut_rd = None

    for index, (played_at, by_bot) in enumerate(periods):
        snapshot = dict(ratings)
        updates = {}
        for bot_id, results in by_bot.items():
            before = snapshot.get(
                bot_id, glicko.Rating(glicko.START_RATING, start_rd,
                                      glicko.START_SIGMA))
            pairs = [
                (snapshot.get(other, glicko.Rating(glicko.START_RATING, start_rd,
                                                   glicko.START_SIGMA)), score)
                for other, score in results
            ]
            days = _days_between(last_played.get(bot_id, played_at), played_at)
            updates[bot_id] = glicko.update(before, pairs, days=days, tau=tau,
                                            scale=scale, max_rd=start_rd)
            # A bot appearing for the first time in this period: what its
            # deviation looks like after one period of real play.
            if bot_id not in snapshot and index >= len(periods) - 1:
                debut_rd = updates[bot_id].rd
        for bot_id, rating in updates.items():
            ratings[bot_id] = rating
            last_played[bot_id] = played_at
    return ratings, debut_rd


def first_period_rd(periods, start_phi, scale, tau=glicko.TAU):
    """The deviation a newcomer would carry out of a single period of real play."""
    if not periods:
        return start_phi * scale
    _played_at, by_bot = periods[-1]
    busiest = max(by_bot.values(), key=len)
    start_rd = start_phi * scale
    fresh = glicko.Rating(glicko.START_RATING, start_rd, glicko.START_SIGMA)
    # Against a typical field: everybody at the start rating, modestly settled.
    # The opponents' deviation is in internal units (0.46, Glickman's 80/173.7178)
    # so that the answer does not move when `scale` does — the whole point of the
    # fit is that `scale` is the free variable.
    opponents = [(glicko.Rating(glicko.START_RATING, 0.46 * scale), s)
                 for _other, s in busiest]
    return glicko.update(fresh, opponents, days=1.0, tau=tau, scale=scale,
                         max_rd=start_rd).rd


def solve_scale(periods, live, spread=None, anchor_top=None):
    """The `SCALE` that stretches the replayed field to the asked-for shape.

    Either target works and they are the same knob seen from two ends: `spread`
    fixes the distance from the bottom bot to the top one, `anchor_top` fixes where
    the top bot lands. `anchor_top` is usually the one that matters, because the
    title floors are absolute — a ladder can have exactly the right spread and
    still sit entirely below `im`.

    One replay at unit scale is enough: `SCALE` multiplies the internal ratings
    afterwards and does not feed back into them.
    """
    ratings, _debut = replay(periods, START_PHI, 1.0)
    shown = [r.rating - glicko.START_RATING
             for i, r in ratings.items() if i in live]
    if len(shown) < 2:
        raise SystemExit("fewer than two live bots in the replay")
    internal_spread = max(shown) - min(shown)
    if not internal_spread:
        raise SystemExit("the replay separated no bots at all")
    if anchor_top is not None:
        if max(shown) <= 0:
            raise SystemExit("no live bot replayed above the starting rating")
        return (anchor_top - glicko.START_RATING) / max(shown), internal_spread
    return spread / internal_spread, internal_spread


def newcomer_climb(periods, ratings, live, bot_id, scale, start_rd, limit=8):
    """Re-enter an established bot cold and watch where each `k` would rank it.

    The honest test of a starting deviation: this bot's results are known to be
    worth a place at the top, so anything but a fast climb is the ranking holding
    back a bot that has already proved itself.
    """
    fresh = glicko.Rating(glicko.START_RATING, start_rd, glicko.START_SIGMA)
    rows = []
    newbie = fresh
    for _played_at, by_bot in periods[-limit:]:
        if bot_id not in by_bot:
            continue
        pairs = [(ratings.get(other, fresh), score)
                 for other, score in by_bot[bot_id]]
        newbie = glicko.update(newbie, pairs, days=1.0, scale=scale,
                               max_rd=start_rd)
        ranks = []
        for k in CANDIDATE_K:
            mine = newbie.rating - k * newbie.rd
            ranks.append(1 + sum(1 for i, r in ratings.items()
                                 if i in live and i != bot_id
                                 and r.rating - k * r.rd > mine))
        rows.append((newbie.rating, newbie.rd, ranks))
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", default=os.environ.get("JANKEN_DB", "janken.db"))
    p.add_argument("--spread", type=float, default=900.0,
                   help="display spread to aim for, top bot minus bottom "
                        "(default 900, roughly what the Elo ladder shows today)")
    p.add_argument("--k", type=float, default=1.0,
                   help="deviations below the rating to rank on (default 1)")
    p.add_argument("--anchor-top", type=float, default=None,
                   help="instead of a spread, set SCALE so the strongest bot "
                        "lands on this rating — the target to use when the title "
                        "floors are what has to stay put")
    p.add_argument("--score", choices=sorted(SCORERS), default="match",
                   help="what a match is worth: 'match' (who won) or 'round' "
                        "(the share of rounds). Default 'match' — see the module "
                        "docstring for why the round fraction cannot carry the "
                        "ladder on its own")
    args = p.parse_args()
    if not os.path.isfile(args.db):
        raise SystemExit("no such registry: %s" % args.db)

    conn = sqlite3.connect(args.db)
    try:
        names = {i: n for i, n in conn.execute("SELECT id, name FROM bots")}
        # `disabled` arrived later than some registries; this script never writes,
        # so it reads around a database that has not been migrated yet rather than
        # migrating one as a side effect of being run.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(bots)")}
        hidden = " AND disabled = 0" if "disabled" in columns else ""
        live = {i for (i,) in conn.execute(
            "SELECT id FROM bots WHERE banned = 0 AND held = 0" + hidden)}
        periods = load_periods(conn, SCORERS[args.score])
    finally:
        conn.close()
    if not periods:
        raise SystemExit("no recorded matches to fit against")

    print()
    print("Replaying %d rating periods from %s, scoring by %s"
          % (len(periods), os.path.basename(args.db), args.score))

    scale, internal_spread = solve_scale(periods, live, args.spread,
                                         args.anchor_top)
    start_rd = START_PHI * scale
    if args.anchor_top is not None:
        print("  internal spread %.4f  ->  SCALE = %.1f to put the top bot at %.0f"
              % (internal_spread, scale, args.anchor_top))
    else:
        print("  internal spread %.4f  ->  SCALE = %.1f for a %.0f-point ladder"
              % (internal_spread, scale, args.spread))
    print()
    print("  SCALE    = %.1f" % scale)
    print("  START_RD = %.0f   (start_phi %.4f, held at Glickman's value)"
          % (start_rd, START_PHI))
    print()

    ratings, _debut = replay(periods, START_PHI, scale)
    rows = sorted(((r.conservative(args.k), r, names.get(i, i))
                   for i, r in ratings.items() if i in live), reverse=True)
    print("  %-4s %-16s %12s %9s %7s"
          % ("#", "bot", "conservative", "rating", "RD"))
    print("  " + "-" * 54)
    for rank, (cons, r, name) in enumerate(rows, 1):
        print("  %-4d %-16s %12.0f %9.0f %7.0f"
              % (rank, name, cons, r.rating, r.rd))
    print()
    tops = [r.rating for _c, r, _n in rows]
    rds = sorted(r.rd for _c, r, _n in rows)
    print("  ladder spans %.0f to %.0f (%.0f points); settled RD %.0f-%.0f, "
          "median %.0f" % (min(tops), max(tops), max(tops) - min(tops),
                           rds[0], rds[-1], rds[len(rds) // 2]))
    print()

    # What the starting deviation actually costs, which is the only honest way to
    # choose `k`: a bot whose results are known to be top-of-field, entering cold.
    best = max((r.rating, i) for i, r in ratings.items() if i in live)[1]
    climb = newcomer_climb(periods, ratings, live, best, scale, start_rd)
    if climb:
        print("  A newcomer playing exactly %s's results, entering cold:"
              % names.get(best, best))
        print("  %-8s %8s %7s   %s"
              % ("period", "rating", "RD",
                 "rank at k = " + " / ".join("%g" % k for k in CANDIDATE_K)))
        for n, (rating, rd, ranks) in enumerate(climb, 1):
            print("  %-8d %8.0f %7.0f   %s"
                  % (n, rating, rd, "  ".join("#%-4d" % r for r in ranks)))
        print()
        print("  (k is a ranking choice, not a rating one: it changes who is shown "
              "on top,\n   never what a bot's rating is or what title it earns.)")
    print()


if __name__ == "__main__":
    main()
