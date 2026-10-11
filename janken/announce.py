"""Announcements: a line the organizer puts on every screen.

Everything else on the dashboard is derived from the registry — standings,
titles, head-to-head, all of it computed from matches that were played. This is
the one channel for something no amount of match data implies: the tournament is
paused for maintenance, the cup starts at five, submissions close on Friday.

An announcement carries no severity. There was a levels enum here for a day
— info, warning, urgent, each with its own tint — and it was overkill for a
channel that gets used a handful of times a season: three ways to say one thing,
and the loud one borrowed an auxiliary colour the brand reserves for data viz.
One look, and the operator chooses the words.

Two further notes, both about what this is *not*.

It is not a chat log. An announcement is written from a terminal by whoever runs
the championship, the same place the cup is driven from, because a broadcast
control that anyone signed in could reach is a different feature with different
risks. Nothing in the dashboard writes here.

And it is not a queue of one. Several announcements can be up at once — a
standing "submissions close Friday" alongside a passing "back in ten minutes" —
so posting never silently destroys what was already there. Clearing is a separate
verb, and it is soft: a cleared announcement stays on the record with the time it
came down, so "what did the banner say during the final?" has an answer.
"""

import dataclasses
import datetime
import re
import sqlite3
from typing import List, Optional

# What a message may carry. Long enough for two sentences and a time, short
# enough that it cannot push the standings off the first screen — a banner that
# fills the viewport is a page, and this is a line.
MAX_MESSAGE = 280

SCHEMA = """
CREATE TABLE IF NOT EXISTS announcements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    posted_by TEXT NOT NULL DEFAULT '',
    -- NULL means "until somebody takes it down". An expiry is for the messages
    -- that stop being true on their own ("back in ten minutes"), which are
    -- exactly the ones an operator forgets to clear.
    expires_at TEXT,
    -- Soft delete: a cleared announcement is history, not a gap.
    cleared_at TEXT
);
CREATE INDEX IF NOT EXISTS announcements_live
    ON announcements (cleared_at, expires_at);
"""

# `2h`, `30min`, `90s`, `1d`. Bare numbers are refused on purpose — see
# `parse_duration`.
_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(s|sec|secs|second|seconds"
                       r"|m|min|mins|minute|minutes"
                       r"|h|hr|hrs|hour|hours"
                       r"|d|day|days)\s*$", re.I)
_UNIT_SECONDS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}


class BadDuration(ValueError):
    """An unparseable `--for` value. Raised rather than defaulted: silently
    treating a typo as "forever" leaves a banner up after the maintenance."""


def parse_duration(text: str) -> float:
    """Seconds in a duration like `2h`, `30min`, `1d`.

    A bare number is refused rather than assumed. The unit really is ambiguous
    here — systemd, which this project's timer is configured in, reads a bare
    number as *seconds*, while anyone typing `--for 30` about a maintenance
    window means minutes. Guessing either way is wrong often enough to ask.
    """
    match = _DURATION.match(text or "")
    if not match:
        raise BadDuration(
            "cannot read %r as a duration — use a unit, e.g. 90s, 30min, 2h, 1d "
            "(a bare number is ambiguous: seconds to systemd, minutes to most "
            "people)" % (text,))
    return float(match.group(1)) * _UNIT_SECONDS[match.group(2).lower()]


def _parse(stamp: Optional[str]) -> Optional[datetime.datetime]:
    """A stored ISO stamp as an aware datetime, or None if it won't parse.

    Unparseable is treated as absent by every caller, which for `expires_at`
    means the announcement stays up. A banner that outlives its window is a
    smaller failure than one that vanishes because a stamp was malformed.
    """
    if not stamp:
        return None
    try:
        moment = datetime.datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment


