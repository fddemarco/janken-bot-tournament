import os
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from janken import broadcast, cup
from janken.match import MatchResult


def write_bot(directory, name, body):
    path = os.path.join(directory, name + ".py")
    with open(path, "w") as f:
        f.write(textwrap.dedent(body))
    return path


def field(n, ratings=None):
    """n entrants, distinctly rated so seeding is unambiguous."""
    return [
        cup.Entrant(
            name="bot%02d" % i,
            path="/nowhere/bot%02d.py" % i,
            rating=(ratings[i] if ratings else 2000 - i),
        )
        for i in range(n)
    ]


def result(wins_a, wins_b, draws=0, **kw):
    return MatchResult(bot_a="a", bot_b="b", wins_a=wins_a, wins_b=wins_b,
                       draws=draws, **kw)


class SeedOrderTest(unittest.TestCase):
    def test_known_orders(self):
        self.assertEqual(cup.seed_order(2), [1, 2])
        self.assertEqual(cup.seed_order(4), [1, 4, 2, 3])
        self.assertEqual(cup.seed_order(8), [1, 8, 4, 5, 2, 7, 3, 6])

    def test_every_seed_appears_once(self):
        for size in (2, 4, 8, 16, 32, 64):
            order = cup.seed_order(size)
            self.assertEqual(sorted(order), list(range(1, size + 1)))

    def test_paired_seeds_always_sum_to_one_more_than_the_bracket(self):
        # The defining property of the first round: 1 plays the weakest, and
        # so on inward.
        for size in (8, 16, 32, 64):
            order = cup.seed_order(size)
            for i in range(0, size, 2):
                self.assertEqual(order[i] + order[i + 1], size + 1)


class PlanTest(unittest.TestCase):
    def test_power_of_two_field_has_no_play_in(self):
        for n in (2, 4, 8, 16, 32, 64):
            plan = cup.plan(field(n))
            self.assertEqual(plan.bracket_size, n)
            # A knockout is n-1 matches, plus the third-place playoff wherever
            # there are semi-finals to lose.
            self.assertEqual(len(plan.matches), n - 1 + (1 if n >= 4 else 0))
            # The playoff is its own round, revealed apart from the final,
            # so a bracket with semi-finals shows a second round of size 2.
            self.assertEqual([size for size, _ in plan.rounds()],
                             [n >> i for i in range(n.bit_length() - 1)]
                             + ([2] if n >= 4 else []))

    def test_seeding_follows_rating(self):
        plan = cup.plan(field(8))
        self.assertEqual([e.seed for e in plan.entrants], list(range(1, 9)))
        self.assertEqual(plan.entrants[0].name, "bot00")   # highest rated
        self.assertEqual(plan.entrants[-1].name, "bot07")

    def test_seeding_breaks_rating_ties_by_name(self):
        tied = [cup.Entrant(name=n, path="/x/%s.py" % n, rating=1500.0)
                for n in ("zeta", "alpha", "mid")]
        plan = cup.plan(tied)
        self.assertEqual([e.name for e in plan.entrants],
                         ["alpha", "mid", "zeta"])

    def test_top_two_seeds_can_only_meet_in_the_final(self):
        for n in (4, 8, 16, 32, 64):
            plan = cup.plan(field(n))
            _advance_by_seed(plan)
            final = plan.matches[-1]
            self.assertEqual({final.a.seed, final.b.seed}, {1, 2},
                             "field of %d" % n)
            self.assertEqual(plan.champion.seed, 1)

    def test_play_in_fills_the_bracket_instead_of_leaving_byes(self):
        plan = cup.plan(field(20))
        self.assertEqual(plan.bracket_size, 16)
        rounds = plan.rounds()
        self.assertEqual([size for size, _ in rounds], [20, 16, 8, 4, 2, 2])
        self.assertEqual(len(rounds[0][1]), 4)      # 4 play-in matches
        self.assertEqual(len(plan.matches), 4 + 15 + 1)   # + third place
        # The play-in pairs the lowest 8 seeds, highest against lowest.
        pairs = [(plan.matches[i].a.seed, plan.matches[i].b.seed)
                 for i in rounds[0][1]]
        self.assertEqual(pairs, [(13, 20), (14, 19), (15, 18), (16, 17)])

    def test_play_in_winners_inherit_the_seed_lines_they_fought_for(self):
        plan = cup.plan(field(6))
        self.assertEqual(plan.bracket_size, 4)
        # Seeds 3-6 play in for the two bottom lines; 1 and 2 go straight in.
        play_in = plan.matches[:2]
        self.assertEqual([(m.a.seed, m.b.seed) for m in play_in],
                         [(3, 6), (4, 5)])
        semis = plan.matches[2:4]
        self.assertEqual(semis[0].a.seed, 1)
        self.assertEqual(semis[0].b_from, 1)        # winner of the 4-v-5 line
        self.assertEqual(semis[1].a.seed, 2)
        self.assertEqual(semis[1].b_from, 0)        # winner of the 3-v-6 line

    def test_no_bracket_slot_is_left_unfed(self):
        for n in (5, 6, 7, 13, 20, 33, 50):
            plan = cup.plan(field(n))
            for i, m in enumerate(plan.matches):
                for side in ("a", "b"):
                    filled = getattr(m, side) is not None
                    fed = getattr(m, side + "_from") is not None
                    self.assertTrue(filled != fed,
                                    "field %d match %d side %s" % (n, i, side))

    def test_every_entrant_enters_exactly_once(self):
        for n in (5, 6, 16, 20, 33):
            plan = cup.plan(field(n))
            seeds = [getattr(m, side).seed
                     for m in plan.matches for side in ("a", "b")
                     if getattr(m, side) is not None]
            self.assertEqual(sorted(seeds), list(range(1, n + 1)))

    def test_two_entrants_is_just_a_final(self):
        plan = cup.plan(field(2))
        self.assertEqual(len(plan.matches), 1)
        self.assertEqual(plan.stage_of(plan.matches[0]), "Final")

    def test_a_cup_needs_two_entrants(self):
        with self.assertRaises(ValueError):
            cup.plan(field(1))


