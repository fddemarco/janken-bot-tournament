"""Referee: runs one match between two bot files, each in its own subprocess."""

import dataclasses
import functools
import os
import select
import shutil
import subprocess
import sys
import time
from typing import List, Optional

from . import MOVES, round_winner

WRAPPER = os.path.realpath(os.path.join(os.path.dirname(__file__), "wrapper.py"))

# The interpreter bot subprocesses run under, fully resolved. It has to be the
# resolved path: the sandbox masks $HOME and re-binds a minimal set of paths over
# it, so exec'ing a venv's bin/python3 *symlink* only works if every hop it
# traverses is also bound. uv's layout breaks that — bin/python3 -> python ->
# .../cpython-3.14-linux-aarch64-gnu/bin/python3.14, where the versionless
# directory is an alias symlink to cpython-3.14.6-..., and only the resolved one
# is exposed. The dangling hop made bwrap fail with "execvp ...: No such file or
# directory", which read as "sandboxing is unavailable" and stopped every bot on
# the host. Resolving here means the path we exec is always a path we bound.
# The subprocess needs no venv: it imports stdlib plus this package, which
# wrapper.py finds from its own directory.
INTERPRETER = os.path.realpath(sys.executable)

STARTUP_TIMEOUT = 5.0
DEFAULT_ROUND_TIMEOUT = 1.0

# How long a match is, unless a caller says otherwise. Lives here with the other
# match-shape defaults so there is one definition: the league reads it, and the cup
# scales its own stages off it ($JANKEN_ROUNDS overrides, set by install.sh).
DEFAULT_ROUNDS = int(os.environ.get("JANKEN_ROUNDS") or 200)

# Invalid moves a bot may return before it forfeits the match. One is a slip and
# costs the round; three is a broken bot, and a broken bot already forfeits when it
# crashes, hangs or fails to import — returning garbage indefinitely was the one
# way to be equally broken and be treated more gently, while still burning the
# full length of every match it played.
#
# Counted over the whole match rather than consecutively: three pieces of garbage
# is three pieces of garbage however they are spaced, and a consecutive rule lets a
# bot alternate junk with a legal move forever.
INVALID_MOVE_LIMIT = 3

# Transcript encoding: two characters per scored round, bot_a's move then
# bot_b's. A 100-round match is 200 bytes, so keeping every match of a whole
# tournament in memory (or in one DB column) costs nothing, and a replay never
# has to re-run the bots.
MOVE_CODE = {"rock": "r", "paper": "p", "scissors": "s"}
INVALID_CODE = "x"


def _code(move: str) -> str:
    return MOVE_CODE.get(move, INVALID_CODE)

# --- sandbox ----------------------------------------------------------------
# Real containment, not just the static scanner. When bubblewrap is available
# and unprivileged user namespaces work, every bot subprocess runs with:
#   * no network at all (--unshare-all drops the net namespace, so a bot that
#     defeats the scanner still can't phone home or reach the AWS metadata IP),
#   * a read-only view of the whole filesystem (it can't tamper with janken.db,
#     the registry, or anything else),
#   * the home dir(s) masked by an empty tmpfs, so SSH keys, cloud creds, and
#     rival submissions are simply not there — only the interpreter, this
#     package, and the bot's own file are re-exposed on top,
#   * a throwaway tmpfs as cwd/HOME, and a cleared environment (no AWS_* etc.).
#
# We bind "/" read-only rather than enumerating system dirs so the same code
# works on this x86/pyenv box and on the aarch64/uv t4g without knowing the
# loader paths: the interpreter, wherever it lives, is already present. Where
# bwrap is missing or userns is blocked (e.g. macOS, hardened kernels) we warn
# once and fall back to a plain subprocess — scanner and rlimits still apply —
# unless JANKEN_REQUIRE_SANDBOX is set, which makes that a hard error instead.
# JANKEN_NO_SANDBOX=1 forces the sandbox off (for debugging on odd hosts).

