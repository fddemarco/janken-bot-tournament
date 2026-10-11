import datetime
import hashlib
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from janken import glicko
from janken import norms
from janken import rating as elo
from janken.match import MatchResult
from janken.registry import (
    ACTIVE_PER_SUBMITTER,
    FieldFull,
    NotANormEvent,
    Registry,
    bot_id_for,
)
from janken.tournament import (
    Standing,
    TournamentResult,
    discover_bots,
    run_tournament,
)

from test_engine import write_bot

ROCK = "def play(m, o):\n    return 'rock'\n"
PAPER = "def play(m, o):\n    return 'paper'\n"
SCISSORS = "def play(m, o):\n    return 'scissors'\n"


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.registry = Registry(os.path.join(self.dir.name, "test.db"))
        self.addCleanup(self.registry.close)

    def sync_one(self, name, body):
        path = write_bot(self.dir.name, name, body)
        return path, self.registry.sync_paths([path]).bots[path]

    def test_registration_defaults(self):
        _, info = self.sync_one("ada", ROCK)
        self.assertEqual(len(info.id), 64)
        self.assertEqual(info.name, "Ada")  # filename, title-cased
        self.assertEqual(info.league, "novice")
        self.assertFalse(info.banned)
        self.assertEqual((info.wins, info.losses), (0, 0))
        self.assertEqual(info.rating, elo.START_RATING)
        self.assertEqual(info.peak_rating, elo.START_RATING)
        self.assertEqual(info.rated_games, 0)

    def test_edit_creates_new_bot_with_fresh_stats(self):
        path, info = self.sync_one("ada", ROCK)
        self.registry.conn.execute(
            "UPDATE bots SET wins = 42 WHERE id = ?", (info.id,))
        self.registry.conn.commit()

        _, edited = self.sync_one("ada", PAPER)
        self.assertNotEqual(edited.id, info.id)
        self.assertEqual(edited.wins, 0)
        self.assertEqual(edited.league, "novice")
        # the old bot's record remains untouched
        self.assertEqual(self.registry.get(info.id).wins, 42)

    def test_rename_keeps_identity(self):
        path, info = self.sync_one("ada", ROCK)
        renamed = write_bot(self.dir.name, "ada_v2", ROCK)
        os.remove(path)
        new_info = self.registry.sync_paths([renamed]).bots[renamed]
        self.assertEqual(new_info.id, info.id)
        self.assertEqual(new_info.name, "Ada_V2")

    def test_set_name_keeps_identity_and_record(self):
        _, info = self.sync_one("ada", ROCK)
        self.registry.conn.execute(
            "UPDATE bots SET wins = 42, rating = ? WHERE id = ?",
            (elo.START_RATING + 75, info.id))
        self.registry.conn.commit()

        renamed = self.registry.set_name(info.id, " Countess Ada ")

        self.assertEqual(renamed.id, info.id)
        self.assertEqual(renamed.name, "Countess Ada")
        self.assertEqual(renamed.wins, 42)
        self.assertEqual(renamed.rating, elo.START_RATING + 75)

    def test_set_name_rejects_missing_or_blank(self):
        _, info = self.sync_one("ada", ROCK)

        self.assertIsNone(self.registry.set_name("deadbeefcafe", "Ghost"))
        self.assertIsNone(self.registry.set_name(info.id, "  "))
        self.assertEqual(self.registry.get(info.id).name, "Ada")

    def test_duplicate_content_dropped(self):
        a = write_bot(self.dir.name, "alice", ROCK)
        b = write_bot(self.dir.name, "bob", ROCK)
        sync = self.registry.sync_paths([a, b])
        self.assertEqual(list(sync.bots) if b in sync.bots else [a], [a])
        self.assertEqual(sync.duplicates, [(b, a)])

    def test_ban_and_auto_unban(self):
        path, info = self.sync_one("sneaky", "import os\n" + ROCK)
        self.assertTrue(info.banned)
        self.assertIn("os", info.ban_reason)

        # Resyncing the unchanged dirty file keeps the ban.
        again = self.registry.sync_paths([path]).bots[path]
        self.assertTrue(again.banned)

        # Fixing the file changes the hash: it's a *new*, clean bot,
        # and the old dirty bot's row keeps its ban.
        _, fixed = self.sync_one("sneaky", ROCK)
        self.assertFalse(fixed.banned)
        self.assertNotEqual(fixed.id, info.id)
        self.assertTrue(self.registry.get(info.id).banned)

    def test_identity_is_the_full_sha256(self):
        # Not a truncation: the whole 256-bit digest, so a different file can
        # never be ground to an existing bot's id.
        path, info = self.sync_one("ada", ROCK)
        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(len(info.id), 64)
        self.assertEqual(info.id, digest)

    def test_a_different_file_cannot_ban_or_hijack_a_rival(self):
        # The reported attack: with a 48-bit id, a different, deliberately-unsafe
        # file could collide with a rival's id, and sync's ON CONFLICT would flip
        # the rival's clean row to banned (and overwrite its owner). A full id
        # makes a different file a different bot, so the dirty upload lands on its
        # own row and the victim is untouched.
        victim = write_bot(self.dir.name, "victim", ROCK)
        vi = self.registry.sync_paths([victim], submitter="victim@x").bots[victim]
        self.assertFalse(vi.banned)

        attacker = write_bot(self.dir.name, "attacker", "import os\n" + ROCK)
        ai = self.registry.sync_paths(
            [attacker], submitter="attacker@y").bots[attacker]
        self.assertTrue(ai.banned)
        self.assertNotEqual(ai.id, vi.id)

        now = self.registry.get(vi.id)
        self.assertFalse(now.banned)
        self.assertEqual(now.submitter, "victim@x")
        self.assertEqual(now.name, "Victim")
        self.assertIn(vi.id, [b.id for b in self.registry.eligible_bots()])

    def test_legacy_short_id_is_lengthened_on_open(self):
        # A row written under the old 12-char scheme is re-keyed to the full hash
        # when the database is opened, and the matches and image that referenced
        # it follow.
        body = PAPER
        path = write_bot(self.dir.name, "legacy", body)
        with open(path, "rb") as f:
            full = hashlib.sha256(f.read()).hexdigest()
        short = full[:12]

        db = os.path.join(self.dir.name, "legacy.db")
        reg = Registry(db)
        reg.conn.execute(
            "INSERT INTO bots (id, name, path, first_seen, last_seen) "
            "VALUES (?, 'Legacy', ?, ?, ?)",
            (short, os.path.abspath(path), "2020-01-01T00:00:00+00:00",
             "2020-01-01T00:00:00+00:00"))
        reg.conn.execute(
            "INSERT INTO matches (played_at, kind, bot_a, bot_b, wins_a, wins_b, "
            "draws) VALUES ('t', 'tournament', ?, 'other', 1, 0, 0)", (short,))
        reg.conn.execute(
            "INSERT INTO bot_images (bot_id, png, updated_at) VALUES (?, ?, 't')",
            (short, sqlite3.Binary(b"png")))
        reg.conn.commit()
        reg.close()

        reg2 = Registry(db)                 # reopen runs the migration
        self.addCleanup(reg2.close)
        self.assertIsNone(reg2.get(short))
        upgraded = reg2.get(full)
        self.assertIsNotNone(upgraded)
        self.assertEqual(upgraded.name, "Legacy")
        self.assertEqual(reg2.conn.execute(
            "SELECT COUNT(*) FROM matches WHERE bot_a = ?", (full,)).fetchone()[0], 1)
        self.assertEqual(reg2.get_image(full), b"png")

    def test_tournament_updates_career_and_ratings(self):
        a = write_bot(self.dir.name, "paperbot", PAPER)
        b = write_bot(self.dir.name, "rockbot", ROCK)
        sync = self.registry.sync_paths([a, b])
        ids = {os.path.splitext(os.path.basename(p))[0]: info.id
               for p, info in sync.bots.items()}

        result = run_tournament([a, b], rounds=10)
        _, scissors = self.sync_one("scissorsbot", SCISSORS)
        record = self.registry.record_tournament(result, ids)

        paper = self.registry.get(ids["paperbot"])
        rock = self.registry.get(ids["rockbot"])
        self.assertEqual((paper.wins, rock.losses), (1, 1))
        self.assertEqual(rock.round_losses, 10)

        # Even start and a mirrored result, so the winner gains exactly what the
        # loser drops. Glicko-2 is not zero-sum in general — two bots with
        # different deviations move by different amounts off the same match — but
        # between two bots that know as little about themselves it is.
        gain = paper.rating - glicko.START_RATING
        self.assertGreater(gain, 0.0)
        self.assertAlmostEqual(rock.rating, glicko.START_RATING - gain)
        # Playing narrowed what we know about both of them, which is the part Elo
        # could not express at all.
        self.assertLess(paper.rd, glicko.START_RD)
        self.assertLess(rock.rd, glicko.START_RD)
        self.assertEqual(paper.peak_rating, paper.rating)   # a new high
        self.assertEqual(rock.peak_rating, glicko.START_RATING)  # peak is kept
        self.assertEqual((paper.rated_games, rock.rated_games), (1, 1))
        self.assertAlmostEqual(record.ratings["paperbot"].delta, gain)
        self.assertEqual(record.ratings["rockbot"].games, 1)

        stored = self.registry.conn.execute(
            "SELECT COUNT(*) FROM matches WHERE kind = 'tournament'"
        ).fetchone()[0]
        self.assertEqual(stored, 1)
        history = self.registry.conn.execute(
            """SELECT bot_id, rating, rank, peak_rating, rated_games
               FROM rating_history ORDER BY rank"""
        ).fetchall()
        self.assertEqual([r["bot_id"] for r in history], [paper.id, scissors.id, rock.id])
        self.assertAlmostEqual(history[0]["rating"], paper.rating)
        self.assertAlmostEqual(history[0]["peak_rating"], paper.peak_rating)
        self.assertEqual(history[0]["rated_games"], paper.rated_games)
        self.assertEqual([r["rank"] for r in history], [1, 2, 3])
        self.assertEqual(history[1]["rated_games"], 0)

    def test_rating_history_table_is_created_on_new_and_existing_dbs(self):
        self.registry.conn.execute(
            "INSERT INTO rating_history (played_at, bot_id, rating, rank, peak_rating, rated_games) VALUES ('t', 'bot', 1500, 1, 1500, 0)")
        self.registry.conn.commit()
        self.assertEqual(
            self.registry.conn.execute(
                "SELECT COUNT(*) FROM rating_history").fetchone()[0],
            1,
        )

        path = os.path.join(self.dir.name, "old.db")
        Registry(path).close()
        legacy = sqlite3.connect(path)
        legacy.execute("DROP TABLE rating_history")
        legacy.commit()
        legacy.close()
        with Registry(path) as reg:
            tables = {
                r["name"] for r in reg.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            self.assertIn("rating_history", tables)

    def test_existing_matches_backfill_rating_history_by_tournament_period(self):
        a = write_bot(self.dir.name, "paperbot", PAPER)
        b = write_bot(self.dir.name, "rockbot", ROCK)
        sync = self.registry.sync_paths([a, b])
        ids = {os.path.splitext(os.path.basename(p))[0]: i.id
               for p, i in sync.bots.items()}
        self.registry.conn.executemany(
            """INSERT INTO matches
                   (played_at, kind, bot_a, bot_b, wins_a, wins_b, draws)
               VALUES (?, 'tournament', ?, ?, ?, ?, 0)""",
            [
                ("2026-01-01T00:00:00+00:00", ids["paperbot"], ids["rockbot"], 10, 0),
                ("2026-01-02T00:00:00+00:00", ids["paperbot"], ids["rockbot"], 0, 10),
            ],
        )
        self.registry.conn.execute("DROP TABLE rating_history")
        self.registry.conn.commit()
        self.registry.close()

        reg = Registry(os.path.join(self.dir.name, "test.db"))
        self.registry = reg
        self.addCleanup(reg.close)
        rows = reg.conn.execute(
            """SELECT played_at, bot_id, rank, rated_games
               FROM rating_history ORDER BY played_at, rank"""
        ).fetchall()

        self.assertEqual(len(rows), 4)
        self.assertEqual({r["played_at"] for r in rows}, {
            "2026-01-01T00:00:00+00:00",
            "2026-01-02T00:00:00+00:00",
        })
        first = [r for r in rows if r["played_at"].startswith("2026-01-01")]
        self.assertEqual(first[0]["bot_id"], ids["paperbot"])
        self.assertEqual([r["rated_games"] for r in first], [1, 1])

    def test_rating_earns_a_title(self):
        a = write_bot(self.dir.name, "paperbot", PAPER)
        b = write_bot(self.dir.name, "rockbot", ROCK)
        sync = self.registry.sync_paths([a, b])
        ids = {os.path.splitext(os.path.basename(p))[0]: info.id
               for p, info in sync.bots.items()}
        title = elo.RATING_TITLES[-1][0]      # the first rating title a bot earns
        floor = elo.floor_for(title)
        # Settled, not just sitting at the floor: a bot still carrying its
        # starting deviation would leap several hundred points off one win and
        # sail past two rungs, which tests the step size rather than the title.
        self.registry.conn.execute(
            "UPDATE bots SET rating = ?, peak_rating = ?, rd = ? WHERE id = ?",
            (floor - 1, floor - 1, 30.0, ids["paperbot"]))
        self.registry.conn.commit()

        result = run_tournament(discover_bots(self.dir.name), rounds=10)
        record = self.registry.record_tournament(result, ids)

        paper = self.registry.get(ids["paperbot"])
        self.assertGreaterEqual(paper.peak_rating, floor)
        self.assertEqual(paper.league, title)
        self.assertIn(("paperbot", "novice", title), record.titles)
        # The bot it beat gains no title from losing.
        self.assertEqual(self.registry.get(ids["rockbot"]).league, "novice")

    def test_the_leader_is_never_stuck_at_novice(self):
        """Rating is the only door, so the lowest floor has to be reachable.

        Everybody plays one pool, so the bot at the top of the standings must
        hold a title — otherwise the Title column contradicts the order the
        table is sorted in, which is what the gauntlet-only first title did.
        """
        _, info = self.sync_one("ada", PAPER)
        floor = elo.floor_for("cm")
        self.registry.conn.execute(
            "UPDATE bots SET rating = ?, peak_rating = ? WHERE id = ?",
            (floor, floor, info.id))
        self.registry.conn.commit()

        self.assertEqual(self.registry._apply_titles(info.id), ("novice", "cm"))
        self.assertEqual(self.registry.get(info.id).league, "cm")

    def test_tournament_is_one_rating_period(self):
        """Ratings must not depend on the order matches finish in.

        Matches run in parallel, so `result.matches` arrives in completion
        order. Every match is scored against the ratings the run started with,
        which makes the same tournament produce the same ratings however its
        results are ordered.
        """
        bodies = {"ay": PAPER, "bee": ROCK, "cee": "def play(m, o):\n    return 'scissors'\n"}
        paths = {name: write_bot(self.dir.name, name, body)
                 for name, body in bodies.items()}
        matches = [
            MatchResult(bot_a="ay", bot_b="bee", wins_a=7, wins_b=3),   # ay wins
            MatchResult(bot_a="ay", bot_b="cee", wins_a=6, wins_b=4),   # ay wins
            MatchResult(bot_a="bee", bot_b="cee", wins_a=5, wins_b=5),  # draw
        ]

        def ratings_for(order, db):
            registry = Registry(os.path.join(self.dir.name, db))
            self.addCleanup(registry.close)
            sync = registry.sync_paths(list(paths.values()))
            ids = {os.path.splitext(os.path.basename(p))[0]: i.id
                   for p, i in sync.bots.items()}
            result = TournamentResult(
                standings=[Standing(name=n) for n in bodies],
                matches=order, rounds=10, matches_per_pair=1)
            record = registry.record_tournament(result, ids)
            return {name: round(c.after, 6) for name, c in record.ratings.items()}

        forward = ratings_for(matches, "forward.db")
        backward = ratings_for(list(reversed(matches)), "backward.db")
        self.assertEqual(forward, backward)
        # And the bot that won both its matches ended up above the two that did
        # not, rather than merely somewhere reproducible.
        self.assertGreater(forward["ay"], glicko.START_RATING)
        self.assertGreater(forward["ay"], forward["bee"])
        self.assertGreater(forward["ay"], forward["cee"])

    def test_a_long_period_sharpens_rather_than_amplifies(self):
        """A bigger rating period narrows the deviation; it does not multiply the
        step.

        Elo needed a step budget split across the period for this: without one, a
        field with more matches in it moved ratings further purely for being
        bigger, and past roughly K*N > 1700 the ladder rang instead of settling.
        Glicko-2 needs no budget, because more evidence enters as a smaller `rd`
        rather than as a longer stride — so thirty games against level opposition
        leave a bot near where it started, but much more certainly there.
        """
        bodies = {"ay": PAPER, "bee": ROCK,
                  "cee": "def play(m, o):\n    return 'scissors'\n",
                  "dee": "def play(m, o):\n    return 'rock'  # dee\n"}
        paths = {name: write_bot(self.dir.name, name, body)
                 for name, body in bodies.items()}
        sync = self.registry.sync_paths(list(paths.values()))
        ids = {os.path.splitext(os.path.basename(p))[0]: i.id
               for p, i in sync.bots.items()}

        # Ten meetings per pair: 60 matches over 4 bots is 30 each, past the
        # point where the budget starts binding.
        others = ["bee", "cee", "dee"]
        matches = [MatchResult(bot_a="ay", bot_b=o, wins_a=7, wins_b=3)
                   for o in others for _ in range(10)]
        matches += [MatchResult(bot_a=a, bot_b=b, wins_a=5, wins_b=5)
                    for i, a in enumerate(others) for b in others[i + 1:]
                    for _ in range(10)]
        result = TournamentResult(
            standings=[Standing(name=n) for n in bodies],
            matches=matches, rounds=10, matches_per_pair=10)
        record = self.registry.record_tournament(result, ids)

        self.assertEqual(record.ratings["ay"].games, 30)
        ay = self.registry.get(ids["ay"])
        # Thirty games is a lot of evidence, so the deviation collapses...
        self.assertLess(ay.rd, glicko.START_RD / 3.0)
        # ...but the rating stays inside the range a rating can reach at all,
        # rather than running off the way K*N would have sent it.
        self.assertGreater(ay.rating, glicko.START_RATING)
        self.assertLess(ay.rating, glicko.START_RATING + 1000.0)

        # The same sweep in a period a third the size moves the rating a
        # comparable distance: the period's size is not a multiplier on the step.
        short = Registry(os.path.join(self.dir.name, "short.db"))
        self.addCleanup(short.close)
        short_ids = {os.path.splitext(os.path.basename(p))[0]: i.id
                     for p, i in short.sync_paths(list(paths.values())).bots.items()}
        few = [MatchResult(bot_a="ay", bot_b=o, wins_a=7, wins_b=3) for o in others]
        few += [MatchResult(bot_a=a, bot_b=b, wins_a=5, wins_b=5)
                for i, a in enumerate(others) for b in others[i + 1:]]
        short.record_tournament(
            TournamentResult(standings=[Standing(name=n) for n in bodies],
                             matches=few, rounds=10, matches_per_pair=1),
            short_ids)
        one_round = short.get(short_ids["ay"])
        self.assertLess(abs(ay.rating - one_round.rating), 400.0)
        # What the longer period bought is certainty, not altitude.
        self.assertLess(ay.rd, one_round.rd)

    # -- norm events -------------------------------------------------------

    def _norm_field(self, size=12, submitters=6, league="master", rating=1900.0):
        """Register `size` bots that together satisfy the composition rules."""
        registered = []
        for i in range(size):
            body = "def play(m, o):\n    return 'rock'  # bot %d\n" % i
            path = write_bot(self.dir.name, "nb%02d" % i, body)
            info = self.registry.sync_paths(
                [path], submitter="who%d@example.com" % (i % submitters)).bots[path]
            self.registry.conn.execute(
                "UPDATE bots SET league = ?, rating = ?, peak_rating = ? WHERE id = ?",
                (league, rating - i, rating - i, info.id))
            registered.append((path, self.registry.get(info.id)))
        self.registry.conn.commit()
        return registered

    def _play_among(self, registered, winner_index=None):
        """A round robin over `registered`; `winner_index` sweeps every match."""
        stems = [os.path.splitext(os.path.basename(p))[0] for p, _ in registered]
        matches = []
        for i, a in enumerate(stems):
            for j, b in enumerate(stems[i + 1:], start=i + 1):
                if winner_index == i:
                    matches.append(MatchResult(bot_a=a, bot_b=b, wins_a=7, wins_b=3))
                elif winner_index == j:
                    matches.append(MatchResult(bot_a=a, bot_b=b, wins_a=3, wins_b=7))
                else:
                    matches.append(MatchResult(bot_a=a, bot_b=b, wins_a=5, wins_b=5))
        result = TournamentResult(
            standings=[Standing(name=s) for s in stems],
            matches=matches, rounds=10, matches_per_pair=1)
        ids = {s: info.id for s, (_p, info) in zip(stems, registered)}
        return result, ids, stems

    def test_a_norm_event_awards_a_norm_for_a_sweep(self):
        registered = self._norm_field()
        result, ids, stems = self._play_among(registered, winner_index=0)
        record = self.registry.record_tournament(result, ids, kind="norm")

        self.assertIn(stems[0], record.norms)
        earned = record.norms[stems[0]]
        self.assertIn(earned.title, ("im", "gm"))
        self.assertEqual(earned.games, len(registered) - 1)
        # And it is on the record as evidence, not just a counter.
        stored = self.registry.norms_for(ids[stems[0]])
        self.assertEqual(len(stored), 1)
        self.assertAlmostEqual(stored[0]["performance"], earned.performance)

    def test_a_reference_bot_cannot_be_awarded_a_norm(self):
        """Belt and braces behind `select_field`.

        Selection keeps the organizer's own bots out of an event, but this method
        awards from whatever field it is handed — so the guard sits where it cannot
        be bypassed. `randy` really did bank an IM norm before this existed.
        """
        registered = self._norm_field()
        # Rename one entrant to a reference bot and strip its submitter, which is
        # what `reference.is_reference` reads.
        bot_id = registered[0][1].id
        self.registry.conn.execute(
            "UPDATE bots SET name = 'Randy', submitter = '' WHERE id = ?", (bot_id,))
        self.registry.conn.commit()
        registered[0] = (registered[0][0], self.registry.get(bot_id))

        result, ids, stems = self._play_among(registered, winner_index=0)
        record = self.registry.record_tournament(result, ids, kind="norm")
        self.assertNotIn(stems[0], record.norms)
        self.assertEqual(self.registry.norm_counts(bot_id), {})
        # The rest of the field is unaffected — it is not the event that is void.
        result, ids, stems = self._play_among(registered, winner_index=1)
        record = self.registry.record_tournament(result, ids, kind="norm")
        self.assertIn(stems[1], record.norms)

    def test_a_bot_farming_its_own_siblings_earns_nothing(self):
        """The attack a norm exists to be immune to.

        Five of the twelve entrants share the sweeper's submitter — a full
        `ACTIVE_PER_SUBMITTER` allocation — so four of its eleven opponents are bots
        its own owner controls, over the one-in-three line. However well it scored,
        the event cannot certify it, while the same sweep by an entrant with no
        siblings in the field does earn a norm.
        """
        # Bot 0 and four others share one submitter; the rest are all distinct.
        registered = []
        for i in range(12):
            body = "def play(m, o):\n    return 'rock'  # sib %d\n" % i
            path = write_bot(self.dir.name, "sb%02d" % i, body)
            who = "clan@example.com" if i < 5 else "solo%d@example.com" % i
            info = self.registry.sync_paths([path], submitter=who).bots[path]
            self.registry.conn.execute(
                "UPDATE bots SET league='master', rating=?, peak_rating=? WHERE id=?",
                (1900.0 - i, 1900.0 - i, info.id))
            registered.append((path, self.registry.get(info.id)))
        self.registry.conn.commit()

        result, ids, stems = self._play_among(registered, winner_index=0)
        record = self.registry.record_tournament(result, ids, kind="norm")
        self.assertNotIn(stems[0], record.norms)          # 4 of 11 own -> refused
        self.assertEqual(self.registry.norm_counts(ids[stems[0]]), {})

        # The control: an identical sweep by a bot with no siblings does count.
        result, ids, stems = self._play_among(registered, winner_index=5)
        record = self.registry.record_tournament(result, ids, kind="norm")
        self.assertIn(stems[5], record.norms)

    def test_a_league_run_awards_nothing_however_well_anyone_played(self):
        """Only a norm event can award a norm — an ordinary run cannot."""
        registered = self._norm_field()
        result, ids, stems = self._play_among(registered, winner_index=0)
        record = self.registry.record_tournament(result, ids)
        self.assertEqual(record.norms, {})
        self.assertEqual(self.registry.norm_counts(ids[stems[0]]), {})

    def test_a_norm_event_needs_a_qualifying_field(self):
        """One person's bots cannot be a norm event, so recording one is refused."""
        registered = self._norm_field(size=12, submitters=1)
        result, ids, _stems = self._play_among(registered, winner_index=0)
        with self.assertRaises(NotANormEvent):
            self.registry.record_tournament(result, ids, kind="norm")

    def test_a_weak_field_awards_no_norm(self):
        """Beating a field of average bots is not a performance, however clean."""
        registered = self._norm_field(rating=elo.START_RATING, league="cm")
        result, ids, stems = self._play_among(registered, winner_index=0)
        record = self.registry.record_tournament(result, ids, kind="norm")
        self.assertEqual(record.norms, {})

    def test_norms_and_a_peak_together_earn_the_title(self):
        registered = self._norm_field()
        bot_id = registered[0][1].id
        needed = elo.NORM_REQUIREMENTS["im"][0]
        self.registry.conn.execute(
            "UPDATE bots SET peak_rating = ?, league = ? WHERE id = ?",
            (elo.floor_for("im"), "novice", bot_id))
        for n in range(needed):
            self.registry.conn.execute(
                """INSERT INTO norms (bot_id, played_at, title, performance,
                                      field_avg, games)
                   VALUES (?, ?, 'im', 2000, 1800, 11)""",
                (bot_id, "2026-01-%02dT00:00:00+00:00" % (n + 1)))
        self.registry.conn.commit()
        self.assertEqual(self.registry._apply_titles(bot_id), ("novice", "im"))

    def test_a_peak_without_norms_stops_at_master(self):
        registered = self._norm_field()
        bot_id = registered[0][1].id
        self.registry.conn.execute(
            "UPDATE bots SET peak_rating = ?, league = ? WHERE id = ?",
            (elo.floor_for("gm"), "novice", bot_id))
        self.registry.conn.commit()
        self.assertEqual(self.registry._apply_titles(bot_id), ("novice", "master"))

    def test_norms_are_due_when_none_has_ever_run(self):
        self.assertTrue(self.registry.norm_event_due())
        self.assertIsNone(self.registry.last_norm_event())

    def test_a_recent_norm_event_is_not_due_again(self):
        registered = self._norm_field()
        result, ids, _stems = self._play_among(registered, winner_index=0)
        self.registry.record_tournament(result, ids, kind="norm")
        self.assertIsNotNone(self.registry.last_norm_event())
        self.assertFalse(self.registry.norm_event_due())
        # ...but it comes due again once the interval has passed.
        later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            hours=norms.NORM_INTERVAL_HOURS + 1)
        self.assertTrue(self.registry.norm_event_due(now=later))

    def test_the_interval_is_configurable(self):
        """A shorter configured interval brings the next event forward."""
        registered = self._norm_field()
        result, ids, _stems = self._play_among(registered, winner_index=0)
        self.registry.record_tournament(result, ids, kind="norm")
        soon = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)
        self.assertFalse(self.registry.norm_event_due(now=soon))
        self.assertTrue(self.registry.norm_event_due(interval_hours=1, now=soon))
        # Zero holds one every run.
        self.assertTrue(self.registry.norm_event_due(interval_hours=0))

    def test_a_reset_clears_norms(self):
        registered = self._norm_field()
        result, ids, stems = self._play_among(registered, winner_index=0)
        self.registry.record_tournament(result, ids, kind="norm")
        self.assertTrue(self.registry.norm_counts(ids[stems[0]]))
        self.registry.reset_rankings()
        self.assertEqual(self.registry.norm_counts(ids[stems[0]]), {})

    def test_removing_a_bot_removes_its_norms(self):
        registered = self._norm_field()
        result, ids, stems = self._play_among(registered, winner_index=0)
        self.registry.record_tournament(result, ids, kind="norm")
        bot_id = ids[stems[0]]
        self.assertTrue(self.registry.norm_counts(bot_id))
        self.registry.remove(bot_id)
        self.assertEqual(
            self.registry.conn.execute(
                "SELECT COUNT(*) FROM norms WHERE bot_id = ?", (bot_id,)
            ).fetchone()[0], 0)

    def test_titles_are_never_taken_away(self):
        top = elo.TITLES[-1]
        _, info = self.sync_one("ada", PAPER)
        self.registry.conn.execute(
            "UPDATE bots SET league = ? WHERE id = ?", (top, info.id))
        self.registry.conn.commit()
        # A rating that sank below the floor the title came from keeps it.
        self.registry.conn.execute(
            "UPDATE bots SET rating = ?, peak_rating = ? WHERE id = ?",
            (elo.START_RATING, elo.START_RATING, info.id))
        self.registry.conn.commit()
        self.registry._apply_titles(info.id)
        self.assertEqual(self.registry.get(info.id).league, top)

    def test_legacy_titles_reset_to_the_first_rung(self):
        """A points-era database opens on the current ladder with its bots intact.

        Old titles aren't translated — they're dropped back to the first rung and
        re-earned. Losing a title is cheap; losing a bot is not.
        """
        import sqlite3

        path = os.path.join(self.dir.name, "legacy.db")
        con = sqlite3.connect(path)          # the pre-Elo schema, titles and all
        con.executescript(
            """CREATE TABLE bots (id TEXT PRIMARY KEY, name TEXT NOT NULL,
                                  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                                  league TEXT NOT NULL DEFAULT 'noob',
                                  banned INTEGER NOT NULL DEFAULT 0,
                                  ban_reason TEXT NOT NULL DEFAULT '',
                                  trial_wins INTEGER NOT NULL DEFAULT 0,
                                  wins INTEGER NOT NULL DEFAULT 0,
                                  losses INTEGER NOT NULL DEFAULT 0,
                                  draws INTEGER NOT NULL DEFAULT 0,
                                  forfeits INTEGER NOT NULL DEFAULT 0,
                                  round_wins INTEGER NOT NULL DEFAULT 0,
                                  round_losses INTEGER NOT NULL DEFAULT 0);""")
        for i, old in enumerate(("noob", "rookie", "pro")):
            con.execute("INSERT INTO bots (id, name, first_seen, last_seen, league)"
                        " VALUES (?, ?, '2026-01-01', '2026-01-01', ?)",
                        ("id%d" % i, old, old))
        con.commit()
        con.close()

        registry = Registry(path)
        self.addCleanup(registry.close)
        bots = registry.list_bots()
        self.assertEqual({b.name for b in bots}, {"noob", "rookie", "pro"})
        self.assertTrue(all(b.league == elo.TITLES[0] for b in bots))
        self.assertTrue(all(b.rating == elo.START_RATING for b in bots))
        # A current title survives re-opening (only unknown ones are dropped).
        registry.conn.execute("UPDATE bots SET league = 'im' WHERE id = 'id0'")
        registry.conn.commit()
        Registry(path).close()
        self.assertEqual(registry.get("id0").league, "im")

    def test_reset_wipes_the_season_but_keeps_the_bots(self):
        a = write_bot(self.dir.name, "paperbot", PAPER)
        b = write_bot(self.dir.name, "rockbot", ROCK)
        sync = self.registry.sync_paths([a, b], submitter="yisus")
        ids = {os.path.splitext(os.path.basename(p))[0]: i.id
               for p, i in sync.bots.items()}
        self.sync_one("evil", "import os\n" + ROCK)      # banned by safety
        self.registry.record_tournament(run_tournament([a, b], rounds=10), ids)
        self.registry.conn.execute(
            "UPDATE bots SET league = 'gm' WHERE id = ?", (ids["paperbot"],))
        self.registry.conn.commit()

        before = {b.id: b for b in self.registry.list_bots()}
        kept, dropped = self.registry.reset_rankings()

        self.assertEqual(kept, 3)
        self.assertGreater(dropped, 0)
        after = {b.id: b for b in self.registry.list_bots()}
        self.assertEqual(set(after), set(before))        # every bot still here
        for bot_id, bot in after.items():
            was = before[bot_id]
            # Kept: who the bot is, and whether it is allowed to play at all.
            self.assertEqual((bot.name, bot.submitter, bot.path, bot.first_seen),
                             (was.name, was.submitter, was.path, was.first_seen))
            self.assertEqual((bot.banned, bot.ban_reason),
                             (was.banned, was.ban_reason))
            # Gone: everything the bot earned.
            self.assertEqual(bot.league, elo.TITLES[0])
            self.assertEqual((bot.rating, bot.peak_rating),
                             (elo.START_RATING, elo.START_RATING))
            self.assertEqual((bot.rated_games, bot.wins, bot.losses, bot.draws,
                              bot.forfeits, bot.round_wins, bot.round_losses),
                             (0, 0, 0, 0, 0, 0, 0))
        self.assertEqual(
            self.registry.conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0], 0)
        self.assertEqual(
            self.registry.conn.execute(
                "SELECT COUNT(*) FROM rating_history").fetchone()[0], 0)
        # A reset registry resets again cleanly (and stays empty of matches).
        self.assertEqual(self.registry.reset_rankings(), (3, 0))

    def test_stores_path_and_lists_eligible(self):
        path_a, info_a = self.sync_one("alpha", PAPER)
        self.assertEqual(info_a.path, os.path.abspath(path_a))
        self.sync_one("evil", "import os\n" + ROCK)  # banned by safety
        eligible = {b.name: b for b in self.registry.eligible_bots()}
        self.assertIn("Alpha", eligible)
        self.assertEqual(eligible["Alpha"].path, os.path.abspath(path_a))
        self.assertNotIn("Evil", eligible)  # banned bots aren't eligible

    # -- holding: a bot on the record but out of the field --------------------

    def test_held_bot_is_registered_but_not_eligible(self):
        path, info = self.sync_one("ada", PAPER)
        self.assertFalse(info.held)
        held = self.registry.set_held(info.id, True)
        self.assertTrue(held.held)
        # Still registered, and everything it had is untouched.
        self.assertEqual(held.id, info.id)
        self.assertEqual(held.first_seen, info.first_seen)
        self.assertEqual(held.rating, info.rating)
        self.assertIn("Ada", {b.name for b in self.registry.list_bots()})
        # ...but out of the field, by either route into a tournament.
        self.assertNotIn("Ada", {b.name for b in self.registry.eligible_bots()})
        self.assertNotIn(path, self.registry.sync_paths([path]).eligible())
        # Releasing puts it straight back.
        self.assertFalse(self.registry.set_held(info.id, False).held)
        self.assertIn("Ada", {b.name for b in self.registry.eligible_bots()})

    def test_include_held_overrides_only_the_hold(self):
        path, info = self.sync_one("ada", PAPER)
        _, evil = self.sync_one("evil", "import os\n" + ROCK)
        self.registry.set_held(info.id, True)
        names = {b.name for b in self.registry.eligible_bots(include_held=True)}
        self.assertIn("Ada", names)
        self.assertNotIn("Evil", names)   # a ban is never overridden
        self.assertIn(path, self.registry.sync_paths([path]).eligible(include_held=True))

    def test_resync_never_overturns_a_hold(self):
        """A routine re-sync of stored files must not release a held bot, nor
        re-hold a released one — the owner's decision outlives the scan."""
        path, info = self.sync_one("ada", PAPER)
        self.registry.set_held(info.id, True)
        self.assertTrue(self.registry.sync_paths([path]).bots[path].held)
        # Even a sync that explicitly asks to hold leaves a released bot alone:
        # `held` applies to first registration only.
        self.registry.set_held(info.id, False)
        self.assertFalse(self.registry.sync_paths([path], held=True).bots[path].held)

    def test_upload_as_held_on_first_registration(self):
        path = write_bot(self.dir.name, "ada", PAPER)
        info = self.registry.sync_paths([path], held=True).bots[path]
        self.assertTrue(info.held)
        self.assertFalse(info.banned)
        self.assertEqual(info.rating, elo.START_RATING)

    def test_hold_survives_a_season_reset(self):
        """`reset` clears what a bot earned, not what its owner decided."""
        _, kept = self.sync_one("ada", PAPER)
        _, held = self.sync_one("bob", ROCK)
        self.registry.set_held(held.id, True)
        self.registry.reset_rankings()
        self.assertTrue(self.registry.get(held.id).held)
        self.assertFalse(self.registry.get(kept.id).held)

    def test_set_held_unknown_id(self):
        self.assertIsNone(self.registry.set_held("deadbeefcafe", True))


    def test_find_resolves_id_prefix_and_name(self):
        _, ada = self.sync_one("ada", PAPER)
        self.assertEqual(self.registry.find(ada.id)[0][0].id, ada.id)
        self.assertEqual(self.registry.find(ada.id[:6])[0][0].id, ada.id)
        self.assertEqual(self.registry.find("ADA")[0][0].id, ada.id)
        self.assertEqual(self.registry.find("nope")[0], [])

    def test_find_reports_ambiguity_instead_of_guessing(self):
        self.sync_one("ada", PAPER)
        self.sync_one("ada2", ROCK)
        matches, how = self.registry.find("")   # empty prefix matches everything
        self.assertEqual(len(matches), 2)
        self.assertEqual(how, "id prefix")

    def test_remove_deletes_bot_and_matches(self):
        a = write_bot(self.dir.name, "ada", PAPER)
        b = write_bot(self.dir.name, "bob", ROCK)
        sync = self.registry.sync_paths([a, b])
        ids = {os.path.splitext(os.path.basename(p))[0]: i.id
               for p, i in sync.bots.items()}
        self.registry.record_tournament(run_tournament([a, b], rounds=10), ids)
        info = self.registry.get(ids["ada"])
        removed = self.registry.remove(info.id)
        self.assertEqual(removed.id, info.id)
        self.assertIsNone(self.registry.get(info.id))
        left = self.registry.conn.execute(
            "SELECT COUNT(*) FROM matches WHERE bot_a = ?", (info.id,)).fetchone()[0]
        self.assertEqual(left, 0)
        self.assertIsNone(self.registry.remove("deadbeef0000"))  # missing id is a no-op

    def test_bot_id_is_content_hash(self):
        a = write_bot(self.dir.name, "x", ROCK)
        b = write_bot(self.dir.name, "y", ROCK)
        self.assertEqual(bot_id_for(a), bot_id_for(b))