class ThirdPlaceTest(unittest.TestCase):
    """The one bracket edge that runs downward: the playoff takes the two bots
    the semi-finals knocked out."""

    def test_the_playoff_is_fed_by_the_semi_final_losers(self):
        plan = cup.plan(field(8))
        playoff = next(m for m in plan.matches if m.label == cup.THIRD_PLACE)
        semis = [i for i, m in enumerate(plan.matches) if m.round_size == 4]
        self.assertEqual([playoff.a_from, playoff.b_from], semis)
        self.assertEqual([playoff.a_takes, playoff.b_takes], ["loser", "loser"])

    def test_it_is_the_round_immediately_before_the_final(self):
        plan = cup.plan(field(8))
        stages = [plan.stage_of(plan.matches[ix[0]]) for _size, ix in plan.rounds()]
        self.assertEqual(stages[-2:], [cup.THIRD_PLACE, "Final"])
        # The champion is read off the last match, so the final must stay last.
        self.assertEqual(plan.stage_of(plan.matches[-1]), "Final")

    def test_the_losers_actually_arrive(self):
        plan = cup.plan(field(8))
        _advance_by_seed(plan)
        playoff = next(m for m in plan.matches if m.label == cup.THIRD_PLACE)
        # Seeds 1-4 reach the semis; 1 and 2 go through, so 3 and 4 play off.
        self.assertEqual({playoff.a.seed, playoff.b.seed}, {3, 4})

    def test_no_playoff_without_semi_finals(self):
        plan = cup.plan(field(2))
        self.assertEqual([m.label for m in plan.matches], [""])

    def test_it_can_be_turned_off(self):
        plan = cup.plan(field(8), third_place=False)
        self.assertEqual(len(plan.matches), 7)
        self.assertFalse(any(m.label for m in plan.matches))

    def test_the_playoff_is_the_undercard_not_the_main_event(self):
        """Same round, same treatment — so without a discount a thrilling
        playoff would out-air a one-sided final."""
        from janken.cupstore import CupStore

        plan = cup.plan(field(4))
        targets = cup.feed_map(plan)
        for i, m in enumerate(plan.matches):
            rounds = cup.rounds_for(m.round_size)
            # Identical, gripping transcripts, so only the discount differs.
            m.result = MatchResult(bot_a="a", bot_b="b", wins_a=rounds // 2,
                                   wins_b=rounds // 2, draws=0,
                                   moves=swinging(rounds))
            m.winner, m.decided_by = cup.decide(m, m.result)
            cup.send_on(plan, i, targets)
        plan.champion = plan.matches[-1].winner

        every = [m for r in broadcast.script(plan)["rounds"] for m in r["matches"]]
        playoff = next(m for m in every if m["stage"] == cup.THIRD_PLACE)
        final = next(m for m in every if m["stage"] == "Final")
        self.assertLess(playoff["budget_ms"], final["budget_ms"])
        self.assertLess(playoff["duration_ms"], final["duration_ms"])

    def test_the_podium_ranks_all_three(self):
        plan = _decided(field(8))
        podium = broadcast.script(plan)["podium"]
        self.assertEqual([(p["place"], p["title"], p["entrant"]["name"])
                          for p in podium],
                         [(1, "Champion", "bot00"),
                          (2, "Runner-up", "bot01"),
                          (3, "Third", "bot02")])

    def test_without_a_playoff_there_is_no_third(self):
        """The two beaten semi-finalists are genuinely tied — putting one on
        the box would be inventing a result nobody played."""
        plan = _decided(field(8), third_place=False)
        podium = broadcast.script(plan)["podium"]
        self.assertEqual([p["place"] for p in podium], [1, 2])

    def test_the_podium_is_empty_until_the_final_is_decided(self):
        plan = cup.plan(field(8))
        self.assertEqual(broadcast.script(plan)["podium"], [])

    def test_the_loser_is_none_until_the_match_is_decided(self):
        plan = cup.plan(field(2))
        match = plan.matches[0]
        self.assertIsNone(match.loser)
        match.winner, match.decided_by = cup.decide(match, result(60, 40))
        self.assertIs(match.loser, match.b)

    def test_a_forfeited_semi_still_sends_someone_to_the_playoff(self):
        # The loser of a match nobody really won is still a loser, and the
        # playoff slot has to be filled either way.
        plan = cup.plan(field(2))
        match = plan.matches[0]
        match.winner, match.decided_by = cup.decide(
            match, result(0, 0, forfeit_a="crashed", forfeit_b="crashed"))
        self.assertIsNotNone(match.loser)
        self.assertIsNot(match.loser, match.winner)