@dataclasses.dataclass
class Announcement:
    id: int
    message: str
    posted_at: str
    posted_by: str = ""
    expires_at: Optional[str] = None
    cleared_at: Optional[str] = None

    def expired(self, now: Optional[datetime.datetime] = None) -> bool:
        until = _parse(self.expires_at)
        if until is None:
            return False
        return (now or datetime.datetime.now(datetime.timezone.utc)) >= until

    def live(self, now: Optional[datetime.datetime] = None) -> bool:
        """Whether this should be on screen: not taken down, not run out."""
        return not self.cleared_at and not self.expired(now)

    @property
    def attribution(self) -> str:
        """`posted_by` and when, for the small print under a banner.

        Trimmed to the minute: seconds and a `+00:00` offset are noise in a line
        whose job is to say roughly how old the message is.
        """
        who = (" by %s" % self.posted_by) if self.posted_by else ""
        return "Posted%s at %s UTC" % (who, self.posted_at[:16].replace("T", " "))


class AnnounceStore:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def post(self, message: str, posted_by: str = "",
             for_seconds: Optional[float] = None,
             now: Optional[datetime.datetime] = None) -> Announcement:
        """Put an announcement up. Returns it, with the id it was given.

        Existing announcements are left alone — see the module docstring on why
        posting is not an implicit replace.
        """
        text = " ".join((message or "").split())     # collapse pasted newlines
        if not text:
            raise ValueError("an announcement needs a message")
        if len(text) > MAX_MESSAGE:
            raise ValueError("message is %d characters; the limit is %d"
                             % (len(text), MAX_MESSAGE))
        moment = now or datetime.datetime.now(datetime.timezone.utc)
        expires_at = None
        if for_seconds is not None:
            if for_seconds <= 0:
                raise ValueError("an announcement cannot expire in the past")
            expires_at = (moment + datetime.timedelta(seconds=for_seconds)
                          ).isoformat(timespec="seconds")
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO announcements
                       (message, posted_at, posted_by, expires_at)
                   VALUES (?, ?, ?, ?)""",
                (text, moment.isoformat(timespec="seconds"),
                 posted_by.strip(), expires_at))
        return self.get(cur.lastrowid)

    def get(self, announcement_id: int) -> Optional[Announcement]:
        row = self.conn.execute("SELECT * FROM announcements WHERE id = ?",
                                (announcement_id,)).fetchone()
        return _info(row) if row else None

    def live(self, now: Optional[datetime.datetime] = None) -> List[Announcement]:
        """What belongs on screen, most recent first.

        Expiry is applied here rather than by a sweep, so nothing has to run for
        a message to come down on time — the read is the clock. Which also means
        a stale `expires_at` never needs cleaning up.
        """
        rows = self.conn.execute(
            """SELECT * FROM announcements
               WHERE cleared_at IS NULL
               ORDER BY id DESC""").fetchall()
        return [a for a in (_info(r) for r in rows) if a.live(now)]

    def history(self, limit: int = 20) -> List[Announcement]:
        """Everything ever posted, newest first — including cleared and expired."""
        rows = self.conn.execute(
            "SELECT * FROM announcements ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
        return [_info(r) for r in rows]

    def clear(self, announcement_id: Optional[int] = None,
              now: Optional[datetime.datetime] = None) -> int:
        """Take down one announcement, or every live one. Returns how many came down.

        Soft: the row keeps its message and gains the time it was cleared.
        Already-expired announcements are stamped too, so "cleared" and "ran out"
        do not have to be told apart when reading history back.
        """
        moment = (now or datetime.datetime.now(datetime.timezone.utc)
                  ).isoformat(timespec="seconds")
        with self.conn:
            if announcement_id is None:
                cur = self.conn.execute(
                    "UPDATE announcements SET cleared_at = ? WHERE cleared_at IS NULL",
                    (moment,))
            else:
                cur = self.conn.execute(
                    """UPDATE announcements SET cleared_at = ?
                       WHERE id = ? AND cleared_at IS NULL""",
                    (moment, announcement_id))
            return cur.rowcount


def _info(row: sqlite3.Row) -> Announcement:
    return Announcement(
        id=row["id"], message=row["message"], posted_at=row["posted_at"],
        posted_by=row["posted_by"], expires_at=row["expires_at"],
        cleared_at=row["cleared_at"])
