"""Persistent bot registry: identity, ratings, titles, career stats, ban list.

A bot's identity is the SHA-256 of its file content (the full digest — the
first `SHORT_ID` chars are only ever shown for display, never stored), so
editing the code creates a *new* bot with a fresh rating and no title, while
renaming the file keeps identity and just updates the display name.

The full digest matters: the id keys bans and ownership, and truncating it to
48 bits (12 hex chars, as it was) let a determined entrant grind a *different*,
deliberately-unsafe file to the same id and, on upload, flip a rival's clean
bot to banned — the collision landed on the victim's row. A 256-bit id makes
that second-preimage infeasible, so a different file is always a different bot.

Skill is a Glicko-2 rating (see `glicko.py`), earned in tournaments against the
one shared pool of bots: `rating` with its deviation `rd`, scored on who won each
match, plus `alt_rating` scored on the share of rounds. A league is a *title* on
top of that rating — a label, kept for good once earned — not a bracket that
decides who a bot may play. `rating.py` keeps the title ladder and the norm maths.

Everything lives in a single SQLite file, queryable with the sqlite3 CLI.
"""

import dataclasses
import datetime
import hashlib
import os
import sqlite3
import types
from typing import Dict, List, Optional, Tuple

from . import glicko
from . import norms
from . import rating as elo
from . import reference
from .safety import scan_file

# Kept under the old names: a league *is* a title now, and callers (CLI,
# dashboard, report) still speak of leagues.
LEAGUES = elo.TITLES
LEAGUE_RANK = elo.TITLE_RANK

# How many bots one person may have *in the field* at once — registered, not
# held, not banned. Held and banned bots are uncapped: the point is to stop one
# entrant crowding the tournament (and the cup's top-N qualifying places), not
# to limit what anyone may upload.
#
# Going over does not refuse the upload. The bot registers held, with its file
# hash and timestamp on the record exactly as any other, and its owner picks
# which five play. Refusing would push people to delete a bot to make room,
# which throws away a rating and a career record for a queueing problem.
ACTIVE_PER_SUBMITTER = 5


class NotANormEvent(Exception):
    """This field does not satisfy the composition rules, so it cannot award norms.

    Raised rather than downgraded to an ordinary tournament: a caller that asked
    to hold a norm event and silently got a league run would have no way to know
    the titles it expected were never on offer.
    """


class FieldFull(Exception):
    """Releasing this bot would put its owner over `ACTIVE_PER_SUBMITTER`.

    Raised rather than silently ignored: a hold the caller asked to lift and
    that quietly stayed on would be worse than an error, and every caller here
    has a person in front of it to tell.
    """

# Every title is a peak-rating floor (elo.RATING_TITLES). A "gauntlet" of fixed
# reference bots used to award the first one; it was dropped because those same
# bots play in the rated tournament, so the trial only re-played known opponents
# for a badge nothing filtered on. Databases written before that still carry the
# `trial_wins` / `trial_beaten` columns and rows of `kind = 'trial'` in `matches`
# — dead but harmless, and left alone rather than dropped from live data.

SCHEMA = """
CREATE TABLE IF NOT EXISTS bots (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    submitter TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    league TEXT NOT NULL DEFAULT '%(novice)s',
    -- Glicko-2 scored on match results (see `glicko.py`). `rd` is how unsure we
    -- are of `rating`, `sigma` how erratic the bot has been. The standings rank on
    -- `rating - rd`, never on `rating` alone.
    rating REAL NOT NULL DEFAULT %(start)g,
    rd REAL NOT NULL DEFAULT %(start_rd)g,
    sigma REAL NOT NULL DEFAULT %(start_sigma)g,
    peak_rating REAL NOT NULL DEFAULT %(start)g,
    -- The same system scored on the share of rounds instead: a finer measurement
    -- of one bot's edge, on a scale that compresses as the field improves. Kept
    -- for the detail view and as a tiebreak; no title is read off it.
    alt_rating REAL NOT NULL DEFAULT %(start)g,
    alt_rd REAL NOT NULL DEFAULT %(alt_start_rd)g,
    alt_sigma REAL NOT NULL DEFAULT %(start_sigma)g,
    -- When this bot was last in a rated period, so the deviation can widen by
    -- elapsed time rather than by how often the operator schedules tournaments.
    last_rated_at TEXT NOT NULL DEFAULT '',
    rated_games INTEGER NOT NULL DEFAULT 0,
    banned INTEGER NOT NULL DEFAULT 0,
    ban_reason TEXT NOT NULL DEFAULT '',
    held INTEGER NOT NULL DEFAULT 0,
    -- Hidden from every listing, by its owner, for good. Distinct from `held`,
    -- which is a pause the owner can undo; a disabled bot does not appear on
    -- the owner's own screen, so the owner cannot be the one to bring it back.
    -- Only `janken enable` can, which means shell access on the box.
    disabled INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    draws INTEGER NOT NULL DEFAULT 0,
    forfeits INTEGER NOT NULL DEFAULT 0,
    round_wins INTEGER NOT NULL DEFAULT 0,
    round_losses INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    played_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    bot_a TEXT NOT NULL,
    bot_b TEXT NOT NULL,
    wins_a INTEGER NOT NULL,
    wins_b INTEGER NOT NULL,
    draws INTEGER NOT NULL,
    forfeit_a TEXT,
    forfeit_b TEXT
);
-- Avatars get their own table rather than a column on `bots`, because SQLite
-- stores a blob inline with the rest of the row: on `bots` a few dozen 20 KB
-- pictures would be paged in by every `SELECT * FROM bots`, which is what the
-- standings, the status table and the report each do on every view. Out here
-- they are read only by whoever actually wants to draw one.
CREATE TABLE IF NOT EXISTS bot_images (
    bot_id TEXT PRIMARY KEY,
    png BLOB NOT NULL,
    updated_at TEXT NOT NULL
);
-- One row per norm earned: the evidence, not just a count, so a title can be
-- audited long after the event that produced it. Cheap at a handful of rows a bot.
CREATE TABLE IF NOT EXISTS norms (
    bot_id TEXT NOT NULL,
    played_at TEXT NOT NULL,
    title TEXT NOT NULL,            -- the norm's level: 'im' or 'gm'
    performance REAL NOT NULL,      -- the rating this showing was worth
    field_avg REAL NOT NULL,        -- how strong the event was
    games INTEGER NOT NULL,
    PRIMARY KEY (bot_id, played_at, title)
);
CREATE TABLE IF NOT EXISTS rating_history (
    played_at TEXT NOT NULL,
    bot_id TEXT NOT NULL,
    rating REAL NOT NULL,
    rd REAL NOT NULL DEFAULT %(start_rd)g,
    rank INTEGER NOT NULL,
    peak_rating REAL NOT NULL,
    rated_games INTEGER NOT NULL,
    PRIMARY KEY (played_at, bot_id)
);
""" % {"start": glicko.START_RATING, "novice": elo.TITLES[0],
       "start_rd": glicko.START_RD, "start_sigma": glicko.START_SIGMA,
       "alt_start_rd": glicko.ALT_START_RD}

