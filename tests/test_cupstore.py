import datetime
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from janken import cup, cupstore
from janken import rating as elo
from janken.match import MatchResult


def played_cup(size=8, name="Test Cup"):
    """A fully played cup, fabricated — no subprocesses, deterministic."""
    entrants = [cup.Entrant(name="bot%02d" % i, path="/x/bot%02d.py" % i,
                            rating=2000 - i * 11, title="novice")
                for i in range(size)]
    plan = cup.plan(entrants, name=name)
    targets = cup.feed_map(plan)
    for i, m in enumerate(plan.matches):
        rounds = cup.rounds_for(m.round_size)
        # The better seed wins by a clear margin, so every result is decisive.
        a_wins = m.a.seed < m.b.seed
        wins_a, wins_b = (rounds, 0) if a_wins else (0, rounds)
        m.result = MatchResult(bot_a=m.a.name, bot_b=m.b.name, wins_a=wins_a,
                               wins_b=wins_b, draws=0,
                               moves=("pr" if a_wins else "rp") * rounds)
        m.winner, m.decided_by = cup.decide(m, m.result)
        cup.send_on(plan, i, targets)
    plan.champion = plan.matches[-1].winner
    return plan


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.db = os.path.join(self.dir.name, "cups.db")
        self.store = cupstore.CupStore(self.db)
        self.addCleanup(self.store.close)
        self.cup_id = self.store.save(played_cup())

    def test_load_round_trips_the_whole_cup(self):
        original = played_cup()
        loaded = self.store.load(self.cup_id)
        self.assertEqual(loaded.bracket_size, original.bracket_size)
        self.assertEqual(len(loaded.matches), len(original.matches))
        self.assertEqual(loaded.champion.name, original.champion.name)
        for was, now in zip(original.matches, loaded.matches):
            self.assertEqual(now.winner.name, was.winner.name)
            self.assertEqual(now.result.moves, was.result.moves)
            self.assertEqual(now.decided_by, was.decided_by)

    def test_a_saved_cup_starts_sealed(self):
        state = self.store.state(self.cup_id)
        self.assertEqual(state.revealed_rounds, 0)
        self.assertEqual(state.status(), "ready")
        self.assertIsNone(state.champion)

    def test_reveal_opens_exactly_one_round_at_a_time(self):
        state = self.store.state(self.cup_id)
        for expected in range(1, state.total_rounds + 1):
            self.assertEqual(self.store.reveal_next(self.cup_id), expected)
        self.assertIsNone(self.store.reveal_next(self.cup_id))   # nothing left

    def test_the_champion_only_lands_when_the_final_is_shown(self):
        state = self.store.state(self.cup_id)
        for _ in range(state.total_rounds - 1):
            self.store.reveal_next(self.cup_id)
            self.assertIsNone(self.store.state(self.cup_id).champion)
        self.store.reveal_next(self.cup_id)
        self.assertEqual(self.store.state(self.cup_id).champion, "bot00")


