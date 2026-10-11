"""CLI for the janken bot tournament.

  python -m janken run bots/ [--league all] [--rounds 200] [--matches 1]
                             [--games N] [--workers W] [--timeout 1.0]
                             [--db janken.db] [--html report.html]
                             [--json results.json] [-v]
  python -m janken tournament [--league all] [--rounds 200] [--games 26]
                              [--workers W] [--db janken.db] [-v]
  python -m janken reset [--db janken.db] [--yes]
  python -m janken announce ["message"] [--for 2h] [--list] [--clear]
                            [--clear-id N] [--db janken.db]
  python -m janken bots [--db janken.db]
  python -m janken hold <bot-id|name> [--db janken.db]
  python -m janken release <bot-id|name> [--db janken.db]
  python -m janken remove <bot-id> [--db janken.db]
  python -m janken check bots/mybot.py
  python -m janken dashboard [--db janken.db] [--port 6888]
  python -m janken cup run [bots/] [--name "Spring Cup"] [--limit 64] [--db janken.db]
  python -m janken cup show [--cup ID] [--auto SECONDS] [--payload static/cup.json]
  python -m janken cup list [--db janken.db]
  python -m janken cup end [--cup ID] [--db janken.db]
"""

import argparse
import dataclasses
import json
import os
import sys

from . import announce
from . import glicko
from . import norms
from . import rating
from . import reference
from .announce import AnnounceStore
from .match import DEFAULT_ROUNDS, play_match
from .registry import ACTIVE_PER_SUBMITTER, LEAGUES, FieldFull, Registry, short_id
from .safety import ALLOWED_MODULES, scan_file
from .tournament import discover_bots, run_tournament

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
BASELINE = os.path.join(PACKAGE_DIR, "_baseline.py")

STATIC_DIR = os.path.join(os.path.dirname(PACKAGE_DIR), "static")

# Games per bot in the periodic run's schedule. It was once tied to where Elo's
# step budget began to bind; Glicko-2 has no budget, so the number is now simply
# how much evidence one period is worth — more games narrow the deviation faster
# without moving anyone further. $JANKEN_GAMES overrides, as install.sh sets it in
# the unit. Match length is `DEFAULT_ROUNDS`, imported from `match` — the cup
# scales its stages off the same number, and a longer match sharpens the ladder
# because match results are what `rating` is scored on.
DEFAULT_GAMES_PER_BOT = int(os.environ.get("JANKEN_GAMES") or 26)


def default_payload(db_path):
    """Where the cup page for this registry polls. Derived from the database
    so a rehearsal against one registry cannot overwrite the broadcast of
    another — must stay in step with cup_page.payload_name."""
    import hashlib

    digest = hashlib.sha1(os.path.realpath(db_path).encode()).hexdigest()[:8]
    return os.path.join(STATIC_DIR, "cup-%s.json" % digest)


def sync_and_report(registry, paths):
    """Sync paths into the registry, printing duplicates and bans."""
    sync = registry.sync_paths(paths)
    for dropped, kept in sync.duplicates:
        print("DUPLICATE %s is identical to %s — skipped"
              % (os.path.basename(dropped), os.path.basename(kept)))
    for info in sync.bots.values():
        if info.banned:
            print("BANNED %s [%s] — %s"
                  % (info.name, short_id(info.id), info.ban_reason))
    return sync


def cmd_run(args):
    with Registry(args.db) as registry:
        sync = sync_and_report(registry, discover_bots(args.bots_dir))
        eligible = sync.eligible(args.league, include_held=args.include_held)
        held = [b for b in sync.bots.values() if b.held]
        if held and not args.include_held:
            for b in held:
                print("HELD %s [%s] — withdrawn by its owner, not fielded "
                      "(pass --include-held to play it anyway)"
                      % (b.name, short_id(b.id)))
        if len(eligible) < 2:
            raise SystemExit(
                "fewer than 2 eligible bots (league=%s, %d banned, %d held)"
                % (args.league, sum(1 for b in sync.bots.values() if b.banned),
                   len(held))
            )

        # Keyed by raw filename stem to match the tournament engine's names
        # (registry display names may be title-cased and are for display only).
        leagues = {os.path.splitext(os.path.basename(p))[0]: info.league
                   for p, info in eligible.items()}
        w = args.workers or (os.cpu_count() or 2)
        if args.games:
            print("Running random schedule: %d bots, ~%d games each, %d round(s)/match, %d worker(s)"
                  % (len(eligible), args.games, args.rounds, w))
        else:
            print("Running round-robin: %d bots, %d round(s) x %d match(es)/pair, %d worker(s)"
                  % (len(eligible), args.rounds, args.matches, w))

        def on_match(m):
            if args.verbose:
                print("  %s" % describe_match(m))

        result = run_tournament(
            sorted(eligible),
            rounds=args.rounds,
            matches_per_pair=args.matches,
            timeout=args.timeout,
            on_match=on_match,
            workers=args.workers,
            games_per_bot=args.games,
        )

        ids_by_name = {os.path.splitext(os.path.basename(p))[0]: info.id
                       for p, info in eligible.items()}
        record = registry.record_tournament(result, ids_by_name)
        leagues.update({nm: new for nm, _old, new in record.titles})

        print()
        print(standings_table(result, leagues, ratings=record.ratings))
        for line in title_lines(record.titles):
            print(line)

        banned = [(b.name, b.ban_reason) for b in sync.bots.values() if b.banned]
        if args.json:
            with open(args.json, "w") as f:
                json.dump(result_to_dict(result, leagues, banned,
                                         record.ratings), f, indent=2)
            print("\nJSON results written to %s" % args.json)
        if args.html:
            from .report import render_html

            with open(args.html, "w") as f:
                f.write(render_html(result, leagues=leagues, banned=banned,
                                    ratings=record.ratings))
            print("HTML report written to %s" % args.html)