class StageTest(unittest.TestCase):
    def test_names_are_keyed_on_bots_remaining(self):
        self.assertEqual(cup.stage_name(2, 16), "Final")
        self.assertEqual(cup.stage_name(4, 16), "Semi-final")
        self.assertEqual(cup.stage_name(8, 16), "Quarter-final")
        self.assertEqual(cup.stage_name(16, 16), "Round of 16")
        self.assertEqual(cup.stage_name(20, 16), "Play-in")

    def test_match_length_grows_as_the_field_narrows(self):
        """Multiples of an ordinary rated match, so a cup's early rounds can never
        end up shorter than a routine one — which is what happened while these were
        fixed at 100/200/300 and the league moved to 200."""
        base = cup.DEFAULT_ROUNDS
        self.assertEqual(cup.rounds_for(32), base)
        self.assertEqual(cup.rounds_for(8), base)
        self.assertEqual(cup.rounds_for(4), 2 * base)
        self.assertEqual(cup.rounds_for(2), 3 * base)

    def test_the_base_is_the_league_s_match_length(self):
        """Explicit `base` for a caller that wants a shorter rehearsal, and the
        default is whatever an ordinary match is."""
        self.assertEqual(cup.rounds_for(8, base=50), 50)
        self.assertEqual(cup.rounds_for(2, base=50), 150)
        self.assertGreaterEqual(cup.rounds_for(8), cup.DEFAULT_ROUNDS)

    def test_wide_rounds_are_a_reveal_and_the_final_is_played_out(self):
        self.assertEqual(cup.treatment(64), "reveal")
        self.assertEqual(cup.treatment(32), "reveal")
        self.assertEqual(cup.treatment(16), "grid")
        self.assertEqual(cup.treatment(8), "featured")
        self.assertEqual(cup.treatment(4), "hero")     # semis get the screen
        self.assertEqual(cup.treatment(2), "hero")


