"""Announcements: the store, and the duration parsing the CLI leans on."""

import datetime
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from janken.announce import (
    MAX_MESSAGE,
    AnnounceStore,
    BadDuration,
    parse_duration,
)

UTC = datetime.timezone.utc


def moment(hour=12, minute=0):
    return datetime.datetime(2026, 8, 19, hour, minute, tzinfo=UTC)


class DurationTest(unittest.TestCase):
    def test_the_usual_forms(self):
        self.assertEqual(parse_duration("90s"), 90)
        self.assertEqual(parse_duration("30min"), 1800)
        self.assertEqual(parse_duration("2h"), 7200)
        self.assertEqual(parse_duration("1d"), 86400)

    def test_spelled_out_and_spaced_and_capitalised(self):
        self.assertEqual(parse_duration("2 hours"), 7200)
        self.assertEqual(parse_duration(" 45 Minutes "), 2700)
        self.assertEqual(parse_duration("1.5h"), 5400)

    def test_a_bare_number_is_refused_not_guessed(self):
        """The unit is genuinely ambiguous — seconds to systemd, minutes to a
        person typing about a maintenance window."""
        for raw in ("30", "0", "", "   ", None, "soon", "2 fortnights", "-5m"):
            with self.assertRaises(BadDuration, msg=repr(raw)):
                parse_duration(raw)


class PostTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = AnnounceStore(os.path.join(self.dir.name, "t.db"))
        self.addCleanup(self.store.close)

    def test_posting_puts_it_on_the_banner(self):
        posted = self.store.post("Tournaments are paused for maintenance",
                                 posted_by="ada")
        self.assertEqual(posted.posted_by, "ada")
        self.assertIsNone(posted.expires_at)
        live = self.store.live()
        self.assertEqual([a.id for a in live], [posted.id])

    def test_posting_does_not_replace_what_is_up(self):
        """Two things can be true at once, and posting must not silently destroy
        a standing notice to add a passing one."""
        first = self.store.post("Submissions close Friday")
        second = self.store.post("Back in ten minutes")
        self.assertEqual([a.id for a in self.store.live()],
                         [second.id, first.id])          # newest first

    def test_whitespace_is_collapsed_and_emptiness_refused(self):
        self.assertEqual(self.store.post("  paused \n  for  maintenance ").message,
                         "paused for maintenance")
        for bad in ("", "   ", "\n\t"):
            with self.assertRaises(ValueError):
                self.store.post(bad)

    def test_an_over_long_message_is_refused(self):
        with self.assertRaises(ValueError):
            self.store.post("x" * (MAX_MESSAGE + 1))
        self.assertTrue(self.store.post("x" * MAX_MESSAGE))

    def test_an_announcement_carries_no_severity(self):
        """There were levels for a day. One channel, one look — the operator says
        how urgent it is in the words, which is where it belongs."""
        import janken.announce as mod

        for gone in ("LEVELS", "DEFAULT_LEVEL"):
            self.assertFalse(hasattr(mod, gone), gone)
        posted = self.store.post("hello")
        self.assertFalse(hasattr(posted, "level"))
        self.assertNotIn("level", mod.SCHEMA)


class ExpiryTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = AnnounceStore(os.path.join(self.dir.name, "t.db"))
        self.addCleanup(self.store.close)

    def test_it_comes_down_by_itself(self):
        self.store.post("Back in ten minutes", for_seconds=600, now=moment())
        self.assertEqual(len(self.store.live(now=moment(12, 9))), 1)
        self.assertEqual(self.store.live(now=moment(12, 10)), [])

    def test_no_expiry_means_until_cleared(self):
        self.store.post("Submissions close Friday", now=moment())
        far = moment() + datetime.timedelta(days=365)
        self.assertEqual(len(self.store.live(now=far)), 1)

    def test_expiry_needs_to_be_in_the_future(self):
        with self.assertRaises(ValueError):
            self.store.post("hello", for_seconds=0)
        with self.assertRaises(ValueError):
            self.store.post("hello", for_seconds=-60)

    def test_reading_is_the_clock(self):
        """Nothing has to run for a message to expire, so an unswept row is not a
        stale banner."""
        a = self.store.post("Back soon", for_seconds=60, now=moment())
        self.assertFalse(a.expired(moment(12, 0)))
        self.assertTrue(a.expired(moment(12, 1)))
        # Still on the record, just not live.
        self.assertEqual(len(self.store.history()), 1)

    def test_an_unparseable_stamp_keeps_it_up(self):
        """A banner outliving its window beats one vanishing on a bad stamp."""
        a = self.store.post("hello", for_seconds=60, now=moment())
        self.store.conn.execute(
            "UPDATE announcements SET expires_at = 'not-a-date' WHERE id = ?", (a.id,))
        self.store.conn.commit()
        self.assertEqual(len(self.store.live(now=moment(23, 59))), 1)


class ClearTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = AnnounceStore(os.path.join(self.dir.name, "t.db"))
        self.addCleanup(self.store.close)

    def test_clearing_everything(self):
        self.store.post("one")
        self.store.post("two")
        self.assertEqual(self.store.clear(), 2)
        self.assertEqual(self.store.live(), [])

    def test_clearing_one_leaves_the_rest(self):
        first = self.store.post("one")
        second = self.store.post("two")
        self.assertEqual(self.store.clear(first.id), 1)
        self.assertEqual([a.id for a in self.store.live()], [second.id])

    def test_clearing_is_soft_so_history_survives(self):
        a = self.store.post("Tournaments are paused")
        self.store.clear(a.id)
        past = self.store.history()
        self.assertEqual(len(past), 1)
        self.assertEqual(past[0].message, "Tournaments are paused")
        self.assertTrue(past[0].cleared_at)
        self.assertFalse(past[0].live())

    def test_clearing_an_unknown_or_already_cleared_id_reports_nothing_done(self):
        a = self.store.post("one")
        self.assertEqual(self.store.clear(a.id), 1)
        self.assertEqual(self.store.clear(a.id), 0)      # already down
        self.assertEqual(self.store.clear(9999), 0)      # never existed

    def test_clearing_stamps_an_already_expired_one(self):
        """So reading history back does not have to tell "cleared" from "ran out"."""
        self.store.post("Back soon", for_seconds=60, now=moment())
        self.assertEqual(self.store.live(now=moment(13)), [])
        self.assertEqual(self.store.clear(now=moment(13)), 1)
        self.assertTrue(self.store.history()[0].cleared_at)


class TabTitleTest(unittest.TestCase):
    """Announcements stay out of the browser tab; only a cup takes it.

    Asserted rather than assumed, because the tab is the loudest channel on the
    page and an earlier version did hand it to announcements — which meant a
    background window shouting about something the page said perfectly well.
    """

    def setUp(self):
        import app

        self.app = app

    def test_the_tab_belongs_to_the_cup(self):
        self.assertEqual(self.app._page_title(None), "Janken Registry Dashboard")
        self.assertNotIn("notice", self.app._page_title.__code__.co_varnames)
        self.assertFalse(hasattr(self.app, "urgent_notice"))


class StoreSharesTheRegistryFileTest(unittest.TestCase):
    def test_it_coexists_with_the_registry(self):
        """One sqlite file is the project's convention, so opening either must not
        disturb the other's tables."""
        from janken.registry import Registry

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "janken.db")
            with Registry(path) as reg:
                reg.conn.execute("SELECT COUNT(*) FROM bots").fetchone()
            with AnnounceStore(path) as store:
                store.post("hello")
            with Registry(path) as reg:          # still a registry
                reg.conn.execute("SELECT COUNT(*) FROM bots").fetchone()
            with AnnounceStore(path) as store:   # still an announcement
                self.assertEqual(len(store.live()), 1)


if __name__ == "__main__":
    unittest.main()