def cmd_tournament(args):
    """Round-robin fielded straight from the registry (the master `bots` table).

    Unlike `run` (which discovers and registers bots from a directory), this
    plays the bots already registered and eligible — ideal for a periodic
    service that refreshes standings as submissions arrive.
    """
    with Registry(args.db) as registry:
        roster = registry.eligible_bots(args.league)
    present = [b for b in roster if b.path and os.path.isfile(b.path)]
    for b in roster:
        if b not in present:
            print("SKIP %s [%s] — file missing: %s"
                  % (b.name, short_id(b.id), b.path or "(no path)"))
    if len(present) < 2:
        print("fewer than 2 eligible bots with files present — nothing to run.")
        return

    w = args.workers or (os.cpu_count() or 2)
    how = ("~%d games each" % args.games) if args.games else "round-robin"
    print("Tournament from registry: %d bots (league=%s), %s, %d round(s)/match, %d worker(s)"
          % (len(present), args.league, how, args.rounds, w))

    def on_match(m):
        if args.verbose:
            print("  %s" % describe_match(m))

    result = run_tournament(
        [b.path for b in present], rounds=args.rounds,
        matches_per_pair=args.matches, timeout=args.timeout, on_match=on_match,
        workers=args.workers, games_per_bot=args.games)

    def stem_of(b):
        return os.path.splitext(os.path.basename(b.path))[0]

    ids_by_name = {stem_of(b): b.id for b in present}
    leagues = {stem_of(b): b.league for b in present}
    names = {stem_of(b): b.name for b in present}
    with Registry(args.db) as registry:
        record = registry.record_tournament(result, ids_by_name)
    leagues.update({nm: new for nm, _old, new in record.titles})
    print()
    print(standings_table(result, leagues, names, record.ratings))
    for line in title_lines(record.titles, names):
        print(line)

    # A norm event runs *in addition* to the ladder, not instead of it, so no bot
    # loses a run and the event is an extra one it played — as in chess.
    if not args.no_norm_event:
        _maybe_norm_event(args)


def _norm_event_field(registry, league="all"):
    """(field, composition) for a norm event, restricted to bots whose files exist."""
    roster = registry.eligible_bots(league)
    present = [b for b in roster if b.path and os.path.isfile(b.path)]
    return norms.select_field(present)


def _play_norm_event(args, field):
    """Play and record a norm event over `field`. Returns the TournamentRecord."""
    result = run_tournament(
        [b.path for b in field], rounds=args.rounds, matches_per_pair=1,
        timeout=args.timeout, workers=args.workers)

    def stem_of(b):
        return os.path.splitext(os.path.basename(b.path))[0]

    ids_by_name = {stem_of(b): b.id for b in field}
    names = {stem_of(b): b.name for b in field}
    with Registry(args.db) as registry:
        record = registry.record_tournament(result, ids_by_name, kind="norm")
    print()
    print(standings_table(result, {stem_of(b): b.league for b in field}, names,
                          record.ratings))
    for line in title_lines(record.titles, names):
        print(line)
    if record.norms:
        for stem, norm in sorted(record.norms.items()):
            print("NORM %s — %s norm earned, performing at %d against a field "
                  "averaging %d over %d games."
                  % (names.get(stem, stem), rating.abbr(norm.title),
                     round(norm.performance), round(norm.field_avg),
                     norm.games))
    else:
        print("No norms earned: nobody performed to the required level.")
    return record


def _maybe_norm_event(args):
    """Hold a norm event if one is due. Quiet about it when it isn't."""
    interval = getattr(args, "norm_interval", None)
    with Registry(args.db) as registry:
        if not registry.norm_event_due(interval_hours=interval):
            return
        field, shape = _norm_event_field(registry, args.league)
    if field is None:
        print("\nNorm event due but the field cannot hold one: %s" % shape.why())
        return
    print("\nNorm event due — %d bots invited (%s)"
          % (len(field), shape.why()))
    _play_norm_event(args, field)


def _implied_score(performance, ratings):
    """One line: the score this performance floor demands of a bot in this field.

    Each entrant plays everyone else, so its opponents are the field minus itself.
    Measured from the weakest and strongest seats, because they differ — the top
    bot faces the easier draw, having itself removed from its own opposition.
    """
    if len(ratings) < 2:
        return "Too small a field to say what it would take."
    ranked = sorted(ratings, reverse=True)
    games = len(ranked) - 1
    asks = []
    for label, seat in (("the top seed", 0), ("the bottom seed", len(ranked) - 1)):
        opponents = ranked[:seat] + ranked[seat + 1:]
        score = rating.score_for_performance(performance, opponents)
        if score is None:
            asks.append("%s: not reachable at any score" % label)
        else:
            asks.append("%s: %.1f of %d games (%.0f%%)"
                        % (label, score, games, 100 * score / games))
    return "Needs — " + "; ".join(asks)


def cmd_norm_event(args):
    """Hold a norm event now, or (with --dry-run) show who would be invited."""
    with Registry(args.db) as registry:
        field, shape = _norm_event_field(registry, args.league)
        last = registry.last_norm_event()
        interval = (args.norm_interval if args.norm_interval is not None
                    else norms.norm_interval_hours())
        due = registry.norm_event_due(interval_hours=interval)
    print("Last norm event: %s (%s; one every %g hours)"
          % (last or "never", "another is due" if due else "not due yet",
             interval))
    if field is None:
        print("No qualifying field: %s" % shape.why())
        print("\nNorms need a field of %d+ bots, at most a quarter of it from any "
              "one submitter, distinct submitters numbering at least a third of "
              "it, and %.0f%% of it already titled."
              % (norms.MIN_NORM_FIELD, 100 * norms.MIN_TITLED_SHARE))
        return
    print("Qualifying field: %s\n" % shape.why())
    print(format_table(
        ["id", "bot", "by", "rating", "title"],
        [[short_id(b.id), b.name, b.submitter or "-", str(round(b.rating)),
          rating.abbr(b.league)] for b in field]))
    ratings = [b.rating for b in field]
    for title, (needed, perf, avg) in sorted(rating.NORM_REQUIREMENTS.items()):
        print("\n%s: %d norms, each performing at %d+ against a field averaging "
              "%d+ (this field averages %d)"
              % (rating.name(title), needed, round(perf), round(avg),
                 round(shape.average_rating)))
        if shape.average_rating < avg:
            print("  Out of reach here: the field is %d short of the %d it has to "
                  "average." % (round(avg - shape.average_rating), round(avg)))
            continue
        # What the floor actually asks of a bot in *this* field. A threshold is
        # only meaningful beside the score it implies — a floor that needs a clean
        # sweep is not a high bar, it is an unreachable one, and the two numbers
        # were picked independently so nothing guarantees they agree.
        print("  %s" % _implied_score(perf, ratings))
    if args.dry_run:
        print("\n--dry-run: nothing played.")
        return
    _play_norm_event(args, field)


def _whoami():
    """This login name, for attributing an announcement without being asked.

    Best-effort: `getuser` consults the environment before the password
    database, and both can be absent in a container. An unattributed
    announcement is fine; a crashed command is not.
    """
    try:
        import getpass
        return getpass.getuser()
    except Exception:
        return ""


def _announce_line(a, now=None):
    """One announcement as a table row's worth of text."""
    state = "live"
    if a.cleared_at:
        state = "cleared %s" % a.cleared_at.replace("T", " ")
    elif a.expired(now):
        state = "expired %s" % (a.expires_at or "").replace("T", " ")
    elif a.expires_at:
        state = "until %s" % a.expires_at.replace("T", " ")
    return [str(a.id), a.message, a.posted_by or "-",
            a.posted_at.replace("T", " "), state]


