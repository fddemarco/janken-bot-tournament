import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from janken.naming import SEPARATOR, display_labels, label_for, person
from janken.registry import SHORT_ID

# Distinct 64-char ids, the shape the registry actually stores.
A = "a3f9c1d4e07b" + "0" * 52
B = "7b21e0fa9c34" + "1" * 52
C = "4c1e88ba9f02" + "2" * 52


class PersonTest(unittest.TestCase):
    def test_an_email_shows_as_its_local_part(self):
        self.assertEqual(person("ivan.terzano@muttdata.ai"), "ivan.terzano")

    def test_a_plain_string_survives_intact(self):
        self.assertEqual(person("Robotics Club"), "Robotics Club")

    def test_no_submitter_is_empty(self):
        self.assertEqual(person(""), "")
        self.assertEqual(person("   "), "")

    def test_an_address_with_no_local_part_is_not_truncated_away(self):
        # "@muttdata.ai" has nothing before the @; returning "" there would
        # silently drop the only attribution the bot has.
        self.assertEqual(person("@muttdata.ai"), "@muttdata.ai")


class UniqueNameTest(unittest.TestCase):
    def test_a_name_that_appears_once_is_printed_alone(self):
        labels = display_labels([(A, "Brainy Boy", "ivan@muttdata.ai")])
        self.assertEqual(labels[A], "Brainy Boy")

    def test_distinct_names_never_gain_a_suffix(self):
        labels = display_labels([
            (A, "Brainy Boy", "ivan@muttdata.ai"),
            (B, "Rocky II", "ivan@muttdata.ai"),
        ])
        self.assertEqual(labels[A], "Brainy Boy")
        self.assertEqual(labels[B], "Rocky II")

    def test_a_nameless_bot_falls_back_to_its_short_id(self):
        labels = display_labels([(A, "", "ivan@muttdata.ai")])
        self.assertEqual(labels[A], A[:SHORT_ID])


class DuplicateNameTest(unittest.TestCase):
    def test_two_owners_are_told_apart_by_person(self):
        labels = display_labels([
            (A, "Rocky II", "ivan@muttdata.ai"),
            (B, "Rocky II", "sofia@muttdata.ai"),
        ])
        self.assertEqual(labels[A], "Rocky II" + SEPARATOR + "ivan")
        self.assertEqual(labels[B], "Rocky II" + SEPARATOR + "sofia")

    def test_one_owner_with_two_same_named_bots_falls_through_to_ids(self):
        labels = display_labels([
            (A, "Rocky II", "ivan@muttdata.ai"),
            (B, "Rocky II", "ivan@muttdata.ai"),
        ])
        self.assertEqual(labels[A], "Rocky II" + SEPARATOR + A[:SHORT_ID])
        self.assertEqual(labels[B], "Rocky II" + SEPARATOR + B[:SHORT_ID])

    def test_colliding_local_parts_escalate_to_the_full_address(self):
        labels = display_labels([
            (A, "Rocky II", "ivan@muttdata.ai"),
            (B, "Rocky II", "ivan@example.com"),
        ])
        self.assertEqual(labels[A], "Rocky II" + SEPARATOR + "ivan@muttdata.ai")
        self.assertEqual(labels[B], "Rocky II" + SEPARATOR + "ivan@example.com")

    def test_names_differing_only_in_case_are_treated_as_ambiguous(self):
        labels = display_labels([
            (A, "Rocky II", "ivan@muttdata.ai"),
            (B, "rocky ii", "sofia@muttdata.ai"),
        ])
        self.assertEqual(labels[A], "Rocky II" + SEPARATOR + "ivan")
        # The name is printed as stored — only the *grouping* is case-folded.
        self.assertEqual(labels[B], "rocky ii" + SEPARATOR + "sofia")

    def test_an_unattributed_bot_drops_the_whole_group_to_ids(self):
        # Labelling two of three by person and the third by hash would put two
        # rules in one table, so the group shares the rule that works for all.
        labels = display_labels([
            (A, "Rocky II", "ivan@muttdata.ai"),
            (B, "Rocky II", "sofia@muttdata.ai"),
            (C, "Rocky II", ""),
        ])
        for bot_id in (A, B, C):
            self.assertEqual(labels[bot_id],
                             "Rocky II" + SEPARATOR + bot_id[:SHORT_ID])

    def test_only_the_duplicated_name_pays_for_the_duplication(self):
        labels = display_labels([
            (A, "Rocky II", "ivan@muttdata.ai"),
            (B, "Rocky II", "sofia@muttdata.ai"),
            (C, "Brainy Boy", "ivan@muttdata.ai"),
        ])
        self.assertEqual(labels[C], "Brainy Boy")
        self.assertNotEqual(labels[A], labels[B])

    def test_every_label_in_a_list_is_distinct(self):
        entries = [
            (A, "Rocky II", "ivan@muttdata.ai"),
            (B, "Rocky II", "ivan@muttdata.ai"),
            (C, "Rocky II", "sofia@muttdata.ai"),
        ]
        labels = display_labels(entries)
        self.assertEqual(len(set(labels.values())), len(entries))


class SubmitterShownTest(unittest.TestCase):
    """A table with its own Submitter column needs less help, not more."""

    def test_different_owners_need_no_suffix_at_all(self):
        labels = display_labels([
            (A, "Rocky II", "ivan@muttdata.ai"),
            (B, "Rocky II", "sofia@muttdata.ai"),
        ], submitter_shown=True)
        self.assertEqual(labels[A], "Rocky II")
        self.assertEqual(labels[B], "Rocky II")

    def test_one_owner_with_two_same_named_bots_still_needs_the_id(self):
        # No column can separate these two, so the id is the only answer.
        labels = display_labels([
            (A, "Rocky II", "ivan@muttdata.ai"),
            (B, "Rocky II", "ivan@muttdata.ai"),
        ], submitter_shown=True)
        self.assertEqual(labels[A], "Rocky II" + SEPARATOR + A[:SHORT_ID])
        self.assertEqual(labels[B], "Rocky II" + SEPARATOR + B[:SHORT_ID])

    def test_the_submitter_is_never_appended_in_this_mode(self):
        labels = display_labels([
            (A, "Rocky II", "ivan@muttdata.ai"),
            (B, "Rocky II", "ivan@muttdata.ai"),
        ], submitter_shown=True)
        for label in labels.values():
            self.assertNotIn("ivan", label)

    def test_unattributed_duplicates_are_told_apart_by_id(self):
        labels = display_labels([
            (A, "Randy", ""),
            (B, "Randy", ""),
        ], submitter_shown=True)
        self.assertEqual(labels[A], "Randy" + SEPARATOR + A[:SHORT_ID])
        self.assertEqual(labels[B], "Randy" + SEPARATOR + B[:SHORT_ID])


class LabelForTest(unittest.TestCase):
    def test_a_labelled_bot_gets_its_label(self):
        labels = display_labels([(A, "Brainy Boy", "ivan@muttdata.ai")])
        self.assertEqual(label_for(labels, A), "Brainy Boy")

    def test_a_bot_missing_from_the_list_falls_back_to_a_short_id(self):
        # The removed-opponent case: it is in somebody's match log but has no
        # registry row, and a 64-char digest in that cell is the bug we fixed.
        self.assertEqual(label_for({}, B), B[:SHORT_ID])
        self.assertEqual(len(label_for({}, B)), SHORT_ID)


if __name__ == "__main__":
    unittest.main()
