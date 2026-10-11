"""Cup: a single-elimination knockout, seeded by rating and rated by nothing.

The league (`tournament.py` + `registry.record_tournament`) is the one that
counts: everyone plays everyone, every match moves your rating. This is the other
competition. One bad afternoon ends your run, and none of it touches your
rating — nothing in this module calls `record_tournament`, which is the only
path that writes ratings and career stats, so a cup cannot move the ladder
because it never reaches the code that would.

The league still matters here, in the one direction that makes the cup worth
entering: rating seeds the draw, so finishing high keeps the strong bots apart
until late. Qualification and seeding are the league's reward; the trophy is
the cup's.

Rounds are indexed by **bots remaining** — 64, 32, 16, 8, 4, 2 — never by an
ordinal. The final is `round_size == 2` whatever the field size, so every rule
keyed off the stage (match length, broadcast treatment) reads the same at 16
entrants as at 64 and needs no per-field-size table.
"""

import concurrent.futures
import dataclasses
import os
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import naming
from . import rating as elo
from .match import DEFAULT_ROUND_TIMEOUT, DEFAULT_ROUNDS, MatchResult, play_match

# Most bots the bracket will take. Beyond this, the lowest-rated miss out —
# see `qualify`.
MAX_FIELD = 64

# A tie after the scheduled rounds goes to sudden death; this caps how long
# that can run before the seed decides it. Two identical deterministic bots
# draw every round forever, so the cap is what keeps a bracket finite.
SUDDEN_DEATH_CAP = 100

STAGE_NAMES = {2: "Final", 4: "Semi-final", 8: "Quarter-final"}
THIRD_PLACE = "Third place"

# The organizer's own bots belong in the league — everybody should have to beat
# them — but not in a cup, which is a knockout between the entries people actually
# wrote. A reference bot lifting the trophy is a worse story than any real result.
# Who they are now lives in `reference`, because norm events need the same answer
# and importing the cup engine to get it would be backwards. Excluded by default;
# the CLI's --include-reference puts them back.


def rounds_for(round_size: int, base: int = DEFAULT_ROUNDS) -> int:
    """Match length for a stage. Longer as the field narrows: with only 15
    matches in a 16-bot cup there is time to spare, and a longer match is one
    where prediction beats luck — so the deciding matches are the ones where
    reading your opponent pays most.

    Multiples of an ordinary rated match rather than fixed numbers. These were
    100/200/300 while a league match was 100 rounds; once the league moved to 200
    the early rounds of a cup were *shorter* than a routine match, which inverted
    the whole point of the rule. Tied to `base` they cannot drift apart again."""
    if round_size <= 2:
        return 3 * base
    if round_size <= 4:
        return 2 * base
    return base


def treatment(round_size: int) -> str:
    """How the broadcast should show a stage.

    Wide early rounds are a *reveal*: who advanced, nothing else. Sixteen R32
    matches resolve in a few seconds instead of being animated one by one, so
    growing the field from 32 to 64 costs the show seconds rather than
    minutes. Real play starts once the field is down to 16.
    """
    if round_size > 16:
        return "reveal"
    if round_size > 8:
        return "grid"
    if round_size > 4:
        return "featured"
    # Semi-finals earn the full screen too, not just the final. By then the
    # field is down to bots that have won their way through, and there are few
    # enough matches left to show them one at a time — which is what a hero
    # round does when it holds more than one match.
    return "hero"


def stage_name(round_size: int, bracket_size: int) -> str:
    if round_size > bracket_size:
        return "Play-in"
    return STAGE_NAMES.get(round_size, "Round of %d" % round_size)