def cmd_announce(args):
    """Post, list or clear the banner every page carries."""
    with AnnounceStore(args.db) as store:
        if args.clear or args.clear_id is not None:
            taken = store.clear(args.clear_id)
            if args.clear_id is not None and not taken:
                print("No live announcement with id %d — `janken announce --list` "
                      "shows what is up." % args.clear_id)
                return
            print("Took down %d announcement(s)." % taken)
            return

        if args.list or not args.message:
            live = store.live()
            if live:
                print("Live now:")
                print(format_table(
                    ["id", "message", "by", "posted", "state"],
                    [_announce_line(a) for a in live]))
            else:
                print("Nothing is on the banner.")
            past = [a for a in store.history(limit=args.history)
                    if not a.live()]
            if past:
                print("\nRecently down:")
                print(format_table(
                    ["id", "message", "by", "posted", "state"],
                    [_announce_line(a) for a in past]))
            if not args.message and not args.list:
                # Reached by a bare `janken announce`, which is a reasonable way
                # to ask "what is up?" — but say how to post, since the name
                # reads like a verb.
                print("\nTo post one:  janken announce \"Tournaments are paused "
                      "for maintenance\" --for 2h")
            return

        for_seconds = None
        if args.duration:
            try:
                for_seconds = announce.parse_duration(args.duration)
            except announce.BadDuration as exc:
                raise SystemExit("janken announce: %s" % exc)
        try:
            posted = store.post(args.message, posted_by=args.by,
                                for_seconds=for_seconds)
        except ValueError as exc:
            raise SystemExit("janken announce: %s" % exc)

    print("Posted #%d: %s" % (posted.id, posted.message))
    if posted.expires_at:
        print("Comes down by itself at %s."
              % posted.expires_at.replace("T", " "))
    else:
        print("Stays up until `janken announce --clear`.")


def cmd_reset(args):
    """Start a fresh season: drop every result, rating and title, keep the bots."""
    with Registry(args.db) as registry:
        bots = registry.list_bots()
        matches = registry.conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        if not bots:
            print("registry is empty (%s) — nothing to reset" % args.db)
            return
        print("This will reset %d bot(s) in %s: ratings back to %d, titles back to "
              "%s, career records cleared, and %d recorded "
              "match(es) deleted."
              % (len(bots), args.db, rating.START_RATING,
                 rating.name(rating.TITLES[0]), matches))
        print("The bots themselves are kept — identity, name, submitter, file, "
              "any safety ban and any hold all survive, so nobody has to "
              "resubmit and no held bot is entered into the new season for them.")
        if not args.yes:
            if not sys.stdin.isatty():
                raise SystemExit(
                    "refusing to reset without a terminal to confirm at — "
                    "pass --yes if you mean it (e.g. from a script).")
            if input("Type 'reset' to confirm: ").strip() != "reset":
                raise SystemExit("aborted — nothing was changed.")
        kept, dropped = registry.reset_rankings()
    print("Reset done: %d bot(s) kept, %d match(es) deleted. The next tournament "
          "starts everyone from %d." % (kept, dropped, rating.START_RATING))


def cmd_bots(args):
    if getattr(args, "disabled", False):
        return _print_disabled(args)
    with Registry(args.db) as registry:
        bots = registry.list_bots()
    if not bots:
        print("registry is empty (%s) — run a tournament first" % args.db)
        return
    headers = ["id", "bot", "by", "rating", "title", "W", "L", "D",
               "uploaded", "held", "banned"]
    rows = [
        [short_id(b.id), b.name, b.submitter or "-",
         str(round(b.rating)),
         rating.abbr(b.league),
         str(b.wins), str(b.losses), str(b.draws),
         b.first_seen[:10],
         "yes" if b.held else "",
         ("yes: " + b.ban_reason) if b.banned else ""]
        for b in bots
    ]
    print(format_table(headers, rows))
    print("\nRating is Glicko-2 from tournament matches: everyone starts at %d, "
          "and how far a result moves a bot depends on how sure of it we already "
          "were." % glicko.START_RATING)
    print("Titles: %s" % rating.ladder())
    if any(b.held for b in bots):
        print("Held bots are on the record but out of the field — their owner "
              "withdrew them (`janken release <id>` puts one back). The id is "
              "the hash of the file, so the upload date above is provable.")
    _field_limit_note(bots)


def _print_disabled(args):
    """`janken bots --disabled` — the only listing that shows hidden bots."""
    with Registry(args.db) as registry:
        bots = registry.list_disabled()
    if not bots:
        print("No disabled bots in %s." % args.db)
        return
    headers = ["id", "bot", "by", "rating", "title", "W", "L", "D", "uploaded"]
    rows = [
        [short_id(b.id), b.name, b.submitter or "-", str(round(b.rating)),
         rating.abbr(b.league), str(b.wins), str(b.losses), str(b.draws),
         b.first_seen[:10]]
        for b in bots
    ]
    print(format_table(headers, rows))
    print("\n%d bot(s) hidden by their submitters. They are on nobody's screen "
          "— not their owner's — and no tournament fields them, but their "
          "matches still stand in everyone else's record. `janken enable <id>` "
          "puts one back; nothing else can." % len(bots))


def _field_limit_note(bots):
    """Say who is over the per-submitter field limit, if anyone is.

    Bots registered before the limit existed are not withdrawn to fit it — a
    migration that quietly pulled somebody's entry would be worse than being
    over — so an existing registry can sit above the cap until its owner picks
    which to hold. This is how the organizer sees that.
    """
    counts = {}
    for b in bots:
        who = b.submitter.strip()
        if who and not b.held and not b.banned:
            counts[who] = counts.get(who, 0) + 1
    over = sorted((n, w) for w, n in counts.items() if n > ACTIVE_PER_SUBMITTER)
    print("\nField limit: %d bots in play per submitter. A further upload "
          "registers held rather than being refused." % ACTIVE_PER_SUBMITTER)
    if over:
        print("Over the limit (registered before it applied, nothing was "
              "withdrawn): %s"
              % ", ".join("%s (%d)" % (who, n) for n, who in reversed(over)))


