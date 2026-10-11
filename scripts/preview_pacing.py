#!/usr/bin/env python3
"""Preview a cup's broadcast pacing without a browser.

Tuning the replay means picking numbers — how fast a dull round goes, how long
a moment holds, how much airtime a match earns. You cannot tell 40ms from 55ms
by reading a diff, but you *can* tell, instantly and without watching, whether
the shape is wrong: a rout hogging the quarter-finals, a final shorter than the
semi before it, a match running ten seconds with nothing emphasised in it. Those
are the mistakes that actually happen, and this prints them.

Plays a real cup (subprocesses, sandbox, the works), scripts the broadcast, and
reports where the time goes. Scripting is a pure function of the stored
transcripts, so the same cup re-scripts in milliseconds — edit a constant in
janken/broadcast.py, run this again, compare.

  python3 scripts/preview_pacing.py                  # synthetic 16-bot field
  python3 scripts/preview_pacing.py --field 32
  python3 scripts/preview_pacing.py --repeat 5       # how much does it vary?
  python3 scripts/preview_pacing.py --bots ./bots --db ./janken.db

The synthetic field is the default on purpose: it is reproducible, and its
archetypes reliably produce the awkward cases — a rout, a stalemate that runs
to sudden death, and a couple of genuine thrillers.
"""

import argparse
import os
import random
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from janken import broadcast, cup  # noqa: E402

# Bot archetypes, chosen to span the pacing cases rather than to play well.
ARCHETYPES = {
    "rocky": "def play(m, o):\n    return 'rock'\n",
    "papery": "def play(m, o):\n    return 'paper'\n",
    "randy": ("import random\n"
              "def play(m, o):\n"
              "    return random.choice(['rock', 'paper', 'scissors'])\n"),
    "copycat": ("VALID = ('rock', 'paper', 'scissors')\n"
                "def play(m, o):\n"
                "    return o[-1] if o and o[-1] in VALID else 'rock'\n"),
    "counter": ("B = {'rock': 'paper', 'paper': 'scissors', 'scissors': 'rock'}\n"
                "def play(m, o):\n"
                "    return B[o[-1]] if o and o[-1] in B else 'rock'\n"),
    "brainy": ("from collections import Counter\n"
               "B = {'rock': 'paper', 'paper': 'scissors', 'scissors': 'rock'}\n"
               "def play(m, o):\n"
               "    o = [x for x in o if x in B]\n"
               "    if len(o) < 3:\n"
               "        return 'rock'\n"
               "    return B[Counter(o[-20:]).most_common(1)[0][0]]\n"),
    "cycler": ("M = ['rock', 'paper', 'scissors']\n"
               "def play(m, o):\n    return M[len(m) % 3]\n"),
    "biased": ("import random\n"
               "def play(m, o):\n"
               "    return random.choice(['rock', 'rock', 'paper', 'scissors'])\n"),
}

BAR_WIDTH = 24


def synthetic_field(directory, size, rng):
    """Write `size` bot files spanning the archetypes, with spread ratings."""
    kinds = list(ARCHETYPES)
    paths, ratings = [], {}
    for i in range(size):
        name = "%s%02d" % (kinds[i % len(kinds)], i)
        path = os.path.join(directory, name + ".py")
        with open(path, "w") as f:
            f.write(ARCHETYPES[kinds[i % len(kinds)]])
        paths.append(path)
        ratings[name] = (1400 + rng.random() * 700, "novice")
    return paths, ratings


def real_field(bots_dir, db_path):
    paths = sorted(os.path.join(bots_dir, f) for f in os.listdir(bots_dir)
                   if f.endswith(".py") and not f.startswith("_"))
    if len(paths) < 2:
        raise SystemExit("need at least 2 bots in %r" % bots_dir)
    ratings = {}
    if db_path:
        conn = sqlite3.connect(db_path)
        try:
            for name, rating, league in conn.execute(
                    "SELECT name, rating, league FROM bots WHERE banned = 0"):
                ratings[name] = (rating, league)
        finally:
            conn.close()
    return paths, ratings


def bar(ms, longest):
    filled = int(round(BAR_WIDTH * ms / longest)) if longest else 0
    return "█" * filled + "░" * (BAR_WIDTH - filled)


def squash(match):
    """How much of the beats' natural pacing survived the budget. 1.0 means
    the constants decided the length; 0.3 means the cap did."""
    return match["duration_ms"] / match["natural_ms"] if match["natural_ms"] else 1.0


def report(script):
    print("  %-14s %-9s %8s %8s" % ("STAGE", "TREATMENT", "PLAY", "RECAP"))
    for rnd in script["rounds"]:
        play_ms = rnd["duration_ms"] - rnd["interstitial_ms"]
        print("  %-14s %-9s %7.1fs %7.1fs   %d matches"
              % (rnd["stage"], rnd["treatment"], play_ms / 1000,
                 rnd["interstitial_ms"] / 1000, len(rnd["matches"])))

        if rnd["treatment"] == "reveal":
            continue
        longest = max(m["duration_ms"] for m in rnd["matches"]) or 1
        for m in sorted(rnd["matches"], key=lambda x: -x["duration_ms"]):
            marks = sorted({k["text"] for k in m["markers"]})
            print("      %s %5.1fs %3d%%  %-9s %3d-%-3d %-9s int %-5s %s"
                  % (bar(m["duration_ms"], longest), m["duration_ms"] / 1000,
                     round(squash(m) * 100),
                     "%s/%s" % (m["a"]["name"][:4], m["b"]["name"][:4]),
                     m["score"][0], m["score"][1], m["decided_by"],
                     m["interest"], ", ".join(marks)[:30]))
        print()


