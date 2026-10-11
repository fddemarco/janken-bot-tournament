#!/usr/bin/env bash
#
# Periodic runner (for the systemd timer / cron). One step: field the round-robin
# straight from the registry (the master `bots` table), so it never rescans
# directories or rewrites display names, and re-rate everyone from the results.
#
# There used to be a `trials` step ahead of this one, playing a fixed gauntlet to
# award the first title. It was dropped: those same reference bots are entrants
# in the tournament, so the trial re-played known opponents for a badge nothing
# filtered on. Every title is a rating floor now, and this run awards them.
#
# A no-op when there is nothing to do (fewer than two eligible bots with their
# files present).
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"

# systemd runs with a minimal PATH, so find uv explicitly.
UV="$(command -v uv || true)"
[ -n "$UV" ] || UV="$HOME/.local/bin/uv"

DB="${JANKEN_DB:-$HERE/janken.db}"

# JANKEN_ROUNDS and JANKEN_GAMES are not passed: the command reads them itself, so
# repeating them here would be two places to change one number — and a stale
# fallback in this script would quietly override the unit's setting.
ARGS=(--db "$DB"
      --league "${JANKEN_LEAGUE:-all}"
      --matches "${JANKEN_MATCHES:-1}")
# JANKEN_WORKERS overrides the parallel worker count (defaults to CPU count).
[ -n "${JANKEN_WORKERS:-}" ] && ARGS+=(--workers "$JANKEN_WORKERS")

exec "$UV" run python -m janken tournament "${ARGS[@]}"
