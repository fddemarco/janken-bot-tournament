"""Janken (rock-paper-scissors) bot tournament engine."""

MOVES = ("rock", "paper", "scissors")

# BEATS[x] is the move that x beats
BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}


def round_winner(move_a: str, move_b: str) -> int:
    """Return 0 if move_a wins, 1 if move_b wins, -1 on draw."""
    if move_a == move_b:
        return -1
    return 0 if BEATS[move_a] == move_b else 1
