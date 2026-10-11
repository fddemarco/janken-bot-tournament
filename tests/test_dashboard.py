import os
import sys
import unittest

import tempfile

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import dashboard
from janken import rating as elo
from janken.announce import AnnounceStore


class AnnouncementBannerTest(unittest.TestCase):
    """The banner renders through unsafe_allow_html, so what it escapes matters."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "t.db")
        self.markup = []
        real = dashboard.st.markdown
        dashboard.st.markdown = lambda body, **kw: self.markup.append(body)
        self.addCleanup(setattr, dashboard.st, "markdown", real)

    def test_nothing_posted_renders_nothing(self):
        """Including on a registry that does not exist yet: the banner renders
        before anything has created one, and must read as empty rather than
        throwing. Opening the store creates the file, the same way `Registry`
        does."""
        self.assertEqual(dashboard.announcements(self.path), 0)
        self.assertEqual(self.markup, [])

    def test_a_message_reaches_the_page(self):
        with AnnounceStore(self.path) as store:
            store.post("Tournaments are paused due to maintenance",
                       posted_by="ada")
        self.assertEqual(dashboard.announcements(self.path), 1)
        body = "".join(self.markup)
        self.assertIn("Tournaments are paused due to maintenance", body)
        self.assertIn('class="md-announce"', body)
        self.assertIn("ada", body)

    def test_markup_in_a_message_is_escaped(self):
        with AnnounceStore(self.path) as store:
            store.post("<script>alert(1)</script> & <b>bold</b>")
        dashboard.announcements(self.path)
        body = "".join(self.markup)
        self.assertNotIn("<script>", body)
        self.assertNotIn("<b>", body)
        self.assertIn("&lt;script&gt;", body)
        self.assertIn("&amp;", body)

    def test_every_announcement_looks_the_same(self):
        """One channel, one treatment. There is no severity to render."""
        with AnnounceStore(self.path) as store:
            store.post("one")
            store.post("two")
        self.assertEqual(dashboard.announcements(self.path), 2)
        self.assertEqual({m[:m.index(">") + 1] for m in self.markup},
                         {'<div class="md-announce">'})
        self.assertFalse(hasattr(dashboard, "ANNOUNCE_GLYPH"))

    def test_the_message_is_the_only_prominent_text(self):
        """No "ANNOUNCEMENT" label over the top of an announcement — the meta line
        carries who and when, which is the part the reader cannot infer."""
        with AnnounceStore(self.path) as store:
            store.post("Cup starts at 17:00", posted_by="yisus")
        dashboard.announcements(self.path)
        body = "".join(self.markup)
        self.assertNotIn("ANNOUNCEMENT", body.upper().replace("MD-ANNOUNCE", ""))
        self.assertIn("Posted by yisus", body)

    def test_the_navy_variant_is_asked_for_explicitly(self):
        with AnnounceStore(self.path) as store:
            store.post("hello")
        dashboard.announcements(self.path, on_navy=True)
        self.assertIn("on-navy", "".join(self.markup))

    def test_an_unreadable_registry_is_not_fatal(self):
        """A dashboard that cannot read the banner is still a dashboard."""
        self.assertEqual(
            dashboard.load_announcements(os.path.join(self.dir.name, "nope/x.db")),
            [])


def sample_bots():
    """Three bots, two submitters — shared by every test below that needs a
    registry frame.

    Every bot carries the same settled deviation, so the standings order here is
    decided by the rating alone. `test_the_deviation_decides_close_calls` is where
    that is varied on purpose.
    """
    return pd.DataFrame([
        {
            "id": "a", "name": "Alpha", "submitter": "ada@example.com",
            "league": "cm", "rating": 1610.0, "peak_rating": 1650.0,
            "rd": 30.0, "sigma": 0.06, "alt_rating": 1590.0, "alt_rd": 80.0,
            "alt_sigma": 0.06, "last_rated_at": "2026-01-01T00:00:00+00:00",
            "rated_games": 3, "banned": False, "held": False,
            "wins": 2, "draws": 0, "losses": 1, "forfeits": 0,
            "round_wins": 16, "round_losses": 14,
            "first_seen": "2026-01-01T00:00:00+00:00",
            "last_seen": "2026-01-01T00:00:00+00:00",
        },
        {
            "id": "b", "name": "Beta", "submitter": "ada@example.com",
            "league": "novice", "rating": 1490.0, "peak_rating": 1500.0,
            "rd": 30.0, "sigma": 0.06, "alt_rating": 1495.0, "alt_rd": 80.0,
            "alt_sigma": 0.06, "last_rated_at": "2026-01-02T00:00:00+00:00",
            "rated_games": 3, "banned": False, "held": True,
            "wins": 1, "draws": 1, "losses": 1, "forfeits": 0,
            "round_wins": 10, "round_losses": 20,
            "first_seen": "2026-01-02T00:00:00+00:00",
            "last_seen": "2026-01-02T00:00:00+00:00",
        },
        {
            "id": "c", "name": "Gamma", "submitter": "bob@example.com",
            "league": "master", "rating": 1710.0, "peak_rating": 1710.0,
            "rd": 30.0, "sigma": 0.06, "alt_rating": 1680.0, "alt_rd": 80.0,
            "alt_sigma": 0.06, "last_rated_at": "2026-01-03T00:00:00+00:00",
            "rated_games": 2, "banned": False, "held": False,
            "wins": 2, "draws": 0, "losses": 0, "forfeits": 0,
            "round_wins": 18, "round_losses": 2,
            "first_seen": "2026-01-03T00:00:00+00:00",
            "last_seen": "2026-01-03T00:00:00+00:00",
        },
    ])


class DashboardHelperTest(unittest.TestCase):
    def bots(self):
        return sample_bots()

    def test_the_deviation_decides_close_calls(self):
        """Ranked on `rating - rd`, so a barely-higher rating nobody trusts sits
        below a settled one.

        This is the whole reason the standings do not sort on the rating column
        they display: Alpha shows a higher number than Gamma here and still ranks
        behind it, because three games is not evidence of being 5 points better.
        """
        bots = sample_bots()
        bots.loc[bots["id"] == "a", ["rating", "rd"]] = [1715.0, 200.0]
        ranked = dashboard.ranked_bots(bots).set_index("id")
        self.assertGreater(ranked.loc["a", "rating"], ranked.loc["c", "rating"])
        self.assertEqual(ranked.loc["c", "Rank"], 1)
        self.assertEqual(ranked.loc["a", "Rank"], 2)

    def test_ranked_bots_assigns_position_only_to_active_bots(self):
        ranked = dashboard.ranked_bots(self.bots()).set_index("id")
        self.assertEqual(ranked.loc["c", "Rank"], 1)
        self.assertEqual(ranked.loc["a", "Rank"], 2)
        self.assertTrue(pd.isna(ranked.loc["b", "Rank"]))
        self.assertEqual(ranked.loc["b", "Status"], "Held")
        # Numeric, so clicking the header doesn't sort 1, 10, 2.
        self.assertEqual(ranked["Rank"].dtype, "Int64")


    def norms(self):
        """Two IM norms for Gamma, none for anyone else."""
        return pd.DataFrame([
            {"bot_id": "c", "played_at": "2026-02-0%dT00:00:00+00:00" % n,
             "title": "im", "performance": 2000.0, "field_avg": 1800.0,
             "games": 11}
            for n in (1, 2)
        ])

    def test_norm_progress_marks_a_rating_without_the_record(self):
        """A peak that reaches a norm-gated rung, with no norms behind it."""
        bots = self.bots()
        bots.loc[bots["id"] == "a", ["peak_rating", "league"]] = [1950.0, "master"]
        ranked = dashboard.ranked_bots(bots, pd.DataFrame()).set_index("id")
        self.assertTrue(ranked.loc["a", "Unproven"])
        self.assertEqual(ranked.loc["a", "Norms"], "0/2 IM")
        # A bot whose rating reaches only the rating-only rungs is not flagged.
        self.assertFalse(ranked.loc["c", "Unproven"])

    def test_norms_earned_clear_the_flag(self):
        bots = self.bots()
        bots.loc[bots["id"] == "c", ["peak_rating", "league"]] = [1950.0, "im"]
        ranked = dashboard.ranked_bots(bots, self.norms()).set_index("id")
        self.assertFalse(ranked.loc["c", "Unproven"])
        self.assertEqual(ranked.loc["c", "Norms"], "2/2 \u2713")
        self.assertEqual(ranked.loc["c", "norm_total"], 2)

    def test_a_registry_with_no_norms_table_still_renders(self):
        """The column has to exist even before any norm event has ever run."""
        ranked = dashboard.ranked_bots(self.bots(), None)
        self.assertIn("Norms", ranked.columns)
        self.assertFalse(ranked["Unproven"].any())
        self.assertTrue((ranked["Norms"] == "\u2014").all())

    def test_the_norms_table_reads_newest_first(self):
        table = dashboard._norms_table(self.norms(), "c")
        self.assertEqual(list(table["Earned"]), ["2026-02-02 00:00", "2026-02-01 00:00"])
        self.assertEqual(list(table["Level"]), ["IM", "IM"])
        self.assertEqual(list(table["Performed at"]), [2000, 2000])

    def test_the_norms_table_is_empty_for_a_bot_without_any(self):
        self.assertTrue(dashboard._norms_table(self.norms(), "a").empty)
        self.assertTrue(dashboard._norms_table(pd.DataFrame(), "c").empty)

    def test_h2h_frame_aggregates_for_selected_bot(self):
        matches = pd.DataFrame([
            {
                "played_at": "2026-01-01T00:00:00+00:00", "kind": "tournament",
                "bot_a": "a", "bot_b": "c", "wins_a": 7, "wins_b": 3,
                "draws": 0, "forfeit_a": "", "forfeit_b": "",
            },
            {
                "played_at": "2026-01-02T00:00:00+00:00", "kind": "tournament",
                "bot_a": "c", "bot_b": "a", "wins_a": 5, "wins_b": 5,
                "draws": 0, "forfeit_a": "", "forfeit_b": "",
            },
        ])
        h2h = dashboard._h2h_frame(self.bots(), matches, "a")
        self.assertEqual(h2h.loc[0, "Opponent"], "Gamma")
        self.assertEqual(h2h.loc[0, "Matches"], 2)
        # wins-draws-losses, the same order as every other record in the app —
        # this table used to print wins-losses-draws, so "1-0-1" meant a draw here
        # and a loss three columns to the left.
        self.assertEqual(h2h.loc[0, "Match W-D-L"], "1-1-0")
        self.assertEqual(h2h.loc[0, "Net rounds"], 4)
        self.assertAlmostEqual(h2h.loc[0, "Round win %"], 60.0)
        self.assertAlmostEqual(h2h.loc[0, "Match win %"], 50.0)

    def match(self, bot_a, bot_b, draws, kind="tournament"):
        return {
            "played_at": "2026-01-01T00:00:00+00:00", "kind": kind,
            "bot_a": bot_a, "bot_b": bot_b, "wins_a": 1, "wins_b": 1,
            "draws": draws, "forfeit_a": "", "forfeit_b": "",
        }

    def test_round_draws_counts_each_side_and_skips_other_kinds(self):
        matches = pd.DataFrame([
            self.match("a", "c", 2),
            self.match("a", "b", 1),          # both selected: counts for each
            self.match("a", "c", 9, "cup"),   # only tournaments feed round stats
        ])
        self.assertEqual(dashboard._round_draws(matches, ["a", "b"]), 4)

    def test_round_win_pct_counts_draws_against_the_bot(self):
        self.assertAlmostEqual(dashboard._round_win_pct(40, 50, 10), 40.0)
        self.assertEqual(dashboard._round_win_pct(0, 0, 0), 0)

    def test_every_record_prints_wins_draws_losses(self):
        """One helper, taking its arguments in the order it prints them."""
        self.assertEqual(dashboard._record(3, 2, 1), "3-2-1")
        self.assertIn("draws", dashboard.RECORD_ORDER)

    def test_match_win_pct_uses_the_same_rule_as_rounds(self):
        """Both percentages must count draws the same way, or showing the pair is
        worse than showing either one."""
        self.assertAlmostEqual(dashboard._match_win_pct(4, 4, 1), 400 / 9.0)
        self.assertAlmostEqual(dashboard._match_win_pct(7, 2, 0), 700 / 9.0)
        self.assertEqual(dashboard._match_win_pct(0, 0, 0), 0)

    def test_the_two_percentages_can_disagree_sharply(self):
        """The reason both are on screen: a bot can win most of its matches while
        losing the round count, by winning narrowly and losing heavily."""
        matches = pd.DataFrame([
            # Seven narrow wins...
            *[{"played_at": "2026-01-01T00:00:00+00:00", "kind": "tournament",
               "bot_a": "a", "bot_b": "c", "wins_a": 34, "wins_b": 33,
               "draws": 33, "forfeit_a": "", "forfeit_b": ""} for _ in range(7)],
            # ...and two heavy losses.
            *[{"played_at": "2026-01-01T00:00:00+00:00", "kind": "tournament",
               "bot_a": "a", "bot_b": "c", "wins_a": 5, "wins_b": 90,
               "draws": 5, "forfeit_a": "", "forfeit_b": ""} for _ in range(2)],
        ])
        h2h = dashboard._h2h_frame(self.bots(), matches, "a")
        self.assertAlmostEqual(h2h.loc[0, "Match win %"], 700 / 9.0)
        self.assertLess(h2h.loc[0, "Round win %"], 40.0)
        self.assertLess(h2h.loc[0, "Net rounds"], 0)

    def test_user_stats_summarize_submitter_bots(self):
        mine = dashboard.by_submitter(self.bots(), "ada@example.com")
        matches = pd.DataFrame([self.match("a", "c", 2), self.match("b", "c", 3)])
        stats = dashboard._user_stats(mine, matches)
        self.assertEqual(stats["registered"], 2)
        self.assertEqual(stats["active"], 1)
        self.assertEqual(stats["held"], 1)
        self.assertEqual(stats["best_rating"], 1610)
        self.assertEqual(stats["best_peak"], 1650)
        self.assertEqual(stats["best_title"], "CM")
        self.assertEqual(stats["record"], "3-1-2")
        self.assertEqual(stats["net_rounds"], -8)
        # 26 round wins, 34 losses, 5 draws — the draws are in the denominator.
        self.assertAlmostEqual(stats["round_win_pct"], 40.0)

    def test_user_profile_options_do_not_include_everyone(self):
        options = [o for o in dashboard.submitter_options(self.bots())
                   if o != dashboard.EVERYONE]
        self.assertNotIn(dashboard.EVERYONE, options)
        self.assertEqual(options, ["ada@example.com", "bob@example.com"])

    def test_bot_history_sorts_and_filters_to_one_bot(self):
        history = pd.DataFrame([
            {"played_at": "2026-01-02T00:00:00+00:00", "bot_id": "a", "rating": 1510, "rank": 2},
            {"played_at": "2026-01-01T00:00:00+00:00", "bot_id": "a", "rating": 1500, "rank": 3},
            {"played_at": "2026-01-01T00:00:00+00:00", "bot_id": "b", "rating": 1490, "rank": 4},
        ])
        filtered = dashboard._bot_history(history, "a")
        self.assertEqual(filtered["bot_id"].tolist(), ["a", "a"])
        self.assertEqual(filtered["rating"].tolist(), [1500, 1510])

    def test_your_bots_tab_requires_user_identity(self):
        self.assertNotIn("Your bots", dashboard.dashboard_tabs(None))
        self.assertIn("Your bots", dashboard.dashboard_tabs(
            {"email": "ada@example.com", "name": "Ada"}))


class BotPickerTest(unittest.TestCase):
    """A dropdown's value is an id and its label is a name; the two used to be
    packed into one string and split back apart, which a name containing the
    separator would have broken."""

    def bots(self):
        return sample_bots()

    def test_options_are_ids_the_selection_round_trips(self):
        options = dashboard.bot_search_options(self.bots())
        self.assertEqual(options[0], dashboard.ALL_BOTS)
        self.assertEqual(sorted(options[1:]), ["a", "b", "c"])
        for bot_id in ("a", "b", "c"):
            self.assertEqual(dashboard.selected_bot_id(bot_id), bot_id)

    def test_the_all_bots_entry_selects_nothing(self):
        self.assertEqual(dashboard.selected_bot_id(dashboard.ALL_BOTS), "")
        self.assertEqual(dashboard.selected_bot_id(""), "")

    def test_an_empty_registry_offers_only_the_all_entry(self):
        empty = self.bots().iloc[0:0]
        self.assertEqual(dashboard.bot_search_options(empty), [dashboard.ALL_BOTS])
        self.assertEqual(dashboard.bot_labeller(empty)(dashboard.ALL_BOTS),
                         dashboard.ALL_BOTS)

    def test_unique_names_are_labelled_bare(self):
        label = dashboard.bot_labeller(self.bots())
        self.assertEqual(label("a"), "Alpha")
        self.assertEqual(label("c"), "Gamma")
        self.assertEqual(label(dashboard.ALL_BOTS), dashboard.ALL_BOTS)

    def test_a_duplicated_name_gains_its_owner_in_the_dropdown(self):
        bots = self.bots()
        bots.loc[bots["id"] == "c", "name"] = "Alpha"     # same name as "a"
        label = dashboard.bot_labeller(bots)
        # "a" is ada's, "c" is bob's — the people tell them apart.
        self.assertEqual(label("a"), "Alpha · ada")
        self.assertEqual(label("c"), "Alpha · bob")
        self.assertNotEqual(label("a"), label("c"))

    def test_two_of_one_persons_bots_sharing_a_name_fall_back_to_ids(self):
        bots = self.bots()
        bots.loc[bots["id"] == "b", "name"] = "Alpha"     # ada owns both a and b
        label = dashboard.bot_labeller(bots)
        # Naming ada twice would separate nothing, so the id is what is left.
        self.assertEqual(label("a"), "Alpha · a")
        self.assertEqual(label("b"), "Alpha · b")

    def test_a_bot_with_no_registry_row_still_gets_a_short_label(self):
        # The removed-opponent case: never a 64-char digest in a cell.
        label = dashboard.bot_labeller(self.bots())
        gone = "f" * 64
        self.assertEqual(label(gone), "f" * 12)


class HiddenBotTest(unittest.TestCase):
    """A disabled bot is gone from every listing but still named in the record
    of the bots it played."""

    def everyone(self):
        """The full roster, with exactly the columns load_everyone selects —
        including a hidden bot that load_tables would have dropped."""
        frame = sample_bots()[["id", "name", "submitter", "held", "banned",
                               "rating", "league"]].copy()
        frame["disabled"] = [False, False, True]      # "c" is hidden
        return frame

    def visible(self):
        return sample_bots().iloc[:2]                 # what load_tables returns

    def test_a_hidden_opponent_keeps_its_name_and_is_marked(self):
        labels = dashboard.opponent_labels(self.everyone())
        self.assertEqual(labels["c"], "⊘ Gamma")

    def test_a_held_opponent_is_marked_too(self):
        labels = dashboard.opponent_labels(self.everyone())
        self.assertEqual(labels["b"], "⏸ Beta")       # "b" is held in the fixture

    def test_a_bot_in_the_field_is_not_marked(self):
        labels = dashboard.opponent_labels(self.everyone())
        self.assertEqual(labels["a"], "Alpha")

    def test_banned_wins_over_held(self):
        frame = self.everyone()
        frame.loc[frame["id"] == "b", "banned"] = True
        labels = dashboard.opponent_labels(frame)
        self.assertEqual(labels["b"], "⛔ Beta")

    def test_head_to_head_names_a_hidden_opponent(self):
        """Hiding one bot must not put a hex string in somebody else's table."""
        matches = pd.DataFrame([{
            "played_at": "2026-01-01T00:00:00+00:00", "kind": "tournament",
            "bot_a": "a", "bot_b": "c", "wins_a": 7, "wins_b": 3,
            "draws": 0, "forfeit_a": "", "forfeit_b": "",
        }])
        h2h = dashboard._h2h_frame(self.visible(), matches, "a", self.everyone())
        self.assertEqual(list(h2h["Opponent"]), ["⊘ Gamma"])

    def test_the_match_log_names_a_hidden_bot(self):
        matches = pd.DataFrame([{
            "played_at": "2026-01-01T00:00:00+00:00", "kind": "tournament",
            "bot_a": "a", "bot_b": "c", "wins_a": 7, "wins_b": 3,
            "draws": 0, "forfeit_a": "", "forfeit_b": "",
        }])
        log = dashboard._format_match_log(matches, self.visible(), self.everyone())
        self.assertEqual(list(log["Bot A"]), ["Alpha"])
        self.assertEqual(list(log["Bot B"]), ["⊘ Gamma"])

    def test_a_hidden_opponent_keeps_its_rating_in_the_record(self):
        """Its rating on the day is part of what the match means, and the column
        exists — so it is filled from the full roster, not blanked."""
        matches = pd.DataFrame([{
            "played_at": "2026-01-01T00:00:00+00:00", "kind": "tournament",
            "bot_a": "a", "bot_b": "c", "wins_a": 7, "wins_b": 3,
            "draws": 0, "forfeit_a": "", "forfeit_b": "",
        }])
        h2h = dashboard._h2h_frame(self.visible(), matches, "a", self.everyone())
        self.assertEqual(list(h2h["Rating"]), [1710])
        self.assertEqual(list(h2h["Title"]), [elo.abbr("master")])

    def test_an_unknown_opponent_leaves_the_rating_numeric(self):
        """One em dash in this column makes it an object column, which Arrow
        refuses to serialise — one deleted opponent used to take the whole
        table down rather than blanking one cell."""
        matches = pd.DataFrame([{
            "played_at": "2026-01-01T00:00:00+00:00", "kind": "tournament",
            "bot_a": "a", "bot_b": "f" * 64, "wins_a": 7, "wins_b": 3,
            "draws": 0, "forfeit_a": "", "forfeit_b": "",
        }])
        h2h = dashboard._h2h_frame(self.visible(), matches, "a", self.everyone())
        self.assertTrue(pd.isna(h2h["Rating"].iloc[0]))
        self.assertFalse(
            any(isinstance(v, str) for v in h2h["Rating"]),
            "Rating must stay numeric — a string in it breaks the whole table")

    def test_an_opponent_in_no_roster_at_all_still_gets_a_short_label(self):
        """`janken remove` leaves matches behind with no bot row anywhere."""
        matches = pd.DataFrame([{
            "played_at": "2026-01-01T00:00:00+00:00", "kind": "tournament",
            "bot_a": "a", "bot_b": "f" * 64, "wins_a": 7, "wins_b": 3,
            "draws": 0, "forfeit_a": "", "forfeit_b": "",
        }])
        h2h = dashboard._h2h_frame(self.visible(), matches, "a", self.everyone())
        self.assertEqual(list(h2h["Opponent"]), ["f" * 12])