class FieldLimitTest(unittest.TestCase):
    """One entrant may have ACTIVE_PER_SUBMITTER bots in the field at a time."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.registry = Registry(os.path.join(self.dir.name, "test.db"))
        self.addCleanup(self.registry.close)

    def bot(self, n, submitter="ada@x.test", body=None):
        """Register bot number `n`. Distinct bodies, so distinct identities."""
        path = write_bot(self.dir.name, "bot%d" % n,
                         body or "def play(m, o):\n    return 'rock'  # %d\n" % n)
        return path, self.registry.sync_paths([path], submitter=submitter)

    def fill(self, submitter="ada@x.test"):
        """Take every slot this submitter has."""
        for n in range(ACTIVE_PER_SUBMITTER):
            self.bot(n, submitter)

    def test_bots_up_to_the_limit_enter_the_field(self):
        self.fill()
        self.assertEqual(self.registry.active_count("ada@x.test"),
                         ACTIVE_PER_SUBMITTER)
        self.assertEqual(len(self.registry.eligible_bots()), ACTIVE_PER_SUBMITTER)

    def test_the_next_one_registers_held_not_refused(self):
        self.fill()
        path, sync = self.bot(99)
        info = sync.bots[path]
        self.assertTrue(info.held)
        self.assertEqual(sync.auto_held, [path])
        # Registered in full: an id, an upload date, a starting rating. The only
        # thing it lacks is a place in the field.
        self.assertIsNotNone(self.registry.get(info.id))
        self.assertEqual(info.rating, elo.START_RATING)
        self.assertFalse(info.banned)
        # ...and it did not take a slot from anybody.
        self.assertEqual(self.registry.active_count("ada@x.test"),
                         ACTIVE_PER_SUBMITTER)

    def test_one_sync_of_many_fills_the_last_slots_then_holds(self):
        """The count tracks *within* a call — otherwise a batch of six would all
        pass a single up-front check and land in the field together."""
        for n in range(ACTIVE_PER_SUBMITTER - 1):
            self.bot(n)
        paths = [write_bot(self.dir.name, "extra%d" % n,
                           "def play(m, o):\n    return 'paper'  # %d\n" % n)
                 for n in range(3)]
        sync = self.registry.sync_paths(paths, submitter="ada@x.test")
        self.assertEqual([sync.bots[p].held for p in paths], [False, True, True])
        self.assertEqual(sync.auto_held, paths[1:])
        self.assertEqual(self.registry.active_count("ada@x.test"),
                         ACTIVE_PER_SUBMITTER)

    def test_the_limit_is_per_submitter(self):
        self.fill("ada@x.test")
        path, sync = self.bot(99, submitter="bob@x.test")
        self.assertFalse(sync.bots[path].held)
        self.assertEqual(self.registry.active_count("bob@x.test"), 1)

    def test_submitters_match_case_and_space_insensitively(self):
        """The same person arrives lower-cased from SSO and as typed from the
        CLI; two spellings must not buy two fields."""
        self.fill("ada@x.test")
        self.assertEqual(self.registry.active_count("  ADA@X.test "),
                         ACTIVE_PER_SUBMITTER)
        path, sync = self.bot(99, submitter="Ada@X.Test")
        self.assertTrue(sync.bots[path].held)

    def test_bots_with_no_submitter_are_uncapped(self):
        """An empty submitter is the reference bots and anything the CLI syncs
        unattributed — not a person, so not one person's field."""
        for n in range(ACTIVE_PER_SUBMITTER + 3):
            path, sync = self.bot(n, submitter="")
            self.assertFalse(sync.bots[path].held)
        self.assertEqual(self.registry.active_count(""), 0)

    def test_a_banned_bot_neither_takes_a_slot_nor_is_held(self):
        for n in range(ACTIVE_PER_SUBMITTER - 1):
            self.bot(n)
        path, sync = self.bot(99, body="import os\n" + ROCK)
        self.assertTrue(sync.bots[path].banned)
        self.assertFalse(sync.bots[path].held)   # the ban is the honest reason
        self.assertEqual(self.registry.active_count("ada@x.test"),
                         ACTIVE_PER_SUBMITTER - 1)

    def test_resync_never_withdraws_a_bot_that_was_playing(self):
        """An organizer re-syncing stored files must not pull entries because
        their owner is over a cap that arrived after they registered."""
        self.fill()
        # Put the registry over the limit the way a database written before it
        # existed would be.
        extra = write_bot(self.dir.name, "extra", PAPER)
        self.registry.sync_paths([extra], submitter="ada@x.test", limit=None)
        self.assertEqual(self.registry.active_count("ada@x.test"),
                         ACTIVE_PER_SUBMITTER + 1)

        for path in [b.path for b in self.registry.eligible_bots()]:
            self.assertFalse(
                self.registry.sync_paths([path], submitter="ada@x.test").bots[path].held)
        self.assertEqual(self.registry.active_count("ada@x.test"),
                         ACTIVE_PER_SUBMITTER + 1)

    def test_release_past_the_limit_is_refused(self):
        self.fill()
        path, sync = self.bot(99)
        held = sync.bots[path]
        with self.assertRaises(FieldFull):
            self.registry.set_held(held.id, False)
        self.assertTrue(self.registry.get(held.id).held)   # unchanged

    def test_release_works_once_a_slot_is_free(self):
        self.fill()
        path, sync = self.bot(99)
        waiting = sync.bots[path].id
        playing = self.registry.eligible_bots()[0].id
        self.registry.set_held(playing, True)             # holding is never blocked
        self.assertFalse(self.registry.set_held(waiting, False).held)
        self.assertEqual(self.registry.active_count("ada@x.test"),
                         ACTIVE_PER_SUBMITTER)

    def test_releasing_a_banned_bot_is_not_blocked(self):
        """A ban keeps it out of the field either way, so lifting the hold
        cannot break a limit on how many are in it."""
        self.fill()
        path, sync = self.bot(99, body="import socket\n" + ROCK)
        banned = sync.bots[path]
        self.registry.set_held(banned.id, True)
        self.assertFalse(self.registry.set_held(banned.id, False).held)

    def test_limit_none_lifts_it(self):
        self.fill()
        extra = write_bot(self.dir.name, "extra", PAPER)
        info = self.registry.sync_paths(
            [extra], submitter="ada@x.test", limit=None).bots[extra]
        self.assertFalse(info.held)
        self.assertEqual(self.registry.active_count("ada@x.test"),
                         ACTIVE_PER_SUBMITTER + 1)

    def test_disabling_frees_a_slot(self):
        """Otherwise the only way out of a full field would be to email the
        organizer."""
        self.fill()
        first = self.registry.eligible_bots()[0]
        self.registry.set_disabled(first.id, True)
        self.assertEqual(self.registry.active_count("ada@x.test"),
                         ACTIVE_PER_SUBMITTER - 1)

    def test_enabling_into_a_full_field_is_refused(self):
        """Disabling must not become the way around the cap: hide one, upload a
        replacement, then bring the hidden one back would make six."""
        self.fill()
        first = self.registry.eligible_bots()[0]
        self.registry.set_disabled(first.id, True)
        self.bot(99)                                   # takes the freed slot
        with self.assertRaises(FieldFull):
            self.registry.set_disabled(first.id, False)


