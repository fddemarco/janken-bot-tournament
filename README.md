# Janken Bot Championship 🪨️📄✂️

A rock-paper-scissors bot tournament. You write one Python file, we battle them
round-robin, the leaderboard settles it. Bots earn a persistent identity and an
**Glicko-2 rating** that carries across runs, plus chess titles (**Novice → Candidate
Master → Master → International Master → Grandmaster**) they keep for good — see
[Ratings, titles & the registry](#ratings-titles--the-registry). Now and then the
organizer also runs [a cup](#the-cup-a-separate-competition): a knockout, seeded
by rating, that settles nothing on the ladder and is watched live.

**Bots are standard-library only** — every submission runs in a sandbox with a
stdlib import allowlist (3.9+, POSIX), so they stay portable and safe to run.
The tournament engine is pure stdlib too and needs no install; the only optional
dependency is the [Streamlit dashboard](#the-dashboard-optional-interactive) for
an interactive organizer view.

## Writing a bot (participants)

Submit **one `.py` file**. Its filename is your bot's name (`ada.py` → `ada`).
The file must define a single function:

```python
def play(my_moves, opp_moves):
    """Decide your next move.

    my_moves:  list of your previous moves this match, oldest first
    opp_moves: list of the opponent's previous moves, oldest first
               (both are empty on round 1)

    Return one of: "rock", "paper", "scissors"
    """
    if not opp_moves:
        return "rock"
    beats = {"rock": "paper", "paper": "scissors", "scissors": "rock"}
    return beats[opp_moves[-1]]
```

### Rules of engagement

- A match is **200 rounds** against each opponent. Most round wins takes the
  match, and every match is **rated**: you gain rating for winning and lose it
  for losing, by how much the ratings said you should have won (beating a
  grandmaster is worth a lot; beating a bot you were expected to beat, barely
  anything). Everybody plays the same one pool, whatever their title.
- Your bot runs in its own process, so module-level state (globals, caches) is
  fine — it resets between matches.
- **Only these imports are allowed** (enforced by a code scan before your bot
  runs): `abc`, `array`, `bisect`, `collections`, `copy`, `dataclasses`,
  `datetime`, `decimal`, `enum`, `fractions`, `functools`, `heapq`,
  `itertools`, `json`, `math`, `numbers`, `random`, `re`,
  `statistics`, `string`, `time`, `typing`.
- You have **1 second per move** and ~512 MB of memory.
- Returning anything that isn't `"rock"`/`"paper"`/`"scissors"` loses you that
  round (and shows up as `"invalid"` in your opponent's `opp_moves`). **Three of
  them in one match forfeits it** — counted over the whole match, not
  consecutively. One is a slip; three is a broken bot, and a broken bot forfeits
  however it breaks.
- **Crashing, hanging, or failing to import forfeits the whole match.**
- Don't try to escape the sandbox or read your opponent. It's rock-paper-scissors.
  Have some dignity.

### Check your bot before submitting

```bash
python3 -m janken check path/to/yourbot.py
```

This first runs the safety screening, then plays your bot against a random
baseline and reports any crashes, timeouts, or invalid moves (tracebacks
included). If it says `OK`, you're good.

### Earning your first title

Every bot starts a **Novice**, rated 1500. The first two titles come from your
**peak rating** alone: **1600** makes you a Candidate Master, **1700** a Master.
The top two need a peak rating *and* **norms** — **1900** plus 2 norms for
International Master, **2100** plus 3 for Grandmaster. You keep them all for good.

A **norm** is a qualifying performance in a *norm event*: a selective tournament
the organizer holds now and then, over a field that has to span several different
submitters and be mostly titled already. You do not enter one — the field is
picked by rating, so being near the top is the invitation. The point of the extra
requirement is that a rating says how well you did while a norm says who you did
it against, and only the second is expensive to fake.

The reference bots (rocky, copycat, brainy, randy) are not invited, the same way
they sit out [a cup](#the-cup-a-separate-competition): they exist to give the
league a baseline, and a norm is a prize none of them is trying to win. They still
play the rated ladder, so beating them still counts toward your rating.

There is nothing to enter and nothing to clear first. Everybody plays one pool
from the moment they register, so your bot meets the whole field — including the
reference bots (rocky, copycat, brainy, randy) — on the organizer's next
scheduled run, and the results move your rating.

> **Removed:** there used to be a *gauntlet* — a fixed set of reference bots to
> beat by 70%+ of decided rounds for the Candidate Master title, played in a
> separate unrated `trial`. It's gone. Those same bots are entrants in the rated
> tournament, so the trial re-played known opponents to award a badge nothing in
> the system filtered on, and a bot could lead the standings while still reading
> `Novice` for never having cleared it. How your bot handles them now shows up in
> the head-to-head grid, rated and at full length. `janken trial` and
> `janken trials` no longer exist.

To try your bot against the field while developing, just run a local tournament:

```bash
python3 -m janken run bots/ --rounds 200
```

Rating is the only thing you climb. The [cup](#the-cup-a-separate-competition)
is a knockout the organizer runs from time to time and watches live — your
rating seeds the draw, but nothing that happens there changes it. There is
nothing to enter for that either.

## Ratings, titles & the registry

Results persist between runs in a single SQLite file (`janken.db` by default —
poke at it with the `sqlite3` CLI). Each bot gets a stable identity, a rating
and a career record.

- **Identity is the hash of your file's contents.** Rename the file and your
  bot keeps its identity, rating, stats and titles (the filename is just the
  display name). Change a single byte of code and it becomes a *new* bot, rated
  1500 again with no titles — so submit deliberately.
- **The ladder is the rating.** Everyone starts at **1500**, and beating a
  stronger bot is worth far more than beating a weaker one, so the leaderboard
  tracks who you beat rather than how many times you played. The rating is
  **Glicko-2**, which carries two numbers alongside it: a **deviation** (how sure
  of the rating we are) and a volatility (how erratic the bot has been). How far a
  result moves you depends on both — a settled bot is nudged by a result that
  would swing a newcomer — so there is no step size to tune and no bigger step for
  being new.
- **The standings rank on `rating − deviation`.** A bot with four matches and a
  bot with four hundred can show the same rating without having earned the same
  place, so the table sorts on the rating we are confident a bot is *at least*.
  That number climbs on its own as a bot keeps playing, without the rating moving
  at all. The deviation also **widens while a bot sits idle**, by elapsed time
  rather than by tournaments held — so how often the organizer schedules a run
  never changes how fast an absent bot's rating goes stale.
- **A tournament is one rating period.** Every match in a run is scored against
  the ratings the bots brought into it. Matches play in parallel and finish in any
  order, so this is what makes a run reproducible — and it stops early wins in a
  run from inflating what later wins are worth.
- **The rating counts matches; a second one counts rounds.** `rating` is scored on
  who won the match. A `round rating` beside it (on each bot's own page) is scored
  on the share of rounds won — a finer measurement, but one that compresses as the
  field improves, because nobody can beat a random opponent above half the rounds.
  Titles are read off the match rating only, so the floors keep meaning the same
  thing as the bots get better.
- **Titles are labels, not brackets.** They say how far a bot has come; they
  never decide who it plays. Everybody is always in the same pool, so a bot
  earning a title never leaves anyone else short of opponents.

  | Title | Stored as | Earned by |
  | --- | --- | --- |
  | Novice | `novice` | where every bot starts |
  | Candidate Master | `cm` | peak rating **1600** |
  | Master | `master` | peak rating **1700** |
  | International Master | `im` | peak rating **1900** and **2 norms** |
  | Grandmaster | `gm` | peak rating **2100** and **3 norms** |

  Titles are permanent, chess-style: a bad run costs rating, never a title. The
  top two also need norms, so a rating can outrun the title it reaches — the
  standings show that gap rather than hiding it, because it is what a strong
  newcomer and an inflated rating have in common, and the norms are how you tell
  them apart.
- **Bans are automatic and reversible.** Every time a bot is seen, it's
  re-screened for safety: a dirty file is banned (with the reason recorded) and
  excluded from tournaments; fix it and the new, clean version runs normally.
- **Identical submissions are de-duplicated** — two files with the same contents
  count as one bot.

See your standing (and everyone else's) at any time:

```bash
python3 -m janken bots --db ./janken.db   # id, name, rating, title, W/L/D, held, bans
```

### Holding a bot back

A bot can be **held**: registered, on the record, and not playing. No tournament
fields it until its owner releases it.

```bash
python3 -m janken hold ada --db ./janken.db      # withdraw it from the field
python3 -m janken release ada --db ./janken.db   # put it back, from the next run
```

Either takes a bot id, an unambiguous id prefix, or a name. In the dashboard,
tick **Hold it back** when you upload, and use the buttons under **Your bots**
to release later — you only get them for bots submitted under your own sign-in.

The point is being able to write a bot now and enter it later without anyone
having to take your word for when you wrote it. **A bot's id is the SHA-256 of
its file**, so publishing the id and the upload date is a commitment: release the
bot a month later, the id still matches, and it is provably the same file that
was sitting there all along — it cannot have been written to answer something
that came after it. `janken bots` and the dashboard's **User profile** tab show
held bots with that upload date. The standings leave them out unless you ask for
them with the **Status** filter, and even then they carry no rank — a bot that
isn't playing has no standing.

Holding is deliberate, which makes it the opposite of a ban in every way that
matters, and the registry treats it accordingly:

- it changes **nothing** a bot has earned — rating, title, career record and
  stored file are exactly as they were, waiting;
- it **survives a re-sync**. Re-registering stored files never releases a held
  bot or holds a released one; only its owner moves it;
- it **survives `reset`**, so starting a fresh season doesn't shove somebody's
  unreleased bot into it. Hold what you're staging, then reset, and the staging
  is still there;
- a **ban always wins**. Releasing a bot that fails safety screening doesn't
  field it, and it says so.

`janken run <dir>` respects holds too, so a held bot's file sitting in a scanned
directory still won't play. To test one locally anyway, `--include-held` fields
it — point that at a throwaway `--db`, since it records rated results for a bot
that isn't supposed to have any.

### Hiding a bot for good

Holding is a pause. **Disabling** is for a bot you don't want to look at any
more: it disappears from the standings, the search, **Your bots** and every cup
field. From **Your bots → Details → Hide this bot**, after typing the bot's name
to confirm.

It is one way from where you're standing. A hidden bot is not in your list any
more, so you can't un-hide it — only the organizer can, from the box:

```bash
python3 -m janken bots --disabled --db ./janken.db   # the hidden ones
python3 -m janken enable ada --db ./janken.db        # put one back
python3 -m janken disable ada --db ./janken.db       # or hide one from here
```

**Nothing is deleted.** The rating, the record and the upload date stay on file,
and — the reason this exists rather than a delete button — the matches it played
still count for the bots it played them against. Those bots keep the results they
earned; the hidden bot simply shows as `⊘` in their head-to-head and match log
instead of vanishing out of their record.

That is what separates it from `janken remove`, which deletes the bot *and its
matches*, quietly rewriting the head-to-head of every opponent it ever had. Reach
for `remove` only for something that should never have been registered at all.

Disabling frees a slot in the five-bot cap, and enabling refuses if it would put
you back over — otherwise hiding a bot would be the way around the limit.

### Five bots in the field each

**One entrant may have five bots playing at a time.** Held, banned and disabled
bots don't count, and there is no limit on how many you may upload — the cap is
on how much of the field any one person occupies, which is also what decides how
many of the [cup's](#the-cup-a-separate-competition) qualifying places they can
take.

Going over doesn't refuse the upload. A sixth bot **registers held**, with the
same id, the same upload date and the same starting rating as any other; the
dashboard says so, and the bot waits under **Your bots** until you hold one of
the five to make room. Refusing would push people to delete a bot to free a slot,
throwing away a rating and a career record over a queueing problem.

The limit is enforced in the registry, so both the dashboard and the CLI obey it:
`janken release` refuses a release that would put you over and names what to hold
first. Two things are deliberately exempt:

- **Bots with no submitter** — the reference bots, and anything `janken run`
  registers from a scanned directory. That is not a person, so it isn't a
  person's field.
- **Bots already playing.** Re-syncing stored files never withdraws one to fit
  the cap, so a registry that predates the limit sits above it until its owner
  picks. `janken bots` prints who is over.

Change the number in one place — `ACTIVE_PER_SUBMITTER` in `janken/registry.py`.

### Safety screening

Every submission is statically scanned (`janken/safety.py`) before it runs,
and bots that fail are excluded from the tournament. Besides the import
allowlist above, the scan rejects:

- file access (`open`)
- dynamic code execution (`eval`, `exec`, `compile`, `__import__`)
- introspection escapes (`getattr`/`setattr`, `globals`/`locals`/`vars`,
  dunder attribute tricks like `().__class__.__bases__`)
- dunder names hidden inside string literals — the
  `operator.attrgetter("__globals__")` and `"{0.__class__}".format(...)`
  escapes the plain attribute check can't see (`operator` is off the allowlist
  for the same reason)
- `input()`, `breakpoint()`, `help()`, `exit()`/`quit()`

The scan runs three times: in `check`, before the tournament, and inside the
bot's own wrapper process as a backstop.

### Sandbox

Static screening is a tripwire, not a jail — a bot that finds an unscreened
trick can still reach code execution. So every bot subprocess also runs under
[bubblewrap](https://github.com/containers/bubblewrap) when it's available:

- **no network** — an escaped bot can't phone home or reach the EC2
  instance-metadata endpoint;
- **read-only filesystem** — it can't tamper with `janken.db` or the code;
- **home masked** — SSH keys, cloud credentials, and rival submissions aren't
  there; only the interpreter, the `janken` package, and the bot's own file are
  re-exposed, read-only;
- **throwaway tmpfs cwd, cleared environment** — no `AWS_*` leakage.

CPU/memory rlimits and per-round timeouts still apply on top. `install.sh`
installs bwrap and, by default, sets `JANKEN_REQUIRE_SANDBOX=1` on the services
so they refuse to run bots when the sandbox isn't working — fail loud rather
than silently run untrusted code unsandboxed. Where bubblewrap is missing or
unprivileged user namespaces are disabled, set `JANKEN_REQUIRE_SANDBOX=0` to
accept the (less safe) unsandboxed fallback, or `JANKEN_NO_SANDBOX=1` to force
the sandbox off for debugging.

## Running the tournament (organizer)

Drop every submission into `bots/` (or any directory), then:

```bash
python3 -m janken run bots/ --db ./janken.db --rounds 200 --matches 3 --html report.html -v
```

Running a tournament also registers every bot, records the results, re-rates the
field, and awards any title the new ratings earn — expect `TITLE: ...` lines when
a bot's peak rating crosses 1600, 1700, 1900 or 2100. The standings print each bot's
rating and the move this run made (`+/-`).

- `--matches N` plays each pairing N times (smooths out randomness).
- `--games N` swaps full round-robin for a **balanced random schedule** —
  each bot plays ~N games against random opponents. That's O(n) matches instead
  of O(n²), and with enough games it ranks about as fairly; use it for large
  fields (see [Scaling](#scaling)).
- `--workers W` sets how many matches run in parallel (default: CPU count).
- `--league {novice,cm,master,im,gm,all}` fields only bots holding that title.
  Default `all` — one shared pool is the point, so reach for this only for a
  one-off exhibition run.
- `--db ./janken.db` picks the registry file (created on first use).
- `--html report.html` writes a self-contained, shareable leaderboard page,
  including ratings, title badges and any banned bots.
- `--json results.json` dumps raw results for your own analysis.
- `-v` prints every match as it finishes.

The example bots in `bots/` (rocky, randy, copycat, brainy) can stay in for
flavor or be moved out before the real event — with a rating they are useful to leave
in, since beating a weak bot is worth almost nothing and they anchor the bottom
of the scale. They also calibrate the ladder: they never change, so how a
submission fares against them is the closest thing to an absolute reading in a
rating that is otherwise relative to whoever showed up.

### The scheduled step

The periodic service (`scripts/run_tournament.sh`, see
[Deploying on a server](#deploying-on-a-server-ec2)) runs one command, a no-op
when there is nothing to do:

```bash
python3 -m janken tournament --db ./janken.db   # field the registry, re-rate everyone
```

Participants never press a button: a bot is in the pool from the moment it
registers, and every run re-rates the whole field and awards any title the new
peak ratings earn. `JANKEN_ROUNDS`, `JANKEN_MATCHES`, `JANKEN_GAMES` and
`JANKEN_WORKERS` tune the schedule; see the script.

### Announcements

The one thing on the dashboard that no match implies. Both pages — the registry
and the cup broadcast — carry a banner above the fold, and it is written from a
terminal:

```bash
python3 -m janken announce "Tournaments are paused due to maintenance"
python3 -m janken announce "Cup starts at 17:00" --for 90min
python3 -m janken announce --list      # what is up, and what recently came down
python3 -m janken announce --clear     # take everything down
python3 -m janken announce --clear-id 3
```

Every announcement looks the same: one lilac card directly under the header, on
both pages. There are deliberately no severity levels — a channel used a handful
of times a season does not need three of them, and how urgent a thing is belongs
in the words. The browser tab stays the cup's; a banner that also took it would be
shouting from a background window about something the page says perfectly well.

`--for` takes a duration with a *unit* — `90s`, `30min`,
`2h`, `1d` — and a bare number is refused rather than guessed, because systemd
reads one as seconds and most people mean minutes. Without `--for`, a message
stays up until it is cleared.

Several announcements can be up at once, so posting never silently replaces a
standing notice with a passing one; the banner shows them newest first. Clearing
is soft — a cleared message keeps its text and gains the time it came down, so
"what did the banner say during the final?" has an answer. Expiry is applied when
the page reads, so nothing has to be running for a message to come down on time.

There is deliberately no way to post one from the dashboard. Like the cup's
controls, this belongs to whoever is running the championship from a terminal;
a broadcast anyone signed in could write to is a different feature.

### Starting a fresh season

To wipe the standings without losing the field — the run-up to a real launch, or
a new season after one:

```bash
python3 -m janken reset --db ./janken.db        # asks for confirmation
python3 -m janken reset --db ./janken.db --yes  # for scripts
```

Ratings go back to 1500, titles back to Novice, career records are cleared, and
the match log is deleted. **The bots themselves are
kept** — identity, name, submitter, stored file, any safety ban and any
[hold](#holding-a-bot-back) all survive, so nobody has to resubmit and the next
scheduled run simply re-earns everything. A held bot stays held, so a reset
never enters somebody's unreleased bot into the new season on their behalf.
Without a terminal to confirm at, `reset` refuses unless you pass `--yes`.

Run the engine's own tests with:

```bash
python3 -m unittest discover tests
```

## The dashboard (optional, interactive)

The static HTML report is great for sharing. For an interactive view that reads
the registry **live** — sortable standings, per-bot status,
head-to-head, and the full match log — there's an optional Streamlit dashboard.
It's the one dependency that lives outside the core engine, so install it into a
local virtualenv:

```bash
uv sync    # creates the venv and installs Streamlit from pyproject.toml
```

Then launch it against any registry file:

```bash
python3 -m janken dashboard --db ./janken.db        # convenience wrapper
# or directly:
uv run streamlit run app.py -- --db ./janken.db
```

Participants submit through the dashboard's **Submit a bot** form: each upload is
safety-screened, stored as `submissions/<name>-<id>.py` next to the registry DB —
the `<id>` is the content hash, so distinct submissions never clobber each other
— and the scheduled run fields it from the next tournament on. Every panel reads
the registry live, so it always reflects the latest run. The submitter is whoever
is signed in (or a typed-in name when sign-in is off, below).

Standings, Bot profile, User profile and the match log each carry their own
**Submitter** filter, so narrowing one leaves the others where you had them.
Standings keep each bot's real position in the full field, so a filtered list
reads 4, 9, 14 rather than renumbering itself 1, 2, 3 and telling
somebody they're winning. **Your bots** shows how many of your five slots are
[in the field](#five-bots-in-the-field-each).

### Bot pictures

The form takes an **optional image** alongside the `.py`. It shows up beside the
bot in the standings, on the **Bot** and **User profile** tabs, and on the
entrant cards in the cup
broadcast. Bots without one look exactly as they always did — the column and the
cards simply leave the space out rather than reserving it.

It is decoration and nothing else: it plays no part in screening, seeding,
ratings or eligibility, and a picture that will not decode costs you the picture
and never the entry.

- Any PNG / JPEG / WebP, re-encoded on upload to a 256px square with
  transparency preserved. The stored file is one Pillow wrote, so nothing bolted
  onto the original — EXIF, appended data — survives. 256 is sized for the
  champion card, which runs to 7rem and would otherwise upscale on a 2× projector.
- **Upload something that isn't square and you choose how it's squared up:**
  *crop the edges* (fills the square, right for a photo) or *fit the whole image*
  (shrinks it in and pads with transparency, right for a wide logo you don't want
  cut in half). Square uploads skip the question — both modes do the same thing.
  The preview shows the exact result before you submit.
- Pictures are stored in the registry's `bot_images` table, keyed by bot id, and
  exported to `static/avatars/<id>.png` for the broadcast to fetch.
- **Changing your bot's code gives you a new bot** (the id is the file's hash),
  so the picture does not follow it over. Re-attach it from **Your bots →
  Details**, which is also where you replace or remove one — or rename a bot —
  without resubmitting.

## The cup (a separate competition)

The league is the ladder: everybody plays everybody, every match moves your rating.
The **cup** is the other thing — a single-elimination knockout, drawn from the
same field, that **never touches a rating**. Lose once and you are out; win it
and your rating is exactly what it was.

That is the whole point of it. Because nothing is at stake on the ladder, the
cup can afford to be unfair in the ways that make an event fun: one bad match
ends a grandmaster's afternoon, and sixteen bots are settled in sixteen matches
where a round-robin would take a hundred and twenty.

The league still matters to it, in one direction — **rating seeds the draw**, so
the strong bots are kept apart until late. Finishing high in the league is what
buys you an easy first round. Qualification and seeding are the league's reward;
the trophy is the cup's.

### What's different from a league match

- **Somebody has to advance.** A match level after the scheduled rounds goes to
  **sudden death** — single rounds, on the same processes so a predictor keeps
  everything it learned, until one bot takes a round outright. If a hundred of
  those all draw, the better seed goes through. There are no draws in a cup.
- **Matches get longer as the field narrows** — one ordinary rated match up to the
  quarter-finals, double in the semis, triple in the final (and in the third-place
  match, which shares the final's round). At the default 200 rounds that is
  200/400/600, and it follows `JANKEN_ROUNDS`, so a cup's early rounds are never
  shorter than a routine league match. A longer match is one where reading
  your opponent beats luck, so the deciding matches are the ones where skill
  counts most. There is time for it: a knockout is only *n* matches.
- **The reference bots sit out**, here and in [norm events](#earning-your-first-title).
  rocky, copycat, brainy and randy exist so the league has a baseline everyone must
  beat. A cup is between the entries people wrote — and randy is a coin flip, which
  over one match wins often enough to take a bracket on luck. `--include-reference`
  puts them back in a cup; nothing puts them into a norm event. A bot counts as one
  of them only if it carries **no submitter**, so naming your own entry `randy.py`
  costs you nothing.
- **A bot that isn't registered can still play**, seeded at 1500 as a guest, and
  is *not* enrolled in the league by taking part.
- **Third place is played for**, by the two beaten semi-finalists, immediately
  before the final. Without that match there is no third place — the two losing
  semi-finalists are genuinely tied.
- Fields that aren't a power of two get a **play-in** among the lowest seeds, so
  the bracket is always full rather than padded with byes.

### Running one

A cup plays out in under a second, which is no use as an event. So it is played
in advance and *released* a round at a time:

```bash
python3 -m janken cup run bots/ --name "Spring Cup"   # draw, play, seal
python3 -m janken cup show                            # Enter reveals each round
python3 -m janken cup list                            # ids and reveal state
python3 -m janken cup end                             # close it out
```

`cup run` prints no scores and no champion — you have to host this thing, and
knowing how it ends makes for a worse show. Pass `--spoil` when something looks
wrong and you need to look. Useful flags: `--limit N` caps the field (top N by
rating qualify) and `--exclude NAME` keeps a bot out. The third-place playoff is
always played.

Because all the play happens before an audience exists, a hanging bot or a silly
bracket is your problem before it is the room's. During the show you are a
director rather than an operator: the pause between rounds lasts exactly as long
as you want to talk over it.

### Watching one

The dashboard grows a **Cup** page. While a cup is on, the browser tab, the
navigation entry and the registry header all say so — the header becomes the
alert rather than growing a banner above it — and nothing auto-redirects, since
somebody who came to check their rating should not have the page yanked away.

The page is a broadcast, not a dashboard: no filters, no search, no tabs. The
bracket fills in between rounds, wide early rounds are a *reveal* of who advanced
rather than played out one by one, and the semi-finals and final get the screen
to themselves one match at a time. Replays are paced by what happened — fast
where nothing is going on, slowing for a lead change or a long streak, and
fast-forwarding once a match is mathematically over.

Each reveal rewrites `static/cup-<hash>.json`, which the page polls, so opening
a round reaches every browser already watching without anyone refreshing. That
needs `enableStaticServing = true` (already set in `.streamlit/config.toml`).
Everyone watching sees the same moment: position is derived from the round's
start time and the server's own clock, so a viewer arriving late — or reloading —
lands on the same frame as the room.

To check the pacing without a browser:

```bash
python3 scripts/preview_pacing.py            # a synthetic 16-bot field
python3 scripts/preview_pacing.py --bots ./bots --db ./janken.db
```

### It cannot move the ladder

Cups live in their own tables (`cups`, `cup_entrants`, `cup_matches`) in the same
file, and nothing in the cup code path writes to `bots` or `matches` — the
guarantee is structural rather than a rule someone has to remember, and there are
tests that assert the registry is byte-identical before and after. Results stay
sealed until you reveal them, so an unrevealed round's transcript is never in
what a viewer's browser receives.

## Sign-in (SSO)

Out of the box the dashboard is **open** — it says so in a banner — which is fine
on your own machine. Point it at anything shared and you'll want the Google
sign-in gate: nothing renders until the visitor signs in with a verified
**@muttdata.ai** Google account, and their address is recorded as the owner of
every bot they submit (no more free-text "your name" field to mistype or borrow).

### 1. Create a Google OAuth client

In the [Google Cloud Console](https://console.cloud.google.com/apis/credentials),
using a project in the **muttdata.ai** organization:

1. **OAuth consent screen** → User type **Internal**. This is the strongest part
   of the setup: an internal app can only be used by accounts in the Mutt Data
   Workspace, so nobody outside can even complete a login.
2. **Credentials → Create credentials → OAuth client ID** → type **Web
   application**.
3. Under **Authorized redirect URIs**, add your app's URL with the
   `/oauth2callback` path — `http://localhost:8501/oauth2callback` for local use.
   It must match `redirect_uri` in the next step character for character. One
   client can hold several, so add the deployed URL (step 4) alongside the
   localhost one and each environment just points at its own.
4. Copy the **client ID** and **client secret**.

### 2. Fill in secrets.toml

```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # cookie_secret
```

Paste the client ID, client secret and cookie secret into
`.streamlit/secrets.toml`, then restart the dashboard. It's git-ignored — keep it
that way. Deleting the file turns sign-in back off.

### 3. Know where it's allowed to run

Google validates redirect URIs strictly, and that decides how you can host this:

- `http://localhost:<port>` — fine. Loopback is the one exemption from the HTTPS
  rule, so local development needs nothing else.
- **A raw IP address, or plain `http://` on any non-loopback host — rejected.**
  So `http://<instance-ip>:6888` *cannot* do Google sign-in. A hostname with a
  real certificate isn't optional here.
- `https://social-project-stg.muttdata.ai` — the deployment target. See below.

### 4. Behind the muttdata.ai load balancer

The dashboard lives at **https://social-project-stg.muttdata.ai**. Its DNS record
and AWS load balancer already exist, and the ALB holds an Amazon-issued wildcard
certificate for `*.muttdata.ai` plus a port-80 → 443 redirect, so **TLS is
solved** — no Caddy, no certbot, nothing to renew. The instance only ever speaks
plain HTTP to the load balancer, which is what `install.sh` sets up on port 6888.

So `redirect_uri` is:

```toml
redirect_uri = "https://social-project-stg.muttdata.ai/oauth2callback"
```

in `secrets.toml` **and** as an authorized redirect URI on the Google client.
What's left is AWS wiring:

1. **Target group** — protocol HTTP, port **6888**, target = the dashboard
   instance. Health check path **`/healthz`** (Streamlit answers 200 there, and
   it stays reachable when sign-in is on, so the ALB never sees the gate).
2. **Listener rule** on the HTTPS listener — host header
   `social-project-stg.muttdata.ai` → that target group. Until a healthy target
   is registered the hostname answers `502`, which is what it does today.
3. **Security group** — allow 6888 on the instance *from the ALB's security
   group only*, not from the internet. Keep `--server.address 0.0.0.0` (the ALB
   connects from another host); the security group is what closes the door.

WebSockets need no special setting: Streamlit's live updates ride the ALB's
native WebSocket support. If you ever run more than one instance behind the
group, enable **stickiness** — a Streamlit session is bound to the process that
started it.

To share the hostname with another app later, serve Janken under a path instead —
a listener rule on `/janken*` plus `--server.baseUrlPath janken`. Everything moves
under the prefix, including both URLs above: `redirect_uri` becomes
`https://social-project-stg.muttdata.ai/janken/oauth2callback` and the health
check path `/janken/healthz`.

Need a gate before the target group is wired up? Keep the dashboard on localhost
on the server and reach it over SSH — `ssh -L 8501:localhost:6888 <server>` with
the localhost redirect URI. Only people with SSH access get in, so it's for
organizers, not participants. (Off AWS entirely, [Caddy](https://caddyserver.com/)
or a [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
would terminate TLS on the box instead, with Streamlit bound to `127.0.0.1`.)

### Settings

| Variable | Effect |
| --- | --- |
| `JANKEN_GOOGLE_CLIENT_ID` | Client id from step 1. Given both this and the secret, `install.sh` writes `secrets.toml` for you. |
| `JANKEN_GOOGLE_CLIENT_SECRET` | Client secret from step 1. |
| `JANKEN_REDIRECT_URI` | The callback URL written into `secrets.toml` — `https://<host>/oauth2callback`. No default; required whenever the installer writes the file. |
| `JANKEN_ALLOWED_DOMAINS` | Domains admitted, comma-separated. Default `muttdata.ai`; e.g. `muttdata.ai,contractor.dev` to let a guest in. |
| `JANKEN_REQUIRE_SSO=1` | Refuse to serve at all if sign-in isn't configured, instead of falling back to open access. Worth setting on any shared box so a missing `secrets.toml` fails loudly rather than quietly. |

The domain check lives in `janken/auth.py` and runs on the returned identity
token, not just in the sign-in URL — the `hd` parameter only *suggests* the Mutt
Data Workspace on Google's account chooser, and a user can ignore it and offer a
personal account. Unverified addresses are refused too, so the gate fails closed.

## Deploying on a server (EC2)

On a fresh instance, one script sets everything up — the Python environment (via
uv, which fetches its own Python if needed), a seeded registry, and two systemd
services: the dashboard and a periodic tournament.

```bash
git clone <this-repo> && cd janken-bot-competition
./install.sh
```

With passwordless sudo (the default for the EC2 login user), that leaves you with:

- the **dashboard** running on port 6888 (`janken-dashboard.service`), reachable
  at `http://<instance-ip>:6888` — open that port in the security group. (6888
  is "MUTT" on a phone keypad — a non-default port keeps drive-by scanners off
  the well-known Streamlit 8501; set another with `JANKEN_PORT`.) Behind a load
  balancer, use whichever port its security group is allowed to reach:
  `JANKEN_PORT=80 ./install.sh`. Ports below 1024 are privileged, so the unit
  gets `AmbientCapabilities=CAP_NET_BIND_SERVICE` — the service still runs as
  your own user, never root.
- a **tournament** that re-runs every 30 minutes (`janken-tournament.timer`),
  fielding every eligible bot from the registry with a balanced random schedule
  (26 games/bot, 2 workers) and refreshing the standings — light enough to run
  all day on a cheap burstable instance (t3.small ≈ $15/mo). No directory scan,
  so display names are never disturbed.

Without credentials this deployment is **open to anyone who can reach the port** —
the installer says so when it finds no `.streamlit/secrets.toml`. Hand it the
[OAuth client](#sign-in-sso) and it writes that file itself (generating the cookie
secret, mode 600) and gates the dashboard:

```bash
JANKEN_GOOGLE_CLIENT_ID=16293660919-....apps.googleusercontent.com \
JANKEN_GOOGLE_CLIENT_SECRET=GOCSPX-... \
JANKEN_REDIRECT_URI=https://social-project-stg.muttdata.ai/oauth2callback \
JANKEN_REQUIRE_SSO=1 \
  ./install.sh
```

All three are required to write the file — the installer has no built-in
hostname, since the callback URL belongs to whichever box you're installing on and
has to match the OAuth client exactly. It rejects a URL that can't work (plain
`http` on a non-loopback host, a trailing slash, a missing `/oauth2callback`)
before writing anything.

An existing `secrets.toml` is never overwritten — rewriting it would rotate
`cookie_secret` and sign everyone out — so re-running the installer is safe, and
editing credentials later means editing that file. `JANKEN_REQUIRE_SSO=1` is what
makes a missing or broken secrets file take the dashboard down loudly instead of
serving it wide open. Google won't accept
`http://<instance-ip>:6888` as a redirect URI, so sign-in requires reaching the
box through `https://social-project-stg.muttdata.ai` as described there — in which
case port 6888 should be open to the load balancer's security group only, never to
the internet.

Defaults are tuned for a small always-on box; override with env vars, e.g.
`JANKEN_INTERVAL=1h JANKEN_GAMES=40 ./install.sh` (`JANKEN_GAMES=0` for a full
round-robin, `JANKEN_WORKERS=N` for a bigger instance). `JANKEN_NO_SERVICE=1
./install.sh` sets up the environment without touching systemd (it just leaves
the units in `deploy/`). No systemd at all? The script prints a cron line for the
tournament and you launch the dashboard with `python3 -m janken dashboard`.

Check on it:

```bash
systemctl status janken-dashboard
systemctl list-timers 'janken*'
journalctl -u janken-tournament --since '1 hour ago'
```

## Scaling

Full round-robin is `n·(n-1)/2` matches — fine for tens of bots, quadratic
beyond that. Two levers keep it in check:

- **Parallelism** — matches are independent and run across `--workers` threads
  (default: CPU count). Each match starts two subprocesses, so on a spawn-bound
  field the speedup is a modest ~2–4× (heavier bots parallelize better).
- **Random schedule** (`--games N`) — each bot plays ~N games instead of
  everyone, so the cost is `n·N/2` matches *regardless of field size*, and with
  enough games the ranking is about as fair. This is the real win at scale.

Rough guide (≈0.05 s/match, light bots):

| Bots | Round-robin | Random, `--games 25` |
|---|---|---|
| 30   | ~440 matches | ~375 matches |
| 300  | ~45k (minutes–hours) | ~3.7k (~1–2 min) |
| 1000 | ~500k (hours) | ~12.5k (~3–5 min) |

So a modest **c7i.2xlarge (8 vCPU)** handles even a thousand bots with random
pairings, where full round-robin would need a far bigger box or an overnight
run. For the periodic service, set `JANKEN_GAMES` (and optionally
`JANKEN_WORKERS`) — `install.sh` bakes them into the tournament unit.

## How the arena works

Each bot runs in its own subprocess with CPU/memory rlimits. A tiny referee
(`janken/match.py`) talks to both over pipes with per-round deadlines, scores
each round, and records forfeits with their reason. Bots never share a process,
can't see each other's code, and stray `print()`s are shunted to stderr so they
can't interfere with the protocol.

```
janken/
├── wrapper.py     # hosts one bot: loads your play(), applies rlimits, speaks the protocol
├── match.py       # referee: 2 subprocesses, deadlines, scoring, forfeits
├── tournament.py  # round-robin / random schedule, per-run standings
├── safety.py      # static screening (import allowlist, banned builtins)
├── registry.py    # SQLite registry: identity, ratings, titles, career stats, bans
├── glicko.py      # Glicko-2: the rating, its deviation, its volatility
├── rating.py      # the chess title ladder, performance ratings, display names
├── report.py      # self-contained HTML leaderboard
├── naming.py      # how a bot is labelled when two share a name
└── __main__.py    # the CLI (run / tournament / reset / bots / hold / release /
                   #          disable / enable / remove / check / dashboard)

app.py             # Streamlit entrypoint: registry page + cup broadcast
dashboard.py       # the registry page (interactive, reads the registry live)
```