_BWRAP = shutil.which("bwrap")

_warned = False


def _env_flag(name: str) -> bool:
    """Truthy env var, treating 0/false/no/off (and unset/empty) as off — so
    JANKEN_REQUIRE_SANDBOX=0 disables, rather than the string "0" reading true."""
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no", "off")


def _warn_once(message: str):
    global _warned
    if not _warned:
        _warned = True
        print("janken: %s" % message, file=sys.stderr)


def _reexpose_paths(bot_path: str) -> List[str]:
    """Absolute paths to re-bind read-only on top of the masked home(s): the
    interpreter (venv *and* its base — for pyenv/uv both sit under $HOME), this
    package (so the wrapper can import janken.safety), and the bot's file.

    Each path is re-exposed in *both* its unresolved and its resolved form, since
    either may be the one something inside the sandbox actually names — a venv
    directory and the interpreter it symlinks to typically live far apart, and a
    submission may sit under a symlinked directory. The path bot subprocesses are
    exec'd by is resolved up front (see INTERPRETER), so it is always in here.
    Sorted so a parent directory is bound before anything nested under it.
    """
    paths = set()
    for p in (sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix,
              sys.executable, os.path.dirname(WRAPPER), bot_path):
        paths.add(os.path.abspath(p))
        paths.add(os.path.realpath(p))

    # Only the top-most paths: binding something *inside* a path already bound
    # would have bwrap create a mount point within a read-only mount, which it
    # refuses ("Can't create file at ..."). A parent bind already exposes the
    # child anyway. Sorting puts parents first, so one pass is enough.
    # A missing source is a hard bwrap error too, so never ask it to bind one.
    minimal: List[str] = []
    for path in sorted(p for p in paths if os.path.exists(p)):
        if not any(path.startswith(kept + os.sep) for kept in minimal):
            minimal.append(path)
    return minimal


def _bwrap_prefix(bot_path: str) -> List[str]:
    argv = [
        _BWRAP, "--unshare-all", "--die-with-parent", "--clearenv",
        "--setenv", "HOME", "/tmp",
        "--setenv", "PATH", "/usr/bin:/bin",
        "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
        "--ro-bind", "/", "/",              # whole tree, read-only: no tampering
        "--dev", "/dev", "--proc", "/proc",  # clean /dev + /proc over the host's
        "--tmpfs", "/tmp", "--chdir", "/tmp",  # writable scratch, discarded on exit
    ]
    # Mask home dirs (SSH keys, cloud creds, rival submissions, the DB) with an
    # empty tmpfs, then re-expose only what the subprocess actually needs. Order
    # matters: the tmpfs masks must precede the re-binds that layer on top.
    for home in sorted({os.path.realpath(os.path.expanduser("~")), "/root"}):
        if os.path.isdir(home):
            argv += ["--tmpfs", home]
    for p in _reexpose_paths(bot_path):
        argv += ["--ro-bind", p, p]
    return argv


