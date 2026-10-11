"""The cup broadcast view: one self-contained page, animated in the browser.

Streamlit is a poor animator — a rerun loop flickers, a fragment tops out
around 1Hz, and a blocking `while` holds a server thread per viewer. So the
whole show is handed to the browser once and driven by requestAnimationFrame,
and Streamlit's only job is to serve the document.

Two rules make that safe:

  * **The page is stateless with respect to mounting.** Position is always
    derived from `round_started_at_ms` and the beats, never accumulated from a
    start event. A reload, a rerun, or a viewer arriving twenty seconds late
    all land on the same frame as everybody else — the worst a remount costs is
    a flicker, never a restart.
  * **The clock is the server's.** The payload carries `server_now_ms` beside
    the round's start, so each page measures its own offset once and corrects.
    Without that, a laptop with a wrong clock quietly watches a different
    moment from the rest of the room.

Colour: the two sides are violet and amber, which is not the obvious choice —
brand blue against violet reads fine to most people but scores ΔE 7.2 under
simulated protanopia (below the 8.0 target), and brand blue sits at 2.84
contrast on navy, under the 3.0 floor. Violet/amber measures 36.6 and 4.86/9.68.
Amber's lightness is above the dark-mode band, which is a deliberate deviation:
the band assumes a mid-dark surface, this is near-black navy seen through
compressed screen-share, and the brand palette has no in-band warm hue.
"""

import json

# Validated on #001237 — see the module docstring.
SIDE_A = "#886AFF"      # --md-violet
SIDE_B = "#F2B03E"      # --md-amber
SPARK = "#00ECFF"       # --md-cyan, the live dot and the winning flash only
NAVY = "#001237"
MIST = "#E1E4ED"

VIEW_HEIGHT = 760

