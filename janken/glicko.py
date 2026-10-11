"""Glicko-2, with the two departures this competition needs.

Glickman's system rates a player by three numbers instead of one: the rating, a
*deviation* saying how sure we are of it, and a *volatility* saying how erratic the
player has been. Elo has only the first, which is why it cannot tell a 1700 that
has played four hundred games from a 1700 that has played four.

**Two scores, and which one carries the ladder matters.** A match is hundreds of
rounds, so two different numbers are available from it:

  * `round_score` — the share of rounds won. The low-variance estimate of one bot's
    edge, and the one that makes a uniform-random bot inert: nobody can beat
    uniform play above half the rounds, so `randy` scores 0.499 over 30,701 real
    matches and injects no noise.
  * `match_score` — who won. One bit against eight, and coarse.

The obvious choice is the first one, and it is wrong. Rock-paper-scissors has a
trivially available Nash equilibrium: any bot can guarantee itself half the rounds
by playing at random. So a round fraction above 0.5 does not measure how good a bot
is, it measures **how exploitable its opponents are** — and that falls as the field
improves. Replaying the real log, the top bot beats the weak bots 95-99% of rounds
but the other strong bots only 51-52%; restricted to a field of competent bots its
rating collapses from 1726 to 1532, and every title above `cm` becomes unreachable
by construction. A bot with a crushing 60% per-round edge over a strong field would
rate 1680, below `master`.

A match of hundreds of rounds is a sigmoid amplifier, which is what fixes this: a
1.3-point round edge becomes a 66.6% match record, and that same 60% round edge
becomes a match record worth a GM rating. Match results stay spread as the field
converges, and `JANKEN_ROUNDS` is the knob that sets how sharply. So `rating` is
scored on match results and the round fraction is kept beside it as `alt_rating`,
where its precision is useful and its collapsing scale does no harm.

The brake on the amplifier is `randy` itself: every bot plays it, no bot can beat
it, so a perfect bot in a field of N tops out at `(N - 1.5)/(N - 1)`.

**The deviation widens by elapsed time, not by periods played.** A rating period
here is a tournament, and tournaments run every half hour — a cadence the operator
can change. Standard Glicko-2 widens once per period, so doubling the tournament
frequency would double how fast an idle bot's rating decays into uncertainty, which
is a strange thing for a scheduling choice to control. Volatility is therefore
*per day*, and `widen` takes the days elapsed.

`SCALE` and `START_RD` are Glickman's published 173.7178 and 350. They were fitted
against the real match log first — `scripts/fit_glicko.py` still does it, and still
has to for `ALT_SCALE` — and the fit landed within 9% of the paper, which is the
sign that match scoring fits the model's assumptions. The published values won on
their merits; see the note above `SCALE`.
"""

import dataclasses
import math
from typing import Callable, Optional, Sequence, Tuple

# Internal units -> familiar rating points: Glickman's own constant, and not a
# fitted one. Fitting it against this match log to land the top bot on a chosen
# number gave 158.9, which is close enough to be tempting and worse on every count
# that matters — `expected()` stopped being Elo's familiar 400-points-is-10-to-1
# curve, the best possible showing in a 19-bot field fell to 2065 and put `gm` out
# of reach entirely, and the strongest bots rated below the `im` floor two of them
# already hold. The published constant is better than a number chosen to make a
# ladder look a particular way, so the match ladder has nothing left to tune.
#
# Still a parameter, because the round-fraction `alt_rating` does need its own.
SCALE = 173.7178

# Where a bot starts, in display units. `START_RD` is `START_PHI x SCALE`: the
# starting deviation is not a free constant but Glickman's "this bot is unknown",
# converted. Loosening it to make a newcomer's penalty look tidier costs eight
# tournaments of convergence, which the fit script measures rather than assumes.
START_RATING = 1500.0
START_PHI = 350.0 / 173.7178
START_RD = START_PHI * SCALE      # 350, the scale being Glickman's own
START_SIGMA = 0.06

# The round-fraction ladder needs its own scale: round margins cluster hard around
# 0.5, so the same field spans far less internal ground and has to be stretched
# much further to read as ratings. Fitted the same way, against the same log.
ALT_SCALE = 444.0
ALT_START_RD = START_PHI * ALT_SCALE