# Columns added after the first schema; ALTER them onto older databases on open.
_MIGRATIONS = {
    "submitter": "ALTER TABLE bots ADD COLUMN submitter TEXT NOT NULL DEFAULT ''",
    "path": "ALTER TABLE bots ADD COLUMN path TEXT NOT NULL DEFAULT ''",
    # Ratings arrive on an existing database with everyone at the starting
    # rating: past results were points-based, and back-fitting Elo to them would
    # invent a history that was never played. Titles already earned are kept.
    "rating": "ALTER TABLE bots ADD COLUMN rating REAL NOT NULL DEFAULT %g"
              % elo.START_RATING,
    "peak_rating": "ALTER TABLE bots ADD COLUMN peak_rating REAL NOT NULL DEFAULT %g"
                   % elo.START_RATING,
    "rated_games": "ALTER TABLE bots ADD COLUMN rated_games INTEGER NOT NULL DEFAULT 0",
    # Existing bots are all in play, so holding defaults off: a migration must
    # never quietly withdraw a bot that was competing yesterday.
    "held": "ALTER TABLE bots ADD COLUMN held INTEGER NOT NULL DEFAULT 0",
    # Same reasoning as `held`, more so: nothing that was visible yesterday may
    # vanish because a database was opened by a newer version.
    "disabled": "ALTER TABLE bots ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0",
    # Glicko-2 replaced Elo as the rating. The columns arrive with a bot's starting
    # deviation rather than a settled one, and `_backfill_glicko` then replays the
    # match log to fill them in properly — an existing `rating` column holds an Elo
    # number on a different scale, so carrying it across as if it were a Glicko
    # rating would be a silent unit error, not a migration.
    "rd": "ALTER TABLE bots ADD COLUMN rd REAL NOT NULL DEFAULT %g"
          % glicko.START_RD,
    "sigma": "ALTER TABLE bots ADD COLUMN sigma REAL NOT NULL DEFAULT %g"
             % glicko.START_SIGMA,
    "alt_rating": "ALTER TABLE bots ADD COLUMN alt_rating REAL NOT NULL DEFAULT %g"
                  % glicko.START_RATING,
    "alt_rd": "ALTER TABLE bots ADD COLUMN alt_rd REAL NOT NULL DEFAULT %g"
              % glicko.ALT_START_RD,
    "alt_sigma": "ALTER TABLE bots ADD COLUMN alt_sigma REAL NOT NULL DEFAULT %g"
                 % glicko.START_SIGMA,
    "last_rated_at": "ALTER TABLE bots ADD COLUMN last_rated_at TEXT NOT NULL "
                     "DEFAULT ''",
}

# `rating_history` gained the same deviation, so a chart can show the band rather
# than a bare line. Keyed separately because it is a different table.
_HISTORY_MIGRATIONS = {
    "rd": "ALTER TABLE rating_history ADD COLUMN rd REAL NOT NULL DEFAULT %g"
          % glicko.START_RD,
}



@dataclasses.dataclass
class BotInfo:
    id: str
    name: str
    submitter: str
    path: str
    league: str              # the bot's title — a label, never a bracket
    rating: float            # Glicko-2 on match results, from tournaments only
    rd: float                # how unsure that rating is
    sigma: float             # how erratic the bot's form has been
    peak_rating: float       # the high-water mark titles are earned from
    alt_rating: float        # the same system scored on the share of rounds
    alt_rd: float
    alt_sigma: float
    last_rated_at: str       # when it last played a rated period; '' if never
    rated_games: int         # rated matches played
    banned: bool
    ban_reason: str
    held: bool               # withdrawn by its owner, not by the safety scan
    disabled: bool           # hidden by its owner; only an admin can undo it
    wins: int
    losses: int
    draws: int
    forfeits: int
    round_wins: int
    round_losses: int
    first_seen: str
    last_seen: str

    def as_glicko(self) -> "glicko.Rating":
        """The match-result rating, as the three numbers the system works in."""
        return glicko.Rating(self.rating, self.rd, self.sigma)

    def alt_as_glicko(self) -> "glicko.Rating":
        """The round-fraction rating, likewise."""
        return glicko.Rating(self.alt_rating, self.alt_rd, self.alt_sigma)

    def standing(self) -> float:
        """What the ladder sorts on: the rating we are confident it is at least."""
        return self.rating - glicko.RANK_K * self.rd


@dataclasses.dataclass
class RatingChange:
    before: float
    after: float
    games: int               # rated matches added by this tournament

    @property
    def delta(self) -> float:
        return self.after - self.before


@dataclasses.dataclass
class NormEarned:
    """A qualifying performance in a norm event."""
    title: str               # the level earned: 'im' or 'gm'
    performance: float       # the rating this showing was worth
    field_avg: float         # the event's average rating — the strength gate
    games: int


@dataclasses.dataclass
class TournamentRecord:
    """What one recorded tournament did to the field."""
    ratings: Dict[str, RatingChange]              # bot name -> rating move
    titles: List[Tuple[str, str, str]]            # (name, old title, new title)
    # bot name -> the norm it earned, for a norm event. Empty for a league run,
    # which cannot award one however well anybody played.
    norms: Dict[str, NormEarned] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class SyncResult:
    bots: Dict[str, BotInfo]                 # path -> current info
    duplicates: List[Tuple[str, str]]        # (dropped path, kept path)
    # Paths registered held because their submitter's field was already full.
    # Separate from `held` on the BotInfo, which cannot say *who* decided.
    auto_held: List[str] = dataclasses.field(default_factory=list)

    def eligible(self, league: str = "all", include_held: bool = False) -> Dict[str, BotInfo]:
        """Bots clear to play, optionally restricted to one league.

        Three things keep a bot out: a safety ban (involuntary), a hold (its
        owner's choice, temporary) and being disabled (its owner's choice,
        final). `include_held` overrides only the hold, for a local run against
        a throwaway database — never for a rated one, and never for a disabled
        bot, whose owner has asked to stop seeing it at all.
        """
        return {
            path: info
            for path, info in self.bots.items()
            if not info.banned and not info.disabled
            and (include_held or not info.held)
            and league in ("all", info.league)
        }


# How many leading hex chars name a bot to a human. The stored id is the full
# SHA-256; this is only ever for display. `find` accepts this prefix, so what
# `janken bots` prints, `janken remove` takes.
#
# Twelve is the whole budget for reading an id off a screen, so nothing should
# print more: a 64-char digest in a table cell or a bracket is not an
# identifier, it is a wall. The full digest belongs only where it is the subject
# rather than the label — the submission receipt and `janken release`, which
# both promise you can re-hash the file and compare, a claim a 12-char prefix
# cannot back.
SHORT_ID = 12


