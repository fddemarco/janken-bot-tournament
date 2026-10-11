"""Norm events: the selective tournament a title is earned in.

FIDE's composition rules — caps on opponents from one federation, a minimum share
of titled opponents, a floor on the opponents' average — do their work because
*players choose which events to enter*. Janken has one pool and a round robin, so
every bot faces the same field and almost every composition metric comes out
identical for everyone: they pass or fail the whole field at once, and cannot tell
a farmer from an honest bot.

So the selectivity has to be real rather than simulated. A norm event is an extra
rated tournament over a field chosen to satisfy composition, and a norm is then
plain performance across that field.

Two different worries, kept apart because conflating them cost real accuracy once.
Whether a small group could *constitute* the event is a property of the field, and
the diversity floor answers it. Whether a bot's score came from bots its own owner
controls is a property of that bot, and `own_opposition_ok` answers it when the
norm is awarded — the same place FIDE puts it. Selection is left to pick purely on
strength.

Nothing here touches the registry; selection takes bots and hands back bots, which
keeps the rules testable against a handful of stub objects.
"""

import collections
import dataclasses
import os
from typing import List, Optional, Sequence, Tuple

from . import rating as elo
from . import reference

# A norm needs at least this many games, so the field needs one more bot than that
# for a round robin. FIDE's minimum is nine and there is no reason to differ.
MIN_NORM_GAMES = 9
MIN_NORM_FIELD = MIN_NORM_GAMES + 1

# Sixteen bots is 120 matches and about the scale of a FIDE invitational. Bigger
# would make the event slower without making it more selective.
MAX_NORM_FIELD = 16

# Composition, ported from FIDE with federation read as submitter — but *not* with
# FIDE's numbers, because the units are not comparable. A federation is a national
# pool of thousands of independent players, so capping one at 3/5 of a field still
# leaves the field made of strangers. A submitter here is one person, holding at
# most `ACTIVE_PER_SUBMITTER` bots. Ported literally, FIDE's caps would let *three*
# colluding people constitute an entire sixteen-bot event and still satisfy "at
# least three federations represented".
#
# So the caps are set against how many *people* an event spans, since the person is
# the unit of collusion:
MIN_SUBMITTER_SHARE = 1.0 / 3.0        # distinct submitters ≥ a third of the field
MIN_SUBMITTERS_FLOOR = 3               # ...and never fewer than this
MIN_TITLED_SHARE = 1.0 / 2.0           # ≥half the field already titled

# No single submitter may supply half or more of the field. Deliberately loose:
# `ACTIVE_PER_SUBMITTER` caps anyone at five bots in play, so for any field of ten
# or more this cannot bind in a registry where that cap was applied. It is a
# backstop for the case where it wasn't — a registry predating it, or one seeded by
# hand — and being non-binding is the point. The tighter version of this rule was
# what distorted selection; see `OWN_OPPONENT_DIVISOR`.
MAX_SHARE_ONE_SUBMITTER = 1.0 / 2.0

# At most one in this many of a bot's *own opponents* may share its submitter, or
# the event cannot certify it. FIDE applies its concentration rule per applicant —
# "a maximum of 3/5 of the opponents may come from the applicant's federation" —
# and this is that rule, at a ratio suited to a submitter being one person rather
# than a nation of thousands.
#
# It belongs here rather than on the field for two reasons. It is what the attack
# actually looks like: a bot inflating its performance on free wins from bots its
# own owner controls. And a field-level cap distorts the selection it is bolted
# onto — measured on the live registry, capping any submitter at a quarter of a
# ten-bot field excluded the third of one entrant's genuine top three (1786) and
# pulled in a reference bot (1207) to fill the slot, dragging the average to 1641,
# just under the 1650 an IM norm needs. The cap was wrecking the event it was
# protecting, and no amount of rounding fixes that: the rule was in the wrong
# place. Per bot, selection stays free to pick the strongest field, and a bot whose
# opposition is too much its own simply does not earn a norm from it.
#
# Concentration of the *field* needs no rule of its own: `ACTIVE_PER_SUBMITTER`
# already caps anyone at five bots in play, and the diversity floor above requires
# distinct submitters numbering a third of the field.
OWN_OPPONENT_DIVISOR = 3