class AdvancementTest(unittest.TestCase):
    """The rule has to be total: every result maps to exactly one winner."""

    def setUp(self):
        entrants = field(2)                      # bot00 is seed 1, bot01 seed 2
        self.plan = cup.plan(entrants)
        self.match = self.plan.matches[0]
        self.top, self.bottom = self.match.a, self.match.b
        self.assertEqual(self.top.seed, 1)

    def test_more_round_wins_advances(self):
        winner, why = cup.decide(self.match, result(60, 40))
        self.assertIs(winner, self.top)
        self.assertEqual(why, "score")

    def test_sudden_death_is_labelled_as_such(self):
        winner, why = cup.decide(self.match, result(51, 50, sudden_death=1))
        self.assertIs(winner, self.top)
        self.assertEqual(why, "sudden death")

    def test_a_forfeit_hands_the_match_over(self):
        winner, why = cup.decide(self.match, result(99, 0, forfeit_a="crashed"))
        self.assertIs(winner, self.bottom)
        self.assertEqual(why, "forfeit")
        winner, why = cup.decide(self.match, result(0, 99, forfeit_b="timed out"))
        self.assertIs(winner, self.top)
        self.assertEqual(why, "forfeit")

    def test_two_dead_bots_still_produce_a_winner(self):
        winner, why = cup.decide(
            self.match, result(0, 0, forfeit_a="crashed", forfeit_b="crashed"))
        self.assertIs(winner, self.top)          # better seed
        self.assertEqual(why, "both forfeited")

    def test_an_exhausted_stalemate_falls_back_to_the_seed(self):
        winner, why = cup.decide(
            self.match, result(0, 0, draws=400, sudden_death=cup.SUDDEN_DEATH_CAP))
        self.assertIs(winner, self.top)
        self.assertEqual(why, "seed")

    def test_the_better_seed_wins_the_fallback_from_either_side(self):
        # Same rule whichever slot the better seed happens to sit in.
        self.match.a, self.match.b = self.bottom, self.top
        winner, _ = cup.decide(self.match, result(0, 0))
        self.assertIs(winner, self.top)

    def test_score_never_overrules_a_forfeit(self):
        # A bot that crashed mid-match may still be "ahead" on rounds played.
        winner, why = cup.decide(self.match, result(80, 5, forfeit_a="crashed"))
        self.assertIs(winner, self.bottom)
        self.assertEqual(why, "forfeit")


class RunTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def _bot(self, name, move):
        return write_bot(self.dir.name, name,
                         "def play(m, o):\n    return %r\n" % move)

    def test_runs_a_four_bot_cup_to_a_champion(self):
        paths = [self._bot("alpha", "paper"), self._bot("bravo", "rock"),
                 self._bot("charlie", "rock"), self._bot("delta", "rock")]
        entrants = cup.entrants_from_paths(
            paths, {"alpha": (1900.0, "im"), "bravo": (1700.0, "master"),
                    "charlie": (1600.0, "cm"), "delta": (1500.0, "novice")})
        plan = cup.plan(entrants, name="Test Cup")

        seen = []
        champion = cup.run(plan, on_match=seen.append)

        # alpha plays paper against three rock bots and cannot lose.
        self.assertEqual(champion.name, "alpha")
        self.assertEqual(len(seen), 4)   # 2 semis, third place, final
        self.assertEqual(plan.matches[-1].winner.name, "alpha")
        for m in plan.matches:
            self.assertIsNotNone(m.result)
            self.assertIsNotNone(m.winner)
            self.assertTrue(m.result.moves, "no transcript to replay")

    def test_match_length_follows_the_stage(self):
        paths = [self._bot("alpha", "paper"), self._bot("bravo", "rock"),
                 self._bot("charlie", "rock"), self._bot("delta", "rock")]
        plan = cup.plan(cup.entrants_from_paths(paths))
        cup.run(plan)
        for m in plan.matches:
            scheduled = len(m.result.moves) // 2 - m.result.sudden_death
            self.assertEqual(scheduled, cup.rounds_for(m.round_size),
                             "%s ran the wrong length" % plan.stage_of(m))
        # The final is three ordinary matches long — longer as the field narrows.
        self.assertEqual(cup.rounds_for(plan.matches[-1].round_size),
                         3 * cup.DEFAULT_ROUNDS)

    def test_identical_bots_are_separated_by_seed_not_left_hanging(self):
        paths = [self._bot("alpha", "rock"), self._bot("bravo", "rock")]
        entrants = cup.entrants_from_paths(
            paths, {"alpha": (1800.0, "master"), "bravo": (1500.0, "novice")})
        plan = cup.plan(entrants)
        champion = cup.run(plan)
        self.assertEqual(champion.name, "alpha")
        self.assertEqual(plan.matches[0].decided_by, "seed")

    def test_a_crashing_bot_does_not_stall_the_bracket(self):
        crashy = write_bot(self.dir.name, "crashy",
                           "def play(m, o):\n    raise RuntimeError('boom')\n")
        paths = [self._bot("alpha", "paper"), self._bot("bravo", "rock"),
                 self._bot("charlie", "rock"), crashy]
        plan = cup.plan(cup.entrants_from_paths(paths))
        champion = cup.run(plan)
        self.assertIsNotNone(champion)
        self.assertTrue(all(m.winner is not None for m in plan.matches))


