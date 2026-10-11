"""Brainy: counters the opponent's most frequent move so far."""

import collections
import random

COUNTER = {"rock": "paper", "paper": "scissors", "scissors": "rock"}


def play(my_moves, opp_moves):
    # Opponent history may contain "invalid" if they misbehaved; ignore those.
    counts = collections.Counter(m for m in opp_moves if m in COUNTER)
    if not counts:
        return random.choice(tuple(COUNTER))
    favourite = counts.most_common(1)[0][0]
    return COUNTER[favourite]