class DisabledTest(unittest.TestCase):
    """Hidden for good by its owner, visible only to the CLI."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.registry = Registry(os.path.join(self.dir.name, "test.db"))
        self.addCleanup(self.registry.close)

    def bot(self, name, body=ROCK, submitter="ada@x.test"):
        path = write_bot(self.dir.name, name, body)
        return path, self.registry.sync_paths([path], submitter=submitter).bots[path]

    def test_a_new_bot_is_not_disabled(self):
        _, info = self.bot("ada")
        self.assertFalse(info.disabled)

    def test_disabling_hides_it_from_every_listing(self):
        _, info = self.bot("ada")
        self.bot("grace", PAPER)
        self.registry.set_disabled(info.id, True)

        self.assertNotIn(info.id, {b.id for b in self.registry.list_bots()})
        self.assertNotIn(info.id, {b.id for b in self.registry.eligible_bots()})

    def test_the_admin_listing_still_shows_it(self):
        _, info = self.bot("ada")
        self.registry.set_disabled(info.id, True)

        self.assertIn(info.id,
                      {b.id for b in self.registry.list_bots(include_disabled=True)})
        self.assertEqual([b.id for b in self.registry.list_disabled()], [info.id])

    def test_it_stays_findable_so_enable_can_reach_it(self):
        """The owner cannot see it to ask for it, so the lookup must not lose
        it — otherwise the flag is a one-way door in fact, not just policy."""
        _, info = self.bot("ada")
        self.registry.set_disabled(info.id, True)

        by_name, how = self.registry.find("Ada")
        self.assertEqual([b.id for b in by_name], [info.id])
        self.assertEqual(how, "name")
        self.assertIsNotNone(self.registry.get(info.id))

    def test_enabling_puts_it_back(self):
        _, info = self.bot("ada")
        self.registry.set_disabled(info.id, True)
        back = self.registry.set_disabled(info.id, False)

        self.assertFalse(back.disabled)
        self.assertIn(info.id, {b.id for b in self.registry.list_bots()})

    def test_nothing_it_earned_is_touched(self):
        _, info = self.bot("ada")
        self.registry.conn.execute(
            "UPDATE bots SET wins = 7, rating = 1712 WHERE id = ?", (info.id,))
        self.registry.conn.commit()

        self.registry.set_disabled(info.id, True)
        hidden = self.registry.get(info.id)
        self.assertEqual(hidden.wins, 7)
        self.assertEqual(hidden.rating, 1712)
        self.assertEqual(hidden.first_seen, info.first_seen)

    def test_its_matches_survive(self):
        """The whole reason this is not `remove`: an opponent's record must not
        change because somebody else hid their bot."""
        _, mine = self.bot("ada")
        _, theirs = self.bot("grace", PAPER, submitter="grace@x.test")
        self.registry.conn.execute(
            """INSERT INTO matches (played_at, kind, bot_a, bot_b, wins_a,
                                    wins_b, draws)
               VALUES ('2026-01-01T00:00:00+00:00', 'tournament', ?, ?, 6, 4, 0)""",
            (mine.id, theirs.id))
        self.registry.conn.commit()

        self.registry.set_disabled(mine.id, True)
        still = self.registry.conn.execute(
            "SELECT COUNT(*) FROM matches WHERE bot_a = ? OR bot_b = ?",
            (mine.id, mine.id)).fetchone()[0]
        self.assertEqual(still, 1)

    def test_a_resync_of_the_file_does_not_un_hide_it(self):
        """The periodic sync re-registers whatever files are on disk; it must
        not undo somebody's decision on the way past."""
        path, info = self.bot("ada")
        self.registry.set_disabled(info.id, True)

        self.registry.sync_paths([path], submitter="ada@x.test")
        self.assertTrue(self.registry.get(info.id).disabled)

    def test_a_disabled_bot_is_not_in_a_sync_result_field(self):
        path, info = self.bot("ada")
        self.registry.set_disabled(info.id, True)

        sync = self.registry.sync_paths([path], submitter="ada@x.test")
        self.assertNotIn(path, sync.eligible())
        # include_held is for a throwaway local run; it must not reach past a
        # decision the owner cannot themselves undo.
        self.assertNotIn(path, sync.eligible(include_held=True))

    def test_disabling_an_unknown_bot_is_none_not_a_crash(self):
        self.assertIsNone(self.registry.set_disabled("f" * 64, True))