class TranscriptReadingTest(unittest.TestCase):
    def test_outcome_matches_the_referee(self):
        self.assertEqual(broadcast.outcome("p", "r"), 1)
        self.assertEqual(broadcast.outcome("r", "p"), -1)
        self.assertEqual(broadcast.outcome("s", "s"), 0)
        self.assertEqual(broadcast.outcome("x", "r"), -1)   # invalid loses
        self.assertEqual(broadcast.outcome("r", "x"), 1)
        self.assertEqual(broadcast.outcome("x", "x"), 0)    # both invalid: draw

    def test_decode_pairs_moves_by_round(self):
        self.assertEqual(broadcast.decode("prsr"), [("p", "r"), ("s", "r")])

    def test_decided_at_finds_the_point_of_no_return(self):
        pairs = broadcast.decode("pr" * 10)         # a wins every round of 10
        # 5-0 with five to play is still catchable; 6-0 with four is not.
        self.assertEqual(broadcast.decided_at(pairs, 10), 5)
        drawn = broadcast.decode("rr" * 10)
        self.assertIsNone(broadcast.decided_at(drawn, 10))

    def test_a_match_that_reached_sudden_death_is_never_decided_early(self):
        pairs = broadcast.decode("rr" * 10 + "pr")  # 10 level, then a decider
        self.assertIsNone(broadcast.decided_at(pairs, 10))

    def test_moments_flag_lead_changes_and_streaks(self):
        # a takes five straight, then b takes six and steals the lead.
        pairs = broadcast.decode("pr" * 5 + "rp" * 6)
        marks = broadcast.moments(pairs, len(pairs))
        self.assertEqual(marks[4], "5 in a row")
        self.assertIn("lead change", marks.values())

    def test_sudden_death_rounds_always_count_as_moments(self):
        pairs = broadcast.decode("rr" * 4 + "pr")
        marks = broadcast.moments(pairs, 4)
        self.assertEqual(marks[4], "sudden death")

    def test_a_stalemate_does_not_flag_every_decider_as_a_tense_finish(self):
        # Past the scheduled rounds every round is level by definition, so the
        # neck-and-neck rule must not reach into sudden death — it would mark
        # all hundred deciders and slow down the very thing the cap cuts short.
        pairs = broadcast.decode("rr" * 40 + "rr" * 100)
        marks = broadcast.moments(pairs, 40)
        counts = {}
        for text in marks.values():
            counts[text] = counts.get(text, 0) + 1
        self.assertLessEqual(counts.get("neck and neck", 0),
                             broadcast._endgame(40))
        self.assertLessEqual(counts.get("sudden death", 0),
                             broadcast.SUDDEN_SHOWN + 1)


class ScriptTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        moves = [("alpha", "paper"), ("bravo", "rock"), ("charlie", "scissors"),
                 ("delta", "rock"), ("echo", "paper"), ("foxtrot", "scissors"),
                 ("golf", "rock"), ("hotel", "paper")]
        paths = [write_bot(self.dir.name, n, "def play(m, o):\n    return %r\n" % mv)
                 for n, mv in moves]
        self.paths8 = paths
        self.plan = cup.plan(cup.entrants_from_paths(paths[:4]), name="Test Cup")
        cup.run(self.plan)
        self.script = broadcast.script(self.plan)

    def test_script_covers_every_round_in_order(self):
        self.assertEqual([r["round_size"] for r in self.script["rounds"]], [4, 2, 2])
        self.assertEqual([r["stage"] for r in self.script["rounds"]],
                         ["Semi-final", cup.THIRD_PLACE, "Final"])
        self.assertEqual(self.script["champion"], self.plan.champion.name)

    def test_beats_cover_exactly_the_rounds_played(self):
        for rnd in self.script["rounds"]:
            for m in rnd["matches"]:
                self.assertEqual(sum(count for count, _ in m["beats"]),
                                 m["rounds_played"])

    def test_duration_is_the_sum_of_its_beats(self):
        for rnd in self.script["rounds"]:
            for m in rnd["matches"]:
                self.assertEqual(m["duration_ms"],
                                 sum(c * ms for c, ms in m["beats"]))

    def test_a_parallel_round_lasts_as_long_as_its_slowest_match(self):
        """Grid and featured rounds animate every match at once, so the round
        costs the longest of them rather than the sum."""
        plan = cup.plan(cup.entrants_from_paths(self.paths8), name="Eight")
        cup.run(plan)
        quarters = broadcast.script(plan)["rounds"][0]
        self.assertFalse(quarters["sequential"])
        slowest = max(m["duration_ms"] for m in quarters["matches"])
        self.assertEqual(quarters["duration_ms"],
                         slowest + quarters["interstitial_ms"])

    def test_a_hero_round_plays_its_matches_one_at_a_time(self):
        """Two semis share a round, so it costs the sum plus a hold on each
        result rather than the longer of the two."""
        semis = next(r for r in self.script["rounds"] if r["stage"] == "Semi-final")
        self.assertTrue(semis["sequential"])
        expected = sum(m["duration_ms"] + semis["hold_ms"]
                       for m in semis["matches"])
        self.assertEqual(semis["duration_ms"],
                         expected + semis["interstitial_ms"])
        self.assertEqual(semis["hold_ms"], broadcast.SEQUENTIAL_HOLD_MS)

    def test_the_playoff_and_the_final_are_revealed_apart(self):
        """Both are two-bot matches, so only the label separates them — and
        they are different occasions, each waiting on the operator."""
        rounds = self.script["rounds"]
        self.assertEqual([r["stage"] for r in rounds[-2:]],
                         [cup.THIRD_PLACE, "Final"])
        for rnd in rounds[-2:]:
            self.assertEqual(len(rnd["matches"]), 1)

    def test_no_match_overruns_its_budget(self):
        for rnd in self.script["rounds"]:
            for m in rnd["matches"]:
                self.assertLessEqual(m["duration_ms"],
                                     broadcast.BUDGET_MS[m["treatment"]])
                if m["budget_ms"]:
                    self.assertLessEqual(m["duration_ms"], m["budget_ms"])

    def test_natural_pacing_is_reported_alongside_the_budgeted_one(self):
        # natural_ms is what the beats wanted before the cap; without it there
        # is no way to tell a match shaped to its length from one squashed
        # into it, and no way to know whether tuning BASE_MS would do anything.
        for rnd in self.script["rounds"]:
            for m in rnd["matches"]:
                if not m["beats"]:
                    continue
                self.assertGreaterEqual(m["natural_ms"], m["duration_ms"])

    def test_script_is_json_serialisable(self):
        import json
        json.loads(json.dumps(self.script))

    def test_the_champion_ships_as_an_entrant_not_a_name_to_look_up(self):
        """The view used to find the winner's portrait by matching its name
        against the draw; now the champion arrives whole."""
        champion = self.script["champion_entrant"]
        self.assertIsNotNone(champion)
        self.assertEqual(champion["name"], self.script["champion"])
        self.assertEqual(champion["seed"], self.plan.champion.seed)
        self.assertIn("image", champion)

    def test_an_undecided_cup_has_no_champion_entrant(self):
        plan = cup.plan(cup.entrants_from_paths(self.paths8[:4]), name="Unplayed")
        self.assertIsNone(broadcast.script(plan)["champion_entrant"])