def bot_id_for(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def short_id(bot_id: str) -> str:
    """The leading chars of a bot id, for display. A prefix, never the identity:
    `find` returns every match and refuses rather than guess, so nothing keys
    off the short form. Two bots sharing a 48-bit prefix is a coincidence the
    registry survives — the id it stores and compares is the full digest."""
    return bot_id[:SHORT_ID]


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _days_between(earlier: str, later: str) -> float:
    """Elapsed days between two stored timestamps, never negative.

    A deviation that *shrank* because two clocks disagreed would be a rating system
    learning something from nothing, so a backwards interval reads as no time at
    all rather than as an error — the tournament that produced it is still perfectly
    good evidence.
    """
    def parse(stamp):
        if not stamp:
            return None
        try:
            moment = datetime.datetime.fromisoformat(stamp)
        except ValueError:
            return None
        return moment if moment.tzinfo else moment.replace(
            tzinfo=datetime.timezone.utc)

    a, b = parse(earlier), parse(later)
    if a is None or b is None:
        return 0.0
    return max(0.0, (b - a).total_seconds() / 86400.0)


def _period_pairs(matches, score):
    """One period's matches as {bot_id: [(opponent_id, score), ...]}.

    `matches` are dict-like rows or objects with the same field names, and `score`
    is one of `glicko`'s two scorers — which is the only difference between the two
    ladders, so both are read off the same rows.
    """
    by_bot: Dict[str, List[Tuple[str, float]]] = {}
    for m in matches:
        get = m.__getitem__ if hasattr(m, "keys") else lambda k: getattr(m, k)
        a, b = get("bot_a"), get("bot_b")
        wa, wb, dr = get("wins_a"), get("wins_b"), get("draws")
        fa, fb = get("forfeit_a"), get("forfeit_b")
        by_bot.setdefault(a, []).append((b, score(wa, wb, dr, fa, fb)))
        by_bot.setdefault(b, []).append((a, score(wb, wa, dr, fb, fa)))
    return by_bot


def _by_id(by_name, ids_by_name):
    """Re-key one period's results from bot names onto bot ids.

    A `MatchResult` carries display names because that is what the runner deals in;
    ratings are keyed by identity, which survives a rename. Two bots may share a
    display name, so this translation has to happen against the map the caller
    built, never by looking names up again.
    """
    return {
        ids_by_name[name]: [(ids_by_name[other], score) for other, score in results]
        for name, results in by_name.items()
    }


def _rate_period(ladder, standing, by_bot, days):
    """Re-rate everyone who played, all against the ratings they brought in.

    The whole tournament is one rating period. Matches play out in parallel and
    land in whatever order they finish, so rating them one after another would make
    the result depend on that order; scoring every match against the *entering*
    ratings makes recording the same tournament twice give the same answer, and
    stops a bot's early wins from inflating what its later wins are worth.
    """
    fresh = ladder.fresh()
    return {
        bot_id: ladder.update(
            standing.get(bot_id, fresh),
            [(standing.get(other, fresh), score) for other, score in results],
            days=days.get(bot_id, 0.0))
        for bot_id, results in by_bot.items()
    }


class Registry:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        """Bring a pre-existing database up to the current columns and ladder."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(bots)")}
        for col, ddl in _MIGRATIONS.items():
            if col not in cols:
                self.conn.execute(ddl)
        history = {r["name"]
                   for r in self.conn.execute("PRAGMA table_info(rating_history)")}
        for col, ddl in _HISTORY_MIGRATIONS.items():
            if col not in history:
                self.conn.execute(ddl)
        self._rename_norm_field_avg()
        # Any title from an older ladder (the points-era noob/rookie/pro) is not
        # translated — it is simply dropped back to the first rung, to be re-earned
        # under the current rules. Bots themselves are never lost; a title and a
        # rating are cheap to win back, and `janken reset` exists to wipe them
        # deliberately anyway.
        self.conn.execute(
            "UPDATE bots SET league = ? WHERE league NOT IN (%s)"
            % ",".join("?" for _ in LEAGUES),
            (LEAGUES[0],) + tuple(LEAGUES),
        )
        self._lengthen_ids()
        self._backfill_glicko(migrated="rd" not in cols)
        self._backfill_rating_history()

    def _backfill_glicko(self, migrated: bool):
        """Replay the match log to fill the Glicko columns on an Elo-era database.

        Only on the open that adds the columns. The existing `rating` holds an Elo
        number on a different scale with a different meaning, so copying it across
        would be a unit error wearing a migration's clothes; and defaulting every
        bot to 1500 would throw away a career that is still sitting in `matches`.
        Replaying is the only option that produces the ratings these results
        actually earned.

        Titles are left exactly as they are. A title is a record of something a bot
        did under the rules of the day, and re-deriving one from a ladder that did
        not exist when it was won would rewrite history rather than migrate it.
        """
        if not migrated:
            return
        periods = [r["played_at"] for r in self.conn.execute(
            """SELECT played_at FROM matches
               WHERE kind IN ('tournament', 'norm')
               GROUP BY played_at ORDER BY played_at""")]
        if not periods:
            return

        main: Dict[str, glicko.Rating] = {}
        alt: Dict[str, glicko.Rating] = {}
        peaks: Dict[str, float] = {}
        games: Dict[str, int] = {}
        last: Dict[str, str] = {}
        for played_at in periods:
            rows = self.conn.execute(
                """SELECT * FROM matches
                   WHERE played_at = ? AND kind IN ('tournament', 'norm')
                   ORDER BY id""", (played_at,)).fetchall()
            by_match = _period_pairs(rows, glicko.MATCH.score)
            by_round = _period_pairs(rows, glicko.ROUND.score)
            days = {b: _days_between(last.get(b, played_at), played_at)
                    for b in by_match}
            main.update(_rate_period(glicko.MATCH, main, by_match, days))
            alt.update(_rate_period(glicko.ROUND, alt, by_round, days))
            for bot_id, results in by_match.items():
                peaks[bot_id] = max(peaks.get(bot_id, glicko.START_RATING),
                                    main[bot_id].rating)
                games[bot_id] = games.get(bot_id, 0) + len(results)
                last[bot_id] = played_at

        blank = glicko.ROUND.fresh()
        self.conn.executemany(
            """UPDATE bots SET rating = ?, rd = ?, sigma = ?, peak_rating = ?,
                               alt_rating = ?, alt_rd = ?, alt_sigma = ?,
                               rated_games = ?, last_rated_at = ?
               WHERE id = ?""",
            [(m.rating, m.rd, m.sigma, peaks[bot_id],
              alt.get(bot_id, blank).rating, alt.get(bot_id, blank).rd,
              alt.get(bot_id, blank).sigma, games[bot_id], last[bot_id], bot_id)
             for bot_id, m in main.items()])

        # The stored history is Elo, on a different scale with a different
        # meaning, and nothing above touched it. Left alone, every bot's rating
        # chart would show its old Elo line and then step sideways at the next
        # tournament, and `scripts/calibration.py` — which reads this table to ask
        # whether the curve predicts results — would be answering about a ladder
        # that no longer exists. Dropped so `_backfill_rating_history`, which runs
        # next and only fills an empty table, rebuilds it from the same replay.
        self.conn.execute("DELETE FROM rating_history")

    def _rename_norm_field_avg(self):
        """`norms.opponent_avg` became `norms.field_avg`.

        The strength gate reads the whole event's average now, not the average of
        a bot's own opponents — see `rating.norm_earned` for why that inverted the
        rule in a single pool. `CREATE TABLE IF NOT EXISTS` leaves an existing
        table alone, so a database opened under the old name keeps it and the next
        norm event would fail on the insert. Renamed rather than rebuilt, so the
        rows (and their meaning, which only ever differed for a bot whose rating
        sat far from its field's) survive.
        """
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(norms)")}
        if "opponent_avg" in cols and "field_avg" not in cols:
            self.conn.execute(
                "ALTER TABLE norms RENAME COLUMN opponent_avg TO field_avg")

    def _lengthen_ids(self):
        """Re-key any legacy truncated bot id to the full SHA-256.

        Identity used to be the first 12 hex chars — 48 bits, few enough that a
        different, unsafe file could be ground to a rival's id and ban it on
        upload (see the module docstring). Ids are the full digest now; rows from
        the old scheme are upgraded in place, along with the `matches` and
        `bot_images` that reference them. Upgrading needs the stored file (only
        it can supply the full hash), and the hash must still start with the old
        id or it is a different file and is left alone. A row whose file is gone
        keeps its short id, which is harmless: a new upload gets a 64-char id and
        so can never collide with a 12-char one anyway.
        """
        legacy = self.conn.execute(
            "SELECT id, path FROM bots WHERE length(id) < 64").fetchall()
        for row in legacy:
            old, path = row["id"], row["path"]
            if not path or not os.path.isfile(path):
                continue
            try:
                full = bot_id_for(path)
            except OSError:
                continue
            if not full.startswith(old):
                continue                      # file changed since — not this bot
            if self.conn.execute("SELECT 1 FROM bots WHERE id = ?",
                                  (full,)).fetchone():
                continue                      # already present under the full id
            self.conn.execute("UPDATE bots SET id = ? WHERE id = ?", (full, old))
            self.conn.execute("UPDATE matches SET bot_a = ? WHERE bot_a = ?",
                              (full, old))
            self.conn.execute("UPDATE matches SET bot_b = ? WHERE bot_b = ?",
                              (full, old))
            self.conn.execute("UPDATE bot_images SET bot_id = ? WHERE bot_id = ?",
                              (full, old))
            self.conn.execute("UPDATE rating_history SET bot_id = ? WHERE bot_id = ?",
                              (full, old))

    def _backfill_rating_history(self):
        """Reconstruct rating snapshots for databases that predate history.

        Ratings are stored after each tournament period now. Older databases only
        have matches, so replay tournament timestamps as the same rating periods
        `record_tournament` uses: every match in one timestamp is scored against
        the ratings that period started with.
        """
        if self.conn.execute("SELECT 1 FROM rating_history LIMIT 1").fetchone():
            return
        periods = [
            r["played_at"] for r in self.conn.execute(
                """SELECT played_at FROM matches
                   WHERE kind IN ('tournament', 'norm')
                   GROUP BY played_at ORDER BY played_at"""
            )
        ]
        if not periods:
            return
        standing = {}
        peaks = {}
        games = {}
        last = {}
        active = {
            r["id"]: r for r in self.conn.execute(
                """SELECT id, name, first_seen FROM bots
                   WHERE banned = 0 AND held = 0"""
            )
        }
        names = {bot_id: row["name"] for bot_id, row in active.items()}
        blank = glicko.MATCH.fresh()
        seen = set()
        for played_at in periods:
            rows = self.conn.execute(
                """SELECT * FROM matches
                   WHERE played_at = ? AND kind IN ('tournament', 'norm')
                   ORDER BY id""", (played_at,)).fetchall()
            ids = {r["bot_a"] for r in rows} | {r["bot_b"] for r in rows}
            snapshot_ids = ids | {
                bot_id for bot_id, bot in active.items()
                if bot["first_seen"] <= played_at
            }
            for bot_id in snapshot_ids - seen:
                standing[bot_id] = blank
                peaks[bot_id] = glicko.START_RATING
                games[bot_id] = 0
            seen |= snapshot_ids

            by_bot = _period_pairs(rows, glicko.MATCH.score)
            days = {b: _days_between(last.get(b, played_at), played_at)
                    for b in by_bot}
            standing.update(_rate_period(glicko.MATCH, standing, by_bot, days))
            for bot_id, results in by_bot.items():
                peaks[bot_id] = max(peaks[bot_id], standing[bot_id].rating)
                games[bot_id] += len(results)
                last[bot_id] = played_at
            self._insert_rating_snapshot(
                played_at,
                sorted(
                    snapshot_ids,
                    key=lambda bot_id: (-standing[bot_id].conservative(),
                                        names.get(bot_id, bot_id)),
                ),
                {b: standing[b].rating for b in snapshot_ids}, peaks, games,
                {b: standing[b].rd for b in snapshot_ids})

    def _insert_rating_snapshot(self, played_at, ranked_ids, ratings, peaks, games,
                                rds=None):
        self.conn.executemany(
            """INSERT OR REPLACE INTO rating_history
                   (played_at, bot_id, rating, rd, rank, peak_rating, rated_games)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (played_at, bot_id, ratings[bot_id],
                 glicko.START_RD if rds is None else rds[bot_id],
                 rank, peaks[bot_id], games[bot_id])
                for rank, bot_id in enumerate(ranked_ids, 1)
            ],
        )

    def _store_current_rating_snapshot(self, played_at):
        """Store the same global active ranking the dashboard standings show.

        Ordered by `rating - RANK_K * rd`, which is what the standings sort on: a
        snapshot ranked on the raw rating would disagree with the page it is meant
        to be a record of.
        """
        rows = self.conn.execute(
            """SELECT id, rating, rd, peak_rating, rated_games FROM bots
               WHERE banned = 0 AND held = 0 AND disabled = 0
               ORDER BY rating - ? * rd DESC, wins DESC, name ASC""",
            (glicko.RANK_K,)
        ).fetchall()
        self._insert_rating_snapshot(
            played_at,
            [r["id"] for r in rows],
            {r["id"]: r["rating"] for r in rows},
            {r["id"]: r["peak_rating"] for r in rows},
            {r["id"]: r["rated_games"] for r in rows},
            {r["id"]: r["rd"] for r in rows},
        )

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- registration / screening ----------------------------------------

    def active_count(self, submitter: str) -> int:
        """How many bots this person has in the field: not held, not banned,
        not disabled.

        Matched case-insensitively, because the same person arrives as a signed
        in address from the dashboard and as whatever they typed from the CLI.
        An empty submitter is nobody in particular — the reference bots and
        anything synced without attribution share it — so it counts as zero
        rather than pooling unrelated bots into one phantom entrant.

        Disabling frees a slot, for the same reason holding does: the cap is on
        bots *in play*, and a hidden bot is not in play. Otherwise the only way
        out of a full field would be to ask the organizer to run `enable`.
        """
        if not submitter.strip():
            return 0
        return self.conn.execute(
            """SELECT COUNT(*) FROM bots
               WHERE lower(trim(submitter)) = lower(trim(?))
                 AND held = 0 AND banned = 0
                 AND disabled = 0""", (submitter,)).fetchone()[0]

    def sync_paths(self, paths: List[str], submitter: str = "",
                   name: Optional[str] = None, held: bool = False,
                   limit: Optional[int] = ACTIVE_PER_SUBMITTER) -> SyncResult:
        """Register/update the bots at these paths and refresh their bans.

        Safety screening runs on every sync, in both directions: a clean file
        lifts an old ban, a dirty file sets one (with the reasons recorded).
        `submitter`, when given, records who submitted the bot (kept on
        re-sync if a later sync omits it). `name` overrides the display name
        (which otherwise comes from the filename) — used when the stored file is
        renamed to include the bot id, so the human name is preserved.

        `held` applies to bots this call *registers for the first time*, for an
        upload that goes on the record without entering the field. It is
        deliberately not part of the update path: a hold is a standing decision
        by the bot's owner, and a routine re-sync of stored files — which the
        organizer may run at any time — has no business overturning it in either
        direction.

        `limit` caps how many bots the submitter may have in the field at once
        (`None` lifts it). Past the cap a *newly registered* bot is held instead
        of refused, and its path is listed in `SyncResult.auto_held` so the
        caller can say so. This too stays out of the update path, for the same
        reason `held` does: a re-sync must not withdraw a bot that was playing
        yesterday, even if its owner is over the cap today — that would let a
        routine organizer command silently pull somebody's entry.
        """
        seen: Dict[str, str] = {}  # bot id -> path already holding it
        bots: Dict[str, BotInfo] = {}
        duplicates: List[Tuple[str, str]] = []
        auto_held: List[str] = []
        now = _now()
        # Counted once and then tracked, so a single call registering three bots
        # fills the last slot and holds the rest rather than letting all three in.
        in_field = self.active_count(submitter) if limit is not None else 0

        with self.conn:
            for path in paths:
                bot_id = bot_id_for(path)
                if bot_id in seen:
                    duplicates.append((path, seen[bot_id]))
                    continue
                seen[bot_id] = path
                if name is not None:
                    display_name = name
                else:
                    stem = os.path.splitext(os.path.basename(path))[0]
                    # Strip our submissions/<name>-<id>.py suffix so a periodic
                    # re-sync of stored files doesn't fold the id into the name.
                    # Both forms: the full id, and a legacy 12-char suffix left
                    # on files stored before ids were lengthened.
                    for suffix in (bot_id, bot_id[:SHORT_ID]):
                        if stem.endswith("-" + suffix):
                            stem = stem[:-(len(suffix) + 1)]
                            break
                    display_name = stem.title()

                violations = scan_file(path)
                banned = bool(violations)
                reason = "; ".join(str(v) for v in violations)

                # A banned bot is already out of the field, so it neither takes
                # a slot nor needs holding — that would be two reasons for one
                # exclusion, and the ban is the honest one.
                hold_it = held
                if self.get(bot_id) is None and not banned:
                    if limit is not None and submitter.strip() and in_field >= limit:
                        hold_it = True
                        auto_held.append(path)
                    if not hold_it:
                        in_field += 1

                self.conn.execute(
                    """INSERT INTO bots (id, name, submitter, path, first_seen,
                                         last_seen, banned, ban_reason, held)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET
                           name = excluded.name,
                           path = excluded.path,
                           last_seen = excluded.last_seen,
                           banned = excluded.banned,
                           ban_reason = excluded.ban_reason,
                           submitter = CASE WHEN excluded.submitter != ''
                                            THEN excluded.submitter
                                            ELSE bots.submitter END""",
                    (bot_id, display_name, submitter, os.path.abspath(path),
                     now, now, int(banned), reason, int(hold_it)),
                )
                bots[path] = self.get(bot_id)

        return SyncResult(bots=bots, duplicates=duplicates, auto_held=auto_held)

    def get(self, bot_id: str) -> Optional[BotInfo]:
        row = self.conn.execute(
            "SELECT * FROM bots WHERE id = ?", (bot_id,)
        ).fetchone()
        return _info(row) if row else None

    def list_bots(self, include_disabled: bool = False) -> List[BotInfo]:
        """Every bot, strongest first — rating is the ladder, so it sorts the
        table; a title only labels how far a bot has come.

        Disabled bots are left out by default, which is the whole point of the
        flag: hiding has to be the behaviour a caller gets without asking, or
        the next listing added to the dashboard shows them again. The admin
        paths that must see them — `find`, so `janken enable` can reach one,
        and `janken bots --disabled` — ask for them explicitly.
        """
        rows = self.conn.execute(
            "SELECT * FROM bots" if include_disabled
            else "SELECT * FROM bots WHERE disabled = 0").fetchall()
        infos = [_info(r) for r in rows]
        # Bots in play first, then the ones held back, then the banned: neither
        # of the latter has a rating that means anything to rank by. Within each
        # group, `standing()` rather than the raw rating, so the CLI listing and
        # the dashboard standings agree about who is ahead.
        infos.sort(key=lambda b: (b.banned, b.held, -b.standing(), -b.wins, b.name))
        return infos

    def eligible_bots(self, league: str = "all",
                      include_held: bool = False) -> List[BotInfo]:
        """The field for a tournament run straight from the master table instead
        of a directory scan.

        Excluded are banned bots (the safety scan's call), held ones (their
        owner's) and disabled ones (their owner's, permanently — `list_bots`
        does not return those at all). `league` slices the field by title,
        which is only ever an organizer's choice for an exhibition run: titles
        are labels, and the default ("all") is one shared pool so that
        promoting a bot never costs the rest of the field an opponent."""
        return [
            b for b in self.list_bots()
            if not b.banned and (include_held or not b.held)
            and league in ("all", b.league)
        ]

    def set_held(self, bot_id: str, held: bool,
                 limit: Optional[int] = ACTIVE_PER_SUBMITTER) -> Optional[BotInfo]:
        """Withdraw a bot from the field, or put it back. Returns the bot as it
        now stands, or None if no bot has that id.

        Holding touches nothing a bot has earned — rating, title, career record
        and its stored file all stay exactly as they are, and `first_seen` keeps
        recording when the file was first uploaded. That date plus the id (which
        *is* the SHA-256 of the contents) is what makes a hold auditable: a bot
        released later is provably the same file that was on the record then, so
        it cannot have been written in answer to anything that came after.

        Releasing raises `FieldFull` when it would put the owner over `limit`
        bots in the field (`None` lifts the cap). Without this the cap would be
        decorative — upload six, then release the sixth. Holding is never
        blocked: taking a bot *out* of the field cannot break a limit on how
        many are in it.
        """
        bot = self.get(bot_id)
        if bot is None:
            return None
        if not held and bot.held and limit is not None and not bot.banned:
            # A banned bot is exempt: releasing it changes a flag but not the
            # field, since the ban keeps it out either way.
            playing = self.active_count(bot.submitter)
            if playing >= limit:
                raise FieldFull(
                    "%s has %d bots in the field and the limit is %d. Hold one "
                    "of those first, then release %s."
                    % (bot.submitter, playing, limit, bot.name))
        with self.conn:
            self.conn.execute("UPDATE bots SET held = ? WHERE id = ?",
                              (int(held), bot_id))
        return self.get(bot_id)

    def set_disabled(self, bot_id: str, disabled: bool,
                     limit: Optional[int] = ACTIVE_PER_SUBMITTER
                     ) -> Optional[BotInfo]:
        """Hide a bot from every listing, or bring it back.

        Disabling is what `remove` should have been. `remove` deletes the bot
        *and its matches*, which silently rewrites the head-to-head record of
        everyone it ever played — results those bots earned, disappearing
        because somebody else tidied up. Disabling touches nothing but this one
        flag: the matches stand, the opponents' records stand, and the bot stops
        appearing anywhere its owner has to look at it.

        The asymmetry is deliberate. A bot is disabled by whoever owns it and
        re-enabled only from the CLI, because a hidden bot is not on the owner's
        screen to un-hide. That makes disabling a decision worth confirming, and
        the dashboard does; here, at the CLI, the caller is already the admin.

        Re-enabling raises `FieldFull` when it would put the owner over `limit`
        bots in the field, exactly as releasing a held bot does — the two cannot
        differ, or disabling becomes the way around the cap.
        """
        bot = self.get(bot_id)
        if bot is None:
            return None
        if not disabled and bot.disabled and limit is not None and not bot.banned:
            playing = self.active_count(bot.submitter)
            if playing >= limit:
                raise FieldFull(
                    "%s has %d bots in the field and the limit is %d. Hold one "
                    "of those first, then enable %s."
                    % (bot.submitter, playing, limit, bot.name))
        with self.conn:
            self.conn.execute("UPDATE bots SET disabled = ? WHERE id = ?",
                              (int(disabled), bot_id))
        return self.get(bot_id)

    def list_disabled(self) -> List[BotInfo]:
        """The hidden bots, for the one operator who can see them."""
        return [b for b in self.list_bots(include_disabled=True) if b.disabled]

    def set_name(self, bot_id: str, name: str) -> Optional[BotInfo]:
        """Rename a bot without touching its identity, rating or record.

        A bot's id belongs to the file contents; the name is just the display
        label people see in the dashboard, reports and CLI tables.
        """
        clean = name.strip()
        if not clean:
            return None
        with self.conn:
            cur = self.conn.execute(
                "UPDATE bots SET name = ? WHERE id = ?",
                (clean, bot_id),
            )
            if cur.rowcount == 0:
                return None
        return self.get(bot_id)

    # -- avatars ------------------------------------------------------------
    # A picture, and nothing more. It is keyed by bot id like everything else,
    # which means it follows the same rule the rest of the record follows: edit
    # the code and you have a new bot, so you attach the picture again. Nothing
    # here is consulted by the safety scan, the tournament or the ratings.

    def set_image(self, bot_id: str, png: Optional[bytes]) -> bool:
        """Attach a normalised PNG to a bot, or drop it with `png=None`.

        Returns False if no bot has that id — an avatar for a bot that isn't
        registered is orphaned data, and the foreign key is not enforced by
        SQLite here, so it is checked.
        """
        if self.get(bot_id) is None:
            return False
        with self.conn:
            if png:
                self.conn.execute(
                    """INSERT INTO bot_images (bot_id, png, updated_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(bot_id) DO UPDATE SET
                           png = excluded.png,
                           updated_at = excluded.updated_at""",
                    (bot_id, sqlite3.Binary(png), _now()))
            else:
                self.conn.execute("DELETE FROM bot_images WHERE bot_id = ?",
                                  (bot_id,))
        return True

    def get_image(self, bot_id: str) -> Optional[bytes]:
        row = self.conn.execute(
            "SELECT png FROM bot_images WHERE bot_id = ?", (bot_id,)).fetchone()
        return bytes(row["png"]) if row else None

    def images(self) -> Dict[str, bytes]:
        """Every avatar, by bot id. For exporting them all to disk at once."""
        return {r["bot_id"]: bytes(r["png"])
                for r in self.conn.execute("SELECT bot_id, png FROM bot_images")}

    def image_ids(self) -> set:
        """Which bots have an avatar — without reading a single pixel."""
        return {r["bot_id"]
                for r in self.conn.execute("SELECT bot_id FROM bot_images")}

    def find(self, needle: str) -> Tuple[List[BotInfo], str]:
        """Resolve a bot id, id prefix or display name. Returns (matches, how).

        Ids are what `janken bots` prints, but names are what people remember,
        so both work; an ambiguous needle returns every match and lets the
        caller refuse rather than guessing which bot was meant.

        Disabled bots are findable. This is the only way back for one — the
        owner cannot see it to ask for it by name, so `janken enable` has to be
        able to reach it, and a lookup that silently skipped it would make the
        flag a one-way door in fact as well as in policy.
        """
        exact = self.get(needle)
        if exact is not None:
            return [exact], "id"
        every = self.list_bots(include_disabled=True)
        by_prefix = [b for b in every if b.id.startswith(needle.lower())]
        if by_prefix:
            return by_prefix, "id prefix"
        return [b for b in every if b.name.lower() == needle.lower()], "name"

    def reset_rankings(self) -> Tuple[int, int]:
        """Wipe every result, rating and title — keep the bots themselves.

        For starting a fresh season: identity, name, submitter, file path, any
        safety ban and any hold survive, so the field is intact and nobody has to
        resubmit — and a bot its owner is holding back for a later release does
        not get shoved into the new season by the reset.
        Everything a bot *earned* goes: rating back to the start, no rated games,
        career record zeroed, title back to the first rung, and the whole match
        log deleted — a match log that outlived the standings it produced would
        just mislead.

        Returns (bots kept, matches deleted).
        """
        with self.conn:
            matches = self.conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
            bots = self.conn.execute("SELECT COUNT(*) FROM bots").fetchone()[0]
            self.conn.execute("DELETE FROM matches")
            self.conn.execute("DELETE FROM rating_history")
            # Norms go with the season that produced them: a title has to be
            # re-earned, and a norm outliving the rating it was measured against
            # would let the next season start with unearned credit.
            self.conn.execute("DELETE FROM norms")
            self.conn.execute(
                """UPDATE bots SET
                       league = ?, rating = ?, rd = ?, sigma = ?, peak_rating = ?,
                       alt_rating = ?, alt_rd = ?, alt_sigma = ?,
                       last_rated_at = '', rated_games = 0,
                       wins = 0, losses = 0, draws = 0, forfeits = 0,
                       round_wins = 0, round_losses = 0""",
                (LEAGUES[0], glicko.START_RATING, glicko.START_RD,
                 glicko.START_SIGMA, glicko.START_RATING, glicko.START_RATING,
                 glicko.ALT_START_RD, glicko.START_SIGMA),
            )
        return bots, matches

    def remove(self, bot_id: str) -> Optional[BotInfo]:
        """Delete a bot and every match it played. Returns the removed BotInfo,
        or None if no bot has that id."""
        info = self.get(bot_id)
        if info is None:
            return None
        with self.conn:
            self.conn.execute(
                "DELETE FROM matches WHERE bot_a = ? OR bot_b = ?", (bot_id, bot_id))
            self.conn.execute("DELETE FROM bot_images WHERE bot_id = ?", (bot_id,))
            self.conn.execute("DELETE FROM rating_history WHERE bot_id = ?", (bot_id,))
            self.conn.execute("DELETE FROM norms WHERE bot_id = ?", (bot_id,))
            self.conn.execute("DELETE FROM bots WHERE id = ?", (bot_id,))
        return info

    # -- results ----------------------------------------------------------

    def record_tournament(self, result, ids_by_name: Dict[str, str],
                          kind: str = "tournament") -> TournamentRecord:
        """Store a TournamentResult, update career stats, and re-rate the field.

        `kind` is the label the matches are stored under, and with `"norm"` the run
        also awards norms (see `norms.py`). A norm event is rated exactly like any
        other tournament — in chess a norm tournament is a FIDE-rated event, not a
        separate category — so the only difference is the extra award pass and the
        composition check guarding it.

        The whole tournament is one *rating period*, rated by Glicko-2: every match
        is scored against the ratings the bots brought into the run. Matches play
        out in parallel and land in whatever order they finish, so rating them one
        after another would make the outcome depend on that order; a rating period
        makes recording the same tournament give the same ratings every time, and
        stops a bot's early wins from inflating what its later wins are worth
        inside a single run.

        Glicko-2 needs no K: how far a bot moves is set by its own deviation and
        its opponents', so a well-established bot is nudged by a result that would
        swing a newcomer, and a period with more matches in it narrows the
        deviation instead of multiplying the step. Both ladders are rated here —
        `rating` on who won, `alt_rating` on the share of rounds — from the same
        matches; see `glicko.py` for why the coarser one is the one that counts.
        """
        now = _now()
        with self.conn:
            start = {bot_id: self.get(bot_id) for bot_id in set(ids_by_name.values())}
            rated = {bot_id: 0 for bot_id in start}     # rated matches added
            # (opponent id, opponent's starting rating, score) per bot — the
            # rating for the performance figure, the id for whose bot it was.
            played: Dict[str, List[Tuple[str, float, float]]] = {
                bot_id: [] for bot_id in start}

            if kind == "norm":
                # Checked here as well as at selection: nothing may award a norm
                # from a field that wouldn't have qualified to hold the event.
                shape = norms.composition(list(start.values()))
                if not shape.ok:
                    raise NotANormEvent(shape.why())

            for m in result.matches:
                id_a, id_b = ids_by_name[m.bot_a], ids_by_name[m.bot_b]
                self._insert_match(now, kind, id_a, id_b, m)
                score_a = _score_a(m)
                for bot_id, other_id, my_wins, their_wins, my_forfeit, score in (
                    (id_a, id_b, m.wins_a, m.wins_b, m.forfeit_a, score_a),
                    (id_b, id_a, m.wins_b, m.wins_a, m.forfeit_b, 1.0 - score_a),
                ):
                    self.conn.execute(
                        """UPDATE bots SET
                               round_wins = round_wins + ?,
                               round_losses = round_losses + ?,
                               forfeits = forfeits + ?,
                               wins = wins + ?,
                               losses = losses + ?,
                               draws = draws + ?
                           WHERE id = ?""",
                        (my_wins, their_wins, int(bool(my_forfeit)),
                         int(score == 1.0), int(score == 0.0),
                         int(score == 0.5), bot_id),
                    )
                    rated[bot_id] += 1
                    played[bot_id].append(
                        (other_id, start[other_id].rating, score))

            earned_norms = (self._award_norms(now, played, start)
                            if kind == "norm" else {})

            # Both ladders, off the same matches. `days` is measured from each
            # bot's own last rated period, so a bot returning from a month away
            # has that month's uncertainty priced in before this run narrows it.
            by_match = _period_pairs(result.matches, glicko.MATCH.score)
            by_round = _period_pairs(result.matches, glicko.ROUND.score)
            days = {bot_id: _days_between(start[bot_id].last_rated_at, now)
                    for bot_id in start}
            entering = {b: start[b].as_glicko() for b in start}
            entering_alt = {b: start[b].alt_as_glicko() for b in start}
            after = _rate_period(glicko.MATCH, entering,
                                 _by_id(by_match, ids_by_name), days)
            after_alt = _rate_period(glicko.ROUND, entering_alt,
                                     _by_id(by_round, ids_by_name), days)

            ratings, titles = {}, []
            for name, bot_id in ids_by_name.items():
                was = start[bot_id]
                new = after.get(bot_id, was.as_glicko())
                new_alt = after_alt.get(bot_id, was.alt_as_glicko())
                now_peak = max(was.peak_rating, new.rating)
                now_games = was.rated_games + rated[bot_id]
                self.conn.execute(
                    """UPDATE bots SET rating = ?, rd = ?, sigma = ?,
                                       peak_rating = ?, alt_rating = ?,
                                       alt_rd = ?, alt_sigma = ?,
                                       rated_games = ?, last_rated_at = ?
                       WHERE id = ?""",
                    (new.rating, new.rd, new.sigma, now_peak, new_alt.rating,
                     new_alt.rd, new_alt.sigma, now_games, now, bot_id),
                )
                ratings[name] = RatingChange(
                    before=was.rating, after=new.rating, games=rated[bot_id])
                change = self._apply_titles(bot_id)
                if change:
                    titles.append((name, change[0], change[1]))
            self._store_current_rating_snapshot(now)
        by_name = {name: earned_norms[bot_id]
                   for name, bot_id in ids_by_name.items()
                   if bot_id in earned_norms}
        return TournamentRecord(ratings=ratings, titles=titles, norms=by_name)

    # -- internals ---------------------------------------------------------

    def _insert_match(self, now, kind, id_a, id_b, m):
        self.conn.execute(
            """INSERT INTO matches (played_at, kind, bot_a, bot_b,
                                    wins_a, wins_b, draws, forfeit_a, forfeit_b)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now, kind, id_a, id_b, m.wins_a, m.wins_b, m.draws,
             m.forfeit_a, m.forfeit_b),
        )

    def _award_norms(self, played_at, played, start) -> Dict[str, "NormEarned"]:
        """Score each bot's showing in a norm event and record what it earned.

        `played` maps bot id to (opponent id, opponent's starting rating, score)
        triples. Performance is measured across the bot's whole event field — no
        opponent is filtered out, because the event's composition is what
        guarantees the opposition was worth beating.

        What *is* checked per bot is how much of that opposition its own submitter
        supplied. A bot whose owner also owns too many of its opponents cannot be
        certified by the event, however well it scored: those are the free wins a
        norm exists to be immune to.
        """
        earned: Dict[str, NormEarned] = {}
        # One number for the whole event, not one per bot: see `elo.norm_earned`
        # on why a bot's own opponents' average is the wrong gate here.
        rated = [r for pairs in played.values() for _i, r, _s in pairs]
        field_avg = (sum(rated) / len(rated)) if rated else 0.0
        for bot_id, pairs in played.items():
            if len(pairs) < norms.MIN_NORM_GAMES:
                continue
            me = start[bot_id]
            # Belt and braces: `norms.select_field` keeps the organizer's own bots
            # out of an event in the first place, but this method will award from
            # whatever field it is handed, and a norm for a reference bot is the
            # exact thing being prevented. Cheap enough to check where it is
            # impossible to bypass.
            if reference.is_reference(me.name, me.submitter):
                continue
            mine = norms.submitter_key(start[bot_id])
            own = sum(1 for other_id, _r, _s in pairs
                      if norms.submitter_key(start[other_id]) == mine)
            if not norms.own_opposition_ok(own, len(pairs)):
                continue
            opponents = [rating for _i, rating, _score in pairs]
            score = sum(s for _i, _rating, s in pairs)
            performance = elo.performance_rating(score, opponents)
            title = elo.norm_earned(performance, field_avg)
            if title is None:
                continue
            self.conn.execute(
                """INSERT OR REPLACE INTO norms
                       (bot_id, played_at, title, performance, field_avg, games)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (bot_id, played_at, title, performance, field_avg, len(pairs)))
            earned[bot_id] = NormEarned(
                title=title, performance=performance,
                field_avg=field_avg, games=len(pairs))
        return earned

    def last_norm_event(self) -> Optional[str]:
        """When the most recent norm event ran, or None if there has never been one.

        Read from the match log rather than tracked in a counter: the timer is
        `Persistent=true`, so missed runs fire late and a counter would drift,
        while an elapsed-time question is self-healing across any downtime.
        """
        row = self.conn.execute(
            "SELECT MAX(played_at) AS last FROM matches WHERE kind = 'norm'"
        ).fetchone()
        return row["last"] if row and row["last"] else None

    def norm_event_due(self, interval_hours: Optional[float] = None,
                       now: Optional[datetime.datetime] = None) -> bool:
        """Whether enough time has passed to hold another norm event.

        `interval_hours` defaults to whatever the deployment is configured for
        (`norms.norm_interval_hours`), resolved per call rather than baked into
        this signature at import time — otherwise a unit file's environment would
        be read once, before the operator's value could matter.
        """
        if interval_hours is None:
            interval_hours = norms.norm_interval_hours()
        last = self.last_norm_event()
        if last is None:
            return True
        moment = now or datetime.datetime.now(datetime.timezone.utc)
        try:
            previous = datetime.datetime.fromisoformat(last)
        except ValueError:
            return True                      # unparseable stamp: treat as ancient
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=datetime.timezone.utc)
        elapsed = (moment - previous).total_seconds() / 3600.0
        return elapsed >= interval_hours

    def norm_counts(self, bot_id: str) -> Dict[str, int]:
        """Norms this bot holds, by level. Empty for a bot that has none."""
        return {row["title"]: row["n"] for row in self.conn.execute(
            """SELECT title, COUNT(*) AS n FROM norms
               WHERE bot_id = ? GROUP BY title""", (bot_id,))}

    def norms_for(self, bot_id: str) -> List[sqlite3.Row]:
        """Every norm this bot has earned, newest first — the audit trail."""
        return self.conn.execute(
            """SELECT * FROM norms WHERE bot_id = ?
               ORDER BY played_at DESC, title DESC""", (bot_id,)).fetchall()

    def _apply_titles(self, bot_id) -> Optional[Tuple[str, str]]:
        """Award any title the bot has now earned. Never takes one away.

        A title is a label, so it is the better of the one already held and the
        one it has now earned — a bad season costs rating, never a title. That is
        also what grandfathers the titles awarded before norms existed: they were
        earned under the rating-only rule, and `best_title` keeps them.
        """
        bot = self.get(bot_id)
        earned = elo.best_title(
            bot.league,
            elo.title_earned(bot.peak_rating, self.norm_counts(bot_id)))
        if LEAGUE_RANK[earned] > LEAGUE_RANK[bot.league]:
            self.conn.execute(
                "UPDATE bots SET league = ? WHERE id = ?", (earned, bot_id)
            )
            return (bot.league, earned)
        return None