def _resolve(registry, needle):
    """One bot from an id, id prefix or name — or exit explaining the ambiguity."""
    matches, how = registry.find(needle)
    if not matches:
        raise SystemExit("no bot matching %r — see `janken bots`" % needle)
    if len(matches) > 1:
        # `find` resolves any id prefix, so the short form is enough to retype —
        # and a 64-char digest is not something anyone retypes. Only if two of
        # these actually collide on the prefix does the full id become the
        # useful thing to print, because then it is the only thing that picks
        # them apart.
        shown = [short_id(b.id) for b in matches]
        if len(set(shown)) < len(shown):
            shown = [b.id for b in matches]
        # One message, not a print plus an exit: those go to different streams
        # and arrive out of order, so the reason lands after the complaint.
        raise SystemExit("\n".join(
            ["%r matches %d bots by %s — pass one of these ids:"
             % (needle, len(matches), how)]
            + ["  %s  %s" % (i, b.name) for i, b in zip(shown, matches)]))
    return matches[0]


def cmd_hold(args):
    """Withdraw a bot from the field without giving up its place on the record."""
    with Registry(args.db) as registry:
        bot = _resolve(registry, args.bot)
        if bot.held:
            print("%s [%s] is already held." % (bot.name, short_id(bot.id)))
            return
        info = registry.set_held(bot.id, True)
    print("HELD %s [%s]." % (info.name, short_id(info.id)))
    print("It keeps its identity, rating %d, title %s and career record, and it "
          "stays on the record as first uploaded %s — but no tournament will "
          "field it until you release it."
          % (round(info.rating), rating.name(info.league), info.first_seen[:10]))


def cmd_release(args):
    """Put a held bot into the field, from the next tournament on."""
    with Registry(args.db) as registry:
        bot = _resolve(registry, args.bot)
        if not bot.held:
            print("%s [%s] is not held — it is already in the field."
                  % (bot.name, short_id(bot.id)))
            return
        try:
            info = registry.set_held(bot.id, False)
        except FieldFull as full:
            raise SystemExit(
                "%s\n\n  janken hold <bot>   frees a slot\n"
                "  janken bots         shows which of theirs are in the field"
                % full)
    print("RELEASED %s [%s]." % (info.name, short_id(info.id)))
    if info.banned:
        print("Note: it is still BANNED (%s), so it will not be fielded until "
              "that is fixed." % info.ban_reason)
        return
    # The one line in this command that prints all 64 characters: the claim is
    # that anyone can re-hash the file and compare, which is not checkable
    # against a 12-char prefix. So the digest appears where it is the subject.
    print("The next tournament fields it. Its id is the SHA-256 of the file, so "
          "anyone can check it against the %s upload and see nothing changed in "
          "between:\n  %s" % (info.first_seen[:10], info.id))


def cmd_disable(args):
    """Hide a bot from every listing, its owner's included."""
    with Registry(args.db) as registry:
        bot = _resolve(registry, args.bot)
        if bot.disabled:
            print("%s [%s] is already disabled." % (bot.name, short_id(bot.id)))
            return
        info = registry.set_disabled(bot.id, True)
    print("DISABLED %s [%s]." % (info.name, short_id(info.id)))
    print("It is off the standings, the pickers and any cup field, and %s "
          "cannot see it to undo this — `janken enable %s` is the only way "
          "back. Nothing was deleted: its matches still stand in the record of "
          "every bot it played."
          % (info.submitter or "its submitter", short_id(info.id)))


def cmd_enable(args):
    """Bring a disabled bot back. The half of `disable` that needs the box."""
    with Registry(args.db) as registry:
        bot = _resolve(registry, args.bot)
        if not bot.disabled:
            print("%s [%s] is not disabled." % (bot.name, short_id(bot.id)))
            return
        try:
            info = registry.set_disabled(bot.id, False)
        except FieldFull as full:
            raise SystemExit(
                "%s\n\n  janken hold <bot>   frees a slot\n"
                "  janken bots         shows which of theirs are in the field"
                % full)
    print("ENABLED %s [%s]." % (info.name, short_id(info.id)))
    if info.held:
        print("It is visible again but still held, so no tournament fields it "
              "until `janken release %s`." % short_id(info.id))
        return
    if info.banned:
        print("Note: it is still BANNED (%s), so it will not be fielded until "
              "that is fixed." % info.ban_reason)
        return
    print("It is back in the standings and the next tournament fields it.")


def cmd_remove(args):
    with Registry(args.db) as registry:
        info = registry.remove(args.id)
    if info is None:
        raise SystemExit("no bot with id %s in %s" % (args.id, args.db))
    print("Removed %s [%s] (and its matches)." % (info.name, short_id(info.id)))
    # Best-effort: drop the stored file, but only if it lives under submissions/
    # (never delete an organizer's source bot in bots/).
    subs = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(args.db)), "submissions"))
    p = os.path.abspath(info.path) if info.path else ""
    if p and p.startswith(subs + os.sep) and os.path.isfile(p):
        try:
            os.remove(p)
            print("Deleted submission file: %s" % os.path.basename(p))
        except OSError:
            pass


def cmd_check(args):
    if not os.path.isfile(args.bot):
        raise SystemExit("no such file: %s" % args.bot)
    name = os.path.splitext(os.path.basename(args.bot))[0]

    violations = scan_file(args.bot)
    if violations:
        print("FAIL: %s did not pass safety screening:" % name)
        for v in violations:
            print("  %s" % v)
        print("Bots may only import: %s" % ", ".join(sorted(ALLOWED_MODULES)))
        sys.exit(1)

    print("Checking %s against the built-in random bot (20 rounds)..." % name)
    # stderr=None inherits the terminal so the participant sees their traceback.
    result = play_match(args.bot, BASELINE, rounds=20, timeout=args.timeout, stderr=None)
    if result.forfeit_a:
        print("FAIL: your bot %s" % result.forfeit_a)
        print("Fix the problem above and re-run the check.")
        sys.exit(1)
    print("OK: bot is valid. Score vs random: %d-%d (%d draws)"
          % (result.wins_a, result.wins_b, result.draws))


# --- cup --------------------------------------------------------------------
# A cup is played all at once and then released one round at a time. The two
# halves are deliberately separate commands: `run` happens with nobody
# watching, `show` happens with everybody watching.

