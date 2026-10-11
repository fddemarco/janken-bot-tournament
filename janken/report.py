"""Self-contained HTML report for a tournament, in the Muttdata brand.

Single file, inline CSS, no external assets. DM Sans/DM Mono are pulled from
Google Fonts when the viewer is online and degrade to system fonts otherwise.
"""

import html
import time

from . import rating as elo

# Official brand assets, inlined so the report stays a single file.
SPARK_SVG = (
    '<svg class="spark" viewBox="0 0 200 200" fill="none" aria-hidden="true">'
    '<g stroke="#00ECFF" stroke-width="13" stroke-linecap="round">'
    '<line x1="100" y1="100" x2="100" y2="20"/>'
    '<line x1="100" y1="100" x2="100" y2="178"/>'
    '<line x1="100" y1="100" x2="26" y2="108"/>'
    '<line x1="100" y1="100" x2="180" y2="96"/>'
    '<line x1="100" y1="100" x2="48" y2="48"/>'
    '<line x1="100" y1="100" x2="154" y2="154"/>'
    '<line x1="100" y1="100" x2="46" y2="152"/>'
    '<line x1="100" y1="100" x2="152" y2="46"/>'
    "</g></svg>"
)

WORDMARK_SVG = (
    '<svg class="wordmark" viewBox="680 450 1020 178" aria-label="Muttdata">'
    '<g fill="#FFFFFF">'
    '<path d="M1014.1,493.1h-24.1v74.9c0,11.2-3.3,19.9-10,26.1-6.7,6.2-16.1,9.4-28.3,9.4s-20.2-3-26.3-8.9c-6-5.9-9-14.7-9-26.4v-75.1h-24.1v80.3c0,15.6,4.7,28.1,14.2,37.3,9.5,9.2,22.5,13.9,39.2,13.9s19.8-1.9,27.5-5.7c7.7-3.8,13.9-9.3,18.5-16.5h9v19.8h25.7v-25h-12.4v-104.1Z"/>'
    '<path d="M1160.6,511.6h33.6v-18.5h-33.8v-37.1h-23.7v37.1h-61.7v-37.1h-23.7v37.1h-25v18.5h24.7v89.5c0,7,1.9,12.3,5.7,15.8,3.8,3.5,9.1,5.3,16,5.3h39.4v-18.9h-37v-91.7h61.2v89.5c0,7,1.9,12.3,5.7,15.8,3.8,3.5,9.1,5.3,16,5.3h35.9v-18.9h-33.6v-91.7Z"/>'
    '<path d="M1326.3,456h-24.1v58h-.9c-3.9-7.6-9.6-13.4-17.1-17.4-7.5-4-16.5-6-26.8-6s-22.7,2.7-31.5,8.2c-8.8,5.5-15.6,13.2-20.3,23.2-4.7,10.1-7.1,22-7.1,35.7s2.4,25.7,7.2,35.7c4.8,10.1,11.8,17.8,20.9,23.2,9.1,5.4,20.1,8.2,33,8.2s19.2-1.9,26.7-5.7c7.5-3.8,13.3-9.4,17.6-16.7h9v19.8h25.8v-25h-12.5v-141.3ZM1302.2,567.1c0,11.2-3.6,20.2-10.7,26.9-7.1,6.7-16.5,10.1-28.3,10.1s-22.9-4.1-30-12.3c-7.1-8.2-10.7-19.6-10.7-34.2s3.6-26,10.7-34.2c7.1-8.2,17.1-12.3,30-12.3s21.2,3.4,28.3,10.1c7.1,6.8,10.7,15.7,10.7,26.9v18.9Z"/>'
    '<path d="M1464,539.8c0-15.9-4.8-28.1-14.3-36.6-9.5-8.5-23.3-12.7-41.2-12.7s-28.7,3.5-38.8,10.6c-10.1,7-16.3,16.9-18.4,29.7h24.3c3.7-13.5,14.6-20.2,32.5-20.2s32.9,10,32.9,29.9v5.8h-44.1c-15.6,0-27.6,3.4-35.9,10.2-8.3,6.8-12.5,16.4-12.5,28.7s4.2,21.3,12.6,28.5c8.4,7.2,19.9,10.9,34.5,10.9s19.1-2,26.9-5.9c7.8-3.9,13.9-9.4,18.3-16.2h9.1v19.8h25.8v-25h-11.6v-57.4ZM1441,569.7c0,6.5-1.9,12.4-5.6,17.9-3.7,5.5-8.8,9.8-15.3,12.9-6.5,3.2-13.6,4.7-21.3,4.7s-15.3-1.9-20.2-5.7c-4.9-3.8-7.3-8.9-7.3-15.4,0-13.5,9-20.2,27.1-20.2h42.6v5.8Z"/>'
    '<path d="M1524.9,511.6h33.6v-18.5h-33.8v-37.1h-23.7v37.1h-24.7v18.5h24.5v89.5c0,7,1.9,12.3,5.7,15.8,3.8,3.5,9.1,5.3,16,5.3h35.9v-18.9h-33.6v-91.7Z"/>'
    '<path d="M1684.4,597.2v-57.4c0-15.9-4.8-28.1-14.3-36.6-9.5-8.5-23.3-12.7-41.2-12.7s-28.7,3.5-38.8,10.6c-10.1,7-16.2,16.9-18.4,29.7h24.3c3.7-13.5,14.6-20.2,32.5-20.2s32.9,10,32.9,29.9v5.8h-44.1c-15.6,0-27.6,3.4-35.9,10.2-8.3,6.8-12.5,16.4-12.5,28.7s4.2,21.3,12.6,28.5c8.4,7.2,19.9,10.9,34.5,10.9s19.2-2,26.9-5.9c7.7-3.9,13.8-9.4,18.3-16.2h9.1v19.8h25.8v-25h-11.6ZM1661.4,569.7c0,6.5-1.9,12.4-5.6,17.9-3.7,5.5-8.8,9.8-15.3,12.9-6.5,3.2-13.6,4.7-21.3,4.7s-15.4-1.9-20.2-5.7c-4.9-3.8-7.3-8.9-7.3-15.4,0-13.5,9-20.2,27.1-20.2h42.6v5.8Z"/>'
    '<polygon points="843.3 456.1 782.9 596.6 782 596.6 721.8 456.1 685.8 456.1 685.8 622.1 708.9 622.1 708.9 484.9 709.7 484.9 768.2 622.1 795.2 622.1 853.5 485.1 854.3 485.1 854.3 622.1 879.1 622.1 879.1 456.1 843.3 456.1"/>'
    "</g></svg>"
)

