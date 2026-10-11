"""Subprocess shim that hosts one participant bot and speaks the referee protocol.

Run as:  python -m janken.wrapper path/to/bot.py

Protocol (on the real stdout / stdin):
  -> READY                    once the bot module is loaded
  <- GO [opp_last_move]       referee asks for a move (no arg on round 1)
  -> <move>                   the bot's move for this round
  <- STOP                     end of match

Everything the participant's code prints goes to stderr: fd 1 is re-pointed
at stderr before the bot is loaded, and the protocol keeps a private dup of
the original stdout. Resource limits (CPU, memory) are applied to this
process before any participant code runs.
"""

import importlib.util
import os
import sys

MOVES = ("rock", "paper", "scissors")

MEM_LIMIT_BYTES = 512 * 1024 * 1024
CPU_LIMIT_SECONDS = 120


def _apply_rlimits():
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (CPU_LIMIT_SECONDS, CPU_LIMIT_SECONDS))
        resource.setrlimit(resource.RLIMIT_AS, (MEM_LIMIT_BYTES, MEM_LIMIT_BYTES))
    except Exception:
        pass  # non-POSIX platform; timeouts in the referee still apply


def _steal_stdout():
    """Return a private handle to the real stdout and point fd 1 at stderr."""
    protocol_fd = os.dup(1)
    os.dup2(2, 1)  # bot code writing to fd 1 (including print()) hits stderr
    return os.fdopen(protocol_fd, "w", buffering=1)


def _load_bot(path):
    spec = importlib.util.spec_from_file_location("participant_bot", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.play


def _safety_gate(bot_path):
    """Refuse to load a bot that fails static screening (defense in depth:
    the CLI screens too, but anything reaching this process is re-checked)."""
    try:
        from janken.safety import scan_file
    except ImportError:  # run as a script: janken/ itself is on sys.path
        from safety import scan_file
    violations = scan_file(bot_path)
    if violations:
        for v in violations:
            print("SAFETY: %s" % v, file=sys.stderr)
        sys.exit(1)


def main():
    bot_path = sys.argv[1]
    protocol = _steal_stdout()
    _apply_rlimits()

    _safety_gate(bot_path)
    play = _load_bot(bot_path)

    protocol.write("READY\n")
    protocol.flush()

    my_moves, opp_moves = [], []
    for line in sys.stdin:
        parts = line.split()
        if not parts or parts[0] == "STOP":
            break
        if len(parts) > 1:  # GO <opp_last_move>
            opp_moves.append(parts[1])

        move = play(list(my_moves), list(opp_moves))
        token = str(move).strip().lower()
        if token not in MOVES:
            token = "invalid"
        my_moves.append(token)
        protocol.write(token + "\n")
        protocol.flush()


if __name__ == "__main__":
    main()