class BracketNamingTest(unittest.TestCase):
    """Two entrants can share a name now that the bracket no longer builds its
    names out of filenames with a digest attached."""

    def field(self):
        return [
            cup.Entrant(name="Rocky II", path="/x/a.py", rating=1900,
                        id="a" * 64, submitter="ivan@muttdata.ai"),
            cup.Entrant(name="Rocky II", path="/x/b.py", rating=1700,
                        id="b" * 64, submitter="sofia@muttdata.ai"),
            cup.Entrant(name="Brainy Boy", path="/x/c.py", rating=1600,
                        id="c" * 64, submitter="ivan@muttdata.ai"),
            cup.Entrant(name="Randy", path="/x/d.py", rating=1500,
                        id="d" * 64, submitter="sofia@muttdata.ai"),
        ]

    def played(self):
        plan = cup.plan(self.field())
        targets = cup.feed_map(plan)
        for i, m in enumerate(plan.matches):
            a_wins = m.a.seed < m.b.seed
            m.result = MatchResult(bot_a=m.a.name, bot_b=m.b.name,
                                   wins_a=10 if a_wins else 0,
                                   wins_b=0 if a_wins else 10,
                                   moves=("pr" if a_wins else "rp") * 10)
            m.winner, m.decided_by = cup.decide(m, m.result)
            cup.send_on(plan, i, targets)
        plan.champion = plan.matches[-1].winner
        return plan

    def test_the_duplicated_name_gains_its_owner_and_the_others_do_not(self):
        script = broadcast.script(self.played())
        names = {e["id"]: e["name"] for e in script["entrants"]}
        self.assertEqual(names["a" * 64], "Rocky II · ivan")
        self.assertEqual(names["b" * 64], "Rocky II · sofia")
        self.assertEqual(names["c" * 64], "Brainy Boy")
        self.assertEqual(names["d" * 64], "Randy")

    def test_a_bot_is_called_the_same_thing_in_every_round(self):
        script = broadcast.script(self.played())
        seen = {}
        for rnd in script["rounds"]:
            for m in rnd["matches"]:
                for side in ("a", "b"):
                    who = m[side]
                    if who:
                        seen.setdefault(who["id"], set()).add(who["name"])
        for bot_id, names in seen.items():
            self.assertEqual(len(names), 1, "%s went by %s" % (bot_id, names))

    def test_every_entrant_carries_its_registry_id(self):
        script = broadcast.script(self.played())
        self.assertEqual(sorted(e["id"] for e in script["entrants"]),
                         sorted(e.id for e in self.field()))

    def test_a_guest_with_no_id_is_still_labelled(self):
        """A bot played from a directory has no registry row, so no id — it is
        keyed on its seed instead and must not collide with the others."""
        field = self.field()
        field[0].id = ""
        plan = cup.plan(field)
        script = broadcast.script(plan)
        names = [e["name"] for e in script["entrants"]]
        self.assertIn("Rocky II · ivan", names)
        self.assertIn("Rocky II · sofia", names)


class RevealTest(unittest.TestCase):
    """At 32 and 64 the wide rounds only say who advanced."""

    def test_reveal_rounds_ship_no_transcript_and_cost_seconds(self):
        plan = cup.plan(field(32))
        for m in plan.matches:
            if m.a is None or m.b is None:
                break
            m.result = MatchResult(bot_a=m.a.name, bot_b=m.b.name,
                                   wins_a=60, wins_b=40, moves="pr" * 100)
            m.winner, m.decided_by = cup.decide(m, m.result)
        first = broadcast.script(plan)["rounds"][0]

        self.assertEqual(first["treatment"], "reveal")
        self.assertEqual(len(first["matches"]), 16)
        self.assertTrue(all(m["moves"] == "" for m in first["matches"]))
        self.assertTrue(all(m["beats"] == [] for m in first["matches"]))
        # Sixteen matches revealed in a handful of seconds, before the recap.
        play_ms = first["duration_ms"] - first["interstitial_ms"]
        self.assertLess(play_ms, 8000)

    def test_reveal_still_reports_who_advanced(self):
        plan = cup.plan(field(32))
        m = plan.matches[0]
        m.result = MatchResult(bot_a=m.a.name, bot_b=m.b.name,
                               wins_a=60, wins_b=40, moves="pr" * 100)
        m.winner, m.decided_by = cup.decide(m, m.result)
        entry = broadcast.script(plan)["rounds"][0]["matches"][0]
        self.assertEqual(entry["winner"], m.winner.name)
        self.assertEqual(entry["score"], [60, 40, 0])
        self.assertEqual(entry["decided_by"], "score")


def scripted(moves, sudden_death=0):
    """Script a one-match cup off a fabricated transcript."""
    plan = cup.plan([cup.Entrant(name="a", path="/x/a.py", rating=1900),
                     cup.Entrant(name="b", path="/x/b.py", rating=1500)])
    m = plan.matches[0]
    pairs = broadcast.decode(moves)
    wins_a = sum(1 for p in pairs if broadcast.outcome(*p) > 0)
    wins_b = sum(1 for p in pairs if broadcast.outcome(*p) < 0)
    m.result = MatchResult(bot_a="a", bot_b="b", wins_a=wins_a, wins_b=wins_b,
                           draws=len(pairs) - wins_a - wins_b, moves=moves,
                           sudden_death=sudden_death)
    m.winner, m.decided_by = cup.decide(m, m.result)
    plan.champion = m.winner
    return broadcast.script(plan)["rounds"][0]["matches"][0]


