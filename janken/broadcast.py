"""Turn a played cup into a timed script the front end can animate.

A match is played at full speed and its transcript kept; the *broadcast* is a
separate pass over that transcript. Because the script is written after the
cup is over, the director knows the future: it can slow down for a comeback
because it already knows the comeback happens.

That is what fixes the pacing problem. A hundred rounds at one uniform speed
is either unreadably fast or tediously slow — the tedium comes from the
uniformity, not the count. So rounds get individual durations: a fast baseline
where nothing is happening, a slow beat around lead changes and long streaks,
full stop for sudden death, and a single blurred fast-forward once the match
is mathematically over.

Output is plain JSON-able dicts. Timings are milliseconds, and every round
duration is expressed as a run — ``[count, ms]`` — so a 300-round final costs
a handful of numbers instead of three hundred.
"""

import dataclasses
from typing import Dict, List, Optional

from . import cup as cup_mod
from . import round_winner

CODE_MOVE = {"r": "rock", "p": "paper", "s": "scissors", "x": "invalid"}

# Per-round baseline and emphasis, by treatment. Rounds at baseline speed are
# texture — the eye reads the score bar and the shape of the move strip, not
# the individual throw. The slow beat is where a round becomes a *moment* and
# is meant to be legible on its own.
BASE_MS = {"grid": 40, "featured": 55, "hero": 60}
SLOW_MS = {"grid": 170, "featured": 260, "hero": 380}

# Ceiling per match, so the show's length is predictable no matter how the
# matches actually go. Over budget, every beat is scaled down proportionally.
BUDGET_MS = {"grid": 12000, "featured": 22000, "hero": 45000}

REVEAL_MS = 300          # per match in a reveal round: who advanced, that's it
REVEAL_HOLD_MS = 1500    # beat at the end of a reveal round before moving on
SEQUENTIAL_HOLD_MS = 2200  # a hero match sits on its result before the cut
PLAYOFF_SHARE = 0.6      # airtime the third-place match gets of a final's
INTERSTITIAL_MS = 5000   # bracket recap between rounds
FAST_FORWARD_MS = 600    # total time for the dead tail of a decided match

STREAK_MIN = 5           # a run this long is worth slowing down for
SLOW_WINDOW = 2          # rounds either side of a moment that also slow down
CLOSE_DIFF = 3           # a lead this small late on counts as neck and neck
SUDDEN_SHOWN = 6         # opening sudden-death rounds that get the slow beat
MIN_MS = 8               # never emit a duration below this