def _score_a(m) -> float:
    """Side A's score for a match: 1 win, 0.5 draw, 0 loss.

    The same rule the `rating` ladder is scored on, which is why it delegates
    rather than restating it — the career W/L/D columns and the rating must agree
    about what a win was. Judged by fields rather than `MatchResult.winner`, which
    compares names and is ambiguous when both sides happen to share one.
    """
    return glicko.match_score(m.wins_a, m.wins_b, m.draws, m.forfeit_a, m.forfeit_b)


def _info(row: sqlite3.Row) -> BotInfo:
    return BotInfo(
        id=row["id"], name=row["name"], submitter=row["submitter"],
        path=row["path"], league=row["league"], rating=row["rating"],
        rd=row["rd"], sigma=row["sigma"],
        peak_rating=row["peak_rating"], alt_rating=row["alt_rating"],
        alt_rd=row["alt_rd"], alt_sigma=row["alt_sigma"],
        last_rated_at=row["last_rated_at"], rated_games=row["rated_games"],
        banned=bool(row["banned"]), held=bool(row["held"]),
        disabled=bool(row["disabled"]),
        ban_reason=row["ban_reason"], wins=row["wins"], losses=row["losses"],
        draws=row["draws"], forfeits=row["forfeits"],
        round_wins=row["round_wins"], round_losses=row["round_losses"],
        first_seen=row["first_seen"], last_seen=row["last_seen"],
    )