class SealTest(unittest.TestCase):
    """Between playing a cup and showing it, nothing may leak."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = cupstore.CupStore(os.path.join(self.dir.name, "cups.db"))
        self.addCleanup(self.store.close)
        self.cup_id = self.store.save(played_cup(16))

    def rounds(self):
        return self.store.payload(self.cup_id)["script"]["rounds"]

    def test_sealed_shows_the_draw_and_nothing_else(self):
        rounds = self.rounds()
        first, rest = rounds[0], rounds[1:]
        # The draw is public — that is the point of a draw.
        self.assertTrue(all(m["a"] and m["b"] for m in first["matches"]))
        self.assertTrue(all(m["winner"] is None for m in first["matches"]))
        self.assertTrue(all(m["moves"] == "" for m in first["matches"]))
        # Everything downstream is an empty bracket.
        for rnd in rest:
            for m in rnd["matches"]:
                self.assertIsNone(m["a"])
                self.assertIsNone(m["b"])
                self.assertIsNone(m["winner"])
                self.assertEqual(m["moves"], "")

    def test_each_reveal_exposes_one_round_and_no_more(self):
        total = self.store.state(self.cup_id).total_rounds
        for opened in range(1, total + 1):
            self.store.reveal_next(self.cup_id)
            rounds = self.rounds()
            for ordinal, rnd in enumerate(rounds):
                shown = ordinal < opened
                for m in rnd["matches"]:
                    if shown:
                        self.assertIsNotNone(m["winner"])
                        self.assertTrue(m["moves"] or m["treatment"] == "reveal")
                    else:
                        self.assertIsNone(m["winner"])
                        self.assertEqual(m["moves"], "")

    def test_an_unrevealed_transcript_is_never_in_the_payload(self):
        # The strongest form: the raw bytes of an unplayed round's moves must
        # not appear anywhere in what a viewer receives.
        import json

        full = self.store.load(self.cup_id)
        final_moves = full.matches[-1].result.moves
        self.store.reveal_next(self.cup_id)
        blob = json.dumps(self.store.payload(self.cup_id))
        self.assertNotIn(final_moves, blob)
        self.assertNotIn('"champion": "%s"' % full.champion.name, blob)

    def test_the_seal_is_structural_not_a_convention(self):
        """Propagated entrants are never written, so the table itself cannot
        leak who reached the later rounds even if a reader forgets to gate."""
        conn = sqlite3.connect(self.store.path)
        rows = conn.execute(
            """SELECT idx, a_seed, b_seed, a_from, b_from FROM cup_matches
               WHERE cup_id = ?""", (self.cup_id,)).fetchall()
        conn.close()
        self.assertTrue(rows)
        for idx, a_seed, b_seed, a_from, b_from in rows:
            if a_from is not None:
                self.assertIsNone(a_seed, "match %d leaked a propagated seed" % idx)
            if b_from is not None:
                self.assertIsNone(b_seed, "match %d leaked a propagated seed" % idx)

    def test_a_revealed_round_fills_the_next_round_pairings(self):
        # Not a leak: once you know who won, you know who meets next.
        self.store.reveal_next(self.cup_id)
        second = self.rounds()[1]
        self.assertTrue(all(m["a"] and m["b"] for m in second["matches"]))
        self.assertTrue(all(m["winner"] is None for m in second["matches"]))


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = cupstore.CupStore(os.path.join(self.dir.name, "cups.db"))
        self.addCleanup(self.store.close)
        self.cup_id = self.store.save(played_cup())

    def _age(self, seconds):
        old = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.timedelta(seconds=seconds)).isoformat(timespec="seconds")
        with self.store.conn:
            self.store.conn.execute("UPDATE cups SET updated_at = ? WHERE id = ?",
                                    (old, self.cup_id))

    def test_ready_then_live_then_ended(self):
        self.assertEqual(self.store.state(self.cup_id).status(), "ready")
        self.store.reveal_next(self.cup_id)
        self.assertEqual(self.store.state(self.cup_id).status(), "live")
        self.store.end(self.cup_id)
        self.assertEqual(self.store.state(self.cup_id).status(), "ended")

    def test_an_abandoned_cup_goes_stale_instead_of_claiming_to_be_live(self):
        # Manual control reintroduces the failure derived liveness ruled out:
        # start a cup, wander off, banner says LIVE forever.
        self.store.reveal_next(self.cup_id)
        self._age(cupstore.STALE_AFTER_SECONDS + 60)
        self.assertEqual(self.store.state(self.cup_id).status(), "stale")

    def test_touching_a_cup_keeps_it_live_while_you_talk(self):
        self.store.reveal_next(self.cup_id)
        self._age(cupstore.STALE_AFTER_SECONDS + 60)
        self.store.touch(self.cup_id)
        self.assertEqual(self.store.state(self.cup_id).status(), "live")

    def test_current_ignores_ended_and_stale_cups(self):
        self.store.reveal_next(self.cup_id)
        self.assertEqual(self.store.current().cup_id, self.cup_id)
        self.store.end(self.cup_id)
        self.assertIsNone(self.store.current())

    def test_current_prefers_the_newest_showable_cup(self):
        self.store.end(self.cup_id)
        newer = self.store.save(played_cup(name="Newer"))
        self.assertEqual(self.store.current().cup_id, newer)

    def test_payload_carries_the_server_clock_for_skew_correction(self):
        self.store.reveal_next(self.cup_id)
        payload = self.store.payload(self.cup_id)
        self.assertIsNotNone(payload["server_now_ms"])
        self.assertIsNotNone(payload["round_started_at_ms"])
        # Viewers correct their own clocks against this, so it must be a real
        # epoch and the round must not appear to start in the future.
        self.assertGreater(payload["server_now_ms"], 1_600_000_000_000)
        self.assertLessEqual(payload["round_started_at_ms"],
                             payload["server_now_ms"])


class FieldSelectionTest(unittest.TestCase):
    """Who gets into the draw."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.db = os.path.join(self.dir.name, "janken.db")
        self.bots = os.path.join(self.dir.name, "bots")
        os.makedirs(self.bots)
        from janken.registry import Registry
        Registry(self.db).close()

    def _write(self, *names):
        for i, name in enumerate(names):
            with open(os.path.join(self.bots, name + ".py"), "w") as f:
                # Distinct bodies so each file hashes to its own bot id.
                f.write("X = %d\ndef play(m, o):\n    return 'rock'\n" % i)

    def _field(self, **over):
        import argparse

        from janken.__main__ import _cup_field

        args = argparse.Namespace(db=self.db, bots_dir=self.bots, league="all",
                                  include_held=False, limit=64,
                                  include_reference=False, exclude=None)
        for key, value in over.items():
            setattr(args, key, value)
        return sorted(e.name for e in _cup_field(args))

    def test_reference_bots_sit_out_by_default(self):
        # randy is the point of this: a coin flip with no strategy, which over
        # one 100-round match wins often enough to take a bracket on luck.
        self._write("randy", "rocky", "brainy", "copycat", "ada", "grace")
        self.assertEqual(self._field(), ["ada", "grace"])

    def test_include_reference_puts_them_back(self):
        self._write("randy", "rocky", "ada", "grace")
        self.assertEqual(self._field(include_reference=True),
                         ["ada", "grace", "randy", "rocky"])

    def test_exclude_keeps_a_named_bot_out(self):
        self._write("ada", "grace", "hopper")
        self.assertEqual(self._field(exclude=["grace"]), ["ada", "hopper"])

    def test_exclude_ignores_case(self):
        # Four bots so that two survive the exclusions: a field of one raises
        # instead of returning, which would mask what this is checking.
        self._write("Ada", "grace", "hopper", "turing")
        self.assertEqual(self._field(exclude=["ADA", "Hopper"]),
                         ["grace", "turing"])

    def test_a_field_emptied_by_exclusions_says_so(self):
        self._write("randy", "rocky")
        with self.assertRaises(SystemExit) as caught:
            self._field()
        self.assertIn("--include-reference", str(caught.exception))


