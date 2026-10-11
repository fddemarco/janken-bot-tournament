"""Randy: uniformly random. Unexploitable, unexploiting."""

import random


def play(my_moves, opp_moves):
    return random.choice(("rock", "paper", "scissors"))
