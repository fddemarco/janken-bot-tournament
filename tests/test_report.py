import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from janken.match import MatchResult
from janken.report import render_html
from janken.tournament import Standing, TournamentResult


def result_of(matches, matches_per_pair):
    # Standings values are cosmetic for these tests; the H2H matrix is built
    # entirely from `matches`. A always ranks first so it is the top row.
    standings = [Standing(name="A"), Standing(name="B")]
    return TournamentResult(
        standings=standings, matches=matches, rounds=100,
        matches_per_pair=matches_per_pair,
    )


class HeadToHeadTest(unittest.TestCase):
    def test_pair_that_never_met_renders_blank(self):
        # A balanced random schedule (--games N) doesn't pair everybody, so the
        # matrix has to cope with cells that have no match behind them.
        standings = [Standing(name=n) for n in ("A", "B", "C")]
        result = TournamentResult(
            standings=standings,
            matches=[MatchResult(bot_a="A", bot_b="B", wins_a=6, wins_b=4)],
            rounds=10, matches_per_pair=1)
        html = render_html(result)
        self.assertIn('<td class="unplayed" title="did not meet">&middot;</td>', html)
        self.assertIn('<td class="win">6&ndash;4</td>', html)   # the pair that did meet

    def test_partial_forfeit_does_not_sink_a_won_pairing(self):
        # A forfeits one of three matches but wins the other two: the pairing
        # is A's (2-1 on matches), so its cell must read as a win, not forfeit.
        matches = [
            MatchResult(bot_a="A", bot_b="B", forfeit_a="crashed"),
            MatchResult(bot_a="A", bot_b="B", wins_a=90, wins_b=5),
            MatchResult(bot_a="A", bot_b="B", wins_a=80, wins_b=10),
        ]
        html = render_html(result_of(matches, matches_per_pair=3))

        # A's cell: win, round-win sum (170-15), flagged with a dagger.
        self.assertIn(
            '<td class="win" title="one or more matches forfeited">'
            "170&ndash;15&dagger;</td>",
            html,
        )
        # B's mirror cell: a plain loss, no dagger.
        self.assertIn('<td class="loss">15&ndash;170</td>', html)

    def test_full_forfeit_still_reads_forfeit(self):
        # The single-match case is unchanged: a forfeit shows "forfeit".
        matches = [MatchResult(bot_a="A", bot_b="B", forfeit_a="timed out")]
        html = render_html(result_of(matches, matches_per_pair=1))
        self.assertIn('<td class="loss">forfeit</td>', html)   # A forfeited
        self.assertIn('<td class="win">0&ndash;0</td>', html)  # B won by default

    def test_clean_pairing_colours_by_match_record(self):
        matches = [MatchResult(bot_a="A", bot_b="B", wins_a=60, wins_b=40)]
        html = render_html(result_of(matches, matches_per_pair=1))
        self.assertIn('<td class="win">60&ndash;40</td>', html)
        self.assertIn('<td class="loss">40&ndash;60</td>', html)


if __name__ == "__main__":
    unittest.main()