def _decided(entrants, third_place=True):
    """A cup played through with the better seed always winning."""
    plan = cup.plan(entrants, third_place=third_place)
    targets = cup.feed_map(plan)
    for i, m in enumerate(plan.matches):
        rounds = cup.rounds_for(m.round_size)
        better = m.a.seed < m.b.seed
        plan.matches[i].result = MatchResult(
            bot_a=m.a.name, bot_b=m.b.name,
            wins_a=rounds if better else 0, wins_b=0 if better else rounds,
            moves=("pr" if better else "rp") * rounds)
        m.winner, m.decided_by = cup.decide(m, m.result)
        cup.send_on(plan, i, targets)
    plan.champion = plan.matches[-1].winner
    return plan


def swinging(n, block=7):
    """A transcript that keeps trading the lead."""
    return "".join("pr" if (i // block) % 2 == 0 else "rp" for i in range(n))


class PacingTest(unittest.TestCase):
    """The point of variable-rate replay is that dull stretches go fast. These
    lock in the cases where an earlier version got it exactly backwards."""

    def test_a_rout_gets_less_airtime_than_a_thriller(self):
        rout = scripted("pr" * 100)                  # 100-0
        thriller = scripted(swinging(100))
        self.assertLess(rout["duration_ms"], thriller["duration_ms"])

    def test_a_rout_is_fast_forwarded_once_it_is_over(self):
        rout = scripted("pr" * 100)
        # Decided at 51-0; the rest is one blurred run at the floor.
        self.assertEqual(broadcast.decided_at(broadcast.decode("pr" * 100), 100), 50)
        self.assertLess(rout["beats"][-1][1], broadcast.BASE_MS["hero"])
        self.assertEqual(sum(c for c, _ in rout["beats"]), 100)

    def test_emphasis_stays_rare_however_eventful_the_match(self):
        m = scripted(swinging(200, block=3))         # a lead change every 3 rounds
        self.assertLessEqual(len(m["markers"]),
                             max(2, m["rounds_played"] // 16))

    def test_a_long_sudden_death_does_not_slow_the_whole_replay(self):
        # 40 level rounds, then 100 drawn deciders before the seed settles it.
        m = scripted("rr" * 40 + "rr" * 100, sudden_death=100)
        self.assertEqual(m["decided_by"], "seed")
        flagged = [k for k in m["markers"] if k["text"] == "sudden death"]
        self.assertLessEqual(len(flagged), broadcast.SUDDEN_SHOWN + 1)
        self.assertLess(m["duration_ms"], broadcast.BUDGET_MS["hero"])

    def test_every_marker_sits_on_a_round_the_replay_slows_for(self):
        m = scripted(swinging(120))
        per_round = []
        for count, ms in m["beats"]:
            per_round.extend([ms] * count)
        # The emphasis beat is the longest one in the match, whether or not
        # the whole thing was scaled to fit its budget.
        for marker in m["markers"]:
            self.assertEqual(per_round[marker["round"]], max(per_round),
                             "round %d captioned but not slowed" % marker["round"])


class IsolationTest(unittest.TestCase):
    """A cup must never move a rating. The guarantee is structural rather than
    conditional: the module has no import of the registry, so there is no path
    from here to the only code that writes ratings and career stats."""

    def test_the_cup_never_imports_the_registry(self):
        import ast

        path = os.path.join(os.path.dirname(__file__), "..", "janken", "cup.py")
        with open(path) as f:
            tree = ast.parse(f.read())

        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(a.name for a in node.names)
        self.assertNotIn("registry", imported)
        self.assertNotIn("Registry", imported)

    def test_running_a_cup_calls_nothing_that_records_results(self):
        from janken import registry

        forbidden = [n for n in dir(registry.Registry)
                     if n.startswith("record") or n == "_insert_match"]
        self.assertTrue(forbidden)          # the guard is watching something real
        with open(os.path.join(os.path.dirname(__file__),
                               "..", "janken", "cup.py")) as f:
            body = f.read().split('"""', 2)[-1]     # skip the module docstring
        for name in forbidden:
            self.assertNotIn(name + "(", body)


def _advance_by_seed(plan):
    """Resolve a bracket without playing anything: the better seed always wins.
    Used to check the *shape* of a draw."""
    targets = cup.feed_map(plan)
    for _size, indices in plan.rounds():
        for i in indices:
            m = plan.matches[i]
            m.winner = cup._better_seed(m.a, m.b)
            m.decided_by = "seed"
            cup.send_on(plan, i, targets)
    plan.champion = plan.matches[-1].winner


if __name__ == "__main__":
    unittest.main()