@dataclasses.dataclass
class Entrant:
    name: str
    path: str
    rating: float = elo.START_RATING
    title: str = elo.TITLES[0]
    seed: int = 0
    # The registry's id for this bot, "" for a guest the registry has never
    # seen. A cup used to identify entrants by name alone, which worked only
    # because the field was built from filename stems and stored submissions
    # carry their own id in the filename — so every bracket printed the bot's
    # name with a 64-character digest welded to it, and anything that needed to
    # find an entrant again (the champion's avatar) matched on that string.
    # Carrying the id means the name can be just the name.
    id: str = ""
    # Who submitted it, for the one case two entrants in a field share a name.
    # A cup is a public broadcast, so this is only ever rendered through
    # `naming.display_labels`, which shows it when it disambiguates and not
    # otherwise — the bracket is not the place to publish everybody's address.
    submitter: str = ""
    # The entrant's avatar, as a path relative to the app's static directory
    # ("avatars/<bot id>.png"), or "" for a bot without one. Stored rather than
    # derived because this module never sees a bot id, and resolved to a URL by
    # whoever renders it — the prefix static files are served under is the
    # dashboard's business, not the bracket's.
    image: str = ""


@dataclasses.dataclass
class CupMatch:
    round_size: int          # bots alive when this round starts
    slot: int                # position within the round, top of bracket first
    a: Optional[Entrant] = None
    b: Optional[Entrant] = None
    # Set instead of a/b while an entrant is undecided: the index in
    # Cup.matches of the earlier match whose winner fills this side. The whole
    # bracket is therefore a static graph built at plan time, which is what
    # lets the broadcast draw the empty bracket before a move is played.
    a_from: Optional[int] = None
    b_from: Optional[int] = None
    # Which side of the feeding match arrives: the winner, as everywhere in a
    # knockout, or the loser — which only the third-place playoff wants, and
    # which is the one place a bracket edge runs downward instead of onward.
    a_takes: str = "winner"
    b_takes: str = "winner"
    # Overrides the stage name derived from round_size. The playoff and the
    # final are both two-bot matches, so the bracket width cannot tell them
    # apart and the label has to be carried.
    label: str = ""
    result: Optional[MatchResult] = None
    winner: Optional[Entrant] = None
    decided_by: str = ""     # score | sudden death | forfeit | both forfeited | seed

    @property
    def loser(self) -> Optional[Entrant]:
        """Whoever did not go through. None until the match is decided, and
        None for a double forfeit only in the sense that the seed still picks
        a winner — the other side is the loser either way."""
        if self.winner is None or self.a is None or self.b is None:
            return None
        return self.b if self.winner is self.a else self.a


@dataclasses.dataclass
class Cup:
    name: str
    entrants: List[Entrant]
    bracket_size: int
    matches: List[CupMatch] = dataclasses.field(default_factory=list)
    champion: Optional[Entrant] = None

    def rounds(self) -> List[Tuple[int, List[int]]]:
        """[(round_size, [match index, ...]), ...], widest round first.

        Grouped by round_size *and* label, because a round is what gets
        revealed in one go and the third-place playoff should not go out with
        the final. They are both two-bot matches, so the width alone cannot
        separate them — the label is the only thing that says they are
        different occasions.
        """
        groups: List[Tuple[int, List[int]]] = []
        key = None
        for i, m in enumerate(self.matches):
            if key != (m.round_size, m.label):
                key = (m.round_size, m.label)
                groups.append((m.round_size, []))
            groups[-1][1].append(i)
        return groups

    def stage_of(self, match: CupMatch) -> str:
        return match.label or stage_name(match.round_size, self.bracket_size)


def _key(e: Entrant) -> str:
    """What labelling keys an entrant on. Its registry id, or its seed when it
    has none — a guest played from a directory is still a distinct entrant and
    still needs its own row in the map."""
    return e.id or "seed:%d" % e.seed


def entrant_labels(entrants: Sequence[Entrant]) -> Dict[str, str]:
    """Display labels for a whole field, keyed by `_key`.

    Decided against the entire field rather than one round at a time, so a bot
    is called the same thing in the quarter-final and the final. See
    `naming.display_labels` for the rule.
    """
    return naming.display_labels((_key(e), e.name, e.submitter) for e in entrants)


def label_of(e: Optional[Entrant], labels: Dict[str, str]) -> str:
    """One entrant's on-screen name, or "?" for an undecided slot."""
    if e is None:
        return "?"
    return labels.get(_key(e)) or e.name


