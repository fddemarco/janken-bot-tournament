"""The chess titles a rating earns, and the performance maths behind norms.

Ranking used to be tournament points (3 a win, 1 a draw) *inside a league*,
which made a league a bracket: a bot only ever met its own rank, so every
promotion thinned the pool underneath it — promote the strong beginners and the
bottom league is left without competitors.

A rating replaced the points. There is one pool, everybody plays everybody, and a
result is worth what the opponent is worth: beating a grandmaster moves you a
lot, beating a bot you were expected to beat moves you barely at all. The old
leagues become *titles* — labels a bot earns and keeps, the way a chess master
stays a master through a bad season — so a title costs a bot nothing in
opponents.

The rating itself is Glicko-2 now and lives in `glicko.py`; this module keeps the
title ladder, the performance-rating maths norms are judged by, and the logistic
expectation both of those are written in. Elo's K-factor machinery is gone with
it — Glicko-2 sets a bot's step from its own deviation, so there is no period
budget left to split.

Every title needs a rating floor, and the top two need *norms* as well — a
performance in a selective event, which is what FIDE requires for IM and GM and
what stops a title being farmable. There was once a "gauntlet" of fixed reference
bots to beat decisively, described at the time as a chess norm; it was dropped
because naming the opponents in advance is the one thing a norm is not, so the
trial only re-played known bots for a badge nothing consulted. `norms.py` has the
real version: the composition rules bind the *event*, not a chosen opponent list.
"""

import math
from typing import Mapping, Optional, Sequence

from . import glicko

# Chess numbers, so a title reads the way it does over a board: club players sit
# around here, and the titled ranks are a few hundred points up.
#
# Re-exported from `glicko` rather than restated: every title floor below is an
# offset from where a bot starts, so the two drifting apart would silently move
# every rung. Callers that speak of titles keep reading it from here.
START_RATING = glicko.START_RATING

# Titles, weakest first, stored as slugs: they end up in a CSS class, a filter
# value and a SQLite column, none of which want spaces. `novice` is where every
# bot starts; every rung above it is a floor on the *peak* rating.
TITLES = ("novice", "cm", "master", "im", "gm")
TITLE_RANK = {name: i for i, name in enumerate(TITLES)}

TITLE_NAMES = {
    "novice": "Novice",
    "cm": "Candidate Master",
    "master": "Master",
    "im": "International Master",
    "gm": "Grandmaster",
}
# Short forms, for fixed-width tables where the full name would dominate a row.
TITLE_ABBR = {
    "novice": "Novice", "cm": "CM", "master": "Master", "im": "IM", "gm": "GM",
}

# Peak rating that earns each title, strongest first. Everyone starts at 1500, so
# these are "beat good bots consistently" marks, not participation trophies — and
# the top rung is meant to stay rare.
#
# `cm` sits at 1600 — a hundred points, four or five clear wins over the field.
#
# It used to be argued for as the *cheapest* rung, on the grounds that whoever
# led the standings had to hold a title or the Title column would contradict the
# order it sat in. That invariant is gone, and deliberately: the standings rank on
# `rating - rd`, titles are read off the untouched peak, and the two answer
# different questions. A newcomer wiping the field tops the table on current form
# while its peak has not yet cleared a floor; a former champion keeps its title
# with its form long gone. Neither is a contradiction to fix — it is the whole
# reason for keeping a title and a ranking apart.
RATING_TITLES = (
    ("gm", 2100.0),
    ("im", 1900.0),
    ("master", 1700.0),
    ("cm", 1600.0),
)

