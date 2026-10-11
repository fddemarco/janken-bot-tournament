"""Norms: the performance maths, and the event composition that gates it."""

import dataclasses
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from janken import norms
from janken import rating as elo


@dataclasses.dataclass
class Bot:
    """Just enough of a BotInfo for the composition rules to read."""
    id: str
    name: str
    rating: float
    submitter: str = ""
    league: str = "novice"


def field(specs):
    """Build a field from (name, rating, submitter, league) tuples."""
    return [Bot(id="id-%s" % n, name=n, rating=r, submitter=s, league=t)
            for n, r, s, t in specs]


def titled_field(size, submitters=None, league="master", rating=1800.0):
    """A field that passes composition, for tests about something else.

    Spread over plenty of submitters by default, so the concentration cap and the
    diversity floor are satisfied and whatever the test is really about shows up.
    """
    if submitters is None:
        submitters = max(norms.MIN_SUBMITTERS_FLOOR, (size + 1) // 2)
    return field([("b%02d" % i, rating - i, "who%d" % (i % submitters), league)
                  for i in range(size)])


class PerformanceRatingTest(unittest.TestCase):
    def test_an_even_score_is_the_field_s_own_level(self):
        """Score exactly half against equals and you performed at their level."""
        opponents = [1600.0] * 10
        self.assertAlmostEqual(
            elo.performance_rating(5.0, opponents), 1600.0, places=6)

    def test_it_is_monotone_in_the_score(self):
        opponents = [1500.0, 1700.0, 1900.0, 1600.0, 1550.0]
        seen = [elo.performance_rating(s, opponents)
                for s in (0.5, 1.5, 2.5, 3.5, 4.5)]
        self.assertEqual(seen, sorted(seen))

    def test_a_sweep_and_a_whitewash_are_clamped(self):
        opponents = [1500.0] * 12
        self.assertAlmostEqual(elo.performance_rating(12.0, opponents),
                               1500.0 + elo.PERFORMANCE_CLAMP)
        self.assertAlmostEqual(elo.performance_rating(0.0, opponents),
                               1500.0 - elo.PERFORMANCE_CLAMP)

    def test_beating_1500s_cannot_look_like_a_norm(self):
        """The whole anti-farming property, stated as a test.

        Both gates have to reject a clean sweep of an average field, so that
        neither is load-bearing on its own: the performance is bounded by the
        clamp, and the opposition is too weak for any norm level.
        """
        for count in (10, 24, 99):
            opponents = [1500.0] * count
            best = elo.performance_rating(float(count), opponents)
            for title, (_n, perf_floor, _avg) in elo.NORM_REQUIREMENTS.items():
                self.assertLess(best, perf_floor, title)
            self.assertIsNone(elo.norm_earned(best, 1500.0))

    def test_a_sweep_of_a_strong_field_does_earn_one(self):
        """The gates must not be so tight that nothing is reachable."""
        _n, _perf, gm_avg = elo.NORM_REQUIREMENTS["gm"]
        opponents = [gm_avg] * 12
        best = elo.performance_rating(12.0, opponents)
        self.assertEqual(elo.norm_earned(best, gm_avg), "gm")

    def test_no_opponents_is_an_error(self):
        with self.assertRaises(ValueError):
            elo.performance_rating(0.0, [])


class ScoreForPerformanceTest(unittest.TestCase):
    """The inverse of the performance figure — what a floor actually asks for.

    A threshold means nothing beside the score it implies: one that needs a clean
    sweep is not a high bar but an unreachable one, and the floors and the clamp
    were chosen independently, so nothing guarantees they agree.
    """

    def test_it_inverts_the_performance_rating(self):
        for avg, target in ((1700, 1950), (1800, 2150), (1650, 1700), (2000, 2150)):
            opponents = [avg - 100, avg, avg + 100] * 3
            score = elo.score_for_performance(target, opponents)
            self.assertIsNotNone(score, (avg, target))
            self.assertAlmostEqual(
                elo.performance_rating(score, opponents), target, places=6)

    def test_half_the_games_is_the_field_s_own_level(self):
        opponents = [1600.0] * 10
        self.assertAlmostEqual(elo.score_for_performance(1600.0, opponents), 5.0)

    def test_the_clamp_bounds_what_is_reachable(self):
        """Not the arithmetic: `expected` is always under 1, so summing it would
        never report a floor as unreachable however high it was set."""
        opponents = [1500.0] * 9
        ceiling = 1500.0 + elo.PERFORMANCE_CLAMP
        self.assertIsNotNone(elo.score_for_performance(ceiling, opponents))
        self.assertIsNone(elo.score_for_performance(ceiling + 1, opponents))
        self.assertIsNone(elo.score_for_performance(ceiling + 500, opponents))

    def test_a_harder_floor_asks_for_more(self):
        opponents = [1700.0] * 9
        asks = [elo.score_for_performance(p, opponents)
                for p in (1750.0, 1850.0, 1950.0, 2050.0)]
        self.assertEqual(asks, sorted(asks))

    def test_no_opponents_is_unanswerable(self):
        self.assertIsNone(elo.score_for_performance(1950.0, []))


class NormEarnedTest(unittest.TestCase):
    def test_both_gates_have_to_clear(self):
        _n, perf, avg = elo.NORM_REQUIREMENTS["im"]
        self.assertEqual(elo.norm_earned(perf, avg), "im")
        self.assertIsNone(elo.norm_earned(perf - 1, avg))       # played too weakly
        self.assertIsNone(elo.norm_earned(perf, avg - 1))       # field too weak

    def test_the_strongest_norm_wins(self):
        _n, gm_perf, gm_avg = elo.NORM_REQUIREMENTS["gm"]
        self.assertEqual(elo.norm_earned(gm_perf, gm_avg), "gm")

    def test_a_gm_norm_counts_as_an_im_norm(self):
        self.assertEqual(elo.norms_toward("im", {"gm": 2}), 2)
        self.assertEqual(elo.norms_toward("im", {"im": 1, "gm": 1}), 2)
        # ...but not the other way round.
        self.assertEqual(elo.norms_toward("gm", {"im": 3}), 0)


class TitleEarnedTest(unittest.TestCase):
    def test_the_lower_rungs_need_no_norms(self):
        for title in ("cm", "master"):
            self.assertEqual(elo.title_earned(elo.floor_for(title), {}), title)

    def test_a_peak_alone_does_not_buy_the_top_rungs(self):
        """A farmed rating gets you the peak and nothing above `master`."""
        self.assertEqual(elo.title_earned(elo.floor_for("gm"), {}), "master")
        self.assertEqual(elo.title_earned(elo.floor_for("im"), {}), "master")

    def test_norms_alone_do_not_buy_them_either(self):
        self.assertEqual(elo.title_earned(elo.START_RATING, {"gm": 5}), "novice")

    def test_both_together_earn_the_title(self):
        im_needed = elo.NORM_REQUIREMENTS["im"][0]
        gm_needed = elo.NORM_REQUIREMENTS["gm"][0]
        self.assertEqual(
            elo.title_earned(elo.floor_for("im"), {"im": im_needed}), "im")
        self.assertEqual(
            elo.title_earned(elo.floor_for("gm"), {"gm": gm_needed}), "gm")

    def test_a_gm_peak_with_only_im_norms_is_an_im(self):
        """Falls to the best rung actually earned, not all the way to novice."""
        im_needed = elo.NORM_REQUIREMENTS["im"][0]
        self.assertEqual(
            elo.title_earned(elo.floor_for("gm"), {"im": im_needed}), "im")

    def test_one_norm_short_is_still_short(self):
        needed = elo.NORM_REQUIREMENTS["im"][0]
        self.assertEqual(
            elo.title_earned(elo.floor_for("im"), {"im": needed - 1}), "master")


class SubmitterKeyTest(unittest.TestCase):
    def test_the_same_person_typed_differently_is_one_entrant(self):
        a = Bot(id="1", name="a", rating=1500, submitter="Someone@Example.com")
        b = Bot(id="2", name="b", rating=1500, submitter=" someone@example.com ")
        self.assertEqual(norms.submitter_key(a), norms.submitter_key(b))

    def test_unattributed_bots_are_each_their_own_entrant(self):
        """Otherwise the reference bots would trip the concentration cap for a
        collusion that isn't there."""
        a = Bot(id="1", name="a", rating=1500, submitter="")
        b = Bot(id="2", name="b", rating=1500, submitter="   ")
        self.assertNotEqual(norms.submitter_key(a), norms.submitter_key(b))


class IntervalTest(unittest.TestCase):
    """How often events run is an operator setting, not a source edit."""

    def test_the_default_applies_when_nothing_is_set(self):
        self.assertEqual(norms.norm_interval_hours(""), norms.NORM_INTERVAL_HOURS)

    def test_a_configured_value_is_used(self):
        self.assertEqual(norms.norm_interval_hours("12"), 12.0)
        self.assertEqual(norms.norm_interval_hours(" 6.5 "), 6.5)

    def test_zero_means_every_run(self):
        """A deliberate choice, so it must survive rather than fall back."""
        self.assertEqual(norms.norm_interval_hours("0"), 0.0)

    def test_junk_and_negatives_fall_back(self):
        """A typo in a unit file must not stop the periodic run, and must not
        silently turn every tournament into a norm event either."""
        for raw in ("", "  ", "soon", "12h", "-1", "None"):
            self.assertEqual(norms.norm_interval_hours(raw),
                             norms.NORM_INTERVAL_HOURS, raw)

    def test_it_reads_the_environment(self):
        prior = os.environ.get(norms.NORM_INTERVAL_ENV)
        os.environ[norms.NORM_INTERVAL_ENV] = "8"
        try:
            self.assertEqual(norms.norm_interval_hours(), 8.0)
        finally:
            if prior is None:
                del os.environ[norms.NORM_INTERVAL_ENV]
            else:
                os.environ[norms.NORM_INTERVAL_ENV] = prior


class CompositionTest(unittest.TestCase):
    def test_a_qualifying_field_passes(self):
        shape = norms.composition(titled_field(12))
        self.assertTrue(shape.ok, shape.failures)
        self.assertEqual(shape.size, 12)
        self.assertGreaterEqual(shape.submitters, norms.min_submitters(12))

    def test_too_small_a_field_fails(self):
        shape = norms.composition(titled_field(norms.MIN_NORM_FIELD - 1))
        self.assertFalse(shape.ok)
        self.assertIn("only", shape.why())

    def test_too_few_submitters_fails(self):
        shape = norms.composition(titled_field(12, submitters=2))
        self.assertFalse(shape.ok)
        self.assertIn("submitters", shape.why())

    def test_a_small_cartel_cannot_constitute_an_event(self):
        """The rule FIDE's numbers would have missed.

        Three people with five bots each satisfy "at least three federations",
        but they are three people — the unit of collusion — so the diversity
        floor has to scale with the field.
        """
        specs = [("c%d-%d" % (who, i), 1900.0 - i, "cartel%d" % who, "master")
                 for who in range(3) for i in range(5)]
        shape = norms.composition(field(specs))
        self.assertFalse(shape.ok)
        self.assertGreaterEqual(shape.submitters, 3)      # FIDE's floor is met...
        self.assertIn("submitters", shape.why())          # ...and ours is not

    def test_one_submitter_dominating_fails(self):
        """No single entrant may be half the event."""
        specs = [("b%02d" % i, 1800.0, "hoarder", "master") for i in range(10)]
        specs += [("c%02d" % i, 1800.0, "other%d" % i, "master") for i in range(6)]
        shape = norms.composition(field(specs))
        self.assertFalse(shape.ok)
        self.assertIn("holds", shape.why())

    def test_an_untitled_field_fails(self):
        """Fresh helpers are novices, so they cannot fill the titled quota."""
        shape = norms.composition(titled_field(12, league="novice"))
        self.assertFalse(shape.ok)
        self.assertIn("titled", shape.why())

    def test_the_average_rating_is_reported_not_enforced(self):
        """The strength floor differs per norm level and applies to a bot's own
        opponents, so `composition` measures it and `norm_earned` judges it."""
        shape = norms.composition(titled_field(12, rating=1500.0))
        self.assertTrue(shape.ok, shape.failures)
        self.assertLess(shape.average_rating, elo.NORM_REQUIREMENTS["im"][2])


class ReferenceBotsAreNotInvitedTest(unittest.TestCase):
    """The organizer's own bots play the ladder but never a norm event.

    Filtered inside `select_field` rather than by its caller, so a future caller
    cannot forget — `randy` is a coin flip and it banked an International Master
    norm off the first event that ran.
    """

    def test_they_are_dropped_however_highly_rated(self):
        specs = [("randy", 2500.0, "", "gm"), ("rocky", 2400.0, "", "gm"),
                 ("brainy", 2300.0, "", "gm"), ("copycat", 2200.0, "", "gm")]
        specs += [("b%02d" % i, 1800.0 - i, "who%d" % i, "master") for i in range(12)]
        chosen, shape = norms.select_field(field(specs))
        self.assertTrue(shape.ok, shape.failures)
        picked = {b.name for b in chosen}
        for name in ("randy", "rocky", "brainy", "copycat"):
            self.assertNotIn(name, picked)

    def test_an_entrant_who_shares_the_name_is_still_invited(self):
        """A submitter settles it — see `reference.is_reference`."""
        specs = [("randy", 2500.0, "ada@example.com", "gm")]
        specs += [("b%02d" % i, 1800.0 - i, "who%d" % i, "master") for i in range(12)]
        chosen, shape = norms.select_field(field(specs))
        self.assertTrue(shape.ok, shape.failures)
        self.assertIn("randy", {b.name for b in chosen})

    def test_dropping_them_can_cost_an_event(self):
        """Honest about the cost: they each counted as their own submitter, so they
        were propping up the diversity floor. A field that only reached the minimum
        with them in it no longer qualifies — which is the right answer, not a
        regression."""
        specs = [("randy", 1800.0, "", "master"), ("rocky", 1790.0, "", "master")]
        specs += [("b%02d" % i, 1780.0 - i, "who%d" % i, "master") for i in range(9)]
        chosen, _shape = norms.select_field(field(specs))
        self.assertIsNone(chosen)          # 9 entrants left, and 10 are needed


class OwnOppositionTest(unittest.TestCase):
    """How much of a bot's opposition its own owner supplied — FIDE's rule, per
    applicant, which is where the concentration check belongs."""

    def test_none_of_your_own_is_fine(self):
        self.assertTrue(norms.own_opposition_ok(0, 9))

    def test_a_third_is_the_line_and_is_allowed(self):
        """Three siblings among nine opponents sits exactly on the boundary, which
        is why the check is integer arithmetic rather than 3/9 <= 1/3."""
        self.assertTrue(norms.own_opposition_ok(3, 9))
        self.assertFalse(norms.own_opposition_ok(4, 9))

    def test_it_scales_with_the_field(self):
        self.assertTrue(norms.own_opposition_ok(4, 15))    # 4 of 15 is under a third
        self.assertFalse(norms.own_opposition_ok(6, 15))


class SelectFieldTest(unittest.TestCase):
    def test_it_invites_the_strongest_first(self):
        chosen, shape = norms.select_field(titled_field(30))
        self.assertTrue(shape.ok, shape.failures)
        ratings = [b.rating for b in chosen]
        self.assertEqual(ratings, sorted(ratings, reverse=True))
        # Nobody outside the field out-rates somebody inside it.
        everyone = {b.name: b.rating for b in titled_field(30)}
        floor = min(ratings)
        outside = [r for n, r in everyone.items()
                   if n not in {b.name for b in chosen}]
        self.assertTrue(all(r <= floor for r in outside))

    def test_it_prefers_a_stronger_field_to_a_bigger_one(self):
        """The bug this rule exists to stop.

        Filling more slots means reaching further down the ladder, and a norm also
        needs the opponents to average above a floor — so the biggest qualifying
        field can award nothing where a smaller one awards freely.
        """
        strong = [("s%02d" % i, 2000.0 - i, "who%d" % i, "master") for i in range(10)]
        weak = [("w%02d" % i, 1100.0 - i, "other%d" % i, "master") for i in range(10)]
        chosen, shape = norms.select_field(field(strong + weak))
        self.assertTrue(shape.ok, shape.failures)
        # It must not drag the weak bots in just to reach MAX_NORM_FIELD.
        self.assertEqual(len(chosen), norms.MIN_NORM_FIELD)
        self.assertTrue(all(b.name.startswith("s") for b in chosen))
        self.assertGreater(shape.average_rating, 1900)

    def test_selection_goes_purely_on_rating(self):
        """Selection does not police ownership, and must not.

        An earlier version skipped bots whose submitter had "filled its share",
        which passed over an entrant's genuine third-best in favour of one rated
        500 points lower and dragged the event under its own strength floor. Who
        owns what is checked when the norm is awarded (`own_opposition_ok`), not
        when the field is drawn.
        """
        specs = [("top%02d" % i, 1900.0 - i, "shared@example.com", "master")
                 for i in range(3)]
        specs += [("oth%02d" % i, 1800.0 - i, "other%d" % i, "master")
                  for i in range(12)]
        chosen, shape = norms.select_field(field(specs))
        self.assertTrue(shape.ok, shape.failures)
        # All three of the shared submitter's bots out-rate the rest, so all
        # three are invited rather than two of them being passed over.
        self.assertEqual(sum(1 for b in chosen if b.submitter == "shared@example.com"), 3)

    def test_a_field_half_owned_by_one_submitter_is_refused(self):
        """The loose backstop: no single entrant may be half the event.

        `ACTIVE_PER_SUBMITTER` means this cannot bind on a registry where that cap
        was applied — it catches one seeded by hand or predating it.
        """
        specs = [("hog%02d" % i, 2500.0 - i, "hoarder", "master") for i in range(20)]
        specs += [("oth%02d" % i, 1700.0 - i, "other%d" % i, "master")
                  for i in range(20)]
        chosen, shape = norms.select_field(field(specs))
        self.assertIsNone(chosen)
        self.assertIn("half the field", shape.why())

    def test_a_farmed_field_cannot_be_selected(self):
        """One person's untitled helpers plus a target: no qualifying field."""
        specs = [("target", 2200.0, "cartel", "master")]
        specs += [("help%02d" % i, 1500.0, "cartel", "novice") for i in range(24)]
        chosen, shape = norms.select_field(field(specs))
        self.assertIsNone(chosen)
        self.assertFalse(shape.ok)

    def test_it_shrinks_the_target_size_to_find_a_field(self):
        """Exactly enough bots for the minimum, and no more."""
        chosen, shape = norms.select_field(titled_field(norms.MIN_NORM_FIELD))
        self.assertTrue(shape.ok, shape.failures)
        self.assertEqual(len(chosen), norms.MIN_NORM_FIELD)

    def test_no_field_explains_itself(self):
        chosen, shape = norms.select_field(titled_field(4))
        self.assertIsNone(chosen)
        self.assertFalse(shape.ok)
        self.assertTrue(shape.why())

    def test_an_empty_registry_does_not_explode(self):
        chosen, shape = norms.select_field([])
        self.assertIsNone(chosen)
        self.assertFalse(shape.ok)

    def test_selection_is_deterministic(self):
        candidates = titled_field(30)
        first, _ = norms.select_field(candidates)
        second, _ = norms.select_field(list(reversed(candidates)))
        self.assertEqual([b.name for b in first], [b.name for b in second])


if __name__ == "__main__":
    unittest.main()