class LadderIsolationTest(unittest.TestCase):
    def test_storing_a_cup_leaves_the_registry_untouched(self):
        from janken.registry import Registry

        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "janken.db")
            Registry(db).close()          # create the registry schema

            conn = sqlite3.connect(db)
            before = (conn.execute("SELECT * FROM bots").fetchall(),
                      conn.execute("SELECT * FROM matches").fetchall())
            conn.close()

            with cupstore.CupStore(db) as store:
                cup_id = store.save(played_cup(16))
                while store.reveal_next(cup_id):
                    pass
                store.end(cup_id)

            conn = sqlite3.connect(db)
            after = (conn.execute("SELECT * FROM bots").fetchall(),
                     conn.execute("SELECT * FROM matches").fetchall())
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            conn.close()

            self.assertEqual(before, after, "a cup moved the ladder")
            # Sharing the file is fine; sharing tables is not.
            self.assertTrue({"cups", "cup_entrants", "cup_matches"} <= tables)

    def test_fielding_a_cup_does_not_register_anything(self):
        """Building a cup field must not enrol bots in the league.

        The natural implementation reused `sync_paths`, the same helper `run`
        and `tournament` use — but that registers every file it is shown, so a
        cup pointed at a directory of experiments quietly added them all to the
        ladder. A cup that can add bots to the registry affects the registry.
        """
        import argparse
        import textwrap

        from janken.__main__ import _cup_field
        from janken.registry import Registry

        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "janken.db")
            bots = os.path.join(d, "bots")
            os.makedirs(bots)
            for name, move in (("alpha", "rock"), ("bravo", "paper"),
                               ("charlie", "scissors")):
                with open(os.path.join(bots, name + ".py"), "w") as f:
                    f.write(textwrap.dedent(
                        "def play(m, o):\n    return %r\n" % move))

            Registry(db).close()                     # empty registry
            conn = sqlite3.connect(db)
            before = conn.execute("SELECT * FROM bots").fetchall()
            conn.close()
            self.assertEqual(before, [])

            args = argparse.Namespace(db=db, bots_dir=bots, league="all",
                                      include_held=False, limit=64,
                                      include_reference=False, exclude=None)
            field = _cup_field(args)

            conn = sqlite3.connect(db)
            after = conn.execute("SELECT * FROM bots").fetchall()
            conn.close()

            self.assertEqual(len(field), 3)          # all three still play
            self.assertEqual(after, [], "fielding a cup registered bots")
            # Unregistered entrants play as guests on the starting rating.
            for entrant in field:
                self.assertEqual(entrant.rating, elo.START_RATING)

    def test_an_unsafe_bot_is_screened_out_without_being_banned(self):
        """Screening still applies — it just no longer writes a ban to the
        registry as a side effect of deciding who plays."""
        import argparse

        from janken.__main__ import _cup_field
        from janken.registry import Registry

        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "janken.db")
            bots = os.path.join(d, "bots")
            os.makedirs(bots)
            for name in ("alpha", "bravo"):
                with open(os.path.join(bots, name + ".py"), "w") as f:
                    f.write("def play(m, o):\n    return 'rock'\n")
            with open(os.path.join(bots, "nasty.py"), "w") as f:
                f.write("import socket\ndef play(m, o):\n    return 'rock'\n")

            Registry(db).close()
            args = argparse.Namespace(db=db, bots_dir=bots, league="all",
                                      include_held=False, limit=64,
                                      include_reference=False, exclude=None)
            field = _cup_field(args)

            self.assertEqual(sorted(e.name for e in field), ["alpha", "bravo"])
            conn = sqlite3.connect(db)
            rows = conn.execute("SELECT * FROM bots").fetchall()
            conn.close()
            self.assertEqual(rows, [], "screening wrote to the registry")

    def test_a_cup_field_carries_each_bots_picture(self):
        """The path is set for bots that have one, and left empty otherwise."""
        import argparse
        import textwrap
        import unittest.mock

        from janken import avatar
        from janken.__main__ import _cup_field
        from janken.registry import Registry, bot_id_for

        with tempfile.TemporaryDirectory() as d:
            # _cup_field exports avatars to the app's static directory as a side
            # effect. Point that at the tempdir: a test must not leave PNGs in
            # the working tree.
            self.enterContext(
                unittest.mock.patch("janken.__main__.STATIC_DIR", d))
            db = os.path.join(d, "janken.db")
            bots = os.path.join(d, "bots")
            os.makedirs(bots)
            paths = {}
            for name, move in (("alpha", "rock"), ("bravo", "paper")):
                p = os.path.join(bots, name + ".py")
                with open(p, "w") as f:
                    f.write(textwrap.dedent(
                        "def play(m, o):\n    return %r\n" % move))
                paths[name] = p

            with Registry(db) as reg:
                reg.sync_paths(list(paths.values()))
                # Only alpha gets a picture, so the empty case is covered too.
                alpha_id = bot_id_for(paths["alpha"])
                reg.set_image(alpha_id, b"\x89PNG-pretend")

            args = argparse.Namespace(db=db, bots_dir=bots, league="all",
                                      include_held=False, limit=64,
                                      include_reference=False, exclude=None)
            # Keyed by id, because the entrant's *name* is now the registry's
            # display name ("Alpha") rather than the filename stem ("alpha").
            field = {e.id: e for e in _cup_field(args)}

            self.assertEqual(field[alpha_id].image, avatar.rel_path(alpha_id))
            self.assertEqual(field[alpha_id].name, "Alpha")
            bravo = field[bot_id_for(paths["bravo"])]
            self.assertEqual(bravo.image, "")
            self.assertEqual(bravo.name, "Bravo")

    def test_a_disabled_bot_is_kept_out_rather_than_fielded_as_a_guest(self):
        """A bot the registry does not hand back is fielded as an unrated
        guest, so hiding one from the *lookup* would have entered it in the cup
        at the starting rating — the opposite of hiding it."""
        import argparse
        import textwrap
        import unittest.mock

        from janken.__main__ import _cup_field
        from janken.registry import Registry, bot_id_for

        with tempfile.TemporaryDirectory() as d:
            self.enterContext(
                unittest.mock.patch("janken.__main__.STATIC_DIR", d))
            db = os.path.join(d, "janken.db")
            bots = os.path.join(d, "bots")
            os.makedirs(bots)
            paths = {}
            for name, move in (("alpha", "rock"), ("bravo", "paper"),
                               ("charlie", "scissors")):
                p = os.path.join(bots, name + ".py")
                with open(p, "w") as f:
                    f.write(textwrap.dedent(
                        "def play(m, o):\n    return %r\n" % move))
                paths[name] = p

            with Registry(db) as reg:
                reg.sync_paths(list(paths.values()))
                hidden = bot_id_for(paths["bravo"])
                reg.set_disabled(hidden, True)

            args = argparse.Namespace(db=db, bots_dir=bots, league="all",
                                      include_held=False, limit=64,
                                      include_reference=False, exclude=None)
            field = _cup_field(args)

            self.assertNotIn(hidden, {e.id for e in field})
            self.assertEqual(len(field), 2)
            # And not smuggled back in under its filename with no id.
            self.assertNotIn("Bravo", {e.name for e in field})

    def test_an_entrants_picture_survives_being_stored_and_read_back(self):
        plan = played_cup(size=4)
        for e in plan.entrants:
            e.image = "avatars/%s.png" % e.name
        with tempfile.TemporaryDirectory() as d:
            with cupstore.CupStore(os.path.join(d, "cups.db")) as store:
                cup_id = store.save(plan)
                for _ in range(store.state(cup_id).total_rounds):
                    store.reveal_next(cup_id)
                loaded = {e.name: e.image for e in store.load(cup_id).entrants}
                self.assertEqual(loaded["bot00"], "avatars/bot00.png")
                # And it reaches the document the browser actually polls.
                drawn = {e["name"]: e["image"]
                         for e in store.payload(cup_id)["script"]["entrants"] if e}
                self.assertEqual(drawn["bot00"], "avatars/bot00.png")

    def test_a_cup_stored_before_avatars_existed_still_opens(self):
        """`image` was ALTERed onto cup_entrants; an older file must migrate."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cups.db")
            with cupstore.CupStore(path) as store:
                store.save(played_cup(size=4))
            aged = sqlite3.connect(path)
            aged.execute("ALTER TABLE cup_entrants DROP COLUMN image")
            aged.commit()
            aged.close()
            with cupstore.CupStore(path) as store:
                cup_id = store.latest().cup_id
                self.assertTrue(
                    all(e.image == "" for e in store.load(cup_id).entrants))

    def test_an_entrants_identity_survives_the_round_trip(self):
        plan = played_cup(size=4)
        for i, e in enumerate(plan.entrants):
            e.id = "%064x" % i
            e.submitter = "player%d@example.com" % i
        with tempfile.TemporaryDirectory() as d:
            with cupstore.CupStore(os.path.join(d, "cups.db")) as store:
                cup_id = store.save(plan)
                loaded = {e.seed: e for e in store.load(cup_id).entrants}
                for was in plan.entrants:
                    self.assertEqual(loaded[was.seed].id, was.id)
                    self.assertEqual(loaded[was.seed].submitter, was.submitter)


class LegacyEntrantNameTest(unittest.TestCase):
    """Cups drawn when the field was built from filenames stored their entrants
    as "<name>-<id>", which is both the illegible bracket and, usefully, a
    complete record of which bot it was."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "cups.db")

    def age(self, rows):
        """Rewrite stored entrants to look like the old format: no bot_id, and
        the digest welded onto the name."""
        conn = sqlite3.connect(self.path)
        for seed, name in rows.items():
            conn.execute(
                "UPDATE cup_entrants SET name = ?, bot_id = '', submitter = '' "
                "WHERE seed = ?", (name, seed))
        conn.commit()
        conn.close()

    def test_the_id_is_recovered_from_the_name_and_the_digest_dropped(self):
        digest = "3f9a" + "0" * 60
        with cupstore.CupStore(self.path) as store:
            cup_id = store.save(played_cup(size=4))
        self.age({1: "Rocky_Ii-" + digest})

        with cupstore.CupStore(self.path) as store:
            entrant = {e.seed: e for e in store.load(cup_id).entrants}[1]
            self.assertEqual(entrant.id, digest)
            self.assertEqual(entrant.name, "Rocky_Ii")

    def test_a_legacy_twelve_char_suffix_resolves_through_the_registry(self):
        full = "a3f9c1d4e07b" + "5" * 52
        with cupstore.CupStore(self.path) as store:
            cup_id = store.save(played_cup(size=4))
        # A registry sharing the database, holding the bot that prefix names.
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE bots (id TEXT PRIMARY KEY, name TEXT, "
                     "submitter TEXT)")
        conn.execute("INSERT INTO bots VALUES (?, ?, ?)",
                     (full, "Rocky II", "ivan@muttdata.ai"))
        conn.commit()
        conn.close()
        self.age({1: "Rocky_Ii-a3f9c1d4e07b"})

        with cupstore.CupStore(self.path) as store:
            entrant = {e.seed: e for e in store.load(cup_id).entrants}[1]
            self.assertEqual(entrant.id, full)
            # The registry knows this bot, so its real name comes back too.
            self.assertEqual(entrant.name, "Rocky II")
            self.assertEqual(entrant.submitter, "ivan@muttdata.ai")

    def test_a_name_with_no_digest_on_it_is_left_alone(self):
        with cupstore.CupStore(self.path) as store:
            cup_id = store.save(played_cup(size=4))
        self.age({1: "Perfectly Ordinary Name"})

        with cupstore.CupStore(self.path) as store:
            entrant = {e.seed: e for e in store.load(cup_id).entrants}[1]
            self.assertEqual(entrant.name, "Perfectly Ordinary Name")
            self.assertEqual(entrant.id, "")

    def test_recovery_runs_once_and_is_not_undone_by_reopening(self):
        digest = "3f9a" + "0" * 60
        with cupstore.CupStore(self.path) as store:
            cup_id = store.save(played_cup(size=4))
        self.age({1: "Rocky_Ii-" + digest})

        for _ in range(3):
            with cupstore.CupStore(self.path) as store:
                entrant = {e.seed: e for e in store.load(cup_id).entrants}[1]
                self.assertEqual(entrant.name, "Rocky_Ii")
                self.assertEqual(entrant.id, digest)

    def test_the_store_writes_only_its_own_tables(self):
        import ast

        path = os.path.join(os.path.dirname(__file__), "..", "janken", "cupstore.py")
        with open(path) as f:
            tree = ast.parse(f.read())
        sql = [n.value for n in ast.walk(tree)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        written = " ".join(s.upper() for s in sql)
        for forbidden in ("INSERT INTO BOTS", "UPDATE BOTS", "DELETE FROM BOTS",
                          "INSERT INTO MATCHES", "UPDATE MATCHES",
                          "DELETE FROM MATCHES"):
            self.assertNotIn(forbidden, written)


if __name__ == "__main__":
    unittest.main()