CSS = """
:root {
  --md-blue: #0045FB; --md-navy: #001237; --md-white: #FFFFFF;
  --md-mist: #E1E4ED; --md-violet: #886AFF; --md-blue-deep: #0805AC;
  --md-cyan: #00ECFF; --md-green: #60D045; --md-orange: #FF7300;
  --md-font: "DM Sans", system-ui, -apple-system, sans-serif;
  --md-mono: "DM Mono", ui-monospace, monospace;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: var(--md-font); color: var(--md-white);
  background-color: var(--md-navy);
  background-image:
    radial-gradient(ellipse 55% 45% at 12% -8%, rgba(0,69,251,.45), transparent 70%),
    radial-gradient(ellipse 45% 40% at 95% 12%, rgba(136,106,255,.30), transparent 70%),
    radial-gradient(ellipse 60% 50% at 55% 115%, rgba(8,5,172,.55), transparent 70%);
  background-attachment: fixed;
  min-height: 100vh;
  padding: 6vmin;
}
.page { max-width: 900px; margin: 0 auto; }
header { margin: 3rem 0 3.5rem; }
.spark { width: 44px; height: 44px; display: block; margin-bottom: 1.25rem; }
.kicker {
  font-family: var(--md-mono); font-size: .8rem; letter-spacing: .18em;
  text-transform: uppercase; color: var(--md-mist); margin-bottom: .75rem;
}
h1 { font-size: clamp(2rem, 5vw, 3rem); font-weight: 500; letter-spacing: -.02em; }
h2 {
  font-size: 1.05rem; font-weight: 500; letter-spacing: -.01em;
  margin: 2.5rem 0 1rem; color: var(--md-mist);
}
.card {
  background: var(--md-white); color: var(--md-navy);
  border-radius: 28px; padding: 1.75rem; overflow-x: auto;
}
table { border-collapse: collapse; width: 100%; font-size: .95rem; }
th {
  font-family: var(--md-mono); font-size: .7rem; letter-spacing: .12em;
  text-transform: uppercase; font-weight: 400; color: var(--md-blue-deep);
  text-align: left; padding: .5rem .75rem;
  border-bottom: 2px solid var(--md-mist);
}
td { padding: .65rem .75rem; border-bottom: 1px solid var(--md-mist); }
tr:last-child td { border-bottom: none; }
td.num, th.num { text-align: right; font-family: var(--md-mono); }
.rank { font-family: var(--md-mono); color: var(--md-blue); }
tr.first td { background: rgba(0, 69, 251, .07); font-weight: 500; }
tr.first .rank { font-weight: 500; }
.botname { font-weight: 500; }
.h2h td { text-align: center; font-family: var(--md-mono); font-size: .85rem; }
.h2h th.rowhead, .h2h td.rowhead {
  text-align: left; font-family: var(--md-font); font-weight: 500;
  font-size: .95rem; text-transform: none; letter-spacing: 0;
  color: var(--md-navy);
}
.h2h .win  { background: rgba(96, 208, 69, .16); }
.h2h .loss { background: rgba(255, 115, 0, .14); }
.h2h .draw { background: rgba(225, 228, 237, .45); }
.h2h .self { color: var(--md-mist); }
.h2h .unplayed { color: var(--md-mist); }   /* pair never met (random schedule) */
.chip {
  display: inline-block; padding: .15rem .65rem; border-radius: 999px;
  font-family: var(--md-mono); font-size: .68rem; letter-spacing: .08em;
  text-transform: uppercase;
}
/* One per title slug (janken.rating.TITLES), weakest to strongest. */
.chip.novice { background: var(--md-mist);   color: var(--md-navy); }
.chip.cm     { background: var(--md-blue);   color: var(--md-white); }
.chip.master { background: var(--md-violet); color: var(--md-white); }
.chip.im     { background: var(--md-blue-deep); color: var(--md-white); }
.chip.gm     { background: var(--md-navy);   color: var(--md-cyan); }
.delta.up   { color: var(--md-green); }
.delta.down { color: var(--md-orange); }
.forfeits li { margin: .4rem 0 .4rem 1.2rem; }
.forfeits .card { font-size: .95rem; }
.forfeits code { font-family: var(--md-mono); color: var(--md-blue-deep); }
footer {
  margin-top: 4rem; display: flex; align-items: center;
  justify-content: space-between; gap: 1rem; flex-wrap: wrap;
}
.wordmark { width: 130px; opacity: .9; }
.meta { font-family: var(--md-mono); font-size: .75rem; color: var(--md-mist); }
"""