# The top two rungs also need *norms* — performances in a selective event, the way
# FIDE gates IM and GM while leaving CM and FM to rating alone. A rating says how
# well a bot did; a norm says who it did it against, and only the second is
# expensive to fake. See `norms.py` for the event and its composition rules.
#
# The numbers are offsets from 1500 rather than FIDE's absolutes, because FIDE's
# encode chess's own history: its system is not zero-sum (K tiers of 40/20/10, a
# rating floor that swallows losses, and a one-off correction in March 2024 that
# raised the floor and handed every player under 2000 a top-up), so its thresholds
# sit on a distribution that has drifted and been adjusted by hand. Every bot here
# enters at the pool average instead, which makes 1500 a permanent anchor — so
# `rating - 1500` reads directly as a *match* win rate against an average bot:
#
#   cm 1600 = 64%   master 1700 = 76%   im 1900 = 91%   gm 2100 = 97%
#
# Match win rate, not round win rate, and the distinction is the whole reason the
# ladder is scored the way it is. Nobody can beat a uniform-random opponent above
# half the *rounds*, so a round fraction measures how exploitable the field is and
# collapses toward 0.5 as the field improves — floors written against it would
# become unreachable by construction. A match of hundreds of rounds amplifies a
# small round edge into a decisive record, which is what keeps these floors
# meaning the same thing as the bots get better. See `glicko.py`.
#
# There is a ceiling, and it is `randy`: every bot plays it, no bot can beat it, so
# the best possible showing in a field of N is `(N - 1.5)/(N - 1)`. At 19 bots that
# is 2118 and at 26 it is 2176, so `gm` sits just inside what a flawless bot could
# reach today and gets a little more room as the field grows. That margin is thin
# on purpose — a title nobody can reach is a decoration, and one anybody reaches is
# not a title.
#
# title -> (norms needed, performance floor, opponents' average floor)
NORM_REQUIREMENTS = {
    "im": (2, 1950.0, 1650.0),   # perform at 92.5%, against a field averaging 70%
    "gm": (3, 2150.0, 1750.0),   # perform at 97.5%, against a field averaging 81%
}

# A perfect (or blank) score has no finite performance rating, so it is pinned
# this far from the opponents' average. FIDE's table stops at 800; that is scaled
# here like every other number, because our ladder is compressed (a GM floor 600
# above the start rather than 1000).
#
# Keeping it *below* the norm offsets is what makes the two gates independent. At
# 800 a clean sweep of a field of 1500s would score 2300 and clear the GM floor of
# 2150 on its own, leaving the opponents'-average gate as the only thing standing
# between a farmer and a title. At 400 the sweep is worth 1900, under both norm
# floors, so performance and opposition each have to be real.
PERFORMANCE_CLAMP = 400.0


def expected(rating: float, opponent: float) -> float:
    """The share of a match a rating gap predicts (0..1).

    A logistic on `glicko.SCALE`, *not* Elo's 400-point curve, and the distinction
    is not cosmetic. Elo's `10 ** (gap/400)` is a logistic with a scale of
    400/ln(10) = 173.7178 baked in. Our ladder is fitted at a different scale, so
    keeping the 400-point form here would leave every performance rating — and
    therefore every norm — measured with a ruler ~9% off the one the ratings are
    printed on. Read from `glicko` so the two cannot drift apart again.

    A 400-point edge predicts ~0.93 at the current scale.
    """
    return 1.0 / (1.0 + math.exp((opponent - rating) / glicko.SCALE))


def performance_rating(score: float, opponent_ratings: Sequence[float]) -> float:
    """The rating at which `score` would have been exactly the expected total.

    `score` is the sum of a bot's match scores (1 a win, 0.5 a draw) over the
    opponents given. Solving `sum(expected(Rp, r)) == score` is monotone in `Rp`,
    so it bisects; a clean sweep or a whitewash has no finite answer and lands on
    the `PERFORMANCE_CLAMP` bound.

    This is what makes farming worthless for a title: performance is bounded by
    who you played, so beating a crowd of 1500s cannot look like a 2150
    performance however many of them there are.
    """
    if not opponent_ratings:
        raise ValueError("performance rating needs at least one opponent")
    average = sum(opponent_ratings) / len(opponent_ratings)
    lo, hi = average - PERFORMANCE_CLAMP, average + PERFORMANCE_CLAMP

    def total(rp):
        return sum(expected(rp, r) for r in opponent_ratings)

    if score <= total(lo):
        return lo
    if score >= total(hi):
        return hi
    for _ in range(80):                 # ~1e-22 of the starting bracket
        mid = (lo + hi) / 2.0
        if total(mid) < score:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def score_for_performance(performance: float,
                          opponent_ratings: Sequence[float]) -> Optional[float]:
    """Score needed against these opponents to perform at `performance`.

    The inverse of `performance_rating`, and the number that says whether a norm
    is demanding or merely theoretical: a floor is only meaningful next to what it
    asks of a bot.

    None means unreachable, and the bound is the clamp rather than the arithmetic:
    a perfect score does not produce an unbounded figure, it produces
    `avg + PERFORMANCE_CLAMP`, so any floor above that cannot be met however well a
    bot plays. Summing expectations would never say so on its own — `expected` is
    always under 1, so the total is always under the game count.
    """
    if not opponent_ratings:
        return None
    average = sum(opponent_ratings) / len(opponent_ratings)
    if performance > average + PERFORMANCE_CLAMP:
        return None
    return sum(expected(performance, r) for r in opponent_ratings)