def _cup_field(args):
    """Entrants for a cup, seeded by league rating — and nothing written back.

    A cup reads the registry; it must never change it. The obvious way to build
    this field was `sync_paths`, which is what `run` and `tournament` use — but
    that *registers* every file it is shown, so pointing a cup at a directory of
    experiments quietly enrolled them all in the league. A cup that can add bots
    to the ladder is a cup that affects the ladder, which is the one thing this
    competition promises it cannot do.

    So the lookup is by content hash, read-only: a bot already registered plays
    on its real rating, and one that is not plays as an unrated guest at the
    starting rating without being enrolled. Safety screening still happens —
    it just happens here, rather than as a side effect of registration.
    """
    from . import avatar
    from . import cup as cup_mod
    from .rating import START_RATING
    from .registry import bot_id_for

    paths, screened = [], []
    for path in discover_bots(args.bots_dir):
        violations = scan_file(path)
        if violations:
            screened.append((os.path.basename(path), violations[0]))
            continue
        paths.append(path)
    for name, reason in screened:
        print("UNSAFE %s — %s (not fielded)" % (name, reason))

    with Registry(args.db) as registry:
        # Disabled bots included deliberately. A bot the registry does not
        # return is treated below as a guest and fielded at the starting
        # rating — so filtering them out here would put a hidden bot into the
        # cup as an unrated stranger, which is the opposite of hiding it. It is
        # skipped explicitly instead, like a ban or a hold.
        known = {bot.id: bot for bot in registry.list_bots(include_disabled=True)}
        # Avatars live in the database, and the broadcast is a sandboxed iframe
        # that can fetch a URL but cannot read SQLite. Exporting here — once,
        # for the whole field, before a match is played — is what puts them
        # where the page can reach them, and it means a cup run on a fresh
        # checkout rebuilds the picture cache without anybody resubmitting.
        drawn = avatar.export(registry.images(), STATIC_DIR)
        if drawn:
            print("%d avatar(s) exported to static/%s/" % (drawn, avatar.DIR_NAME))
        with_image = registry.image_ids()

    excluded = {n.strip().lower() for n in (args.exclude or [])}
    entrants, guests, baseline, by_name = [], 0, [], []
    for path in paths:
        name = os.path.splitext(os.path.basename(path))[0]
        # The registry row is read before the baseline check, not after, because
        # what makes a bot the organizer's is the *absence* of a submitter as well
        # as the name — otherwise an entrant who called their bot randy.py would be
        # dropped from the cup for a naming coincidence. A file nobody has
        # registered has no submitter to consult, which is the right answer for an
        # unattributed file sitting in bots/.
        info = known.get(bot_id_for(path))
        if not args.include_reference and reference.is_reference(
                name, info.submitter if info else ""):
            baseline.append(name)
            continue
        if name.lower() in excluded:
            by_name.append(name)
            continue
        if info is not None:
            if info.disabled:
                # No line printed: its owner asked for it to stop appearing,
                # and a cup roster announcing "hidden bot sat out" would put
                # the name back on the one screen the room is looking at.
                continue
            if info.banned:
                print("BANNED %s [%s] — %s"
                      % (info.name, short_id(info.id), info.ban_reason))
                continue
            if info.held and not args.include_held:
                print("HELD %s [%s] — withdrawn by its owner, not fielded "
                      "(pass --include-held to play it anyway)"
                      % (info.name, short_id(info.id)))
                continue
            if args.league not in ("all", info.league):
                continue
        else:
            guests += 1
        entrants.append(cup_mod.Entrant(
            # The registry's display name, not the filename stem. A stored
            # submission is `submissions/<name>-<64 hex>.py`, so the stem is the
            # name with the whole digest on the end — which is what the bracket
            # has been putting on screen. The registry already strips that when
            # it registers the file; the cup just never asked it for the result.
            # A guest the registry has never seen has nothing but its filename.
            name=info.name if info else name,
            path=path,
            rating=info.rating if info else START_RATING,
            title=info.league if info else LEAGUES[0],
            id=info.id if info else "",
            submitter=info.submitter if info else "",
            image=(avatar.rel_path(info.id)
                   if info and info.id in with_image else "")))

    if baseline:
        print("Baseline bots sitting out: %s (--include-reference enters them)"
              % ", ".join(sorted(baseline)))
    if by_name:
        print("Excluded: %s" % ", ".join(sorted(by_name)))
    if guests:
        print("%d bot(s) are not in the registry — seeded at %d as guests, and "
              "left unregistered." % (guests, START_RATING))
    if len(entrants) < 2:
        raise SystemExit(
            "fewer than 2 eligible bots — a cup needs at least 2%s"
            % (" (--include-reference would field %d more)" % len(baseline)
               if baseline else ""))

    field = cup_mod.qualify(entrants, args.limit)
    missed = len(entrants) - len(field)
    if missed:
        print("%d bot(s) missed the cut — the top %d by rating qualify"
              % (missed, args.limit))
    return field


def cmd_cup_run(args):
    from . import cup as cup_mod
    from .cupstore import CupStore

    plan = cup_mod.plan(_cup_field(args), name=args.name)
    print("Draw — %s: %d entrants, bracket of %d, %d matches"
          % (plan.name, len(plan.entrants), plan.bracket_size, len(plan.matches)))
    for stage_size, indices in plan.rounds():
        print("  %-14s %d matches, %d rounds each"
              % (cup_mod.stage_name(stage_size, plan.bracket_size),
                 len(indices), cup_mod.rounds_for(stage_size)))

    played = [0]

    def on_match(_m):
        played[0] += 1
        print("\r  playing... %d/%d" % (played[0], len(plan.matches)),
              end="", flush=True)

    cup_mod.run(plan, timeout=args.timeout, workers=args.workers,
                on_match=on_match)
    print()

    with CupStore(args.db) as store:
        cup_id = store.save(plan, name=args.name)

    # Deliberately no scores and no champion: the operator has to host this
    # thing, and knowing how it ends makes for a worse show. --spoil is there
    # for when something looks wrong and you need to look.
    forfeits = sum(1 for m in plan.matches
                   if m.result and (m.result.forfeit_a or m.result.forfeit_b))
    print("\nCup #%d played and sealed — %d matches, %d forfeit(s)."
          % (cup_id, len(plan.matches), forfeits))
    if args.spoil:
        print()
        print(_cup_bracket_text(plan))
    else:
        print("Results stay hidden until you show them (--spoil to peek).")
    print("\n  python3 -m janken cup show --cup %d" % cup_id)


def _cup_bracket_text(cup_obj):
    from . import cup as cup_mod

    labels = cup_mod.entrant_labels(cup_obj.entrants)
    lines = []
    for stage_size, indices in cup_obj.rounds():
        lines.append(cup_mod.stage_name(stage_size, cup_obj.bracket_size))
        for i in indices:
            m = cup_obj.matches[i]
            if m.result is None or m.winner is None:
                lines.append("    %-13s %s v %s — to play"
                             % (cup_obj.stage_of(m),
                                _short(cup_mod.label_of(m.a, labels)),
                                _short(cup_mod.label_of(m.b, labels))))
                continue
            lines.append(_cup_match_line(cup_obj, m, labels))
    return "\n".join(lines)


