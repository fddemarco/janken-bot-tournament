import os
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from janken import round_winner
from janken.match import INVALID_MOVE_LIMIT, play_match
from janken.tournament import Standing, discover_bots, run_tournament


def write_bot(directory, name, body):
    path = os.path.join(directory, name + ".py")
    with open(path, "w") as f:
        f.write(textwrap.dedent(body))
    return path


class RulesTest(unittest.TestCase):
    def test_round_winner(self):
        self.assertEqual(round_winner("rock", "scissors"), 0)
        self.assertEqual(round_winner("scissors", "paper"), 0)
        self.assertEqual(round_winner("paper", "rock"), 0)
        self.assertEqual(round_winner("scissors", "rock"), 1)
        self.assertEqual(round_winner("rock", "rock"), -1)


class MatchTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.rock = write_bot(
            self.dir.name, "rock", "def play(m, o):\n    return 'rock'\n"
        )
        self.paper = write_bot(
            self.dir.name, "paper", "def play(m, o):\n    return 'paper'\n"
        )

    def test_deterministic_match(self):
        result = play_match(self.paper, self.rock, rounds=10)
        self.assertEqual((result.wins_a, result.wins_b, result.draws), (10, 0, 0))
        self.assertEqual(result.winner, "paper")

    def test_histories_reach_the_bot(self):
        counter = write_bot(
            self.dir.name,
            "counterer",
            """
            BEATS_ME = {'rock': 'paper', 'paper': 'scissors', 'scissors': 'rock'}
            def play(my_moves, opp_moves):
                assert len(my_moves) == len(opp_moves)
                if not opp_moves:
                    return 'scissors'
                return BEATS_ME[opp_moves[-1]]
            """,
        )
        result = play_match(counter, self.rock, rounds=10)
        # Loses round 1 (scissors vs rock), counters with paper ever after.
        self.assertEqual((result.wins_a, result.wins_b), (9, 1))

    def test_hanging_bot_forfeits(self):
        hangs = write_bot(
            self.dir.name,
            "hangs",
            "def play(m, o):\n    while True:\n        pass\n",
        )
        result = play_match(hangs, self.rock, rounds=5, timeout=0.3)
        self.assertIn("timed out", result.forfeit_a)
        self.assertIsNone(result.forfeit_b)
        self.assertEqual(result.winner, "rock")

    def test_crashing_bot_forfeits(self):
        crashy = write_bot(
            self.dir.name,
            "crashy",
            "def play(m, o):\n    raise RuntimeError('boom')\n",
        )
        result = play_match(self.rock, crashy, rounds=5)
        self.assertIsNotNone(result.forfeit_b)
        self.assertEqual(result.winner, "rock")

    def test_broken_import_forfeits_at_startup(self):
        broken = write_bot(self.dir.name, "broken", "this is not python\n")
        result = play_match(broken, self.rock, rounds=5)
        self.assertIn("failed to start", result.forfeit_a)
        self.assertEqual(result.winner, "rock")

    def test_a_few_invalid_moves_cost_only_their_rounds(self):
        """Under the limit, an invalid move is a lost round and nothing more."""
        slips = INVALID_MOVE_LIMIT - 1
        weirdo = write_bot(
            self.dir.name,
            "weirdo",
            """
            def play(my_moves, opp_moves):
                if len(my_moves) < %d:
                    return 'banana'
                return 'paper'
            """ % slips,
        )
        result = play_match(weirdo, self.rock, rounds=10)
        self.assertIsNone(result.forfeit_a)
        # Paper beats rock, so every legal round is a win; the junk ones are lost.
        self.assertEqual((result.wins_a, result.wins_b), (10 - slips, slips))

    def test_invalid_moves_forfeit_the_match_at_the_limit(self):
        weirdo = write_bot(
            self.dir.name, "weirdo", "def play(m, o):\n    return 'banana'\n")
        result = play_match(weirdo, self.rock, rounds=50)
        self.assertEqual(result.forfeit_a, "returned %d invalid moves" % INVALID_MOVE_LIMIT)
        self.assertEqual(result.winner, "rock")
        # It stopped at the limit instead of burning all fifty rounds, and the
        # round that tipped it over still counted: the move did arrive.
        self.assertEqual(len(result.moves) // 2, INVALID_MOVE_LIMIT)
        self.assertEqual(result.wins_b, INVALID_MOVE_LIMIT)

    def test_the_limit_is_cumulative_not_consecutive(self):
        """Junk spaced out between legal moves still adds up — otherwise a bot
        could alternate and emit garbage forever."""
        weirdo = write_bot(
            self.dir.name,
            "weirdo",
            """
            def play(my_moves, opp_moves):
                if len(my_moves) % 2 == 0:
                    return 'banana'
                return 'paper'
            """,
        )
        result = play_match(weirdo, self.rock, rounds=100)
        self.assertEqual(result.forfeit_a, "returned %d invalid moves" % INVALID_MOVE_LIMIT)
        # Alternating, so the limit lands on round 2*limit - 1.
        self.assertEqual(len(result.moves) // 2, 2 * INVALID_MOVE_LIMIT - 1)

    def test_stdout_printer_does_not_corrupt_protocol(self):
        loud = write_bot(
            self.dir.name,
            "loud",
            """
            def play(m, o):
                print('shouting on stdout')
                return 'paper'
            """,
        )
        result = play_match(loud, self.rock, rounds=5)
        self.assertIsNone(result.forfeit_a)
        self.assertEqual(result.wins_a, 5)

    def test_transcript_records_every_scored_round(self):
        result = play_match(self.paper, self.rock, rounds=10)
        self.assertEqual(result.moves, "pr" * 10)

    def test_transcript_marks_invalid_moves(self):
        weirdo = write_bot(
            self.dir.name, "weirdo", "def play(m, o):\n    return 'banana'\n"
        )
        result = play_match(weirdo, self.rock, rounds=3)
        self.assertEqual(result.moves, "xr" * 3)

    def test_transcript_stops_at_a_forfeit(self):
        crashy = write_bot(
            self.dir.name,
            "crashy",
            """
            def play(my_moves, opp_moves):
                if len(my_moves) == 2:
                    raise RuntimeError('boom')
                return 'paper'
            """,
        )
        result = play_match(crashy, self.rock, rounds=10)
        # Two rounds scored, then the crash: nothing is recorded for the round
        # that never completed.
        self.assertEqual(result.moves, "pr" * 2)
        self.assertEqual(
            len(result.moves),
            2 * (result.wins_a + result.wins_b + result.draws),
        )

    def test_sudden_death_breaks_a_level_match(self):
        late = write_bot(
            self.dir.name,
            "late",
            """
            def play(my_moves, opp_moves):
                return 'rock' if len(my_moves) < 4 else 'paper'
            """,
        )
        result = play_match(late, self.rock, rounds=4, sudden_death=10)
        # Four scheduled rounds all draw rock-v-rock, then one decider.
        self.assertEqual(result.draws, 4)
        self.assertEqual(result.sudden_death, 1)
        self.assertEqual((result.wins_a, result.wins_b), (1, 0))
        self.assertEqual(result.winner, "late")

    def test_sudden_death_gives_up_on_a_true_stalemate(self):
        other = write_bot(
            self.dir.name, "otherrock", "def play(m, o):\n    return 'rock'\n"
        )
        result = play_match(self.rock, other, rounds=3, sudden_death=5)
        self.assertEqual(result.sudden_death, 5)      # capped, not infinite
        self.assertEqual((result.wins_a, result.wins_b), (0, 0))
        self.assertIsNone(result.winner)              # the cup breaks this on seed

    def test_sudden_death_is_off_by_default(self):
        other = write_bot(
            self.dir.name, "otherrock", "def play(m, o):\n    return 'rock'\n"
        )
        result = play_match(self.rock, other, rounds=3)
        self.assertEqual(result.sudden_death, 0)
        self.assertTrue(result.is_draw)

    def test_slow_opponent_does_not_penalize_fast_bot(self):
        hangs = write_bot(
            self.dir.name, "hangs", "def play(m, o):\n    while True:\n        pass\n"
        )
        result = play_match(hangs, self.rock, rounds=3, timeout=0.3)
        self.assertIsNotNone(result.forfeit_a)
        self.assertIsNone(result.forfeit_b)


class TournamentTest(unittest.TestCase):
    def test_round_robin_standings(self):
        with tempfile.TemporaryDirectory() as d:
            write_bot(d, "rock", "def play(m, o):\n    return 'rock'\n")
            write_bot(d, "paper", "def play(m, o):\n    return 'paper'\n")
            write_bot(d, "scissors", "def play(m, o):\n    return 'scissors'\n")
            result = run_tournament(discover_bots(d), rounds=10)
            # Perfect rock-paper-scissors cycle: everyone 1 win, 1 loss.
            self.assertEqual(len(result.matches), 3)
            for s in result.standings:
                self.assertEqual(s.points, 3)
                self.assertEqual((s.wins, s.losses), (1, 1))

    def test_random_schedule_is_balanced(self):
        import random
        from collections import Counter
        from janken.tournament import _random_schedule
        random.seed(1234)
        bots = ["/b/%d.py" % i for i in range(12)]
        sched = _random_schedule(bots, 6)
        deg = Counter()
        for a, b in sched:
            self.assertNotEqual(a, b)            # never a self-match
            deg[a] += 1
            deg[b] += 1
        self.assertEqual(len(sched), 36)         # 12 bots * 6 games / 2
        self.assertTrue(all(d == 6 for d in deg.values()))  # balanced

    def test_random_schedule_tournament_runs(self):
        with tempfile.TemporaryDirectory() as d:
            paths = [
                write_bot(d, nm, "def play(m, o):\n    return %r\n" % mv)
                for nm, mv in [("a", "rock"), ("b", "paper"),
                               ("c", "scissors"), ("e", "rock")]
            ]
            result = run_tournament(paths, rounds=5, games_per_bot=2)
            self.assertEqual(len(result.matches), 4)   # 4 bots * 2 / 2
            self.assertEqual(len(result.standings), 4)

    def test_underscore_files_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            write_bot(d, "a", "def play(m, o):\n    return 'rock'\n")
            write_bot(d, "b", "def play(m, o):\n    return 'paper'\n")
            write_bot(d, "_helper", "def play(m, o):\n    return 'rock'\n")
            paths = discover_bots(d)
            self.assertEqual([os.path.basename(p) for p in paths], ["a.py", "b.py"])

    def test_sort_key_tiebreakers(self):
        strong = Standing("strong", points=6, round_wins=50, round_losses=10)
        weak = Standing("weak", points=6, round_wins=30, round_losses=20)
        self.assertLess(strong.sort_key(), weak.sort_key())


if __name__ == "__main__":
    unittest.main()