@functools.lru_cache(maxsize=1)
def _sandbox_available() -> bool:
    """True if bwrap can actually contain here. Probed once by booting Python
    under the same prefix a match would use; any failure means fall back."""
    if _env_flag("JANKEN_NO_SANDBOX"):
        return False
    if not _BWRAP:
        _warn_once("bubblewrap (bwrap) not found — bots run WITHOUT sandbox isolation")
        return False
    argv = _bwrap_prefix(WRAPPER) + [INTERPRETER, "-c", "pass"]
    try:
        proc = subprocess.run(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, timeout=STARTUP_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _warn_once("bwrap present but not functional (%s) — running WITHOUT sandbox" % exc)
        return False
    if proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip().splitlines()[-1:] or [""]
        _warn_once("bwrap probe failed — running WITHOUT sandbox: %s" % detail[0][:200])
        return False
    return True


def _enforce_sandbox_policy():
    """With JANKEN_REQUIRE_SANDBOX set, refuse to run bots unsandboxed rather
    than silently degrading — the posture we want on the AWS host."""
    if _env_flag("JANKEN_REQUIRE_SANDBOX") and not _sandbox_available():
        raise RuntimeError(
            "JANKEN_REQUIRE_SANDBOX is set but bubblewrap sandboxing is "
            "unavailable here — refusing to run bots unsandboxed. Install "
            "bwrap and enable unprivileged user namespaces, or unset "
            "JANKEN_REQUIRE_SANDBOX to allow the (less safe) fallback."
        )


@dataclasses.dataclass
class MatchResult:
    bot_a: str
    bot_b: str
    wins_a: int = 0
    wins_b: int = 0
    draws: int = 0
    forfeit_a: Optional[str] = None  # reason bot_a forfeited, if it did
    forfeit_b: Optional[str] = None
    # Every scored round, two chars each (see MOVE_CODE). Rounds lost to a
    # forfeit are never scored, so len(moves) == 2 * rounds actually played.
    moves: str = ""
    # Rounds played past the scheduled ones to break a tie (cup only; 0 in the
    # league, where a drawn match is simply a draw).
    sudden_death: int = 0

    @property
    def winner(self) -> Optional[str]:
        """Name of the winning bot, or None on a draw."""
        if self.forfeit_a and not self.forfeit_b:
            return self.bot_b
        if self.forfeit_b and not self.forfeit_a:
            return self.bot_a
        if self.forfeit_a and self.forfeit_b:
            return None
        if self.wins_a > self.wins_b:
            return self.bot_a
        if self.wins_b > self.wins_a:
            return self.bot_b
        return None

    @property
    def is_draw(self) -> bool:
        return self.winner is None


class BotProcess:
    """One bot subprocess with buffered, deadline-based line reads."""

    def __init__(self, path: str, stderr=subprocess.DEVNULL, sandbox: bool = True):
        self.name = os.path.splitext(os.path.basename(path))[0]
        # Canonical paths: the sandbox re-binds by realpath, so the exec targets
        # must resolve to those same paths (a `..` or symlink in sys.executable
        # otherwise points at something not bound inside the sandbox).
        bot = os.path.realpath(path)
        argv = [INTERPRETER, WRAPPER, bot]
        if sandbox and _sandbox_available():
            argv = _bwrap_prefix(bot) + argv
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            bufsize=0,
        )
        self._buf = b""

    def send(self, line: str):
        self.proc.stdin.write((line + "\n").encode())
        self.proc.stdin.flush()

    def read_line(self, deadline: float) -> str:
        """Read one protocol line, raising TimeoutError/EOFError on failure."""
        fd = self.proc.stdout.fileno()
        while b"\n" not in self._buf:
            # Even past the deadline, drain data that already arrived: a bot
            # that answered in time must not be failed because its opponent
            # burned the shared round clock first.
            remaining = max(0.0, deadline - time.monotonic())
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                raise TimeoutError("timed out")
            chunk = os.read(fd, 4096)
            if not chunk:
                raise EOFError("crashed (process exited)")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line.decode(errors="replace").strip()

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        for stream in (self.proc.stdin, self.proc.stdout):
            if stream:
                stream.close()


def play_match(
    path_a: str,
    path_b: str,
    rounds: int = DEFAULT_ROUNDS,
    timeout: float = DEFAULT_ROUND_TIMEOUT,
    stderr=subprocess.DEVNULL,
    sudden_death: int = 0,
) -> MatchResult:
    """Play one match. With ``sudden_death`` > 0, a match still level after the
    scheduled rounds keeps playing single rounds — up to that many — until one
    bot takes a round outright. The league leaves it at 0 (a draw is a draw);
    the cup sets it, because somebody has to advance."""
    _enforce_sandbox_policy()
    bot_a = BotProcess(path_a, stderr=stderr)
    bot_b = BotProcess(path_b, stderr=stderr)
    result = MatchResult(bot_a=bot_a.name, bot_b=bot_b.name)
    try:
        _run_rounds(bot_a, bot_b, result, rounds, timeout, sudden_death)
    finally:
        for bot in (bot_a, bot_b):
            try:
                bot.send("STOP")
            except OSError:
                pass
            bot.close()
    return result