# Deviations below the rating that the ladder ranks on. One, not two: at two a bot
# whose results are already known to be top-of-field is held down for six
# tournaments, and a newcomer that is wiping the field should show it at once.
RANK_K = 1.0

# How much the volatility itself may move. Small means "this bot's form is
# steady"; Glickman suggests 0.3-1.2. The field here is stable by construction —
# an improved bot is a new file and therefore a new identity, so a rating never has
# to chase a bot that got better overnight.
TAU = 0.5

# Convergence tolerance for the volatility solver, from the paper.
EPSILON = 1e-6


@dataclasses.dataclass
class Rating:
    """A bot's three numbers, in display units."""
    rating: float = START_RATING
    rd: float = START_RD
    sigma: float = START_SIGMA

    def conservative(self, k: float = RANK_K) -> float:
        """What the ladder ranks on: the rating we are confident it is *at least*.

        One deviation below the estimate, so a bot that has played four games sits
        below an equally-rated bot that has played four hundred. It is not a
        handicap — it is the same rating read at its pessimistic end, and it rises
        on its own as the bot keeps playing.

        A ranking choice, never a rating one: `k` changes who is shown on top and
        touches neither the rating nor the title it earns.
        """
        return self.rating - k * self.rd


def _forfeit_score(my_forfeit, their_forfeit) -> Optional[float]:
    """A forfeit's flat score, or None if nobody forfeited.

    Flat rather than the share of rounds played before the walkout: otherwise a bot
    losing badly could trip the invalid-move limit on purpose and bank the better
    fraction it held over three rounds instead of the worse one it was heading for
    over two hundred.
    """
    if my_forfeit and their_forfeit:
        return 0.5
    if my_forfeit:
        return 0.0
    if their_forfeit:
        return 1.0
    return None


def match_score(my_wins: int, their_wins: int, draws: int,
                my_forfeit=None, their_forfeit=None) -> float:
    """Who won the match: 1, 0.5 or 0. What `rating` is scored on."""
    forfeit = _forfeit_score(my_forfeit, their_forfeit)
    if forfeit is not None:
        return forfeit
    if my_wins > their_wins:
        return 1.0
    if my_wins < their_wins:
        return 0.0
    return 0.5


def round_score(my_wins: int, their_wins: int, draws: int,
                my_forfeit=None, their_forfeit=None) -> float:
    """The share of the rounds, in [0, 1]. What `alt_rating` is scored on."""
    forfeit = _forfeit_score(my_forfeit, their_forfeit)
    if forfeit is not None:
        return forfeit
    rounds = my_wins + their_wins + draws
    if not rounds:
        return 0.5
    return (my_wins + 0.5 * draws) / rounds


def _g(phi: float) -> float:
    return 1.0 / math.sqrt(1.0 + 3.0 * phi * phi / (math.pi * math.pi))


def _expected(mu: float, mu_j: float, phi_j: float) -> float:
    return 1.0 / (1.0 + math.exp(-_g(phi_j) * (mu - mu_j)))


def expected_score(player: Rating, opponent: Rating, scale: float = SCALE) -> float:
    """The score this rating gap predicts, in [0, 1] — for display and for norms."""
    mu = (player.rating - START_RATING) / scale
    mu_j = (opponent.rating - START_RATING) / scale
    return _expected(mu, mu_j, opponent.rd / scale)


def _solve_sigma(phi: float, sigma: float, delta: float, v: float,
                 tau: float) -> float:
    """The paper's Illinois-variant root find for the new volatility.

    Kept faithful to Glickman rather than simplified: it is the one step of
    Glicko-2 with no closed form, and the published worked example is the only
    external check this file has.
    """
    a = math.log(sigma * sigma)
    d2, p2 = delta * delta, phi * phi

    def f(x):
        ex = math.exp(x)
        num = ex * (d2 - p2 - v - ex)
        den = 2.0 * (p2 + v + ex) ** 2
        return num / den - (x - a) / (tau * tau)

    A = a
    if d2 > p2 + v:
        B = math.log(d2 - p2 - v)
    else:
        k = 1
        while f(a - k * tau) < 0:
            k += 1
        B = a - k * tau

    fA, fB = f(A), f(B)
    while abs(B - A) > EPSILON:
        C = A + (A - B) * fA / (fB - fA)
        fC = f(C)
        if fC * fB <= 0:
            A, fA = B, fB
        else:
            fA = fA / 2.0
        B, fB = C, fC
    return math.exp(A / 2.0)