def norm_earned(performance: float, field_avg: float) -> Optional[str]:
    """The strongest norm this performance earns, or None.

    Both gates have to clear: the performance floor says the bot played well, the
    field average says the event was worth playing well in.

    The strength gate reads the average of the *whole event field*, not of a bot's
    own opponents, and that is a deliberate departure from FIDE. Excluding
    yourself from your opponents' average is harmless in a chess invitational
    where everyone is within a hundred points of everyone else. In a single pool
    it inverts the rule: in a field spanning 1080 to 2004, the strongest bot's
    opponents average *below* the field and the weakest bot's average above it, so
    the gate would open for exactly the bots that cannot clear the performance
    floor and close for the ones that can. The event is either strong enough or it
    is not, and that is a property of the event.
    """
    for title, (_needed, perf_floor, avg_floor) in sorted(
            NORM_REQUIREMENTS.items(), key=lambda kv: -TITLE_RANK[kv[0]]):
        if performance >= perf_floor and field_avg >= avg_floor:
            return title
    return None


def norms_toward(title: str, norms: Mapping[str, int]) -> int:
    """How many of `norms` count toward `title`.

    A norm at a higher level also satisfies a lower one, as in FIDE — a GM norm
    is an IM norm too, so a bot never has to go back and earn the lesser one.
    """
    return sum(count for held, count in norms.items()
               if TITLE_RANK.get(held, -1) >= TITLE_RANK.get(title, -1))


def title_for_rating(peak_rating: float) -> str:
    """The strongest title this peak rating *reaches* (`novice` if none).

    The rating half of the requirement only. `im` and `gm` also need norms, so
    this is not on its own what a bot is awarded — see `title_earned`.
    """
    for name, floor in RATING_TITLES:
        if peak_rating >= floor:
            return name
    return TITLES[0]


def title_earned(peak_rating: float, norms: Mapping[str, int]) -> str:
    """The strongest title actually earned: rating floor *and* norms.

    Walks down from the top, so a bot with a GM peak but only two norms is an IM
    if it has the IM norms for it, rather than dropping all the way to novice.
    """
    for name, floor in RATING_TITLES:
        if peak_rating < floor:
            continue
        needed = NORM_REQUIREMENTS.get(name)
        if needed is None or norms_toward(name, norms) >= needed[0]:
            return name
    return TITLES[0]


def best_title(*titles: str) -> str:
    """The strongest of several titles — titles are kept, never taken away."""
    return max(titles, key=lambda t: TITLE_RANK.get(t, -1))


def name(title: str) -> str:
    """Full display name, for prose and announcements."""
    return TITLE_NAMES.get(title, title)


def abbr(title: str) -> str:
    """Short display name, for table cells."""
    return TITLE_ABBR.get(title, title)


def floor_for(title: str):
    """The rating floor that earns `title`, or None for `novice`, the start."""
    return dict(RATING_TITLES).get(title)


def norms_required(title: str) -> int:
    """How many norms `title` needs (0 for the rating-only rungs)."""
    needed = NORM_REQUIREMENTS.get(title)
    return needed[0] if needed else 0


def ladder() -> str:
    """The ladder as one line of prose, e.g. for a caption or a help string.

    Spells out the norms as well as the floors, since a rating alone no longer
    reaches the top two rungs and a caption that implied otherwise would be the
    only place in the system still saying so.
    """
    parts = []
    for title in TITLES:
        floor = floor_for(title)
        if not floor:
            parts.append(name(title))
            continue
        needed = norms_required(title)
        rung = "%s %d+" % (name(title), floor)
        if needed:
            rung += " & %d norm%s" % (needed, "" if needed == 1 else "s")
        parts.append(rung)
    return " → ".join(parts)