def _write_payload(store, cup_id, path):
    """Publish the viewer document. Written aside and renamed, so a browser
    polling at the wrong moment never reads half a reveal."""
    if path is None:
        path = default_payload(store.path)
    if not path:
        return
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(store.payload(cup_id), f)
    os.replace(tmp, path)


def cmd_cup_show(args):
    import time

    from . import broadcast
    from . import cup as cup_mod
    from .cupstore import CupStore

    with CupStore(args.db) as store:
        state = (store.state(args.cup) if args.cup
                 else store.current() or store.latest())
        if state is None:
            raise SystemExit("no cup found — run `janken cup run` first")
        if state.ended_at:
            raise SystemExit("cup #%d has already ended" % state.cup_id)

        cup_id = state.cup_id
        visible = store.visible(cup_id)
        rounds = visible.rounds()
        print("%s — cup #%d, %d entrants, %d rounds"
              % (state.name, cup_id, len(visible.entrants), state.total_rounds))
        print("The draw is public; results are not. Nothing moves until you say so.\n")
        _write_payload(store, cup_id, args.payload)

        while True:
            state = store.state(cup_id)
            if state.complete:
                break
            ordinal = state.revealed_rounds
            stage_size = rounds[ordinal][0]
            # Name every stage the round holds — "Final" alone hides the fact
            # that the third-place playoff is about to go out with it.
            here = []
            for i in rounds[ordinal][1]:
                label = visible.stage_of(visible.matches[i])
                if label not in here:
                    here.append(label)
            prompt = ("  [%d/%d] %s — %d matches. Enter to reveal: "
                      % (ordinal + 1, state.total_rounds, " + ".join(here),
                         len(rounds[ordinal][1])))
            if args.auto:
                print(prompt.rstrip() + " (auto in %gs)" % args.auto)
                time.sleep(args.auto)
            else:
                try:
                    input(prompt)
                except (EOFError, KeyboardInterrupt):
                    print("\nleaving cup #%d where it is — rerun to continue"
                          % cup_id)
                    return
                store.touch(cup_id)      # talking over the recap is not idling

            store.reveal_next(cup_id)
            _write_payload(store, cup_id, args.payload)

            shown = store.visible(cup_id)
            script = broadcast.script(shown)
            segment = script["rounds"][ordinal]
            print(_cup_round_text(shown, rounds[ordinal][1]))
            print("    (%.1fs of play, then the recap)\n"
                  % ((segment["duration_ms"] - segment["interstitial_ms"]) / 1000))

        champion = store.state(cup_id).champion
        print("  Champion: %s" % champion)
        if not args.auto:
            try:
                input("  Enter to end the cup: ")
            except (EOFError, KeyboardInterrupt):
                print()
                return
        store.end(cup_id)
        _write_payload(store, cup_id, args.payload)
        print("  cup #%d ended." % cup_id)


def _short(name, width=18):
    """Clip a name to the column. Entrants used to arrive as "Rocky_Ii-3f9a…",
    a filename stem with the file's whole id on it, and this was the only thing
    keeping the bracket inside its columns; entrants now carry the registry's
    name, so this is back to being ordinary truncation for a genuinely long
    one. Clipped from the right, keeping the front, where a name is."""
    name = name or "?"
    return name if len(name) <= width else name[:width - 1] + "…"


def _cup_match_line(cup_obj, m, labels):
    r = m.result
    from . import cup as cup_mod
    # Draws are shown because without them the score reads as wrong: a 300-round
    # final that ends 105-106 has 89 ties in it, and a reader who does not know
    # that quite reasonably wonders where the other 89 rounds went.
    return ("    %-13s %-18s %3d-%-3d %-18s %3d drawn  %-14s -> %s"
            % (cup_obj.stage_of(m), _short(cup_mod.label_of(m.a, labels)),
               r.wins_a, r.wins_b,
               _short(cup_mod.label_of(m.b, labels)), r.draws, m.decided_by,
               _short(cup_mod.label_of(m.winner, labels))))


def _cup_round_text(cup_obj, indices):
    from . import cup as cup_mod

    # Stage per match, not per round: the playoff and the final share a round,
    # and telling them apart is the operator's job during the show.
    labels = cup_mod.entrant_labels(cup_obj.entrants)
    return "\n".join(_cup_match_line(cup_obj, cup_obj.matches[i], labels)
                     for i in indices)


def cmd_cup_list(args):
    from .cupstore import CupStore

    with CupStore(args.db) as store:
        cups = store.all()
    if not cups:
        print("no cups yet")
        return
    print("%-5s %-22s %-8s %-9s %s" % ("ID", "NAME", "STATUS", "REVEALED", "CHAMPION"))
    for state in cups:
        # A champion is only news once the final has been shown.
        champion = state.champion if state.complete else "—"
        print("%-5d %-22s %-8s %d/%-7d %s"
              % (state.cup_id, state.name[:22], state.status(),
                 state.revealed_rounds, state.total_rounds, champion or "—"))


def cmd_cup_end(args):
    from .cupstore import CupStore

    with CupStore(args.db) as store:
        state = store.state(args.cup) if args.cup else store.current()
        if state is None:
            raise SystemExit("no live cup to end")
        store.end(state.cup_id)
        _write_payload(store, state.cup_id, args.payload)
    print("cup #%d ended." % state.cup_id)


def cmd_dashboard(args):
    """Launch the optional Streamlit dashboard via uv (kept out of the core engine)."""
    import shutil
    import subprocess

    project_root = os.path.dirname(PACKAGE_DIR)
    script = os.path.join(project_root, "app.py")
    uv = shutil.which("uv") or os.path.expanduser("~/.local/bin/uv")
    if not os.path.exists(uv):
        raise SystemExit(
            "uv not found — install uv and run `uv sync` in the project, then retry."
        )
    cmd = [uv, "run", "streamlit", "run", script,
           "--server.port", str(args.port), "--", "--db", args.db]
    raise SystemExit(subprocess.call(cmd, cwd=project_root))


def describe_match(m):
    base = "%s vs %s: " % (m.bot_a, m.bot_b)
    forfeits = [(m.bot_a, m.forfeit_a), (m.bot_b, m.forfeit_b)]
    notes = ["%s forfeited (%s)" % (n, r) for n, r in forfeits if r]
    if notes:
        return base + "; ".join(notes)
    outcome = "draw" if m.is_draw else "%s wins" % m.winner
    return base + "%d-%d (%d draws) -> %s" % (m.wins_a, m.wins_b, m.draws, outcome)


def format_table(headers, rows):
    widths = [
        max(len(h), *(len(r[c]) for r in rows)) if rows else len(h)
        for c, h in enumerate(headers)
    ]
    fmt = "  ".join("%%-%ds" % w for w in widths)
    lines = [fmt % tuple(headers), fmt % tuple("-" * w for w in widths)]
    lines += [fmt % tuple(r) for r in rows]
    return "\n".join(lines)