def _endgame(scheduled: int) -> int:
    """How many closing rounds count as the run-in."""
    return max(3, scheduled // 12)


def script(cup: cup_mod.Cup) -> dict:
    """The whole broadcast: rounds in order, each with its matches and timing."""
    # One label per entrant, decided once for the whole broadcast: a bot must
    # not be "Rocky II" in the quarter-final and "Rocky II · sofia" in the
    # semi, so ambiguity is judged against the entire field. Computed here
    # rather than stored, so a bracket re-rendered after a rename shows the
    # name the bot has now.
    labels = cup_mod.entrant_labels(cup.entrants)

    rounds = []
    for round_size, indices in cup.rounds():
        how = cup_mod.treatment(round_size)
        matches = [_match(cup, cup.matches[i], how, labels) for i in indices]
        rounds.append(_round(cup, round_size, how, matches))

    return {
        "cup": cup.name,
        "bracket_size": cup.bracket_size,
        "champion": _label(cup.champion, labels),
        # The champion as an entrant, not just a name to look back up. The view
        # used to find the winner's portrait by matching the champion's name
        # against the draw, which only worked while every name in a field was
        # unique — true only because each carried its own file's digest.
        "champion_entrant": _entrant(cup.champion, labels),
        "podium": _podium(cup, labels),
        "entrants": [_entrant(e, labels) for e in cup.entrants],
        "rounds": rounds,
        "duration_ms": sum(r["duration_ms"] for r in rounds),
    }


def _label(e, labels: Dict[str, str]) -> Optional[str]:
    """What this entrant is called on screen, or None for an empty slot — the
    payload distinguishes "nobody yet" from "?", which is a thing to print."""
    return None if e is None else cup_mod.label_of(e, labels)


PLACES = {1: "Champion", 2: "Runner-up", 3: "Third"}


def _podium(cup, labels: Dict[str, str]) -> List[dict]:
    """First, second and third — as much of it as has been decided.

    Second is the beaten finalist. Third is the playoff winner, and *only* the
    playoff winner: without that match the two beaten semi-finalists are
    genuinely tied, and picking one to stand on the box would be inventing a
    result nobody played.

    Derived from the cup it is handed, which under the reveal gate is the cup
    as the audience may see it — so this stays empty until the final is shown.
    """
    if not cup.matches:
        return []
    final = cup.matches[-1]
    if final.winner is None:
        return []

    standing = [(1, final.winner)]
    if final.loser is not None:
        standing.append((2, final.loser))
    playoff = next((m for m in cup.matches if m.label == cup_mod.THIRD_PLACE), None)
    if playoff is not None and playoff.winner is not None:
        standing.append((3, playoff.winner))
    return [{"place": place, "title": PLACES[place],
             "entrant": _entrant(who, labels)}
            for place, who in standing]


def _round(cup, round_size, how, matches) -> dict:
    # The hero treatment gives a match the whole screen, so a round with more
    # than one of them plays them in turn rather than side by side. Two
    # semi-finals become two events instead of a split screen, at the cost of
    # the round lasting the sum of them rather than the longer one.
    sequential = how == "hero" and len(matches) > 1

    if how == "reveal":
        # Nothing is played on screen: the bracket fills in, one match every
        # REVEAL_MS. Sixteen matches go by in about six seconds, which is why
        # doubling the field costs the show seconds rather than minutes.
        play_ms = len(matches) * REVEAL_MS + REVEAL_HOLD_MS
    elif sequential:
        # Each match holds on its final score before the cut, so the result is
        # readable rather than being replaced the instant the last beat lands.
        play_ms = sum(m["duration_ms"] + SEQUENTIAL_HOLD_MS for m in matches)
    else:
        # Everything in the round animates at once, so the round lasts as long
        # as its slowest match rather than the sum of them.
        play_ms = max(m["duration_ms"] for m in matches)

    interstitial = 0 if round_size <= 2 else INTERSTITIAL_MS
    return {
        "round_size": round_size,
        # From the matches, not the width: the playoff round is round_size 2
        # and would otherwise call itself "Final".
        "stage": matches[0]["stage"],
        "treatment": how,
        "matches": matches,
        "sequential": sequential,
        "hold_ms": SEQUENTIAL_HOLD_MS if sequential else 0,
        "reveal_ms": REVEAL_MS if how == "reveal" else 0,
        "interstitial_ms": interstitial,
        "duration_ms": play_ms + interstitial,
    }


def _entrant(e, labels: Dict[str, str]) -> Optional[dict]:
    """None for a slot the bracket has not filled yet. Scripting a partly
    played cup has to work: it is what lets the front end draw the whole draw,
    pending slots and all, before a move is played."""
    if e is None:
        return None
    return {"name": _label(e, labels), "seed": e.seed,
            # The registry id, so anything that has to find this entrant again
            # joins on it instead of matching the name it happens to print.
            "id": e.id,
            "rating": round(e.rating, 1), "title": e.title,
            # Relative to the static directory, "" for a bot with no avatar.
            # The view prefixes it; see cup.Entrant.image.
            "image": e.image}


def _match(cup, m: cup_mod.CupMatch, how: str, labels: Dict[str, str]) -> dict:
    result = m.result
    pairs = decode(result.moves) if result else []
    scheduled = len(pairs) - (result.sudden_death if result else 0)

    out = {
        "stage": cup.stage_of(m),
        "round_size": m.round_size,
        "slot": m.slot,
        "treatment": how,
        "a": _entrant(m.a, labels),
        "b": _entrant(m.b, labels),
        # Where an unfilled slot's entrant will come from, so the bracket can
        # be drawn with its connectors before those matches are played.
        "a_from": m.a_from,
        "b_from": m.b_from,
        "winner": _label(m.winner, labels),
        "decided_by": m.decided_by,
        "score": [result.wins_a, result.wins_b, result.draws] if result else [0, 0, 0],
        "forfeit_a": result.forfeit_a if result else None,
        "forfeit_b": result.forfeit_b if result else None,
        "sudden_death": result.sudden_death if result else 0,
        "rounds_played": len(pairs),
        "interest": interest(m),
    }

    if how == "reveal" or not pairs:
        # A reveal never animates the play, so it does not ship the transcript
        # either — at 64 entrants that is half the field's moves left out of
        # the payload for something nobody watches. An unplayed match has no
        # transcript to ship in the first place.
        out["moves"] = ""
        out["beats"] = []
        out["markers"] = []
        out["budget_ms"] = 0
        out["natural_ms"] = 0
        out["duration_ms"] = REVEAL_MS if how == "reveal" else 0
        return out

    cut = decided_at(pairs, scheduled)
    head = len(pairs) if cut is None else min(len(pairs), cut + 1)
    # Prune once and use the survivors for both timing and captions, so a
    # caption never lands on a round the replay did not slow down for.
    kept = _prune(moments(pairs, scheduled), head)
    budget = _budget(how, out["interest"])
    if m.label == cup_mod.THIRD_PLACE:
        # A consolation match shares the final's round and its treatment, so
        # on interest alone a thrilling playoff could out-air a one-sided
        # final. It is still the undercard, and the running order should say so.
        budget = int(budget * PLAYOFF_SHARE)
    beats, natural = _beats(pairs, kept, cut, how, budget)

    # Exported so tooling can tell a match that was *shaped* to this length
    # from one that was *squashed* into it. natural_ms is what the beats wanted
    # before the budget got involved; the ratio between them is the honest
    # measure of whether the constants are doing the work or the cap is.
    out["budget_ms"] = budget
    out["natural_ms"] = natural
    out["moves"] = result.moves
    out["beats"] = beats
    out["markers"] = [{"round": i, "text": text} for i, text in sorted(kept.items())]
    out["duration_ms"] = sum(count * ms for count, ms in beats)
    return out


# -- reading a transcript ----------------------------------------------------

def decode(moves: str) -> List[tuple]:
    """"rp sr" -> [('r','p'), ('s','r')] — one pair of move codes per round."""
    return [(moves[i], moves[i + 1]) for i in range(0, len(moves), 2)]


def outcome(code_a: str, code_b: str) -> int:
    """+1 if a took the round, -1 if b did, 0 for a draw.

    Mirrors the referee's scoring, including that an invalid move loses the
    round to a valid one and two invalids are a draw.
    """
    valid_a, valid_b = code_a in "rps", code_b in "rps"
    if valid_a and valid_b:
        winner = round_winner(CODE_MOVE[code_a], CODE_MOVE[code_b])
        return 0 if winner == -1 else (1 if winner == 0 else -1)
    if valid_a:
        return 1
    if valid_b:
        return -1
    return 0


def moments(pairs, scheduled: int) -> Dict[int, str]:
    """Round index -> why it matters. These are the rounds worth slowing for."""
    marks: Dict[int, str] = {}
    diff = lead = 0
    streak_side = streak_len = 0

    for i, (code_a, code_b) in enumerate(pairs):
        got = outcome(code_a, code_b)
        diff += got

        if got == 0:
            streak_side = streak_len = 0
        elif got == streak_side:
            streak_len += 1
        else:
            streak_side, streak_len = got, 1
        # Mark a run when it becomes notable, and once more if it turns into a
        # rout — never on every round of it. A 97-round streak is a blowout,
        # and slowing for all of it made the dullest match the longest one on
        # screen, which is precisely backwards.
        if streak_len in (STREAK_MIN, STREAK_MIN * 2):
            marks[i] = "%d in a row" % streak_len

        # Neck and neck down the stretch. A match can be tense without either
        # a lead change or a streak, and those are the ones that otherwise
        # play as uniform texture with nothing to look at.
        # Only over the scheduled rounds: past them every round is level by
        # definition, which would flag an entire sudden death as a tense
        # finish and slow down the thing the cap exists to cut short.
        alive = abs(diff) <= max(CLOSE_DIFF, scheduled - 1 - i)
        if scheduled - _endgame(scheduled) <= i < scheduled and alive:
            marks[i] = "neck and neck"

        sign = (diff > 0) - (diff < 0)
        if sign and lead and sign != lead:
            marks[i] = "lead change"     # rarer than a streak, so it wins the label
        if sign:
            lead = sign

        # Sudden death overrides everything — but only while it is still a
        # moment. Two bots that draw a hundred deciders are a curiosity for
        # about six rounds and an endurance test after that, so mark the open
        # of it and the round it finally breaks on.
        if i >= scheduled and (i < scheduled + SUDDEN_SHOWN or i == len(pairs) - 1):
            marks[i] = "sudden death"
    return marks


# Emphasis only reads as emphasis if it is rare. Where a match throws up more
# candidate moments than this, the least interesting are dropped rather than
# slowing half the match down — highest priority first, then earliest.
MARK_PRIORITY = {"sudden death": 0, "lead change": 1, "neck and neck": 2}


def _prune(marks: Dict[int, str], head: int) -> Dict[int, str]:
    live = {i: text for i, text in marks.items() if i < head}
    limit = max(2, head // 16)
    if len(live) <= limit:
        return live
    ranked = sorted(live.items(),
                    key=lambda kv: (MARK_PRIORITY.get(kv[1], 3), kv[0]))
    return dict(ranked[:limit])


def _budget(how: str, score: float) -> int:
    """Airtime earned. A rout does not deserve the same minutes as a thriller
    just because it happens to be the final, so the budget scales with how
    much there was to watch."""
    share = 0.35 + 0.65 * min(1.0, score / 60.0)
    return int(BUDGET_MS[how] * share)


def decided_at(pairs, scheduled: int) -> Optional[int]:
    """First round after which the trailing bot cannot catch up, if any.

    Only the scheduled rounds count: a match that reached sudden death was
    level at the end of them, so it was never decided early.
    """
    diff = 0
    for i in range(scheduled):
        diff += outcome(*pairs[i])
        if abs(diff) > scheduled - 1 - i:
            return i
    return None


def interest(m: cup_mod.CupMatch) -> float:
    """Rough "was this worth watching" score, for picking what to enlarge.

    Only meaningful within a round — it is a sort key, not a measurement.
    """
    if m.result is None:
        return 0.0
    score = 0.0
    if m.decided_by == "sudden death":
        score += 40
    if m.result.forfeit_a or m.result.forfeit_b:
        score += 25
    total = m.result.wins_a + m.result.wins_b
    if total:
        score += 30 * (1 - abs(m.result.wins_a - m.result.wins_b) / total)
    # An upset: the worse seed won, and the bigger the gap the better the story.
    if m.winner is not None:
        loser = m.b if m.winner is m.a else m.a
        if m.winner.seed > loser.seed:
            score += min(30, m.winner.seed - loser.seed)
    pairs = decode(m.result.moves)
    score += 3 * sum(1 for text in moments(pairs, len(pairs)).values()
                     if text == "lead change")
    return round(score, 1)


# -- turning moments into timing ---------------------------------------------

def _beats(pairs, marks, cut, how: str, budget: int) -> List[List[int]]:
    base, slow = BASE_MS[how], SLOW_MS[how]
    total = len(pairs)

    # A decided match still plays out to the end — the bots do not know it is
    # over — but nobody needs to watch it, so the tail becomes one blur.
    head = total if cut is None else min(total, cut + 1)
    durations = []
    for i in range(head):
        near = any((i + d) in marks for d in range(-SLOW_WINDOW, SLOW_WINDOW + 1))
        durations.append(slow if near else base)

    runs = _runs(durations)
    tail = total - head
    if tail:
        runs.append([tail, max(MIN_MS, FAST_FORWARD_MS // tail)])
    natural = sum(count * ms for count, ms in runs)
    return _fit(runs, budget), natural


def _runs(durations: List[int]) -> List[List[int]]:
    runs: List[List[int]] = []
    for ms in durations:
        if runs and runs[-1][1] == ms:
            runs[-1][0] += 1
        else:
            runs.append([1, ms])
    return runs


def _fit(runs: List[List[int]], budget: int) -> List[List[int]]:
    """Scale beats down proportionally if the match overruns its budget, so
    the show's length stays predictable however the matches actually go.

    Beats already at the MIN_MS floor cannot shrink — a fast-forwarded tail is
    usually there — so scaling everything by one factor overshoots. Each pass
    holds the floored beats fixed and rescales the rest into what is left,
    which settles in two or three passes. If the floor alone exceeds the
    budget then the match simply is that long; there is nothing left to give.
    """
    runs = [[count, ms] for count, ms in runs]
    for _ in range(4):
        total = sum(count * ms for count, ms in runs)
        if total <= budget:
            break
        movable = [r for r in runs if r[1] > MIN_MS]
        if not movable:
            break
        movable_total = sum(count * ms for count, ms in movable)
        room = budget - (total - movable_total)
        if room <= 0:
            for r in movable:
                r[1] = MIN_MS
            break
        factor = room / movable_total
        for r in movable:
            r[1] = max(MIN_MS, int(r[1] * factor))
    return runs