# The titled quota raises the bar without being a wall on its own: `cm` and
# `master` are rating-only, so a determined farmer can reach them and count toward
# it. What stops a cartel constituting an event is the diversity floor; what stops
# one entrant farming a norm inside an event is `OWN_OPPONENT_DIVISOR`.


def min_submitters(size: int) -> int:
    """Distinct submitters a field of this size must span."""
    return max(MIN_SUBMITTERS_FLOOR, -(-size // 3))     # ceil(size / 3)


def own_opposition_ok(own: int, total: int) -> bool:
    """Whether few enough of a bot's opponents shared its submitter.

    Integer arithmetic on purpose: `3/9 <= 1/3` sits exactly on a floating-point
    knife-edge, and a bot with three siblings among nine opponents is a real case
    that must not turn on rounding.
    """
    return own * OWN_OPPONENT_DIVISOR <= total

# How often an event runs, against a ladder that runs every half hour.
#
# The spacing is not ceremony. Three norms is stronger evidence than one only if
# the fields differ, and two events an hour apart would select nearly the same ten
# bots — so a bot clearing the bar once clears it three times, and the requirement
# collapses into one norm counted thrice. What the interval buys is turnover: new
# submissions arrive, ratings move, and the invited ten actually change between
# events. Setting it to 0 holds one every run, which is a choice rather than an
# error, but it makes the third norm worth about as much as the first.
NORM_INTERVAL_HOURS = 24.0

# Operators tune it the way they tune everything else here — an env var written
# into the systemd unit by install.sh (see JANKEN_ROUNDS, JANKEN_GAMES).
NORM_INTERVAL_ENV = "JANKEN_NORM_INTERVAL_HOURS"


def norm_interval_hours(raw: Optional[str] = None) -> float:
    """Hours between norm events: `$JANKEN_NORM_INTERVAL_HOURS`, else the default.

    Junk and negative values fall back to the default rather than propagating: a
    typo in a unit file must not stop the periodic run, and must not turn every
    tournament into a norm event by accident either.
    """
    if raw is None:
        raw = os.environ.get(NORM_INTERVAL_ENV, "")
    try:
        hours = float(str(raw).strip())
    except (TypeError, ValueError):
        return NORM_INTERVAL_HOURS
    return hours if hours >= 0 else NORM_INTERVAL_HOURS


def submitter_key(bot) -> Tuple[str, str]:
    """The identity the caps and the diversity count are keyed on.

    An unattributed bot counts as its own distinct entrant rather than pooling
    with every other unattributed one: the reference bots are independent by
    construction, and folding them into a single phantom entrant would fail the
    concentration cap for a collusion that isn't there.
    """
    who = (bot.submitter or "").strip().lower()
    return ("submitter", who) if who else ("anonymous", bot.id)


def is_titled(bot) -> bool:
    """Whether this bot already holds a title, at any rung above the start."""
    return elo.TITLE_RANK.get(bot.league, 0) > 0


@dataclasses.dataclass
class Composition:
    """Whether a field may award norms, and if not, what is missing."""
    size: int
    submitters: int
    largest_submitter_share: float
    titled_share: float
    average_rating: float
    failures: List[str] = dataclasses.field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def why(self) -> str:
        """One line, for a log or a dry run."""
        if self.ok:
            return ("%d bots, %d submitters, %.0f%% titled, average rating %.0f"
                    % (self.size, self.submitters, 100 * self.titled_share,
                       self.average_rating))
        return "; ".join(self.failures)


def composition(field: Sequence) -> Composition:
    """Check a candidate field against the composition rules.

    Only properties of the *event* are checked here. Two rules live elsewhere
    because they are not: the strength floor differs per norm level, so
    `rating.norm_earned` applies it; and how much of a bot's opposition was its own
    submitter's is per bot, so `own_opposition_ok` applies it when the norm is
    awarded.
    """
    size = len(field)
    counts = collections.Counter(submitter_key(b) for b in field)
    submitters = len(counts)
    largest = (max(counts.values()) / size) if size else 0.0
    titled = (sum(1 for b in field if is_titled(b)) / size) if size else 0.0
    average = (sum(b.rating for b in field) / size) if size else 0.0

    failures = []
    if size < MIN_NORM_FIELD:
        failures.append("only %d bots, need %d so each plays %d games"
                        % (size, MIN_NORM_FIELD, MIN_NORM_GAMES))
    if size and submitters < min_submitters(size):
        failures.append("only %d distinct submitters, need %d for a field of %d"
                        % (submitters, min_submitters(size), size))
    if size and max(counts.values()) * 2 >= size:
        failures.append("one submitter holds %d of %d bots, which is half the field"
                        % (max(counts.values()), size))
    if titled < MIN_TITLED_SHARE:
        failures.append("only %.0f%% of the field is titled, need %.0f%%"
                        % (100 * titled, 100 * MIN_TITLED_SHARE))
    return Composition(size=size, submitters=submitters,
                       largest_submitter_share=largest, titled_share=titled,
                       average_rating=average, failures=failures)


def _fill(ranked: Sequence, size: int) -> Optional[List]:
    """The strongest `size` bots, or None if there aren't that many."""
    return list(ranked[:size]) if len(ranked) >= size else None


def select_field(candidates: Sequence,
                 max_size: int = MAX_NORM_FIELD,
                 min_size: int = MIN_NORM_FIELD) -> Tuple[Optional[List], Composition]:
    """Pick the *strongest* field that qualifies, or explain why none does.

    Invitation is earned by ladder position, and nothing else — strong events
    invite strong players, and any bot in contention for a title is in the top
    slice anyway, since the peak-rating floor gates it regardless. Selection is not
    asked to police who owns what: that is `own_opposition_ok`'s job when the norm
    is awarded, and mixing it in here is what made an earlier version pass over a
    1786 bot in favour of a 1207 one.

    Every allowed size is tried and the strongest qualifying field wins, rather
    than simply the largest. Largest-first is the intuitive rule and it is wrong:
    filling more slots means reaching further down the ladder, and because a norm
    also requires the *opponents* to average above a floor, a bigger field can
    award nothing where a smaller one awards freely. Measured on a real registry,
    a thirteen-bot field averaged 1580 and put the IM floor out of everyone's
    reach while a ten-bot field from the same candidates averaged 1636 and left it
    reachable for three of them. Small and strong is also what a norm
    invitational looks like over a board.

    Size is not monotone in strength either, because the per-submitter cap moves
    with it — one more slot can raise the cap enough to admit another strong bot,
    or force a weak one in — so every size really does have to be tried.

    The organizer's reference bots are dropped before any of that, and it happens
    here rather than in the caller so that a new one cannot forget — the same
    reasoning `cupstore.visible()` uses for the reveal gate. They play the rated
    ladder, but a norm is a prize and none of them is trying to win one: `randy` is
    a coin flip, and it banked an International Master norm off the first event that
    ran. Excluding them costs less than it looks — they each counted as their own
    submitter, so they were propping up the diversity floor, and on a real registry
    the qualifying field still formed at ten bots averaging 1702 instead of 1706.

    Returns `(field, composition)`; `field` is None when nothing qualifies, and the
    composition returned is then the best attempt's, so a caller can say what was
    missing.
    """
    candidates = [b for b in candidates
                  if not reference.is_reference(b.name, b.submitter)]
    ranked = sorted(candidates, key=lambda b: (-b.rating, b.name))
    ceiling = min(max_size, len(ranked))
    qualifying = []
    attempted = None
    for size in range(ceiling, min_size - 1, -1):
        chosen = _fill(ranked, size)
        if chosen is None:
            continue
        found = composition(chosen)
        if found.ok:
            qualifying.append((found.average_rating, len(chosen), chosen, found))
        elif attempted is None:
            attempted = found
    if qualifying:
        # Strongest field; a tie goes to the bigger one, since more games is more
        # evidence for the same strength of opposition.
        qualifying.sort(key=lambda q: (q[0], q[1]), reverse=True)
        return qualifying[0][2], qualifying[0][3]
    return None, (attempted if attempted is not None
                  else composition(ranked[:ceiling]))