def standings_table(result, leagues=None, names=None, ratings=None):
    """Rank the field by rating — the ladder — with the title as a label.

    Falls back to the engine's own points order when no ratings are on hand
    (a run nobody recorded), so the table always has a defined order.
    """
    names = names or {}   # map a result name (filename stem) -> registry display name
    standings = result.standings
    if ratings:
        standings = sorted(
            standings,
            key=lambda s: (-ratings[s.name].after, s.name) if s.name in ratings
            else (0.0, s.name))
    headers = ["#", "bot", "title", "rating", "+/-", "W", "D", "L", "forf",
               "rounds +/-"]
    rows = []
    for i, s in enumerate(standings):
        move = (ratings or {}).get(s.name)
        rows.append([
            str(i + 1), (names.get(s.name) or s.name.title()),
            rating.abbr((leagues or {}).get(s.name, "?")),
            ("%d" % round(move.after)) if move else "-",
            ("%+d" % round(move.delta)) if move else "-",
            str(s.wins), str(s.draws), str(s.losses), str(s.forfeits),
            "%+d" % s.diff,
        ])
    return format_table(headers, rows)


def title_lines(titles, names=None):
    """One announcement per title earned this run (chess-style: kept for good)."""
    names = names or {}
    def article(title):
        # "an International Master", not "a International Master" — the two top
        # titles are the vowel-initial ones, and they read out loud on the stream.
        return "an" if title[:1].lower() in "aeiou" else "a"
    return ["TITLE: %s is now %s %s (was %s) — titles are labels and are never lost."
            % (names.get(nm, nm.title()), article(rating.name(new)),
               rating.name(new), rating.name(old))
            for nm, old, new in titles]