def render_html(result, leagues=None, banned=None, ratings=None) -> str:
    e = html.escape
    generated = time.strftime("%Y-%m-%d %H:%M")
    leagues = leagues or {}
    ratings = ratings or {}

    # --- standings -------------------------------------------------------
    # Ranked by rating, which is what the ladder is; the league is a title
    # riding along as a chip. Without ratings (an unrecorded run) the engine's
    # own points order stands in.
    standings = result.standings
    if ratings:
        standings = sorted(
            standings,
            key=lambda s: (-ratings[s.name].after, s.name) if s.name in ratings
            else (0.0, s.name))
    rows = []
    for i, s in enumerate(standings):
        league = leagues.get(s.name, "")
        # Slug picks the colour, full name is what a reader wants to see.
        chip = ('<span class="chip %s" title="%s">%s</span>'
                % (e(league), e(elo.name(league)), e(elo.abbr(league)))) if league else ""
        move = ratings.get(s.name)
        rating = "%d" % round(move.after) if move else "&mdash;"
        if move:
            arrow = "up" if move.delta >= 0 else "down"
            delta = '<span class="delta %s">%+d</span>' % (arrow, round(move.delta))
        else:
            delta = "&mdash;"
        rows.append(
            '<tr class="%s"><td class="rank">%d</td><td class="botname">%s</td>'
            "<td>%s</td>"
            '<td class="num">%s</td><td class="num">%s</td>'
            '<td class="num">%d</td><td class="num">%d</td><td class="num">%d</td>'
            '<td class="num">%d</td><td class="num">%+d</td></tr>'
            % ("first" if i == 0 else "", i + 1, e(s.name.title()), chip,
               rating, delta,
               s.wins, s.draws, s.losses, s.forfeits, s.diff)
        )
    standings_html = (
        '<div class="card"><table>'
        '<tr><th>#</th><th>Bot</th><th>Title</th><th class="num">Rating</th>'
        '<th class="num">+/-</th><th class="num">W</th>'
        '<th class="num">D</th><th class="num">L</th><th class="num">Forf</th>'
        '<th class="num">Rounds +/-</th></tr>%s</table></div>' % "".join(rows)
    )

    # --- head-to-head matrix (ordered by final rank) ---------------------
    # Each cell is coloured by the match record over the whole pairing (the
    # same basis as the standings), not by round wins with a forfeit override:
    # a single forfeit in one of several matches must not sink a cell the bot
    # otherwise won.
    names = [s.name for s in standings]
    pair_stats = {}
    forfeit_notes = []
    for m in result.matches:
        winner = m.winner
        for me, them, my_wins, their_wins, my_forfeit in (
            (m.bot_a, m.bot_b, m.wins_a, m.wins_b, m.forfeit_a),
            (m.bot_b, m.bot_a, m.wins_b, m.wins_a, m.forfeit_b),
        ):
            s = pair_stats.setdefault(
                (me, them),
                {"rw": 0, "orw": 0, "wins": 0, "losses": 0, "matches": 0, "forfeits": 0},
            )
            s["rw"] += my_wins
            s["orw"] += their_wins
            s["matches"] += 1
            if winner == me:
                s["wins"] += 1
            elif winner is not None:
                s["losses"] += 1
            if my_forfeit:
                s["forfeits"] += 1
                forfeit_notes.append((me, them, my_forfeit))

    head = "".join("<th>%s</th>" % e(n.title()) for n in names)
    body = []
    for row_bot in names:
        cells = ['<td class="rowhead">%s</td>' % e(row_bot.title())]
        for col_bot in names:
            if row_bot == col_bot:
                cells.append('<td class="self">&mdash;</td>')
                continue
            s = pair_stats.get((row_bot, col_bot))
            if s is None:
                # A balanced random schedule (--games N) doesn't pair everybody,
                # so plenty of cells have no match behind them at all.
                cells.append('<td class="unplayed" title="did not meet">&middot;</td>')
                continue
            cls = ("win" if s["wins"] > s["losses"]
                   else "loss" if s["wins"] < s["losses"] else "draw")
            if s["forfeits"] == s["matches"]:
                # Every match forfeited: no meaningful score to show.
                cell = '<td class="%s">forfeit</td>' % cls
            elif s["forfeits"]:
                # Partial forfeit: keep the round score, flag it (details below).
                cell = ('<td class="%s" title="one or more matches forfeited">'
                        "%d&ndash;%d&dagger;</td>" % (cls, s["rw"], s["orw"]))
            else:
                cell = '<td class="%s">%d&ndash;%d</td>' % (cls, s["rw"], s["orw"])
            cells.append(cell)
        body.append("<tr>%s</tr>" % "".join(cells))
    h2h_html = (
        '<div class="card"><table class="h2h">'
        '<tr><th class="rowhead"></th>%s</tr>%s</table></div>'
        % (head, "".join(body))
    )

    # --- forfeits --------------------------------------------------------
    forfeits_html = ""
    if forfeit_notes:
        items = "".join(
            "<li><strong>%s</strong> vs %s &mdash; <code>%s</code></li>"
            % (e(who.title()), e(vs.title()), e(reason))
            for who, vs, reason in sorted(set(forfeit_notes))
        )
        forfeits_html = (
            '<section class="forfeits"><h2>Forfeits</h2>'
            '<div class="card"><ul>%s</ul></div></section>' % items
        )

    # --- banned bots ------------------------------------------------------
    banned_html = ""
    if banned:
        items = "".join(
            "<li><strong>%s</strong> &mdash; <code>%s</code></li>" % (e(name), e(reason))
            for name, reason in sorted(banned)
        )
        banned_html = (
            '<section class="forfeits"><h2>Banned &mdash; failed safety screening</h2>'
            '<div class="card"><ul>%s</ul></div></section>' % items
        )

    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Janken Bot Championship</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>%(css)s</style>
</head>
<body>
<div class="page">
<header>
%(spark)s
<p class="kicker">Muttdata &middot; Bot Championship</p>
<h1>Janken Tournament Results</h1>
</header>
<section>
<h2>Standings</h2>
<p class="meta">ranked by rating &middot; +/- is this run's rating move &middot;
a title is a label a bot keeps, not a bracket it plays in<br>%(ladder)s</p>
%(standings)s
</section>
<section>
<h2>Head to head &mdash; rounds won (row vs column)</h2>
%(h2h)s
</section>
%(forfeits)s
%(banned)s
<footer>
%(wordmark)s
<p class="meta">generated %(generated)s &middot; %(rounds)d rounds &times; %(mpp)d match(es) per pair</p>
</footer>
</div>
</body>
</html>
""" % {
        "css": CSS,
        "spark": SPARK_SVG,
        "wordmark": WORDMARK_SVG,
        "standings": standings_html,
        "ladder": e(elo.ladder()),
        "h2h": h2h_html,
        "forfeits": forfeits_html,
        "banned": banned_html,
        "generated": generated,
        "rounds": result.rounds,
        "mpp": result.matches_per_pair,
    }
