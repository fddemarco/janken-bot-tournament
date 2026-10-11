"""Copycat: repeats the opponent's last move (rock on round 1)."""


def play(my_moves, opp_moves):
    if not opp_moves:
        return "rock"
    return opp_moves[-1]