def widen(current: Rating, days: float, scale: float = SCALE,
          max_rd: Optional[float] = None) -> Rating:
    """Grow the deviation for time passed without playing.

    Standard Glicko-2 does this once per rating period. Here it is per day, so the
    ladder behaves the same whether tournaments run hourly or every half hour — see
    the module docstring. Days below zero are treated as zero rather than allowed to
    *shrink* the deviation, which a clock skew could otherwise do.

    `max_rd` is the ceiling uncertainty grows to, and defaults to the starting
    deviation: a bot nobody has seen for a year is not less known than one that has
    never played. It must be passed whenever `scale` is, since both are in display
    units — clamping a scaled deviation against the unscaled default silently
    truncates every rating above the default, which is exactly the fit this
    constant is being tuned by.
    """
    ceiling = START_RD if max_rd is None else max_rd
    if days <= 0:
        return Rating(current.rating, min(current.rd, ceiling), current.sigma)
    phi = current.rd / scale
    phi_star = math.sqrt(phi * phi + current.sigma * current.sigma * days)
    return Rating(current.rating, min(phi_star * scale, ceiling), current.sigma)


def update(current: Rating, results: Sequence[Tuple[Rating, float]],
           days: float = 0.0, tau: float = TAU,
           scale: float = SCALE, max_rd: Optional[float] = None) -> Rating:
    """One rating period: every result a bot got, applied at once.

    `results` pairs each opponent's rating *as it stood entering the period* with
    the score against it, in [0, 1]. Batching is the point of a rating period —
    scoring results one after another would make the answer depend on the order
    matches happened to finish in.

    With no results the rating is unchanged and only the deviation widens, which is
    what makes a bot come back from a pause at its old rating with honest
    uncertainty about whether it still deserves it.
    """
    if not results:
        return widen(current, days, scale, max_rd)

    mu = (current.rating - START_RATING) / scale
    phi = current.rd / scale

    inv_v = 0.0
    delta_sum = 0.0
    for opponent, score in results:
        mu_j = (opponent.rating - START_RATING) / scale
        phi_j = opponent.rd / scale
        g_j = _g(phi_j)
        e_j = _expected(mu, mu_j, phi_j)
        inv_v += g_j * g_j * e_j * (1.0 - e_j)
        delta_sum += g_j * (score - e_j)
    if inv_v <= 0:                     # every opponent infinitely far away
        return widen(current, days, scale, max_rd)

    v = 1.0 / inv_v
    delta = v * delta_sum

    sigma_new = _solve_sigma(phi, current.sigma, delta, v, tau)
    # Time first, then the period's evidence: a bot that sat out a week and then
    # played should have that week's uncertainty priced in before its results
    # narrow it again.
    phi_star = math.sqrt(phi * phi + sigma_new * sigma_new * max(days, 0.0))
    phi_new = 1.0 / math.sqrt(1.0 / (phi_star * phi_star) + inv_v)
    mu_new = mu + phi_new * phi_new * delta_sum

    return Rating(
        rating=mu_new * scale + START_RATING,
        rd=min(phi_new * scale, START_RD if max_rd is None else max_rd),
        sigma=sigma_new,
    )


@dataclasses.dataclass(frozen=True)
class Ladder:
    """One rating scale, and the starting deviation that belongs to it.

    The two travel together or not at all. They are both in display units, so
    scoring on one scale while clamping the deviation against another silently
    truncates every rating above the clamp — a bug that is invisible in the numbers
    and fatal to the fit. Pairing them here means a caller picks a ladder rather
    than two loose floats.
    """
    scale: float
    start_rd: float
    score: "Callable[..., float]"

    def fresh(self) -> Rating:
        """A bot nobody has seen play."""
        return Rating(START_RATING, self.start_rd, START_SIGMA)

    def update(self, current: Rating, results: Sequence[Tuple[Rating, float]],
               days: float = 0.0, tau: float = TAU) -> Rating:
        return update(current, results, days=days, tau=tau, scale=self.scale,
                      max_rd=self.start_rd)


# `rating`: who won, the ladder that titles and standings read.
MATCH = Ladder(SCALE, START_RD, match_score)
# `alt_rating`: the share of rounds, kept beside it as the finer measurement.
ROUND = Ladder(ALT_SCALE, ALT_START_RD, round_score)