def qualify(entrants: Sequence[Entrant], limit: int = MAX_FIELD) -> List[Entrant]:
    """The field, best-rated first, cut to what the bracket holds."""
    return sorted(entrants, key=lambda e: (-e.rating, e.name))[:limit]


def seed_order(size: int) -> List[int]:
    """Seed positions for a power-of-two bracket, in slot order.

    The standard construction: start with [1] and repeatedly pair every seed
    already placed with its complement in the doubled bracket. Gives
    [1, 2] -> [1, 4, 2, 3] -> [1, 8, 4, 5, 2, 7, 3, 6] ..., so 1 meets the
    weakest survivor at every stage and 1 and 2 can only meet in the final.
    """
    order = [1]
    while len(order) < size:
        n = len(order) * 2
        paired: List[int] = []
        for seed in order:
            paired.append(seed)
            paired.append(n + 1 - seed)
        order = paired
    return order


def plan(entrants: Sequence[Entrant], name: str = "",
         third_place: bool = True) -> Cup:
    """Draw the whole bracket up front, before anything is played.

    With ``third_place`` the two beaten semi-finalists meet for third, played
    immediately before the final. Skipped when there are no semi-finals to lose
    — a bracket of two is a final and nothing else.
    """
    if len(entrants) < 2:
        raise ValueError("a cup needs at least 2 entrants, got %d" % len(entrants))

    ranked = sorted(entrants, key=lambda e: (-e.rating, e.name))
    seeded = [dataclasses.replace(e, seed=i + 1) for i, e in enumerate(ranked)]
    n = len(seeded)

    # Bracket size is the largest power of two the field *fills*, with the
    # overflow settled in a play-in. Rounding up instead would leave the top
    # seeds with byes — at 20 entrants, twelve empty slots sitting on screen.
    bracket_size = 1 << (n.bit_length() - 1)
    play_in = n - bracket_size

    matches: List[CupMatch] = []
    direct = seeded[: bracket_size - play_in]
    fringe = seeded[bracket_size - play_in:]     # exactly 2 * play_in of them

    # Play-in: the lowest seeds pair off highest-against-lowest, and each
    # winner inherits the bracket seed line of the better seed in its pair.
    feeder: Dict[int, int] = {}                  # bracket seed -> match index
    for i in range(play_in):
        feeder[len(direct) + 1 + i] = len(matches)
        matches.append(CupMatch(round_size=n, slot=i, a=fringe[i], b=fringe[-1 - i]))

    order = seed_order(bracket_size)
    first_bracket_round = len(matches)
    for slot in range(bracket_size // 2):
        m = CupMatch(round_size=bracket_size, slot=slot)
        for side, seed in (("a", order[2 * slot]), ("b", order[2 * slot + 1])):
            if seed in feeder:
                setattr(m, side + "_from", feeder[seed])
            else:
                setattr(m, side, direct[seed - 1])
        matches.append(m)

    # Everything after: winners of adjacent matches meet.
    previous = list(range(first_bracket_round, len(matches)))
    size = bracket_size // 2
    while size >= 2:
        current: List[int] = []
        for slot in range(size // 2):
            current.append(len(matches))
            matches.append(CupMatch(round_size=size, slot=slot,
                                    a_from=previous[2 * slot],
                                    b_from=previous[2 * slot + 1]))
        if size == 2 and third_place and len(previous) == 2:
            # `previous` is the semi-final pair, and the playoff takes the two
            # bots they knocked out. Inserted *before* the final so the two
            # two-bot matches form one round that plays third place first and
            # the final last — which also keeps matches[-1] the decider, where
            # the champion is read from. Nothing references the final's index,
            # and the semis sit before the insertion point, so shifting it
            # along by one breaks no edge.
            matches.insert(current[0], CupMatch(
                round_size=size, slot=0, label=THIRD_PLACE,
                a_from=previous[0], b_from=previous[1],
                a_takes="loser", b_takes="loser"))
        previous, size = current, size // 2

    return Cup(name=name, entrants=seeded, bracket_size=bracket_size, matches=matches)


def decide(match: CupMatch, result: MatchResult) -> Tuple[Entrant, str]:
    """Who advances, and why.

    Total by construction: every possible result maps to exactly one winner.
    A knockout has no way to recover from an undecided match — the bracket
    simply stalls — so there is no branch here that returns None.

    Order matters. A bot that never started cannot win on score, and two dead
    bots cannot play a decider, so the seed (earned in the league) is the last
    word rather than the first.
    """
    a, b = match.a, match.b
    if result.forfeit_a and result.forfeit_b:
        return _better_seed(a, b), "both forfeited"
    if result.forfeit_a:
        return b, "forfeit"
    if result.forfeit_b:
        return a, "forfeit"
    if result.wins_a != result.wins_b:
        winner = a if result.wins_a > result.wins_b else b
        return winner, "sudden death" if result.sudden_death else "score"
    # Still level, so sudden death ran out of rope: two bots that drew every
    # decider are genuinely inseparable, and the better seed goes through.
    return _better_seed(a, b), "seed"


def _better_seed(a: Entrant, b: Entrant) -> Entrant:
    return a if a.seed < b.seed else b


def run(
    cup: Cup,
    timeout: float = DEFAULT_ROUND_TIMEOUT,
    workers: Optional[int] = None,
    on_match: Optional[Callable[[CupMatch], None]] = None,
) -> Optional[Entrant]:
    """Play a planned cup to completion, filling it in place.

    Rounds are inherently sequential — the next one needs this one's winners —
    but the matches inside a round are independent, so each round runs across
    a thread pool. Peak width is half the bracket.
    """
    targets = feed_map(cup)
    for _round_size, indices in cup.rounds():
        for index, result in _play_round(cup, indices, timeout, workers):
            match = cup.matches[index]
            match.result = result
            match.winner, match.decided_by = decide(match, result)
            send_on(cup, index, targets)
            if on_match:
                on_match(match)
    cup.champion = cup.matches[-1].winner
    return cup.champion


def feed_map(cup: Cup) -> Dict[int, List[Tuple[int, str, str]]]:
    """match index -> [(match it feeds, which side, winner|loser), ...]"""
    targets: Dict[int, List[Tuple[int, str, str]]] = {}
    for i, m in enumerate(cup.matches):
        for side, source, takes in (("a", m.a_from, m.a_takes),
                                    ("b", m.b_from, m.b_takes)):
            if source is not None:
                targets.setdefault(source, []).append((i, side, takes))
    return targets


def send_on(cup: Cup, index: int, targets):
    """Push a decided match's winner (and loser) into the slots they feed."""
    match = cup.matches[index]
    for target, side, takes in targets.get(index, ()):
        setattr(cup.matches[target], side,
                match.winner if takes == "winner" else match.loser)


def _play_round(cup, indices, timeout, workers):
    """Yield (match index, MatchResult) for one round, played concurrently."""
    for i in indices:
        m = cup.matches[i]
        if m.a is None or m.b is None:
            raise RuntimeError(
                "match %d (%s) still has an empty slot — the bracket did not "
                "fill" % (i, cup.stage_of(m)))

    rounds = rounds_for(cup.matches[indices[0]].round_size)

    def job(index):
        m = cup.matches[index]
        return play_match(m.a.path, m.b.path, rounds=rounds, timeout=timeout,
                          sudden_death=SUDDEN_DEATH_CAP)

    workers = workers or (os.cpu_count() or 2)
    if workers <= 1 or len(indices) == 1:
        for index in indices:
            yield index, job(index)
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(job, index): index for index in indices}
        for future in concurrent.futures.as_completed(futures):
            yield futures[future], future.result()


def entrants_from_paths(
    paths: Sequence[str],
    ratings: Optional[Dict[str, Tuple[float, str]]] = None,
) -> List[Entrant]:
    """Build a field from bot files, taking (rating, title) from the registry
    where it knows the bot and the starting rating where it does not."""
    ratings = ratings or {}
    field = []
    for path in paths:
        name = os.path.splitext(os.path.basename(path))[0]
        rating, title = ratings.get(name, (elo.START_RATING, elo.TITLES[0]))
        field.append(Entrant(name=name, path=path, rating=rating, title=title))
    return field
