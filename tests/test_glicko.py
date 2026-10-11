import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from janken import glicko


class PaperTest(unittest.TestCase):
    """Glickman's own worked example, from the Glicko-2 paper.

    The only external check this implementation has. Every other test here asserts
    a property we *want*; this one asserts we implemented the published algorithm,
    which is the difference between a rating system and a plausible-looking one.
    The paper applies one rating period of decay, hence `days=1`.
    """

    def test_the_published_example(self):
        me = glicko.Rating(1500.0, 200.0, 0.06)
        opponents = [
            (glicko.Rating(1400.0, 30.0), 1.0),
            (glicko.Rating(1550.0, 100.0), 0.0),
            (glicko.Rating(1700.0, 300.0), 0.0),
        ]
        got = glicko.update(me, opponents, days=1.0, scale=173.7178, max_rd=350.0)
        self.assertAlmostEqual(got.rating, 1464.06, places=1)
        self.assertAlmostEqual(got.rd, 151.52, places=1)
        self.assertAlmostEqual(got.sigma, 0.05999, places=4)


class ScoringTest(unittest.TestCase):
    def test_a_match_is_won_lost_or_drawn(self):
        self.assertEqual(glicko.match_score(120, 80, 0), 1.0)
        self.assertEqual(glicko.match_score(80, 120, 0), 0.0)
        self.assertEqual(glicko.match_score(100, 100, 0), 0.5)

    def test_a_one_round_win_is_a_whole_win(self):
        """Coarse on purpose. Two bots that are genuinely equal split their
        matches, which is the right answer arrived at over a season rather than
        inside one match."""
        self.assertEqual(glicko.match_score(101, 99, 0), 1.0)

    def test_rounds_are_a_fraction_with_draws_worth_half(self):
        self.assertAlmostEqual(glicko.round_score(120, 80, 0), 0.6)
        self.assertAlmostEqual(glicko.round_score(60, 40, 100), 0.55)
        self.assertAlmostEqual(glicko.round_score(0, 0, 90), 0.5)

    def test_a_match_that_never_started_is_a_draw(self):
        """Nothing played tells us nothing, and dividing by zero rounds would
        take the whole tournament down."""
        self.assertEqual(glicko.round_score(0, 0, 0), 0.5)
        self.assertEqual(glicko.match_score(0, 0, 0), 0.5)

    def test_a_forfeit_is_a_flat_loss_on_both_scales(self):
        """Flat, not the fraction held before walking out: otherwise a bot heading
        for a bad loss could trip the invalid-move limit on purpose and bank the
        better number it had over three rounds."""
        for score in (glicko.match_score, glicko.round_score):
            self.assertEqual(score(5, 2, 0, "bad move", None), 0.0)
            self.assertEqual(score(2, 5, 0, None, "bad move"), 1.0)
            self.assertEqual(score(5, 2, 0, "bad", "bad"), 0.5)

    def test_forfeiting_beats_no_amount_of_winning(self):
        """A bot ahead 199-0 that then forfeits still scores zero."""
        self.assertEqual(glicko.match_score(199, 0, 0, "crashed", None), 0.0)
        self.assertEqual(glicko.round_score(199, 0, 0, "crashed", None), 0.0)


class ConservativeTest(unittest.TestCase):
    def test_it_reads_the_rating_at_its_pessimistic_end(self):
        r = glicko.Rating(1700.0, 50.0)
        self.assertEqual(r.conservative(0.0), 1700.0)
        self.assertEqual(r.conservative(1.0), 1650.0)
        self.assertEqual(r.conservative(2.0), 1600.0)

    def test_the_default_is_the_ladder_s_k(self):
        r = glicko.Rating(1700.0, 50.0)
        self.assertEqual(r.conservative(), r.rating - glicko.RANK_K * r.rd)

    def test_an_untested_rating_ranks_below_a_settled_equal(self):
        """The whole point: same number, different amount of evidence."""
        newcomer = glicko.Rating(1700.0, glicko.START_RD)
        settled = glicko.Rating(1700.0, 30.0)
        self.assertLess(newcomer.conservative(), settled.conservative())


