import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from janken import glicko
from janken import rating as elo


class ExpectedScoreTest(unittest.TestCase):
    def test_equal_ratings_are_a_coin_flip(self):
        self.assertAlmostEqual(elo.expected(1000, 1000), 0.5)

    def test_expectations_are_complementary(self):
        self.assertAlmostEqual(elo.expected(1234, 1010) + elo.expected(1010, 1234), 1.0)

    def test_the_curve_is_the_ladder_s_own_scale(self):
        """Not Elo's 400-point curve, which is a logistic with 173.7178 baked in.

        Every performance rating and therefore every norm is measured with this
        curve, so a scale that disagrees with the one ratings are printed on would
        bias each one. Asserted against `glicko.SCALE` rather than a literal, so
        re-fitting the ladder cannot leave the two out of step.
        """
        for gap in (50.0, 200.0, 400.0, 900.0):
            self.assertAlmostEqual(
                elo.expected(1500.0 + gap, 1500.0),
                1.0 / (1.0 + math.exp(-gap / glicko.SCALE)), places=9)

    def test_a_bigger_gap_predicts_more(self):
        previous = 0.5
        for gap in (10.0, 100.0, 300.0, 600.0, 1200.0):
            now = elo.expected(1500.0 + gap, 1500.0)
            self.assertGreater(now, previous)
            previous = now
        self.assertLess(previous, 1.0)


class NoStepSizeTest(unittest.TestCase):
    """Glicko-2 replaced Elo, and with it the whole K-factor apparatus.

    How far a result moves a bot is now set by its own deviation and its
    opponents' — see `glicko.py`. There is no step to size, so there is no
    constant to tune, no per-period budget to split, and above all no career stage
    to read: the provisional tier that let a debut run apply a fast step to a whole
    field at once cannot come back through a door that no longer exists.
    """

    def test_the_k_factor_machinery_is_gone(self):
        for name in ("delta", "k_for_period", "k_factor", "K_MAX", "K_TOTAL",
                     "K_FACTOR", "K_PROVISIONAL", "PROVISIONAL_GAMES"):
            self.assertFalse(hasattr(elo, name), "rating.%s came back" % name)

    def test_what_survives_is_the_curve_and_the_titles(self):
        """`expected` stays: performance ratings and norms are written in it."""
        for name in ("expected", "performance_rating", "title_earned",
                     "norm_earned", "RATING_TITLES", "NORM_REQUIREMENTS"):
            self.assertTrue(hasattr(elo, name), "rating.%s went missing" % name)


class TitleTest(unittest.TestCase):
    def test_titles_are_ordered_weakest_first(self):
        self.assertEqual(elo.TITLES, ("novice", "cm", "master", "im", "gm"))
        floors = [floor for _, floor in elo.RATING_TITLES]
        self.assertEqual(floors, sorted(floors, reverse=True))
        # Every rating title is a real title, and none is handed out at the start.
        for name, floor in elo.RATING_TITLES:
            self.assertIn(name, elo.TITLES)
            self.assertGreater(floor, elo.START_RATING)
        # Every rung but the start is a floor, so nothing can out-rate the
        # ladder: the bot leading the standings always holds a title.
        self.assertIsNone(elo.floor_for("novice"))
        self.assertEqual(len(elo.RATING_TITLES), len(elo.TITLES) - 1)

    def test_the_first_title_is_the_cheapest_rung(self):
        """A bot that only just out-rates the field is a CM and nothing more.

        The lowest floor exists so a leader never reads `Novice`, not to hand
        out the higher titles early, so it sits strictly between the starting
        rating and the next rung up.
        """
        floor = elo.floor_for("cm")
        self.assertEqual(elo.RATING_TITLES[-1], ("cm", floor))   # the lowest floor
        self.assertGreater(floor, elo.START_RATING)
        self.assertLess(floor, elo.floor_for("master"))
        self.assertEqual(elo.title_for_rating(floor), "cm")
        self.assertEqual(elo.title_for_rating(floor - 0.5), "novice")

    def test_every_title_has_a_display_name(self):
        for title in elo.TITLES:
            self.assertNotEqual(elo.name(title), title)   # a real name, not the slug
            self.assertTrue(elo.abbr(title))
        # Abbreviations have to stay unique — the dashboard maps them back to slugs.
        abbrs = [elo.abbr(t) for t in elo.TITLES]
        self.assertEqual(len(set(abbrs)), len(abbrs))

    def test_title_tracks_the_peak_rating(self):
        self.assertEqual(elo.title_for_rating(elo.START_RATING), "novice")
        for name, floor in elo.RATING_TITLES:
            self.assertEqual(elo.title_for_rating(floor), name)
            self.assertNotEqual(elo.title_for_rating(floor - 1), name)
        self.assertEqual(elo.title_for_rating(9999), elo.TITLES[-1])

    def test_best_title_wins(self):
        self.assertEqual(elo.best_title("novice", "master"), "master")
        self.assertEqual(elo.best_title("gm", "cm"), "gm")
        self.assertEqual(elo.best_title("novice"), "novice")

    def test_ladder_reads_as_a_ladder(self):
        line = elo.ladder()
        for title in elo.TITLES:
            self.assertIn(elo.name(title), line)
        self.assertIn("2100+", line)             # the floors are spelled out
        self.assertIn("Candidate Master 1600+", line)
        # `novice` is the start, not a floor, so it carries no number.
        self.assertNotIn("Novice 1", line)


if __name__ == "__main__":
    unittest.main()