TEMPLATE = r"""
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500&family=DM+Mono:wght@400;500&display=swap');
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
#stage {
  font-family: 'DM Sans', system-ui, sans-serif;
  color: #FFFFFF;
  background-color: __NAVY__;
  background-image:
    radial-gradient(ellipse 55% 60% at 8% -10%, rgba(0,69,251,.55), transparent 70%),
    radial-gradient(ellipse 45% 55% at 98% 10%, rgba(136,106,255,.40), transparent 70%),
    radial-gradient(ellipse 60% 60% at 60% 120%, rgba(8,5,172,.60), transparent 70%);
  border-radius: 28px;
  height: __HEIGHT__px;
  padding: 1.6rem 2rem 1.8rem;
  display: flex; flex-direction: column; gap: 1rem;
  overflow: hidden;
}
.mono { font-family: 'DM Mono', ui-monospace, monospace; }
.topbar { display: flex; align-items: center; justify-content: space-between; gap: 1rem; }
.topbar .left { display: flex; align-items: center; gap: .8rem; }
.kicker {
  font-family: 'DM Mono', monospace; font-size: .72rem; letter-spacing: .2em;
  text-transform: uppercase; color: __MIST__;
}
.dot {
  width: .6rem; height: .6rem; border-radius: 50%; background: __SPARK__;
  box-shadow: 0 0 0 0 rgba(0,236,255,.7); animation: pulse 1.8s infinite;
}
@keyframes pulse {
  0%   { box-shadow: 0 0 0 0 rgba(0,236,255,.6); }
  70%  { box-shadow: 0 0 0 .7rem rgba(0,236,255,0); }
  100% { box-shadow: 0 0 0 0 rgba(0,236,255,0); }
}
.stage-name { font-size: clamp(1.1rem, 2.4vw, 1.7rem); font-weight: 500; letter-spacing: -.02em; }
.body { flex: 1; min-height: 0; display: flex; flex-direction: column; justify-content: center; }

/* --- match panels ------------------------------------------------------- */
.grid { display: grid; gap: .9rem; }
.panel {
  background: rgba(255,255,255,.05); border-radius: 20px; padding: .9rem 1.1rem;
  display: flex; flex-direction: column; gap: .55rem; min-width: 0;
}
.panel.hero { padding: 1.6rem 2rem; gap: 1.1rem; }
.names { display: flex; justify-content: space-between; align-items: baseline; gap: .8rem; }
/* A row, so an optional avatar can sit beside the name without the two of them
   sharing one ellipsis. The name is the part allowed to truncate. */
.name { font-weight: 500; white-space: nowrap; display: flex; align-items: center; min-width: 0; }
.name .who { overflow: hidden; text-overflow: ellipsis; }
.name .seed { color: __MIST__; opacity: .65; font-size: .78em; margin-right: .35em; flex: none; }
.side-a { color: __SIDE_A__; }
.side-b { color: __SIDE_B__; }
.hero .name { font-size: clamp(1.3rem, 3vw, 2.1rem); }

/* --- avatars -------------------------------------------------------------
   Optional, so every rule here has to look deliberate when the picture is
   absent as well as when it is there. Nothing reserves space for a missing
   avatar: the element is simply not created, and the name closes up. An avatar
   is also never the only thing identifying an entrant — the seed and the name
   carry that — which is what lets a broken file be hidden outright. */
.pic {
  width: 1.5em; height: 1.5em; border-radius: 8px; object-fit: cover;
  vertical-align: -.4em; margin-right: .4em; flex: none;
  background: rgba(255,255,255,.10);
}
/* Side B reads right-to-left, so its avatar sits after the name. */
.side-b .pic { margin-right: 0; margin-left: .4em; }
.name.side-b { justify-content: flex-end; }
.hero .pic { border-radius: 12px; }
.tie .pic { width: 1.15em; height: 1.15em; border-radius: 5px; margin-right: .35em; }
.tie div span:first-child { display: flex; align-items: center; min-width: 0; }
.champion .pic {
  width: clamp(4rem, 11vw, 7rem); height: clamp(4rem, 11vw, 7rem);
  border-radius: 24px; margin: 0;
}
.score { display: flex; justify-content: space-between; align-items: center;
         font-family: 'DM Mono', monospace; }
.hero .score { font-size: clamp(1.6rem, 4vw, 2.6rem); }

/* Who is ahead *right now*. Two mirrored bars meeting at a centre read well
   for a rout and badly for 35-33 — comparing a left extent against a right
   extent is the judgement people are worst at, and a margin of two is a couple
   of pixels. The trophy is the scannable answer and moves the instant the lead
   changes; the margin beside it says by how much. */
.crown { opacity: 0; transition: opacity .25s; margin-left: .3em; }
.name.leading .crown { opacity: 1; }
.hero .crown { margin-left: .4em; }
.delta {
  font-size: .78rem; font-weight: 500; letter-spacing: .04em;
  padding: .1rem .5rem; border-radius: 999px;
  background: rgba(255,255,255,.07); white-space: nowrap;
}
.hero .delta { font-size: 1rem; padding: .18rem .8rem; }
.delta.level { color: __MIST__; opacity: .55; }
.name.trailing { opacity: .45; font-weight: 400; }

/* Centre-out difference bar: two categorical fills meeting at the midpoint,
   with a 2px surface gap so adjacent fills never touch. */
.bar { position: relative; height: .5rem; background: rgba(255,255,255,.10); border-radius: 999px; }
.hero .bar { height: .9rem; }
.bar .fill { position: absolute; top: 0; bottom: 0; border-radius: 999px; }
.bar .fill.a { right: calc(50% + 1px); background: __SIDE_A__; }
.bar .fill.b { left: calc(50% + 1px); background: __SIDE_B__; }
.bar .mid { position: absolute; top: -3px; bottom: -3px; left: 50%; width: 1px;
            background: rgba(255,255,255,.35); }

.throw { display: flex; justify-content: center; align-items: center; gap: 2.5rem; font-size: 3.4rem; line-height: 1; }
.strip { display: flex; gap: 2px; height: .7rem; }
.strip i { flex: 1; border-radius: 2px; background: rgba(255,255,255,.14); }
.strip i.a { background: __SIDE_A__; }
.strip i.b { background: __SIDE_B__; }
.caption {
  text-align: center; font-family: 'DM Mono', monospace; font-size: .8rem;
  letter-spacing: .18em; text-transform: uppercase; color: __SPARK__;
  min-height: 1.1rem; opacity: 0; transition: opacity .18s;
}
.caption.on { opacity: 1; }
.lead { width: 100%; height: 90px; display: block; }
.won { color: __SPARK__; }
.dim { opacity: .35; }

/* --- bracket recap ------------------------------------------------------ */
.bracket { display: flex; gap: 1.4rem; justify-content: center; align-items: stretch;
           font-size: .82rem; overflow-x: auto; padding-bottom: .4rem; }
.col { display: flex; flex-direction: column; justify-content: space-around; gap: .35rem; min-width: 8.5rem; }
.col h4 { margin: 0 0 .3rem; font-family: 'DM Mono', monospace; font-size: .66rem;
          letter-spacing: .16em; text-transform: uppercase; color: __MIST__; opacity: .7; }
.tie { background: rgba(255,255,255,.05); border-radius: 10px; padding: .3rem .5rem; }
.tie div { display: flex; justify-content: space-between; gap: .5rem; white-space: nowrap; }
.tie .out { opacity: .35; }
.tie .thru { font-weight: 500; }

/* --- champion ----------------------------------------------------------- */
.champion { text-align: center; display: flex; flex-direction: column; gap: .6rem; align-items: center; }
.champion .who { font-size: clamp(2rem, 6vw, 3.6rem); font-weight: 500; letter-spacing: -.02em; }
.champion .spark { font-size: 2rem; color: __SPARK__; line-height: 1; }

/* The podium. Rank is carried by size and weight rather than by a number, so
   it reads at a glance and in the right order without anyone parsing it —
   which is also why the drop between tiers is steep. */
.podium { display: flex; flex-direction: column; align-items: center; gap: 1.5rem; }
.step { display: flex; flex-direction: column; align-items: center; gap: .3rem; }
.step .kicker { font-size: .66rem; }
.step .who { font-weight: 500; letter-spacing: -.02em; line-height: 1.1; }
.step-1 .who { font-size: clamp(2rem, 6vw, 3.6rem); }
.step-2 .who { font-size: clamp(1.15rem, 3vw, 1.8rem); opacity: .82; }
.step-3 .who { font-size: clamp(.95rem, 2.1vw, 1.25rem); opacity: .62; }
.step-2 .kicker, .step-3 .kicker { opacity: .7; }
.step-1 .pic { width: clamp(4rem, 11vw, 7rem); height: clamp(4rem, 11vw, 7rem);
               border-radius: 24px; margin: 0; }
.step-2 .pic { width: 2.4rem; height: 2.4rem; border-radius: 12px; margin: 0; }
.step-3 .pic { width: 1.7rem; height: 1.7rem; border-radius: 9px; margin: 0; }
.podium .spark { font-size: 2rem; color: __SPARK__; line-height: 1; }
.waiting { text-align: center; color: __MIST__; }
</style>

<div id="stage">
  <div class="topbar">
    <div class="left"><span class="dot" id="dot"></span><span class="kicker" id="status">—</span></div>
    <div class="stage-name" id="stage-name"></div>
    <div class="kicker" id="progress"></div>
  </div>
  <div class="body" id="body"></div>
</div>

<script>
(function () {
  const GLYPH = { r: "✊", p: "✋", s: "✌️", x: "⁉️" };
  const POLL_MS = 1500;
  const STRIP = 24;

  let payload = __PAYLOAD__;
  const pollUrl = __POLL_URL__;
  let signature = "";                 // what the DOM was last built for
  let nodes = null;

  // Offset between the server's clock and ours, measured ONCE against the
  // payload Streamlit rendered into this page — which was stamped as it was
  // served, so the difference really is our clock error.
  //
  // It must never be re-measured from a polled payload: static/cup.json is a
  // file, and its server_now_ms is frozen at the moment the operator revealed
  // the round. Adopting that as "now" makes now() return the reveal time on
  // every frame, elapsed pins to zero, and the match sits on round three
  // forever. Poll for *what* to show; never for what time it is.
  const skew = payload.server_now_ms ? payload.server_now_ms - Date.now() : 0;

  function now() { return Date.now() + skew; }

  // The show only ever moves forward. Streamlit renders a payload read live
  // from the database; the poll reads a file, which can lag behind it — an
  // operator who forgot --payload, a write that has not landed yet. Adopting
  // that unconditionally drags a finished cup back to round one a second and
  // a half after it loads, which looks exactly like the page being broken.
  function ahead(next) {
    if (!next || !next.cup_id) return false;
    if (next.cup_id !== payload.cup_id) return true;         // a different cup
    if (next.revealed_rounds !== payload.revealed_rounds)
      return next.revealed_rounds > payload.revealed_rounds;
    return (next.round_started_at_ms || 0) >= (payload.round_started_at_ms || 0);
  }
  function adopt(next) { if (ahead(next)) payload = next; }

  // --- reading a transcript (mirrors janken/broadcast.py) -------------------
  function outcome(a, b) {
    const va = "rps".includes(a), vb = "rps".includes(b);
    if (va && vb) {
      if (a === b) return 0;
      const beats = { r: "s", p: "r", s: "p" };
      return beats[a] === b ? 1 : -1;
    }
    if (va) return 1;
    if (vb) return -1;
    return 0;
  }

  function prepare(match) {
    if (match._ready) return match;
    const pairs = [];
    for (let i = 0; i + 1 < match.moves.length; i += 2)
      pairs.push([match.moves[i], match.moves[i + 1]]);
    const diff = [], wins = [];
    let d = 0, wa = 0, wb = 0;
    for (const [x, y] of pairs) {
      const got = outcome(x, y);
      d += got;
      if (got > 0) wa++; else if (got < 0) wb++;
      diff.push(d); wins.push([wa, wb]);
    }
    // Cumulative beat time, so a position is a lookup rather than a replay.
    const stops = [0];
    for (const [count, ms] of match.beats)
      for (let i = 0; i < count; i++) stops.push(stops[stops.length - 1] + ms);
    match._pairs = pairs; match._diff = diff; match._wins = wins;
    match._stops = stops; match._ready = true;
    match._marks = Object.fromEntries(match.markers.map(m => [m.round, m.text]));
    return match;
  }

  function frameOf(match, elapsed) {
    const stops = match._stops;
    if (elapsed <= 0) return -1;
    let lo = 0, hi = stops.length - 1;
    while (lo < hi) {                       // last stop <= elapsed
      const mid = (lo + hi + 1) >> 1;
      if (stops[mid] <= elapsed) lo = mid; else hi = mid - 1;
    }
    return Math.min(lo, match._pairs.length) - 1;
  }

  // --- geometry ------------------------------------------------------------
  function currentRound() {
    const n = payload.revealed_rounds;
    return n > 0 ? payload.script.rounds[n - 1] : null;
  }

  function elapsedInRound() {
    if (!payload.round_started_at_ms) return 0;
    return now() - payload.round_started_at_ms;
  }

  function phase() {
    if (payload.revealed_rounds === 0) return "draw";
    const round = currentRound();
    const play = round.duration_ms - round.interstitial_ms;
    if (elapsedInRound() < play) return "play";
    const done = payload.revealed_rounds >= payload.total_rounds;
    return done ? "champion" : "recap";
  }

  // --- building ------------------------------------------------------------
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  // An entrant's avatar, or null if it has none. `image` is a path relative to
  // the static directory; the prefix is this page's to add, because the payload
  // is also written by the CLI, which does not know one.
  //
  // A file that 404s — static/ wiped by a deploy, say — removes the element
  // rather than leaving a browser's broken-image glyph on a projector. The
  // layout does not depend on it: a bracket with no pictures at all is the
  // bracket as it was before avatars existed.
  const avatar = (entrant) => {
    if (!entrant || !entrant.image) return null;
    const img = document.createElement("img");
    img.className = "pic";
    img.src = "/app/static/" + entrant.image;
    img.alt = "";                              // decorative: the name is the label
    img.loading = "lazy";
    img.onerror = () => img.remove();
    return img;
  };
  const seeded = (side, entrant) => {
    const n = el("div", "name side-" + side);
    const pic = avatar(entrant);
    // Side B is right-aligned, so its avatar trails the name; CSS mirrors the
    // margin to match.
    if (pic && side === "a") n.appendChild(pic);
    n.appendChild(el("span", "seed", "#" + entrant.seed));
    n.appendChild(el("span", "who", entrant.name));
    n.appendChild(el("span", "crown", "🏆"));   // shown only while leading
    if (pic && side === "b") n.appendChild(pic);
    return n;
  };

  // Mark whoever is ahead by `lead` (positive = side a, 0 = level).
  function showLead(nameA, nameB, delta, lead) {
    nameA.classList.toggle("leading", lead > 0);
    nameB.classList.toggle("leading", lead < 0);
    nameA.classList.toggle("trailing", lead < 0);
    nameB.classList.toggle("trailing", lead > 0);
    if (!delta) return;
    delta.textContent = lead === 0 ? "level" : "+" + Math.abs(lead);
    delta.className = "delta" + (lead === 0 ? " level" : "");
    delta.style.color = lead === 0 ? "" : (lead > 0 ? "__SIDE_A__" : "__SIDE_B__");
  }

  function buildPanel(match, hero) {
    const panel = el("div", "panel" + (hero ? " hero" : ""));
    const names = el("div", "names");
    const nameA = seeded("a", match.a), nameB = seeded("b", match.b);
    names.append(nameA, nameB);
    const score = el("div", "score");
    const sa = el("span", "side-a", "0"), sb = el("span", "side-b", "0");
    const delta = el("span", "delta level", "level");
    score.append(sa, delta, sb);
    const bar = el("div", "bar");
    const fa = el("div", "fill a"), fb = el("div", "fill b");
    bar.append(fa, fb, el("div", "mid"));
    panel.append(names, score, bar);

    let throwRow = null, lead = null;
    if (hero) {
      throwRow = el("div", "throw");
      throwRow.append(el("span", "", ""), el("span", "", ""));
      lead = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      lead.setAttribute("class", "lead");
      lead.setAttribute("preserveAspectRatio", "none");
      panel.append(throwRow, lead);
    }
    const strip = el("div", "strip");
    for (let i = 0; i < STRIP; i++) strip.appendChild(el("i"));
    const caption = el("div", "caption");
    panel.append(strip, caption);
    return { panel, sa, sb, fa, fb, throwRow, lead, strip, caption, match,
             nameA, nameB, delta, last: -1 };
  }

  // A sequential round gives each match the screen in turn. Which one is on
  // air, and how far into it we are — the hold after a match is part of its
  // slot, so the final score stays up instead of being cut the instant the
  // last beat lands.
  function currentSegment(round, elapsed) {
    let start = 0;
    for (let i = 0; i < round.matches.length; i++) {
      const slot = round.matches[i].duration_ms + round.hold_ms;
      if (elapsed < start + slot || i === round.matches.length - 1)
        return { index: i, into: elapsed - start };
      start += slot;
    }
    return { index: 0, into: elapsed };
  }

  function buildPlay(round, segment) {
    const body = el("div");
    const grid = el("div", "grid");
    const hero = round.treatment === "hero";
    const showing = segment ? [round.matches[segment.index]] : round.matches;
    const n = showing.length;
    grid.style.gridTemplateColumns =
      "repeat(" + (hero ? 1 : n > 4 ? 4 : 2) + ", minmax(0, 1fr))";
    const panels = showing.map(m => {
      const built = buildPanel(prepare(m), hero);
      grid.appendChild(built.panel);
      return built;
    });
    body.appendChild(grid);
    return { root: body, panels };
  }

  function buildReveal(round) {
    const body = el("div");
    const grid = el("div", "grid");
    grid.style.gridTemplateColumns = "repeat(4, minmax(0, 1fr))";
    const cards = round.matches.map(m => {
      const panel = el("div", "panel dim");
      const names = el("div", "names");
      const nameA = seeded("a", m.a), nameB = seeded("b", m.b);
      names.append(nameA, nameB);
      const score = el("div", "score mono");
      score.append(el("span", "", ""), el("span", "", ""));
      panel.append(names, score);
      grid.appendChild(panel);
      return { panel, score, nameA, nameB, match: m };
    });
    body.appendChild(grid);
    return { root: body, cards };
  }

  function buildBracket() {
    const body = el("div");
    const wrap = el("div", "bracket");
    for (const round of payload.script.rounds) {
      const col = el("div", "col");
      col.appendChild(el("h4", "", round.stage));
      for (const m of round.matches) {
        const tie = el("div", "tie");
        for (const side of ["a", "b"]) {
          const who = m[side];
          const row = el("div");
          const through = m.winner && who && m.winner === who.name;
          row.className = m.winner ? (through ? "thru" : "out") : "";
          const label = el("span", "");
          const pic = avatar(who);
          if (pic) label.appendChild(pic);
          label.appendChild(el("span", "who", who ? who.name : "—"));
          row.appendChild(label);
          if (m.winner)
            row.appendChild(el("span", "mono", String(m.score[side === "a" ? 0 : 1])));
          tie.appendChild(row);
        }
        col.appendChild(tie);
      }
      wrap.appendChild(col);
    }
    body.appendChild(wrap);
    return { root: body };
  }

  function buildChampion() {
    const body = el("div");
    const box = el("div", "podium");
    const places = (payload.script.podium || []).length
      ? payload.script.podium
      // A cup that somehow reached the end without a podium still has to name
      // its winner rather than showing an empty screen. The script carries the
      // champion as an entrant — picture and all — so there is nothing to look
      // up; this used to match the champion's name against the draw, which
      // stopped being safe the moment two entrants could share one.
      : [{ place: 1, title: "Champion",
           entrant: payload.script.champion_entrant
                    || { name: payload.champion || "—", image: "" } }];

    places.forEach(spot => {
      const step = el("div", "step step-" + spot.place);
      const pic = avatar(spot.entrant);
      // The spark is the flourish when there is no picture; a portrait this
      // size *is* the flourish, so the two never both appear — and only the
      // winner gets either.
      if (spot.place === 1) step.appendChild(pic || el("div", "spark", "✳"));
      else if (pic) step.appendChild(pic);
      step.appendChild(el("div", "kicker", spot.title));
      step.appendChild(el("div", "who", spot.entrant ? spot.entrant.name : "—"));
      box.appendChild(step);
    });
    body.appendChild(box);
    return { root: body };
  }

  function buildWaiting() {
    const body = el("div");
    const box = el("div", "waiting");
    box.appendChild(el("div", "kicker", "The draw"));
    body.appendChild(box);
    const bracket = buildBracket();
    body.appendChild(bracket.root);
    return { root: body };
  }

  // --- painting ------------------------------------------------------------
  function paintPanel(built, elapsed) {
    const m = built.match;
    const i = frameOf(m, elapsed);
    // Nothing below changes between rounds, and a round lasts 40-380ms — so
    // repainting on all sixty frames a second is sixty times the work for the
    // same pixels. Redraw when the round advances, not when the clock ticks.
    if (i < 0 || i === built.last) return;
    built.last = i;
    const [wa, wb] = m._wins[i];
    built.sa.textContent = wa;
    built.sb.textContent = wb;
    // Scaled against the whole match, not against rounds decided so far. The
    // latter makes a 1-0 look like a rout — one round in, the leader owns
    // 100% of decided rounds and the bar slams to full. Against the match
    // length both halves grow outward from the centre as it runs, so the bar
    // shows how far in we are and who is ahead at the same time.
    const span = Math.max(1, m.rounds_played);
    built.fa.style.width = (50 * wa / span) + "%";
    built.fb.style.width = (50 * wb / span) + "%";
    showLead(built.nameA, built.nameB, built.delta, wa - wb);

    if (built.throwRow) {
      built.throwRow.children[0].textContent = GLYPH[m._pairs[i][0]] || "";
      built.throwRow.children[1].textContent = GLYPH[m._pairs[i][1]] || "";
    }
    const start = Math.max(0, i - STRIP + 1);
    for (let k = 0; k < STRIP; k++) {
      const at = start + k;
      const cell = built.strip.children[k];
      if (at > i) { cell.className = ""; continue; }
      const got = outcome(m._pairs[at][0], m._pairs[at][1]);
      cell.className = got > 0 ? "a" : got < 0 ? "b" : "";
    }
    const text = m._marks[i];
    built.caption.textContent = text || "";
    built.caption.className = "caption" + (text ? " on" : "");

    // If the chart could not be built yet (no layout on the frame the panel
    // was inserted), forget this round so the next frame draws it rather than
    // skipping straight past it.
    if (built.lead && !paintLead(built, m, i)) built.last = -1;
  }

  const SVG_NS = "http://www.w3.org/2000/svg";

  function paintLead(built, m, upTo) {
    const svg = built.lead;
    if (!built.leadPath) {
      const w = svg.clientWidth, h = svg.clientHeight || 90;
      if (!w) return false;           // not laid out yet; try again next frame
      // Scaled to the match's own largest lead, computed once. Rescaling to
      // the running maximum made the whole curve jump sideways every time a
      // new high-water mark landed, which reads as the line glitching rather
      // than as the lead growing.
      const span = Math.max(8, Math.max.apply(null, m._diff.map(Math.abs)));
      const step = w / Math.max(1, m._pairs.length - 1);

      const base = document.createElementNS(SVG_NS, "line");
      base.setAttribute("x1", 0); base.setAttribute("x2", w);
      base.setAttribute("y1", h / 2); base.setAttribute("y2", h / 2);
      base.setAttribute("stroke", "rgba(255,255,255,.18)");
      base.setAttribute("stroke-width", "1");
      const path = document.createElementNS(SVG_NS, "path");
      path.setAttribute("fill", "none");
      path.setAttribute("stroke-width", "2");
      path.setAttribute("stroke-linejoin", "round");
      svg.append(base, path);

      built.leadPath = path;
      built.leadPts = m._diff.map((d, i) =>
        (i * step).toFixed(1) + "," + (h / 2 - (d / span) * (h / 2 - 4)).toFixed(1));
    }
    // Two attribute writes, against reparsing the whole SVG from a string.
    built.leadPath.setAttribute("d", "M" + built.leadPts.slice(0, upTo + 1).join("L"));
    built.leadPath.setAttribute("stroke",
      m._diff[upTo] >= 0 ? "__SIDE_A__" : "__SIDE_B__");
    return true;
  }

  // Chart geometry is measured once, so a resized window would leave the line
  // drawn to the old width. Dropping the signature rebuilds the panels, which
  // re-measures everything — and costs nothing, since position comes from the
  // clock rather than from anything the DOM was holding.
  window.addEventListener("resize", () => { signature = ""; });

  function paintReveal(view, round, elapsed) {
    const per = round.reveal_ms || 300;
    view.cards.forEach((card, i) => {
      if (elapsed < (i + 1) * per) return;
      const m = card.match;
      card.panel.className = "panel";
      const [x, y] = card.score.children;
      x.textContent = m.score[0];
      y.textContent = m.score[1];
      // Same trophy, and here it means what it looks like: this one went
      // through. A reveal round is only ever shown after the fact.
      const aWon = m.winner === m.a.name;
      showLead(card.nameA, card.nameB, null, aWon ? 1 : -1);
      (aWon ? card.nameA : card.nameB).classList.add("won");
    });
  }

  // --- loop ----------------------------------------------------------------
  function render() {
    const state = phase();
    const round = currentRound();
    const elapsed = elapsedInRound();
    const segment = (state === "play" && round && round.sequential)
      ? currentSegment(round, elapsed) : null;
    // The match index joins the signature, so moving to the next semi-final
    // rebuilds the panel — a cut, which is what a change of match should be.
    const sig = [payload.cup_id, payload.revealed_rounds, payload.status, state,
                 payload.round_started_at_ms,
                 segment ? segment.index : ""].join("|");
    const body = document.getElementById("body");

    if (sig !== signature) {
      signature = sig;
      body.innerHTML = "";
      if (state === "play")
        nodes = round.treatment === "reveal"
          ? buildReveal(round) : buildPlay(round, segment);
      else if (state === "recap") nodes = buildBracket();
      else if (state === "champion") nodes = buildChampion();
      else nodes = buildWaiting();
      body.appendChild(nodes.root);
    }

    const live = payload.status === "live";
    document.getElementById("dot").style.display = live ? "" : "none";
    document.getElementById("status").textContent =
      live ? "Live · " + payload.name : payload.name;
    // In a sequential round the match on air names the stage, not the round —
    // a third-place playoff and the final share a round but are not the same
    // occasion, and the header has to say which one you are watching.
    const onAir = segment ? round.matches[segment.index] : null;
    document.getElementById("stage-name").textContent =
      state === "champion" ? "Champion"
      : onAir ? onAir.stage
      : round ? round.stage : "The draw";
    document.getElementById("progress").textContent =
      payload.revealed_rounds ? payload.revealed_rounds + " / " + payload.total_rounds : "";

    if (state === "play") {
      if (round.treatment === "reveal") paintReveal(nodes, round, elapsed);
      else if (segment)
        // Clamped, so the hold after the last beat freezes the final score
        // rather than running off the end of the transcript.
        paintPanel(nodes.panels[0],
                   Math.min(segment.into, onAir.duration_ms));
      else nodes.panels.forEach(p => paintPanel(p, elapsed));
    }
    requestAnimationFrame(render);
  }
  requestAnimationFrame(render);

  // Polling is what turns "the operator advanced" into everyone's screen
  // moving. It is deliberately outside the animation: within a round nothing
  // needs the server at all.
  if (pollUrl) {
    setInterval(async () => {
      try {
        const res = await fetch(pollUrl + "?t=" + Date.now(), { cache: "no-store" });
        if (res.ok) adopt(await res.json());
      } catch (e) { /* keep showing what we have */ }
    }, POLL_MS);
  }
})();
</script>
"""


def render(payload: dict, poll_url: str = "", height: int = VIEW_HEIGHT) -> str:
    """The whole broadcast view as one self-contained HTML document.

    Returned already stripped: st.iframe decides what it has been handed by
    inspecting the string, and leading whitespace only makes "is this markup or
    a path?" a harder question than it needs to be.
    """
    return (TEMPLATE.strip()
            .replace("__PAYLOAD__", json.dumps(payload))
            .replace("__POLL_URL__", json.dumps(poll_url or ""))
            .replace("__HEIGHT__", str(height))
            .replace("__NAVY__", NAVY)
            .replace("__MIST__", MIST)
            .replace("__SPARK__", SPARK)
            .replace("__SIDE_A__", SIDE_A)
            .replace("__SIDE_B__", SIDE_B))