class WidenTest(unittest.TestCase):
    def test_sitting_idle_widens_the_deviation(self):
        before = glicko.Rating(1700.0, 60.0)
        after = glicko.widen(before, days=30.0)
        self.assertGreater(after.rd, before.rd)
        self.assertEqual(after.rating, before.rating)   # form unknown, not worse

    def test_it_never_shrinks_on_a_backwards_clock(self):
        """Two disagreeing clocks must not teach the system anything."""
        before = glicko.Rating(1700.0, 60.0)
        self.assertEqual(glicko.widen(before, days=-5.0).rd, before.rd)
        self.assertEqual(glicko.widen(before, days=0.0).rd, before.rd)

    def test_uncertainty_stops_at_never_having_played(self):
        """A bot nobody has seen for a year is not *less* known than one that has
        never played at all."""
        stale = glicko.widen(glicko.Rating(1700.0, glicko.START_RD), days=100000.0)
        self.assertLessEqual(stale.rd, glicko.START_RD)

    def test_the_ceiling_follows_the_scale_it_was_given(self):
        """`scale` and the deviation are both in display units, so clamping a
        scaled deviation against the unscaled default silently truncates every
        rating above it — which is invisible in the numbers and fatal to a fit."""
        wide = glicko.Rating(1500.0, 800.0, 0.06)
        kept = glicko.widen(wide, days=1.0, scale=444.0, max_rd=895.0)
        self.assertGreater(kept.rd, glicko.START_RD)


class UpdateTest(unittest.TestCase):
    def test_no_results_only_widens(self):
        """What lets a bot come back from a pause at its old rating with honest
        uncertainty about whether it still deserves it."""
        before = glicko.Rating(1800.0, 40.0)
        after = glicko.update(before, [], days=14.0)
        self.assertEqual(after.rating, before.rating)
        self.assertGreater(after.rd, before.rd)

    def test_the_order_results_arrive_in_does_not_matter(self):
        """A rating period is the unit. Matches finish in whatever order the
        workers hand them back, and that must not reach the rating."""
        me = glicko.Rating(1500.0, 200.0, 0.06)
        results = [(glicko.Rating(1400.0, 30.0), 1.0),
                   (glicko.Rating(1550.0, 100.0), 0.0),
                   (glicko.Rating(1700.0, 300.0), 0.0)]
        forward = glicko.update(me, results)
        backward = glicko.update(me, list(reversed(results)))
        self.assertAlmostEqual(forward.rating, backward.rating, places=9)
        self.assertAlmostEqual(forward.rd, backward.rd, places=9)

    def test_playing_narrows_the_deviation(self):
        fresh = glicko.MATCH.fresh()
        after = glicko.update(fresh, [(glicko.Rating(1500.0, 30.0), 1.0)] * 18)
        self.assertLess(after.rd, fresh.rd / 2.0)

    def test_winning_raises_and_losing_lowers(self):
        fresh = glicko.MATCH.fresh()
        opponents = [(glicko.Rating(1500.0, 30.0), 1.0)] * 10
        losses = [(glicko.Rating(1500.0, 30.0), 0.0)] * 10
        self.assertGreater(glicko.update(fresh, opponents).rating, fresh.rating)
        self.assertLess(glicko.update(fresh, losses).rating, fresh.rating)

    def test_a_settled_bot_moves_less_than_a_newcomer(self):
        """The reason there is no K to tune: the step is the deviation."""
        result = [(glicko.Rating(1500.0, 30.0), 1.0)]
        newcomer = glicko.Rating(1500.0, glicko.START_RD, glicko.START_SIGMA)
        veteran = glicko.Rating(1500.0, 25.0, glicko.START_SIGMA)
        moved_new = glicko.update(newcomer, result).rating - 1500.0
        moved_old = glicko.update(veteran, result).rating - 1500.0
        self.assertGreater(moved_new, moved_old * 3)

    def test_an_even_result_between_equals_moves_nobody(self):
        pair = glicko.Rating(1600.0, 40.0, glicko.START_SIGMA)
        after = glicko.update(pair, [(glicko.Rating(1600.0, 40.0), 0.5)])
        self.assertAlmostEqual(after.rating, 1600.0, places=6)

    def test_a_mirrored_result_between_equals_is_symmetric(self):
        """Glicko-2 is not zero-sum in general, but two bots that know as little
        about themselves move equal and opposite off the same match."""
        a = glicko.Rating(1500.0, 120.0, glicko.START_SIGMA)
        b = glicko.Rating(1500.0, 120.0, glicko.START_SIGMA)
        won = glicko.update(a, [(b, 1.0)])
        lost = glicko.update(b, [(a, 0.0)])
        self.assertAlmostEqual(won.rating - 1500.0, 1500.0 - lost.rating, places=9)

    def test_beating_a_stronger_bot_is_worth_more(self):
        me = glicko.Rating(1500.0, 60.0, glicko.START_SIGMA)
        upset = glicko.update(me, [(glicko.Rating(1900.0, 30.0), 1.0)]).rating
        routine = glicko.update(me, [(glicko.Rating(1100.0, 30.0), 1.0)]).rating
        self.assertGreater(upset, routine)