def result_to_dict(result, leagues=None, banned=None, ratings=None):
    return {
        "rounds": result.rounds,
        "matches_per_pair": result.matches_per_pair,
        "leagues": leagues or {},
        "ratings": {
            name: {"before": round(c.before, 1), "after": round(c.after, 1),
                   "delta": round(c.delta, 1), "rated_matches": c.games}
            for name, c in (ratings or {}).items()
        },
        "banned": [{"name": n, "reason": r} for n, r in (banned or [])],
        "standings": [dataclasses.asdict(s) for s in result.standings],
        "matches": [dataclasses.asdict(m) for m in result.matches],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(prog="janken", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run a round-robin tournament")
    p_run.add_argument("bots_dir", help="directory containing bot .py files")
    p_run.add_argument("--league", choices=LEAGUES + ("all",), default="all",
                       help="field only bots holding this title (default: all — "
                            "one shared pool, so no title costs a bot opponents)")

    p_run.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS, help="rounds per match")
    p_run.add_argument("--matches", type=int, default=1, help="matches per pair (round-robin)")
    p_run.add_argument("--games", type=int, default=None,
                       help="random schedule: ~this many games per bot instead of full round-robin")
    p_run.add_argument("--workers", type=int, default=None,
                       help="parallel workers (default: CPU count)")
    p_run.add_argument("--timeout", type=float, default=1.0, help="seconds per move")
    p_run.add_argument("--include-held", action="store_true",
                       help="field bots their owners have held back too — for "
                            "local testing against a throwaway --db, since it "
                            "records rated results for a bot not meant to play")
    p_run.add_argument("--db", default="janken.db", help="registry database file")
    p_run.add_argument("--html", help="write a self-contained HTML report here")
    p_run.add_argument("--json", help="write raw results as JSON here")
    p_run.add_argument("-v", "--verbose", action="store_true", help="print every match")
    p_run.set_defaults(func=cmd_run)

    p_tourn = sub.add_parser(
        "tournament", help="run a round-robin fielded from the registry (for the periodic service)")
    p_tourn.add_argument("--league", choices=LEAGUES + ("all",), default="all",
                         help="field only bots holding this title (default: all)")
    p_tourn.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS, help="rounds per match")
    p_tourn.add_argument("--matches", type=int, default=1, help="matches per pair (round-robin)")
    p_tourn.add_argument("--games", type=int, default=DEFAULT_GAMES_PER_BOT,
                         help="games per bot in a random schedule (default: %d); "
                              "0 plays a full round-robin instead"
                              % DEFAULT_GAMES_PER_BOT)
    p_tourn.add_argument("--workers", type=int, default=None,
                         help="parallel workers (default: CPU count)")
    p_tourn.add_argument("--timeout", type=float, default=1.0, help="seconds per move")
    p_tourn.add_argument("--db", default="janken.db", help="registry database file")
    p_tourn.add_argument("-v", "--verbose", action="store_true", help="print every match")
    p_tourn.add_argument("--no-norm-event", action="store_true",
                         help="skip the periodic norm event even if one is due")
    p_tourn.add_argument("--norm-interval", type=float, default=None, dest="norm_interval",
                         help="hours between norm events (default: $%s, else %g)"
                              % (norms.NORM_INTERVAL_ENV, norms.NORM_INTERVAL_HOURS))
    p_tourn.set_defaults(func=cmd_tournament)

    p_norm = sub.add_parser(
        "norm-event",
        help="hold a norm event: a selective tournament that awards IM/GM norms")
    p_norm.add_argument("--dry-run", action="store_true",
                        help="show who would be invited, play nothing")
    p_norm.add_argument("--league", choices=LEAGUES + ("all",), default="all",
                        help="draw candidates from this title only (default: all)")
    p_norm.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS, help="rounds per match")
    p_norm.add_argument("--workers", type=int, default=None,
                        help="parallel workers (default: CPU count)")
    p_norm.add_argument("--timeout", type=float, default=1.0, help="seconds per move")
    p_norm.add_argument("--norm-interval", type=float, default=None, dest="norm_interval",
                        help="hours between norm events (default: $%s, else %g)"
                             % (norms.NORM_INTERVAL_ENV, norms.NORM_INTERVAL_HOURS))
    p_norm.add_argument("--db", default="janken.db", help="registry database file")
    p_norm.set_defaults(func=cmd_norm_event)

    p_ann = sub.add_parser(
        "announce",
        help="put a message on the banner every page carries (or list/clear them)")
    p_ann.add_argument("message", nargs="?",
                       help="what to say; omit to list what is up")
    p_ann.add_argument("--for", dest="duration", metavar="DURATION", default=None,
                       help="take it down by itself after this long, e.g. 30min, 2h, 1d")
    p_ann.add_argument("--by", default=_whoami(),
                       help="who is posting (default: this login name)")
    p_ann.add_argument("--list", action="store_true",
                       help="show what is up, and what recently came down")
    p_ann.add_argument("--clear", action="store_true",
                       help="take down every live announcement")
    p_ann.add_argument("--clear-id", type=int, default=None, metavar="ID",
                       help="take down just this one")
    p_ann.add_argument("--history", type=int, default=10,
                       help="how many past announcements to list (default: 10)")
    p_ann.add_argument("--db", default="janken.db", help="registry database file")
    p_ann.set_defaults(func=cmd_announce)

    p_reset = sub.add_parser(
        "reset", help="start a fresh season: wipe results, ratings and titles, keep the bots")
    p_reset.add_argument("--db", default="janken.db", help="registry database file")
    p_reset.add_argument("-y", "--yes", action="store_true",
                         help="skip the confirmation prompt")
    p_reset.set_defaults(func=cmd_reset)

    p_bots = sub.add_parser("bots", help="list registered bots and their stats")
    p_bots.add_argument("--db", default="janken.db", help="registry database file")
    p_bots.add_argument("--disabled", action="store_true",
                        help="list the disabled bots instead — the ones hidden "
                             "from the dashboard, which only this command and "
                             "`janken enable` can see")
    p_bots.set_defaults(func=cmd_bots)

    p_hold = sub.add_parser(
        "hold", help="withdraw a bot from the field, keeping its place on the record")
    p_hold.add_argument("bot", help="bot id, id prefix or name (from `janken bots`)")
    p_hold.add_argument("--db", default="janken.db", help="registry database file")
    p_hold.set_defaults(func=cmd_hold)

    p_release = sub.add_parser("release", help="put a held bot back into the field")
    p_release.add_argument("bot", help="bot id, id prefix or name (from `janken bots`)")
    p_release.add_argument("--db", default="janken.db", help="registry database file")
    p_release.set_defaults(func=cmd_release)

    p_disable = sub.add_parser(
        "disable", help="hide a bot from every listing (reversible only here)")
    p_disable.add_argument("bot", help="bot id, id prefix or name")
    p_disable.add_argument("--db", default="janken.db", help="registry database file")
    p_disable.set_defaults(func=cmd_disable)

    p_enable = sub.add_parser(
        "enable", help="bring a disabled bot back — the admin half of `disable`")
    p_enable.add_argument("bot", help="bot id, id prefix or name "
                                      "(from `janken bots --disabled`)")
    p_enable.add_argument("--db", default="janken.db", help="registry database file")
    p_enable.set_defaults(func=cmd_enable)

    p_remove = sub.add_parser("remove", help="remove a bot (and its matches) from the registry")
    p_remove.add_argument("id", help="bot id (from `janken bots`)")
    p_remove.add_argument("--db", default="janken.db", help="registry database file")
    p_remove.set_defaults(func=cmd_remove)

    p_check = sub.add_parser("check", help="validate a bot before submitting")
    p_check.add_argument("bot", help="path to the bot .py file")
    p_check.add_argument("--timeout", type=float, default=1.0, help="seconds per move")
    p_check.set_defaults(func=cmd_check)

    p_dash = sub.add_parser("dashboard", help="launch the interactive Streamlit dashboard")
    p_dash.add_argument("--db", default="janken.db", help="registry database file")
    p_dash.add_argument("--port", type=int, default=6888,
                        help="port to serve on (default 6888 = 'MUTT' on a keypad)")
    p_dash.set_defaults(func=cmd_dashboard)

    p_cup = sub.add_parser(
        "cup", help="knockout cup — a separate competition that never moves ratings")
    cup_sub = p_cup.add_subparsers(dest="cup_command", required=True)

    p_cup_run = cup_sub.add_parser(
        "run", help="draw and play a whole cup, sealed until you show it")
    p_cup_run.add_argument("bots_dir", nargs="?", default="bots",
                           help="directory of bot files (default: bots)")
    p_cup_run.add_argument("--name", default="Janken Cup", help="cup name")
    p_cup_run.add_argument("--db", default="janken.db", help="registry database file")
    p_cup_run.add_argument("--league", default="all", choices=["all"] + list(LEAGUES),
                           help="restrict the field to one title (default: all)")
    p_cup_run.add_argument("--limit", type=int, default=64,
                           help="most entrants the bracket takes (default 64)")
    p_cup_run.add_argument("--include-held", action="store_true",
                           help="field bots their owners have withdrawn")
    p_cup_run.add_argument("--include-reference", action="store_true",
                           help="field the organizer's baseline bots (%s), which "
                                "a cup leaves out by default"
                                % ", ".join(sorted(reference.REFERENCE_BOTS)))
    p_cup_run.add_argument("--exclude", metavar="NAME", action="append",
                           help="keep this bot out of the draw (repeatable)")
    p_cup_run.add_argument("--timeout", type=float, default=1.0,
                           help="seconds per move")
    p_cup_run.add_argument("--workers", type=int, help="parallel matches per round")
    p_cup_run.add_argument("--spoil", action="store_true",
                           help="print the full bracket — you will know how it ends")
    p_cup_run.set_defaults(func=cmd_cup_run)

    p_cup_show = cup_sub.add_parser(
        "show", help="release a sealed cup to the audience, one round at a time")
    p_cup_show.add_argument("--cup", type=int, help="cup id (default: the current one)")
    p_cup_show.add_argument("--db", default="janken.db", help="registry database file")
    p_cup_show.add_argument("--auto", type=float, metavar="SECONDS",
                            help="advance on a timer instead of on Enter")
    p_cup_show.add_argument("--payload", metavar="PATH",
                            help="viewer payload rewritten on every reveal "
                                 "(default: static/cup-<db>.json). This is what "
                                 "the cup page polls, so a reveal reaches open "
                                 "browsers without anyone refreshing.")
    p_cup_show.add_argument("--no-payload", dest="payload", action="store_const",
                            const="", help="do not write the viewer payload")
    p_cup_show.set_defaults(func=cmd_cup_show)

    p_cup_list = cup_sub.add_parser("list", help="list cups and their reveal state")
    p_cup_list.add_argument("--db", default="janken.db", help="registry database file")
    p_cup_list.set_defaults(func=cmd_cup_list)

    p_cup_end = cup_sub.add_parser("end", help="close a cup out")
    p_cup_end.add_argument("--cup", type=int, help="cup id (default: the current one)")
    p_cup_end.add_argument("--db", default="janken.db", help="registry database file")
    p_cup_end.add_argument("--payload", metavar="PATH",
                           help="viewer payload to rewrite "
                                "(default: static/cup-<db>.json)")
    p_cup_end.set_defaults(func=cmd_cup_end)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
