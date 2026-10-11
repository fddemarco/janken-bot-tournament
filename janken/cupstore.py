"""Storage and reveal state for cups.

Two things live here that `cup.py` deliberately knows nothing about: a database,
and an operator.

A cup is played all at once — the whole bracket, in under a second — and then
*sealed*. What the audience sees is released one round at a time by whoever is
running the show. That split is the point: all the compute happens with nobody
watching, so a hanging bot or a stupid-looking bracket is your problem before
it is the room's, and during the show you are a director rather than an
operator. The pause between rounds is where the commentary goes, and it lasts
exactly as long as you want it to.

Two timescales, two owners:

  * **Between rounds — the operator.** Nothing advances until `reveal_next`.
  * **Within a round — the clock.** Once a round opens, every viewer animates
    it from `round_started_at` and the beats in the broadcast script. A human
    cannot pace 40ms beats; that stays arithmetic, which is also what keeps a
    dozen laptops showing the same thing at the same moment.

The gate is enforced when *reading*, not when rendering, so a future page
cannot forget it: `visible()` reconstructs the cup with only the revealed
rounds filled in, and everything after that is a bracket with empty slots.
Nothing here writes to `bots` or `matches` — a cup cannot move the ladder.
"""

import dataclasses
import datetime
import re
import sqlite3
from typing import Dict, List, Optional, Tuple

from . import broadcast
from . import cup as cup_mod
from .match import MatchResult

