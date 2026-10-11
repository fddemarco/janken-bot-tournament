"""Which bots are the organizer's own — the rule the cup and norm events share."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from janken import reference


class ReferenceBotTest(unittest.TestCase):
    def test_the_baseline_bots_are_named(self):
        self.assertEqual(reference.REFERENCE_BOTS,
                         frozenset({"rocky", "copycat", "brainy", "randy"}))

    def test_recognised_whatever_the_casing(self):
        # The registry title-cases display names; a field built from files gives
        # stems. Randy and randy are the same bot.
        for name in ("randy", "Randy", " RANDY "):
            self.assertTrue(reference.is_reference(name), name)

    def test_a_different_name_is_somebody_s_entry(self):
        for name in ("ada", "randynator", "rocky2", ""):
            self.assertFalse(reference.is_reference(name), name)

    def test_a_submitter_settles_it(self):
        """The clause that removes the ambiguity. An entrant who calls their bot
        `randy` is still an entrant — being barred from the cup and from norms for
        a naming coincidence would be a strange punishment."""
        self.assertFalse(reference.is_reference("randy", "ada@example.com"))
        self.assertFalse(reference.is_reference("Randy", "Yisus"))
        # ...and the organizer's own bots have none, however it is spelled.
        for blank in ("", "   ", "\t"):
            self.assertTrue(reference.is_reference("randy", blank), repr(blank))

    def test_a_submitter_does_not_promote_an_ordinary_bot(self):
        """The name still has to match — the submitter clause only ever exempts."""
        self.assertFalse(reference.is_reference("ada", ""))
        self.assertFalse(reference.is_reference("ada", "ada@example.com"))


if __name__ == "__main__":
    unittest.main()
