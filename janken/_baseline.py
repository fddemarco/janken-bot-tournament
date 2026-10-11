"""Built-in random opponent used by `python -m janken check`."""

import random


def play(my_moves, opp_moves):
    return random.choice(("rock", "paper", "scissors"))