# An entrant name from the era when the field was built from filenames:
# "Rocky_Ii-3f9a…", the stem with a stored submission's id welded on. 64 hex
# for a full SHA-256, 12 for the truncated ids used before that.
_NAME_WITH_ID = re.compile(r"^(.*)-([0-9a-fA-F]{64}|[0-9a-fA-F]{12})$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    bracket_size INTEGER NOT NULL,
    total_rounds INTEGER NOT NULL,
    revealed_rounds INTEGER NOT NULL DEFAULT 0,
    round_started_at TEXT,
    ended_at TEXT,
    champion TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cup_entrants (
    cup_id INTEGER NOT NULL,
    seed INTEGER NOT NULL,
    name TEXT NOT NULL,
    path TEXT NOT NULL DEFAULT '',
    rating REAL NOT NULL,
    title TEXT NOT NULL,
    -- The avatar the entrant played under, relative to the static directory.
    -- Copied onto the cup rather than looked up when the payload is built, so
    -- a bracket from last month keeps showing the pictures that were on screen
    -- at the time even if the bot has since changed or dropped its own.
    image TEXT NOT NULL DEFAULT '',
    -- The registry id of the bot that played, "" for a guest or for a cup drawn
    -- before entrants carried one. Stored rather than resolved through the name,
    -- which is what the bracket used to do: names are not unique and a rename
    -- would have silently repointed a finished cup at a different bot.
    bot_id TEXT NOT NULL DEFAULT '',
    -- Copied for the same reason the image is: it is only ever used to tell two
    -- same-named entrants apart on screen, and it should say what it said on
    -- the day, not who owns that name now.
    submitter TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (cup_id, seed)
);
CREATE TABLE IF NOT EXISTS cup_matches (
    cup_id INTEGER NOT NULL,
    idx INTEGER NOT NULL,
    round_size INTEGER NOT NULL,
    slot INTEGER NOT NULL,
    a_seed INTEGER,
    b_seed INTEGER,
    a_from INTEGER,
    b_from INTEGER,
    -- Which side of the feeding match arrives. 'winner' everywhere in a
    -- knockout except the third-place playoff, which takes the two the
    -- semi-finals knocked out.
    a_takes TEXT NOT NULL DEFAULT 'winner',
    b_takes TEXT NOT NULL DEFAULT 'winner',
    -- Stage name where round_size cannot supply one: the playoff and the final
    -- are both two-bot matches.
    label TEXT NOT NULL DEFAULT '',
    wins_a INTEGER,
    wins_b INTEGER,
    draws INTEGER,
    forfeit_a TEXT,
    forfeit_b TEXT,
    moves TEXT NOT NULL DEFAULT '',
    sudden_death INTEGER NOT NULL DEFAULT 0,
    winner_seed INTEGER,
    decided_by TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (cup_id, idx)
);
CREATE INDEX IF NOT EXISTS cup_matches_by_cup ON cup_matches (cup_id, idx);
"""

# Columns added after the first schema. `CREATE TABLE IF NOT EXISTS` leaves an
# existing table alone, so a database with cups already in it needs the column
# ALTERed on. Old cups get the empty default, which is exactly right: they were
# played before avatars existed, so they had none.
_MIGRATIONS = {
    "cup_entrants": {
        "image": "ALTER TABLE cup_entrants ADD COLUMN image TEXT NOT NULL DEFAULT ''",
        "bot_id": "ALTER TABLE cup_entrants ADD COLUMN bot_id TEXT NOT NULL DEFAULT ''",
        "submitter": "ALTER TABLE cup_entrants ADD COLUMN submitter TEXT NOT NULL "
                     "DEFAULT ''",
    },
    # Cups drawn before the third-place playoff existed had only winner edges
    # and no labels, which is exactly what these defaults describe — so an old
    # bracket keeps replaying as the bracket it was.
    "cup_matches": {
        "a_takes": "ALTER TABLE cup_matches ADD COLUMN a_takes TEXT NOT NULL "
                   "DEFAULT 'winner'",
        "b_takes": "ALTER TABLE cup_matches ADD COLUMN b_takes TEXT NOT NULL "
                   "DEFAULT 'winner'",
        "label": "ALTER TABLE cup_matches ADD COLUMN label TEXT NOT NULL DEFAULT ''",
    },
}

# A cup nobody has advanced for this long is treated as abandoned rather than
# live. Manual control brings back the failure the old derived-liveness design
# ruled out — start a cup, wander off, and the banner says LIVE forever — and
# this is the cheap guard against it.
STALE_AFTER_SECONDS = 30 * 60

EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _epoch_ms(stamp: Optional[str]) -> Optional[int]:
    if not stamp:
        return None
    moment = datetime.datetime.fromisoformat(stamp)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return int((moment - EPOCH).total_seconds() * 1000)


@dataclasses.dataclass
class CupState:
    cup_id: int
    name: str
    created_at: str
    bracket_size: int
    total_rounds: int
    revealed_rounds: int
    round_started_at: Optional[str]
    ended_at: Optional[str]
    champion: Optional[str]
    updated_at: str

    def status(self, now: Optional[str] = None) -> str:
        """ready → live → ended, with `stale` as the safety valve.

        `ready` is a sealed cup nobody has started showing; `stale` is one that
        was started and then abandoned, which must not keep claiming the
        audience's attention.
        """
        if self.ended_at:
            return "ended"
        if self.revealed_rounds == 0:
            return "ready"
        if self.age_seconds(now) > STALE_AFTER_SECONDS:
            return "stale"
        return "live"

    def age_seconds(self, now: Optional[str] = None) -> float:
        return (_epoch_ms(now or _now()) - _epoch_ms(self.updated_at)) / 1000.0

    @property
    def complete(self) -> bool:
        return self.revealed_rounds >= self.total_rounds


class CupStore:
    """Cups in their own tables, in whatever database you point it at."""

    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        """Bring a database written by an older version up to the current columns."""
        for table, columns in _MIGRATIONS.items():
            have = {r["name"] for r in self.conn.execute("PRAGMA table_info(%s)" % table)}
            for column, ddl in columns.items():
                if column not in have:
                    self.conn.execute(ddl)
        self._resplit_rounds()
        self._recover_entrant_ids()

    def _resplit_rounds(self):
        """Re-derive total_rounds for cups drawn before the playoff got its own.

        The third-place match used to be revealed together with the final, both
        being round_size 2. Now a round is split on its label too, so those cups
        have one more round than their stored count says — and replaying one
        would call it complete with the final never shown.

        Recomputed rather than incremented, so running it again is a no-op. A
        cup that was fully revealed stays fully revealed; one stopped part-way
        keeps its position, because the split only ever adds a round at the end.
        """
        rows = self.conn.execute(
            "SELECT id, total_rounds, revealed_rounds FROM cups").fetchall()
        for row in rows:
            groups = self.conn.execute(
                """SELECT COUNT(*) FROM (SELECT DISTINCT round_size, label
                                         FROM cup_matches WHERE cup_id = ?)""",
                (row["id"],)).fetchone()[0]
            if not groups or groups == row["total_rounds"]:
                continue
            revealed = row["revealed_rounds"]
            if revealed >= row["total_rounds"]:      # was complete, still is
                revealed = groups
            self.conn.execute(
                "UPDATE cups SET total_rounds = ?, revealed_rounds = ? WHERE id = ?",
                (groups, revealed, row["id"]))

    def _recover_entrant_ids(self):
        """Give older cups the entrant ids that were sitting in their names.

        A cup's field used to be built from filenames, and a stored submission
        is `<name>-<id>.py` — so every entrant in every finished bracket is
        recorded as "Rocky_Ii-3f9a…" with the whole digest attached. That is the
        illegible name on screen, and it is also, conveniently, a lossless
        record of which bot it was: the id can be read straight back out of the
        string, with no need for the original file to still exist.

        So the same pass that makes old brackets readable also makes them
        identifiable. Where the registry still knows the bot, its current
        display name is restored too; where it does not, the name keeps its
        stem and loses only the digest, because inventing capitalisation for a
        bracket somebody already watched would be rewriting it.

        Idempotent: it only ever looks at rows whose bot_id is still empty.
        """
        rows = self.conn.execute(
            "SELECT cup_id, seed, name FROM cup_entrants WHERE bot_id = ''"
        ).fetchall()
        if not rows:
            return
        known = self._registry_bots()
        for row in rows:
            match = _NAME_WITH_ID.match(row["name"] or "")
            if not match:
                continue
            stem, suffix = match.group(1), match.group(2).lower()
            # A 12-char suffix is a legacy filename from before ids were
            # lengthened; it is a prefix, so it only identifies a bot if
            # exactly one in the registry starts with it.
            candidates = [i for i in known if i.startswith(suffix)]
            bot_id = suffix if len(suffix) == 64 else (
                candidates[0] if len(candidates) == 1 else "")
            name, submitter = known.get(bot_id, ("", ""))
            self.conn.execute(
                "UPDATE cup_entrants SET bot_id = ?, name = ?, submitter = ? "
                "WHERE cup_id = ? AND seed = ?",
                (bot_id, name or stem, submitter, row["cup_id"], row["seed"]))

    def _registry_bots(self) -> Dict[str, Tuple[str, str]]:
        """{id: (name, submitter)} from the registry sharing this database, or
        {} if there is no registry here — a CupStore is only ever *pointed* at a
        database, and nothing promises the bots table is in it."""
        table = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'bots'"
        ).fetchone()
        if not table:
            return {}
        return {r["id"]: (r["name"], r["submitter"])
                for r in self.conn.execute("SELECT id, name, submitter FROM bots")}

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- writing -----------------------------------------------------------

    def save(self, cup: cup_mod.Cup, name: Optional[str] = None) -> int:
        """Store a played cup, sealed. Returns its id."""
        now = _now()
        rounds = cup.rounds()
        with self.conn:
            cursor = self.conn.execute(
                """INSERT INTO cups (name, created_at, bracket_size, total_rounds,
                                     revealed_rounds, updated_at)
                   VALUES (?, ?, ?, ?, 0, ?)""",
                (name or cup.name or "Cup", now, cup.bracket_size, len(rounds), now))
            cup_id = cursor.lastrowid

            self.conn.executemany(
                """INSERT INTO cup_entrants
                   (cup_id, seed, name, path, rating, title, image, bot_id,
                    submitter)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(cup_id, e.seed, e.name, e.path, e.rating, e.title, e.image,
                  e.id, e.submitter)
                 for e in cup.entrants])

            self.conn.executemany(
                """INSERT INTO cup_matches
                   (cup_id, idx, round_size, slot, a_seed, b_seed, a_from, b_from,
                    a_takes, b_takes, label,
                    wins_a, wins_b, draws, forfeit_a, forfeit_b, moves,
                    sudden_death, winner_seed, decided_by)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [self._match_row(cup_id, i, m) for i, m in enumerate(cup.matches)])
        return cup_id

    @staticmethod
    def _match_row(cup_id: int, index: int, m: cup_mod.CupMatch) -> tuple:
        # A played cup has `a` and `b` set on *every* match, because running it
        # propagates winners forward. Storing those seeds would hand the whole
        # bracket to anyone reading the table — the semi-finalists would be on
        # record before the quarter-finals were shown. So a side is only stored
        # when it was assigned by the draw (`a_from is None`); the rest are
        # rederived from revealed results, which is what makes the seal
        # structural rather than a rule the reader has to remember.
        a_seed = m.a.seed if (m.a and m.a_from is None) else None
        b_seed = m.b.seed if (m.b and m.b_from is None) else None
        r = m.result
        return (
            cup_id, index, m.round_size, m.slot, a_seed, b_seed, m.a_from, m.b_from,
            m.a_takes, m.b_takes, m.label,
            r.wins_a if r else None, r.wins_b if r else None, r.draws if r else None,
            r.forfeit_a if r else None, r.forfeit_b if r else None,
            r.moves if r else "", r.sudden_death if r else 0,
            m.winner.seed if m.winner else None, m.decided_by,
        )

    def reveal_next(self, cup_id: int) -> Optional[int]:
        """Open the next round to the audience. Returns the new count, or None
        if there is nothing left to reveal."""
        state = self.state(cup_id)
        if state is None or state.ended_at or state.complete:
            return None
        now = _now()
        opened = state.revealed_rounds + 1
        champion = None
        if opened >= state.total_rounds:
            champion = self._champion_name(cup_id)
        with self.conn:
            self.conn.execute(
                """UPDATE cups SET revealed_rounds = ?, round_started_at = ?,
                                   updated_at = ?, champion = COALESCE(?, champion)
                   WHERE id = ?""",
                (opened, now, now, champion, cup_id))
        return opened

    def touch(self, cup_id: int):
        """Keep a live cup from going stale while you talk over the recap."""
        with self.conn:
            self.conn.execute("UPDATE cups SET updated_at = ? WHERE id = ?",
                              (_now(), cup_id))

    def end(self, cup_id: int):
        now = _now()
        with self.conn:
            self.conn.execute(
                "UPDATE cups SET ended_at = ?, updated_at = ? WHERE id = ?",
                (now, now, cup_id))

    def delete(self, cup_id: int):
        with self.conn:
            for table in ("cup_matches", "cup_entrants"):
                self.conn.execute("DELETE FROM %s WHERE cup_id = ?" % table,
                                  (cup_id,))
            self.conn.execute("DELETE FROM cups WHERE id = ?", (cup_id,))

    # -- reading -----------------------------------------------------------

    def state(self, cup_id: int) -> Optional[CupState]:
        row = self.conn.execute("SELECT * FROM cups WHERE id = ?",
                                (cup_id,)).fetchone()
        return self._state(row) if row else None

    def latest(self) -> Optional[CupState]:
        row = self.conn.execute(
            "SELECT * FROM cups ORDER BY id DESC LIMIT 1").fetchone()
        return self._state(row) if row else None

    def current(self, now: Optional[str] = None) -> Optional[CupState]:
        """The cup that deserves the audience's attention, if any: the newest
        one that is ready or live. An ended or abandoned cup is not it."""
        for row in self.conn.execute("SELECT * FROM cups ORDER BY id DESC LIMIT 8"):
            state = self._state(row)
            if state.status(now) in ("ready", "live"):
                return state
        return None

    def all(self) -> List[CupState]:
        return [self._state(r) for r in
                self.conn.execute("SELECT * FROM cups ORDER BY id DESC")]

    @staticmethod
    def _state(row) -> CupState:
        return CupState(
            cup_id=row["id"], name=row["name"], created_at=row["created_at"],
            bracket_size=row["bracket_size"], total_rounds=row["total_rounds"],
            revealed_rounds=row["revealed_rounds"],
            round_started_at=row["round_started_at"], ended_at=row["ended_at"],
            champion=row["champion"], updated_at=row["updated_at"])

    def load(self, cup_id: int) -> cup_mod.Cup:
        """The whole cup, results and all. For the operator, never the page."""
        cup = self._skeleton(cup_id)
        by_seed = {e.seed: e for e in cup.entrants}
        targets = cup_mod.feed_map(cup)
        for index, (result, winner_seed, decided_by) in self._results(cup_id).items():
            match = cup.matches[index]
            match.result, match.decided_by = result, decided_by
            match.winner = by_seed.get(winner_seed)
            cup_mod.send_on(cup, index, targets)
        cup.champion = cup.matches[-1].winner if cup.matches else None
        return cup

    def visible(self, cup_id: int) -> cup_mod.Cup:
        """The cup as the audience is allowed to see it.

        Rounds past the reveal point contribute their bracket shape and nothing
        else — no scores, no winners, no champion. This is the gate, and it is
        here rather than in a view so that nothing downstream can skip it.
        """
        state = self.state(cup_id)
        cup = self._skeleton(cup_id)
        results = self._results(cup_id)
        by_seed = {e.seed: e for e in cup.entrants}
        targets = cup_mod.feed_map(cup)

        for ordinal, (_size, indices) in enumerate(cup.rounds()):
            if ordinal >= state.revealed_rounds:
                break
            for index in indices:
                stored = results.get(index)
                if stored is None:
                    continue
                result, winner_seed, decided_by = stored
                match = cup.matches[index]
                match.result, match.decided_by = result, decided_by
                match.winner = by_seed.get(winner_seed)
                cup_mod.send_on(cup, index, targets)

        if state.complete and cup.matches:
            cup.champion = cup.matches[-1].winner
        return cup

    def payload(self, cup_id: int, now: Optional[str] = None) -> dict:
        """Everything a viewer needs, gated — this is the polled document.

        Carries the server's own clock beside the round's start time so a page
        can correct for a wrong laptop clock: the offset between them is the
        only thing standing between a dozen viewers and a dozen different
        ideas of where the round is.
        """
        state = self.state(cup_id)
        stamp = now or _now()
        return {
            "cup_id": state.cup_id,
            "name": state.name,
            "status": state.status(stamp),
            "bracket_size": state.bracket_size,
            "revealed_rounds": state.revealed_rounds,
            "total_rounds": state.total_rounds,
            "round_started_at_ms": _epoch_ms(state.round_started_at),
            "server_now_ms": _epoch_ms(stamp),
            "champion": state.champion,
            "script": broadcast.script(self.visible(cup_id)),
        }

    # -- internals ---------------------------------------------------------

    def _skeleton(self, cup_id: int) -> cup_mod.Cup:
        """The draw: entrants and the bracket graph, with no results applied."""
        entrants = [
            cup_mod.Entrant(name=r["name"], path=r["path"], rating=r["rating"],
                            title=r["title"], seed=r["seed"], image=r["image"],
                            id=r["bot_id"], submitter=r["submitter"])
            for r in self.conn.execute(
                "SELECT * FROM cup_entrants WHERE cup_id = ? ORDER BY seed",
                (cup_id,))
        ]
        by_seed = {e.seed: e for e in entrants}
        row = self.conn.execute("SELECT * FROM cups WHERE id = ?",
                                (cup_id,)).fetchone()
        matches = [
            cup_mod.CupMatch(
                round_size=m["round_size"], slot=m["slot"],
                a=by_seed.get(m["a_seed"]), b=by_seed.get(m["b_seed"]),
                a_from=m["a_from"], b_from=m["b_from"],
                a_takes=m["a_takes"], b_takes=m["b_takes"], label=m["label"])
            for m in self.conn.execute(
                "SELECT * FROM cup_matches WHERE cup_id = ? ORDER BY idx", (cup_id,))
        ]
        return cup_mod.Cup(name=row["name"], entrants=entrants,
                           bracket_size=row["bracket_size"], matches=matches)

    def _results(self, cup_id: int) -> Dict[int, Tuple[MatchResult, Optional[int], str]]:
        out = {}
        for row in self.conn.execute(
                "SELECT * FROM cup_matches WHERE cup_id = ? ORDER BY idx", (cup_id,)):
            if row["wins_a"] is None:
                continue                      # planned but never played
            out[row["idx"]] = (
                MatchResult(
                    bot_a="", bot_b="", wins_a=row["wins_a"], wins_b=row["wins_b"],
                    draws=row["draws"], forfeit_a=row["forfeit_a"],
                    forfeit_b=row["forfeit_b"], moves=row["moves"],
                    sudden_death=row["sudden_death"]),
                row["winner_seed"], row["decided_by"])
        return out

    def _champion_name(self, cup_id: int) -> Optional[str]:
        row = self.conn.execute(
            """SELECT e.name FROM cup_matches m
               JOIN cup_entrants e ON e.cup_id = m.cup_id AND e.seed = m.winner_seed
               WHERE m.cup_id = ? ORDER BY m.idx DESC LIMIT 1""", (cup_id,)).fetchone()
        return row["name"] if row else None