def smells(script):
    """Review a scripted cup without watching it.

    Returns (problems, notes). Problems are pacing that is actually wrong —
    emphasis inverted, an anticlimactic final, stretches with nothing to look
    at. Notes are things worth knowing before you reach for a constant, chiefly
    which matches are governed by their budget rather than by the beats, since
    editing BASE_MS will do nothing at all for those.
    """
    found, notes = [], []

    played = [r for r in script["rounds"] if r["treatment"] != "reveal"]
    for rnd in played:
        matches = rnd["matches"]
        if len(matches) < 2:
            continue
        longest = max(matches, key=lambda m: m["duration_ms"])
        dullest = min(matches, key=lambda m: m["interest"])
        if longest is dullest and len({m["interest"] for m in matches}) > 1:
            found.append(
                "%s: the least interesting match is also the longest (%s, "
                "interest %s, %.1fs) — emphasis is inverted."
                % (rnd["stage"], "%s v %s" % (longest["a"]["name"],
                                              longest["b"]["name"]),
                   longest["interest"], longest["duration_ms"] / 1000))

    # A final that gets less airtime than a semi reads as an anticlimax even
    # when the arithmetic behind it is defensible.
    final = next((r for r in script["rounds"] if r["round_size"] == 2), None)
    prior = [r for r in played if r["round_size"] > 2]
    if final and prior:
        final_ms = final["duration_ms"] - final["interstitial_ms"]
        longest_before = max(m["duration_ms"] for r in prior for m in r["matches"])
        if final_ms < longest_before:
            found.append(
                "The final (%.1fs) is shorter than the longest earlier match "
                "(%.1fs)." % (final_ms / 1000, longest_before / 1000))

    for rnd in played:
        for m in rnd["matches"]:
            if not m["markers"] and m["duration_ms"] > 6000:
                found.append(
                    "%s: %s v %s runs %.1fs with nothing emphasised — %d rounds "
                    "of uniform texture."
                    % (rnd["stage"], m["a"]["name"], m["b"]["name"],
                       m["duration_ms"] / 1000, m["rounds_played"]))
            # Not a fault: a dull match being cut short is the budget doing its
            # job. But it means the beat constants had no say in this one, so
            # it is the budget curve you would tune, not BASE_MS.
            if m["natural_ms"] and squash(m) < 0.8:
                notes.append(
                    "%s: %s v %s is budget-governed — beats wanted %.1fs, "
                    "interest %s allowed %.1fs (%d%%)."
                    % (rnd["stage"], m["a"]["name"], m["b"]["name"],
                       m["natural_ms"] / 1000, m["interest"],
                       m["budget_ms"] / 1000, round(squash(m) * 100)))

    # The final is the one match everybody is definitely watching. A dull one
    # that is also the longest thing in the show is the worst way to end.
    if final and final["matches"]:
        decider = final["matches"][0]
        longest = max((m["duration_ms"] for r in played for m in r["matches"]),
                      default=0)
        if decider["interest"] < 20 and decider["duration_ms"] >= longest:
            found.append(
                "The final is a rout (interest %s, %d-%d) yet still the longest "
                "match in the show at %.1fs."
                % (decider["interest"], decider["score"][0], decider["score"][1],
                   decider["duration_ms"] / 1000))
    return found, notes


def play_one(paths, ratings, seed):
    random.seed(seed)
    plan = cup.plan(cup.entrants_from_paths(paths, ratings), name="Pacing preview")
    started = time.monotonic()
    cup.run(plan)
    play_s = time.monotonic() - started

    started = time.monotonic()
    script = broadcast.script(plan)
    script_ms = (time.monotonic() - started) * 1000
    return plan, script, play_s, script_ms


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--field", type=int, default=16,
                    help="synthetic field size (default 16)")
    ap.add_argument("--bots", help="use real bot files from this directory instead")
    ap.add_argument("--db", help="take seeding ratings from this registry")
    ap.add_argument("--seed", type=int, default=7, help="RNG seed (default 7)")
    ap.add_argument("--repeat", type=int, default=1,
                    help="play N cups and summarise the spread (default 1)")
    ap.add_argument("--quiet", action="store_true",
                    help="only the summary and the smells")
    args = ap.parse_args()

    workdir = None
    if args.bots:
        paths, ratings = real_field(args.bots, args.db)
    else:
        workdir = tempfile.mkdtemp(prefix="janken-pacing-")
        paths, ratings = synthetic_field(workdir, args.field,
                                         random.Random(args.seed))

    try:
        lengths = []
        for run in range(args.repeat):
            plan, script, play_s, script_ms = play_one(paths, ratings,
                                                       args.seed + run)
            lengths.append(script["duration_ms"] / 1000)

            if args.repeat > 1:
                print("\n=== run %d/%d ===" % (run + 1, args.repeat))
            print("\n%d entrants, bracket %d, %d matches — champion %s (seed %d)"
                  % (len(plan.entrants), plan.bracket_size, len(plan.matches),
                     plan.champion.name, plan.champion.seed))
            print("played in %.2fs · scripted in %.1fms · show %.1fs\n"
                  % (play_s, script_ms, script["duration_ms"] / 1000))

            if not args.quiet:
                report(script)

            problems, notes = smells(script)
            if problems:
                print("  PACING SMELLS")
                for p in problems:
                    print("   ! %s" % p)
            else:
                print("  no pacing smells")
            if notes and not args.quiet:
                print("\n  NOTES")
                for n in notes:
                    print("   · %s" % n)

        if args.repeat > 1:
            print("\nshow length over %d runs: min %.1fs  median %.1fs  max %.1fs"
                  % (args.repeat, min(lengths), statistics.median(lengths),
                     max(lengths)))
    finally:
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