class StatusMarkTest(unittest.TestCase):
    def test_each_state_gets_its_glyph(self):
        self.assertEqual(dashboard._status_mark("Held"), "⏸ Held")
        self.assertEqual(dashboard._status_mark("Banned"), "⛔ Banned")
        self.assertEqual(dashboard._status_mark("In field"), "✓ In field")

    def test_an_unknown_status_is_passed_through(self):
        self.assertEqual(dashboard._status_mark("Whatever"), "Whatever")

    def test_the_filter_keys_are_left_alone(self):
        """`ranked_bots` builds Status as a filter key; the multiselect above the
        standings matches its exact text, so the glyph belongs to the rendered
        table and not to the column."""
        ranked = dashboard.ranked_bots(sample_bots(), pd.DataFrame())
        self.assertEqual(set(ranked["Status"]) - {"In field", "Held", "Banned"},
                         set())


class StandingsNamingTest(unittest.TestCase):
    """The standings carry a Submitter column, so the name column should not
    repeat what it says."""

    def bots(self):
        return sample_bots()

    def _names(self, bots):
        from janken.naming import display_labels
        return display_labels(zip(bots["id"], bots["name"], bots["submitter"]),
                              submitter_shown=True)

    def test_two_owners_sharing_a_name_need_no_suffix_here(self):
        bots = self.bots()
        bots.loc[bots["id"] == "c", "name"] = "Alpha"
        names = self._names(bots)
        self.assertEqual(names["a"], "Alpha")
        self.assertEqual(names["c"], "Alpha")

    def test_one_owner_sharing_a_name_gets_the_id(self):
        bots = self.bots()
        bots.loc[bots["id"] == "b", "name"] = "Alpha"
        names = self._names(bots)
        self.assertNotEqual(names["a"], names["b"])


if __name__ == "__main__":
    unittest.main()