def _run_rounds(bot_a, bot_b, result, rounds, timeout, sudden_death=0):
    if not _handshake(bot_a, bot_b, result):
        return

    last = [None, None]  # each bot's previous move, for the opponent's GO line
    # Invalid moves each bot has returned, for the whole match including sudden
    # death: a bot that spent its allowance in the scheduled rounds does not get a
    # fresh one for the decider.
    invalid = [0, 0]
    for _ in range(rounds):
        if not _play_round(bot_a, bot_b, result, last, invalid, timeout):
            return

    # Sudden death runs on the *same* subprocesses, so the bots' histories
    # simply continue — a predictor keeps everything it learned over the
    # scheduled rounds rather than facing a stranger in the decider.
    while result.sudden_death < sudden_death and result.wins_a == result.wins_b:
        if not _play_round(bot_a, bot_b, result, last, invalid, timeout):
            return
        result.sudden_death += 1


def _handshake(bot_a, bot_b, result) -> bool:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    for bot, side in ((bot_a, "a"), (bot_b, "b")):
        try:
            if bot.read_line(deadline) != "READY":
                raise EOFError("bad handshake")
        except (TimeoutError, EOFError) as exc:
            _set_forfeit(result, side, "failed to start: " + str(exc))
    return not (result.forfeit_a or result.forfeit_b)


def _play_round(bot_a, bot_b, result, last, invalid, timeout) -> bool:
    """Play and score one round. False if a bot forfeited (match over).

    ``last`` is a two-slot list of each bot's previous move, and ``invalid`` a
    two-slot list of how many invalid moves each has returned; both are updated in
    place so the counts survive across rounds and into sudden death.
    """
    # Both bots get the round concurrently; the clock starts at send.
    bot_a.send("GO" if last[1] is None else "GO " + last[1])
    bot_b.send("GO" if last[0] is None else "GO " + last[0])
    deadline = time.monotonic() + timeout

    moves = [None, None]
    for i, (bot, side) in enumerate(((bot_a, "a"), (bot_b, "b"))):
        try:
            moves[i] = bot.read_line(deadline)
        except (TimeoutError, EOFError) as exc:
            _set_forfeit(result, side, str(exc))
    if result.forfeit_a or result.forfeit_b:
        return False

    last[0], last[1] = moves
    result.moves += _code(moves[0]) + _code(moves[1])
    _score_round(result, moves[0], moves[1])

    # Scored first, then judged. An invalid move is a move that arrived: it lost
    # its round fairly and belongs in the transcript, so the round that tips a bot
    # over the limit still counts. Only then does the match end — unlike a crash,
    # where there is no move to score.
    for i, side in enumerate(("a", "b")):
        if moves[i] not in MOVES:
            invalid[i] += 1
            if invalid[i] >= INVALID_MOVE_LIMIT:
                # Phrased as a verb: `janken check` prints this into "FAIL: your
                # bot %s", beside "timed out" and "crashed (process exited)".
                _set_forfeit(result, side,
                             "returned %d invalid moves" % invalid[i])
    return not (result.forfeit_a or result.forfeit_b)


def _score_round(result, move_a, move_b):
    valid_a, valid_b = move_a in MOVES, move_b in MOVES
    if valid_a and valid_b:
        outcome = round_winner(move_a, move_b)
        if outcome == 0:
            result.wins_a += 1
        elif outcome == 1:
            result.wins_b += 1
        else:
            result.draws += 1
    elif valid_a:  # b returned garbage: b loses the round
        result.wins_a += 1
    elif valid_b:
        result.wins_b += 1
    else:
        result.draws += 1


def _set_forfeit(result, side, reason):
    if side == "a":
        result.forfeit_a = reason
    else:
        result.forfeit_b = reason
