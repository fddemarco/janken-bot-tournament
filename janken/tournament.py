"""Tournament over a set of bot files — round-robin or balanced-random,
played in parallel (matches are independent).

This is the engine, and it knows nothing about the registry: it plays a field and
summarises *that run* with points, which is all it can do without a rating
history. The ladder people care about is the Elo rating the registry keeps
(`rating.py`); every view that has a registry to hand — the CLI standings, the
HTML report, the dashboard — ranks by rating instead, and these points only
break ties inside a single unrecorded run.
"""

import concurrent.futures
import dataclasses
import itertools
import os
import random
from typing import Callable, Dict, List, Optional, Tuple

from .match import DEFAULT_ROUNDS, MatchResult, play_match

POINTS_WIN = 3    # this run only — career ranking is Elo, see rating.py
POINTS_DRAW = 1


@dataclasses.dataclass
class Standing:
    name: str
    points: int = 0
    wins: int = 0
    draws: int = 0
    losses: int = 0
    forfeits: int = 0
    round_wins: int = 0
    round_losses: int = 0

    @property
    def diff(self) -> int:
        return self.round_wins - self.round_losses

    def sort_key(self):
        # Higher is better on every axis; name breaks remaining ties.
        return (-self.points, -self.diff, -self.round_wins, self.name)


@dataclasses.dataclass
class TournamentResult:
    standings: List[Standing]
    matches: List[MatchResult]
    rounds: int
    matches_per_pair: int

    def head_to_head(self) -> Dict[Tuple[str, str], List[MatchResult]]:
        table: Dict[Tuple[str, str], List[MatchResult]] = {}
        for m in self.matches:
            table.setdefault((m.bot_a, m.bot_b), []).append(m)
        return table


def discover_bots(directory: str) -> List[str]:
    """All .py files in the directory (non-recursive); '_'-prefixed are skipped."""
    paths = sorted(
        os.path.join(directory, f)
        for f in os.listdir(directory)
        if f.endswith(".py") and not f.startswith("_")
    )
    if len(paths) < 2:
        raise SystemExit(
            "need at least 2 bots in %r, found %d" % (directory, len(paths))
        )
    return paths


def run_tournament(
    bot_paths: List[str],
    rounds: int = DEFAULT_ROUNDS,
    matches_per_pair: int = 1,
    timeout: float = 1.0,
    on_match: Optional[Callable[[MatchResult], None]] = None,
    workers: Optional[int] = None,
    games_per_bot: Optional[int] = None,
) -> TournamentResult:
    """Play a tournament and return the standings.

    Pairings are a full round-robin by default, or — when ``games_per_bot`` is
    set — a balanced random schedule where each bot plays about that many games.
    A random schedule costs O(n) instead of O(n^2) and, with enough games, ranks
    about as fairly. Matches are independent, so they run concurrently across
    ``workers`` threads (default: CPU count); each match still isolates its two
    bots in their own subprocesses.
    """
    names = [os.path.splitext(os.path.basename(p))[0] for p in bot_paths]
    standings = {name: Standing(name=name) for name in names}

    if games_per_bot:
        schedule = _random_schedule(bot_paths, games_per_bot)
    else:
        schedule = [pair for pair in itertools.combinations(bot_paths, 2)
                    for _ in range(matches_per_pair)]

    matches: List[MatchResult] = []
    for result in _play_schedule(schedule, rounds, timeout, workers):
        matches.append(result)
        _apply_result(standings, result)
        if on_match:
            on_match(result)

    ordered = sorted(standings.values(), key=Standing.sort_key)
    return TournamentResult(
        standings=ordered,
        matches=matches,
        rounds=rounds,
        matches_per_pair=matches_per_pair,
    )


def _random_schedule(bot_paths: List[str], games_per_bot: int) -> List[Tuple[str, str]]:
    """A balanced random schedule: each bot appears in ~games_per_bot matches.

    Give every bot that many tokens, shuffle, and pair adjacent tokens (swapping
    to avoid self-matches). Balanced games-per-bot keeps points-based ranking
    fair without playing all O(n^2) pairs.
    """
    tokens = [p for p in bot_paths for _ in range(games_per_bot)]
    random.shuffle(tokens)
    pairs: List[Tuple[str, str]] = []
    i, n = 0, len(tokens)
    while i + 1 < n:
        a, b = tokens[i], tokens[i + 1]
        if a == b:  # avoid a self-match: swap in a later, different token
            swap = next((j for j in range(i + 2, n) if tokens[j] != a), None)
            if swap is None:
                break
            tokens[i + 1], tokens[swap] = tokens[swap], tokens[i + 1]
            b = tokens[i + 1]
        pairs.append((a, b))
        i += 2
    return pairs


def _play_schedule(schedule, rounds, timeout, workers):
    """Yield MatchResults, running matches concurrently across worker threads."""
    workers = workers or (os.cpu_count() or 2)
    if workers <= 1 or len(schedule) <= 1:
        for a, b in schedule:
            yield play_match(a, b, rounds=rounds, timeout=timeout)
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(play_match, a, b, rounds=rounds, timeout=timeout)
                   for a, b in schedule]
        for future in concurrent.futures.as_completed(futures):
            yield future.result()


def _apply_result(standings: Dict[str, Standing], m: MatchResult):
    a, b = standings[m.bot_a], standings[m.bot_b]
    a.round_wins += m.wins_a
    a.round_losses += m.wins_b
    b.round_wins += m.wins_b
    b.round_losses += m.wins_a
    if m.forfeit_a:
        a.forfeits += 1
    if m.forfeit_b:
        b.forfeits += 1

    winner = m.winner
    if winner is None:
        a.points += POINTS_DRAW
        b.points += POINTS_DRAW
        a.draws += 1
        b.draws += 1
    else:
        won, lost = (a, b) if winner == m.bot_a else (b, a)
        won.points += POINTS_WIN
        won.wins += 1
        lost.losses += 1