class ExpectedScoreTest(unittest.TestCase):
    def test_equal_ratings_are_a_coin_flip(self):
        even = glicko.Rating(1600.0, 40.0)
        self.assertAlmostEqual(glicko.expected_score(even, even), 0.5)

    def test_expectations_are_complementary(self):
        a, b = glicko.Rating(1700.0, 30.0), glicko.Rating(1450.0, 30.0)
        self.assertAlmostEqual(
            glicko.expected_score(a, b) + glicko.expected_score(b, a), 1.0)

    def test_a_higher_rating_expects_more(self):
        weak, strong = glicko.Rating(1300.0, 30.0), glicko.Rating(1800.0, 30.0)
        self.assertGreater(glicko.expected_score(strong, weak), 0.9)
        self.assertLess(glicko.expected_score(weak, strong), 0.1)


class LadderTest(unittest.TestCase):
    """The two scales, and the pairing that keeps them straight."""

    def test_a_fresh_bot_starts_unknown_on_either_ladder(self):
        for ladder in (glicko.MATCH, glicko.ROUND):
            fresh = ladder.fresh()
            self.assertEqual(fresh.rating, glicko.START_RATING)
            self.assertEqual(fresh.rd, ladder.start_rd)
            self.assertEqual(fresh.sigma, glicko.START_SIGMA)

    def test_each_ladder_carries_its_own_scorer(self):
        self.assertIs(glicko.MATCH.score, glicko.match_score)
        self.assertIs(glicko.ROUND.score, glicko.round_score)

    def test_the_round_ladder_is_stretched_further(self):
        """Round margins cluster hard around 0.5, so the same field spans far less
        internal ground and has to be stretched more to read as ratings."""
        self.assertGreater(glicko.ROUND.scale, glicko.MATCH.scale)

    def test_a_ladder_clamps_against_its_own_starting_deviation(self):
        """The bug this type exists to prevent: `Ladder.update` must pass its own
        `start_rd` as the ceiling, not the module default."""
        idle = glicko.ROUND.update(glicko.ROUND.fresh(), [], days=5000.0)
        self.assertAlmostEqual(idle.rd, glicko.ROUND.start_rd)
        self.assertGreater(idle.rd, glicko.START_RD)

    def test_the_starting_deviation_is_glickman_s_converted(self):
        """Not a free constant: it is "this bot is unknown" on each scale. Fitting
        it instead buys a tidier newcomer penalty and costs eight tournaments of
        convergence — see scripts/fit_glicko.py."""
        self.assertAlmostEqual(glicko.START_RD,
                               glicko.START_PHI * glicko.MATCH.scale)
        self.assertAlmostEqual(glicko.ALT_START_RD,
                               glicko.START_PHI * glicko.ROUND.scale)
        self.assertAlmostEqual(glicko.START_PHI, 350.0 / 173.7178)


class RandomBotIsInertTest(unittest.TestCase):
    """A uniform-random bot cannot be beaten above half the rounds, so it is a
    coin flip in every match and belongs at the pool average on both ladders.

    This is the property that makes it safe to leave `randy` in the field: it
    anchors the scale instead of injecting noise. It is also the ceiling on
    everyone else — every bot plays it, so a perfect bot in a field of N tops out
    at `(N - 1.5)/(N - 1)`.
    """

    def test_scoring_half_against_everyone_leaves_it_where_it_started(self):
        for ladder in (glicko.MATCH, glicko.ROUND):
            randy = ladder.fresh()
            field = [glicko.Rating(1500.0, 40.0)] * 18
            for _period in range(12):
                randy = ladder.update(randy, [(o, 0.5) for o in field], days=1.0)
            self.assertAlmostEqual(randy.rating, glicko.START_RATING, places=6)
            self.assertLess(randy.rd, ladder.start_rd / 4.0)

    def test_the_field_size_ceiling_clears_the_top_title(self):
        """What a flawless bot can actually reach, held back by the one match it
        can never win more than half of — and it has to clear `gm`, or the top
        rung is a decoration rather than a target.

        Asserted as an inequality against the floor rather than as the numbers
        themselves, because the point is the relationship: a re-scaled ladder that
        quietly put `gm` out of reach should fail here.
        """
        for n in (19, 26, 100):
            best = (n - 1.5) / (n - 1)
            rating = (glicko.START_RATING
                      + glicko.MATCH.scale * math.log(best / (1 - best)))
            self.assertGreater(rating, 2100.0,
                               "a flawless bot in a field of %d cannot reach gm" % n)

    def test_the_ceiling_rises_with_the_field(self):
        def ceiling(n):
            best = (n - 1.5) / (n - 1)
            return glicko.MATCH.scale * math.log(best / (1 - best))
        self.assertLess(ceiling(19), ceiling(26))
        self.assertLess(ceiling(26), ceiling(100))


if __name__ == "__main__":
    unittest.main()