class AvatarTest(unittest.TestCase):
    """Pictures are stored beside the record, and change nothing about it."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.registry = Registry(os.path.join(self.dir.name, "test.db"))
        self.addCleanup(self.registry.close)
        path = write_bot(self.dir.name, "ada", PAPER)
        self.bot = self.registry.sync_paths([path]).bots[path]
        self.path = path

    def test_round_trips(self):
        self.assertTrue(self.registry.set_image(self.bot.id, b"\x89PNG-pretend"))
        self.assertEqual(self.registry.get_image(self.bot.id), b"\x89PNG-pretend")

    def test_a_bot_without_one_reads_as_none(self):
        self.assertIsNone(self.registry.get_image(self.bot.id))
        self.assertEqual(self.registry.image_ids(), set())

    def test_replacing_overwrites_rather_than_accumulating(self):
        self.registry.set_image(self.bot.id, b"first")
        self.registry.set_image(self.bot.id, b"second")
        self.assertEqual(self.registry.get_image(self.bot.id), b"second")
        self.assertEqual(len(self.registry.images()), 1)

    def test_setting_none_drops_it(self):
        self.registry.set_image(self.bot.id, b"gone soon")
        self.registry.set_image(self.bot.id, None)
        self.assertIsNone(self.registry.get_image(self.bot.id))
        self.assertEqual(self.registry.image_ids(), set())

    def test_refuses_an_avatar_for_a_bot_that_is_not_registered(self):
        self.assertFalse(self.registry.set_image("deadbeef0000", b"orphan"))
        self.assertEqual(self.registry.images(), {})

    def test_a_picture_changes_nothing_the_bot_has_earned(self):
        before = self.registry.get(self.bot.id)
        self.registry.set_image(self.bot.id, b"decoration")
        after = self.registry.get(self.bot.id)
        self.assertEqual(before, after)

    def test_re_syncing_the_same_file_keeps_it(self):
        # The organizer re-syncs stored files on a timer; that must not cost
        # anybody their picture.
        self.registry.set_image(self.bot.id, b"keep me")
        self.registry.sync_paths([self.path])
        self.assertEqual(self.registry.get_image(self.bot.id), b"keep me")

    def test_a_new_season_keeps_it(self):
        # reset_rankings keeps the bots, so it keeps their pictures: what it
        # wipes is what was *earned*, and a picture is not that.
        self.registry.set_image(self.bot.id, b"keep me")
        self.registry.reset_rankings()
        self.assertEqual(self.registry.get_image(self.bot.id), b"keep me")

    def test_removing_the_bot_removes_it(self):
        self.registry.set_image(self.bot.id, b"goes with it")
        self.registry.remove(self.bot.id)
        self.assertEqual(self.registry.images(), {})

    def test_images_are_keyed_by_bot_id(self):
        other = write_bot(self.dir.name, "bob", ROCK)
        second = self.registry.sync_paths([other]).bots[other]
        self.registry.set_image(self.bot.id, b"one")
        self.registry.set_image(second.id, b"two")
        self.assertEqual(self.registry.images(),
                         {self.bot.id: b"one", second.id: b"two"})
        self.assertEqual(self.registry.image_ids(), {self.bot.id, second.id})

    def test_an_older_database_gains_the_table_on_open(self):
        # `bot_images` arrived after the first schema, so a registry file
        # written before it must pick the table up rather than fail to open.
        path = os.path.join(self.dir.name, "old.db")
        Registry(path).close()                   # a current file...
        legacy = sqlite3.connect(path)
        legacy.execute("DROP TABLE bot_images")  # ...aged back a version
        legacy.commit()
        legacy.close()
        with Registry(path) as reg:
            self.assertEqual(reg.images(), {})


if __name__ == "__main__":
    unittest.main()
