"""Interactive Streamlit dashboard for the Janken bot registry.

Unlike the static HTML report, this reads the SQLite registry *live*: submit a
bot here, let the scheduled run play the tournament, and every panel reflects it
on the next interaction. It shows the standings, per-bot status, head-to-head
results and the full match log — and lets participants upload a bot straight into
the registry.

    uv run streamlit run app.py -- --db path/to/janken.db

Every panel sits behind Google sign-in when `.streamlit/secrets.toml` carries an
`[auth]` section: only verified @muttdata.ai accounts get in, and the signed-in
address becomes the submitter recorded against each bot. Without that section
the dashboard still runs wide open (handy locally) and says so in a banner.

Styling follows the Muttdata brand: light surface for the dense tables, the
navy gradient-blob treatment reserved for the header, the championship mark
paired with the official white wordmark, DM Sans / DM Mono type, brand palette
only.
"""

import argparse
import base64
import html
import io
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import altair as alt
import pandas as pd
import streamlit as st

# Make `janken` importable no matter the working directory Streamlit runs from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from janken import avatar
from janken import glicko
from janken import norms
from janken import rating as elo
from janken.announce import AnnounceStore
from janken.auth import allowed_domains, rejection_reason
from janken.rating import START_RATING
from janken.naming import display_labels, label_for
from janken.registry import (ACTIVE_PER_SUBMITTER, LEAGUES, LEAGUE_RANK, FieldFull,
                             Registry, bot_id_for, short_id)

# --- brand palette (verified against the muttdata-brand skill) --------------
BLUE = "#0045FB"; NAVY = "#001237"; WHITE = "#FFFFFF"; MIST = "#E1E4ED"
VIOLET = "#886AFF"; BLUE_DEEP = "#0805AC"; CYAN = "#00ECFF"
GREEN = "#60D045"; ORANGE = "#FF7300"

REQUIRE_SSO_ENV = "JANKEN_REQUIRE_SSO"  # set to 1 to refuse to serve without SSO
# Sidebar submitter filter: the two entries that aren't somebody's address.
EVERYONE = "Everyone"
UNATTRIBUTED = "— no submitter"
ALL_BOTS = "All bots"
# How often the periodic tournament fires, matching install.sh's JANKEN_INTERVAL
# (which sets OnUnitActiveSec on the timer). Only used to estimate the next run,
# so a wrong value misdates that caption and nothing else.
RUN_EVERY_MIN = int(os.environ.get("JANKEN_INTERVAL_MIN", "30"))
ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
LOGO_ICON = "janken-icon.png"    # 200px mark — header lockup, favicon, app chrome
LOGO_FULL = "janken-logo.png"    # 1398px master, kept for print/slides
MUTT_LOCKUP = "muttdata-logo.svg"                  # positive lockup — st.logo, light chrome
MUTT_WORDMARK_WHITE = "muttdata-white-wordmark.svg"  # negative wordmark — the navy banner


def _asset(name):
    try:
        with open(os.path.join(ASSETS, name), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


@st.cache_data(show_spinner=False)
def _asset_data_uri(name):
    """Inline a binary asset so the header stays one self-contained markdown blob.

    Cached for the life of the server process, so re-exporting an asset needs a
    restart (or ⋮ → Clear caches) before the new pixels show up.
    """
    try:
        with open(os.path.join(ASSETS, name), "rb") as f:
            payload = base64.b64encode(f.read()).decode("ascii")
    except OSError:
        return ""
    return "data:image/png;base64," + payload


def get_db_path():
    """The registry file is fixed for the session (CLI --db or $JANKEN_DB)."""
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=os.environ.get("JANKEN_DB", "janken.db"))
    args, _ = p.parse_known_args(sys.argv[1:])  # streamlit args come after `--`
    return args.db


def db_version(db_path):
    """A value that changes whenever the registry file does.

    Tournaments run in their own process (the systemd timer), so the file
    itself is the only signal the dashboard gets that new results landed.
    Size rides along with the timestamp because coarse-grained filesystems can
    round two writes in the same second to the same mtime.
    """
    s = os.stat(db_path)
    return s.st_mtime_ns, s.st_size


@st.cache_data(show_spinner=False)
def load_tables(db_path, version):
    """Read the registry. `version` busts the cache when the file changes.

    That name must *not* start with an underscore: Streamlit leaves
    underscore-prefixed arguments out of the cache key on purpose, which would
    pin every panel to the first read until the server is restarted.
    """
    con = sqlite3.connect(db_path)
    try:
        # Disabled bots are filtered out at the source, once, rather than in
        # each panel: this frame feeds the standings, the pickers, both
        # profiles and the match log, and a flag enforced panel by panel is a
        # flag the next panel forgets. Nothing downstream has to know.
        # (registry_page opens the Registry first, which migrates the column
        # onto an older file, so it is always there to filter on.)
        bots = pd.read_sql_query("SELECT * FROM bots WHERE disabled = 0", con)
        matches = pd.read_sql_query("SELECT * FROM matches", con)
    finally:
        con.close()
    return bots, matches


@st.cache_data(show_spinner=False)
def load_everyone(db_path, version):
    """Name, owner and state for every bot — the hidden ones included.

    `load_tables` drops disabled bots, which is right for every panel that
    *lists* bots. It is wrong for the two that describe a bot's record: a
    disabled bot is still in its opponents' head-to-head and match log, and
    those matches still count. Without this the opponent column would fall back
    to a bare id, so hiding one bot would put a hex string in somebody else's
    table.

    Rating and title come along because the head-to-head has columns for them:
    a hidden bot's rating is a fact about a match that was played, and leaving it
    blank there mixed "—" into an integer column, which Arrow refuses to
    serialise — so hiding one bot broke the table on somebody else's profile.
    """
    con = sqlite3.connect(db_path)
    try:
        return pd.read_sql_query(
            """SELECT id, name, submitter, held, banned, disabled, rating,
                      league FROM bots""", con)
    finally:
        con.close()


@st.cache_data(show_spinner=False)
def load_history(db_path, version):
    con = sqlite3.connect(db_path)
    try:
        return pd.read_sql_query("SELECT * FROM rating_history", con)
    finally:
        con.close()


@st.cache_data(show_spinner=False)
def load_norms(db_path, version):
    """Every norm earned, for the profile's audit trail and the standings count.

    Its own read rather than a third DataFrame out of `load_tables`: norms are a
    handful of rows that two panels touch, and a registry with none pays a single
    empty query for them.
    """
    con = sqlite3.connect(db_path)
    try:
        return pd.read_sql_query("SELECT * FROM norms", con)
    except pd.errors.DatabaseError:
        # A registry written before norms existed. The table is created on open,
        # so this only bites a read of a file nothing has opened yet.
        return pd.DataFrame(columns=["bot_id", "played_at", "title",
                                     "performance", "field_avg", "games"])
    finally:
        con.close()


@st.cache_data(show_spinner=False)
def load_avatars(db_path, version):
    """Bot id -> `data:` URI, for the image column in the tables.

    Kept out of `load_tables` deliberately. That one is read by every panel on
    every interaction and its result is a pair of DataFrames the whole page
    passes around; avatars are a few dozen PNGs that only two tables draw, so
    they get their own read and their own cache entry off the same version key.
    A registry with no avatars pays nothing for this.
    """
    with Registry(db_path) as reg:
        return {bot_id: avatar.data_uri(png)
                for bot_id, png in reg.images().items()}


# --- sign-in gate -----------------------------------------------------------
def sso_configured():
    """True when secrets.toml carries an [auth] section to log in against."""
    try:
        return "auth" in st.secrets
    except Exception:       # no secrets.toml on disk at all
        return False


def sso_provider():
    """The `st.login()` argument: "google" if [auth.google] exists, else default."""
    try:
        return "google" if "google" in st.secrets["auth"] else None
    except Exception:
        return None


def current_user():
    """The signed-in identity, or None when SSO isn't configured.

    Stops the script for anyone who isn't a verified account on an allowed
    domain, so every panel below this call can assume the visitor belongs here.
    """
    if not sso_configured():
        if os.environ.get(REQUIRE_SSO_ENV) == "1":
            st.error(
                "**Sign-in is required but not configured.** Add an `[auth]` "
                "section to `.streamlit/secrets.toml` (see the SSO section of "
                "the README), or unset `%s` to run without it." % REQUIRE_SSO_ENV
            )
            st.stop()
        st.warning(
            "**Sign-in isn't configured** — anyone who can reach this URL can "
            "submit bots. See the SSO section of the README to switch it on."
        )
        return None

    if not st.user.is_logged_in:
        _sign_in_screen()
        st.stop()

    reason = rejection_reason(st.user.get("email"),
                              verified=st.user.get("email_verified"))
    if reason:
        # Signed in with Google, but not as someone this registry admits.
        st.error("⛔ Sorry — %s." % reason)
        if st.button("Sign in with a different account"):
            st.logout()
        st.stop()

    return {"email": st.user["email"].strip().lower(),
            "name": (st.user.get("name") or "").strip()}


def _sign_in_screen():
    _, mid, _ = st.columns([1, 2, 1])
    with mid:
        st.subheader("Sign in to continue")
        st.markdown(
            "The Janken registry is open to %s Google accounts — sign in to see "
            "the standings and submit a bot."
            % " or ".join("**@%s**" % d for d in allowed_domains())
        )
        if st.button("Sign in with Google", type="primary"):
            st.login(sso_provider())


# --- styling ----------------------------------------------------------------
def inject_css():
    st.markdown(
        """
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500&family=DM+Mono:wght@400;500&display=swap');
html, body, [class*="css"], .stApp { font-family: 'DM Sans', system-ui, sans-serif; }
.md-mono { font-family: 'DM Mono', ui-monospace, monospace; }

/* Compact header: brand signal without consuming the working viewport. */
.md-header {
  border-radius: 12px; padding: .85rem 1rem; margin-bottom: .75rem;
  background-color: #001237;
  background-image:
    radial-gradient(ellipse 55% 60% at 8% -10%, rgba(0,69,251,.55), transparent 70%),
    radial-gradient(ellipse 45% 55% at 98% 10%, rgba(136,106,255,.40), transparent 70%),
    radial-gradient(ellipse 60% 60% at 60% 120%, rgba(8,5,172,.60), transparent 70%);
  display: flex; align-items: center; justify-content: space-between; gap: 1rem;
  flex-wrap: wrap;
}
/* The championship mark, locked up with the title. */
.md-header .head-main { display: flex; align-items: center; gap: .8rem; }
.md-header .mark { flex: 0 0 auto; width: 44px; }
.md-header .mark img { width: 100%; height: auto; display: block; }
.md-header .kicker {
  font-family: 'DM Mono', monospace; font-size: .68rem; letter-spacing: .12em;
  text-transform: uppercase; color: #E1E4ED; margin: 0 0 .05rem;
}
.md-header h1 {
  color: #FFFFFF; font-weight: 500; letter-spacing: 0;
  font-size: 1.15rem; margin: 0; padding: 0;
}
.md-header .wordmark { flex: 0 0 auto; width: 96px; opacity: .92; }
.md-header .wordmark svg { width: 100%; height: auto; display: block; }

/* Live-cup variant of the header. The navy block is already the first thing
   the eye lands on, so it becomes the alert rather than growing a banner
   above it. Cyan is the brand's spark — highlight only, tiny doses — which is
   exactly what a live dot is. CSS only: Streamlit strips <script>, keyframes
   it leaves alone. */
.md-header .live-flag {
  display: inline-flex; align-items: center; gap: .5rem;
  font-family: 'DM Mono', monospace; font-size: .68rem; letter-spacing: .12em;
  text-transform: uppercase; color: #00ECFF; margin: 0 0 .05rem;
}
.md-header .live-dot {
  width: .55rem; height: .55rem; border-radius: 50%; background: #00ECFF;
  animation: md-pulse 1.8s infinite;
}
@keyframes md-pulse {
  0%   { box-shadow: 0 0 0 0 rgba(0,236,255,.6); }
  70%  { box-shadow: 0 0 0 .7rem rgba(0,236,255,0); }
  100% { box-shadow: 0 0 0 0 rgba(0,236,255,0); }
}

/* Announcements. Violet, because the brand documents it for container fills and
   it is the only lilac on a page of navy, blue and white — a colour that appears
   nowhere else is doing the noticing for us. The earlier version reached for
   orange, which the palette reserves for data viz and status "never as a primary
   surface or large area".

   A tint rather than a saturated fill, and that is about hierarchy rather than
   timidity: this sits directly under the navy header, and two saturated blocks
   stacked read as a stack of bars. The header stays the heaviest thing on the
   page; the banner is the first thing *in* it. The solid rule carries the colour
   at full strength where it costs nothing, and 12px matches the header's radius
   so the two read as siblings rather than a masthead with a card stuck to it. */
.md-announce {
  border-radius: 12px; padding: .75rem 1rem; margin: 0 0 .6rem;
  background: #F1EDFF; border: 1px solid #d9d0ff;
  border-left: 4px solid #886AFF;
}
.md-announce + .md-announce { margin-top: -.25rem; }
.md-announce .body {
  color: #001237; font-size: 1rem; font-weight: 500; line-height: 1.4;
}
.md-announce .meta {
  font-family: 'DM Mono', monospace; font-size: .66rem; letter-spacing: .1em;
  text-transform: uppercase; color: #6552b0; margin: .25rem 0 0;
}
/* On the cup page the surface is navy, so the card inverts rather than punching a
   pale hole in a broadcast. Violet against navy is the pairing the brand names
   outright, so the accent survives the switch. */
.md-announce.on-navy {
  background: rgba(136,106,255,.20); border-color: rgba(136,106,255,.55);
  border-left-color: #886AFF;
}
.md-announce.on-navy .body { color: #FFFFFF; }
.md-announce.on-navy .meta { color: #E1E4ED; }

[data-testid="stMetricValue"] { font-family:'DM Mono', monospace; color:#001237; }
</style>
""",
        unsafe_allow_html=True,
    )


def brand_chrome():
    """Streamlit's own logo slot: Muttdata in the sidebar, the championship mark up top."""
    lockup = os.path.join(ASSETS, MUTT_LOCKUP)
    icon = os.path.join(ASSETS, LOGO_ICON)
    if not os.path.exists(lockup):
        return
    kwargs = {"size": "large", "link": "https://muttdata.ai"}
    if os.path.exists(icon):
        kwargs["icon_image"] = icon   # the mark Streamlit shows in the app's top-left
    st.logo(lockup, **kwargs)


def header(cup=None):
    """The championship header, or its live-cup variant.

    `cup` is a CupState or None. When a cup is on, the kicker becomes the
    alert — same block, same position, so it cannot be missed and costs no
    extra real estate.
    """
    wordmark = _asset(MUTT_WORDMARK_WHITE)
    # The shipped wordmark's viewBox (775 478 920 122) crops its own glyphs —
    # the "M" starts at x=685.8 and the letters span y=456..622. Widen it to
    # the true path bounds so nothing is trimmed.
    wordmark = wordmark.replace(
        'viewBox="775 478 920 122"', 'viewBox="680 450 1020 178"')
    endorsement = ('<div class="wordmark">%s</div>' % wordmark) if wordmark else ""
    logo = _asset_data_uri(LOGO_ICON)
    mark = ('<div class="mark"><img src="%s" alt="Janken Bot Championship"></div>'
            % logo) if logo else ""
    status = cup.status() if cup else None
    if status == "live":
        kicker = ('<p class="live-flag"><span class="live-dot"></span>'
                  'Live · %s</p>' % html.escape(cup.name))
        title = "Round %d of %d is on air" % (cup.revealed_rounds,
                                              cup.total_rounds)
    elif status == "ready":
        kicker = ('<p class="live-flag"><span class="live-dot"></span>'
                  '%s · about to start</p>' % html.escape(cup.name))
        title = "The draw is up"
    else:
        kicker = '<p class="kicker">Bot Championship</p>'
        title = "Janken Registry Dashboard"

    st.markdown(
        '<div class="md-header"><div class="head-main">%s<div>'
        '%s<h1>%s</h1>'
        '</div></div>%s</div>' % (mark, kicker, title, endorsement),
        unsafe_allow_html=True,
    )


# --- announcements ----------------------------------------------------------
def load_announcements(db_path):
    """Live announcements, newest first. Never raises.

    Uncached, and so it takes no `version` argument — unlike every other loader
    here. That key is the registry file's mtime, and while posting does change
    the file, so does every tournament; a message whose whole purpose is to say
    "we are paused *right now*" is the one read that must not be a moment stale.
    It is one indexed query over a table with a handful of rows.

    Not taking the argument is the point rather than a saving. It first took one
    to match the loaders above and ignored it, which meant computing an mtime
    that nothing used — and `os.stat` on a registry that does not exist yet threw
    from inside the banner, where a missing file should read as "no announcements".
    """
    try:
        with AnnounceStore(db_path) as store:
            return store.live()
    except Exception:
        # A dashboard that cannot read the banner is still a dashboard — the same
        # posture app.py takes for the cup.
        return []


def announcements(db_path, on_navy=False):
    """Render the banner. Returns how many announcements were shown.

    `html.escape` on the message is not optional: this renders through
    `unsafe_allow_html`, so an announcement containing a bracket would otherwise
    be markup. The message reaches here from an operator's terminal, which makes
    it trusted-ish and beside the point — the escape costs nothing and removes
    the question.
    """
    live = load_announcements(db_path)
    classes = "md-announce on-navy" if on_navy else "md-announce"
    for a in live:
        window = ""
        if a.expires_at:
            window = " · until %s" % html.escape(a.expires_at[11:16] + " UTC")
        # No label above the message. The card's colour already says what it is,
        # and "ANNOUNCEMENT" over the top of an announcement is a word doing no
        # work — the meta line says who and when, which is the part you cannot see.
        st.markdown(
            '<div class="%s"><div class="body">%s</div>'
            '<p class="meta">%s%s</p></div>'
            % (classes, html.escape(a.message),
               html.escape(a.attribution), window),
            unsafe_allow_html=True)
    return len(live)


# --- instructions & submission ---------------------------------------------
def how_it_works():
    with st.expander("📖 How the championship works", expanded=False):
        st.markdown(
            """
**1. Write a bot.** One Python file defining:
```python
def play(my_moves, opp_moves):
    # your past moves and the opponent's, oldest first (both empty on round 1)
    return "rock"  # or "paper" / "scissors"
```
Bots may import only from the [standard-library allowlist](https://github.com/MuttData/janken-bot-tournament#rules-of-engagement) and get ~1 second per move.

**2. Submit it.** Upload the `.py` file below. It is **safety-screened** (no file
access, no dynamic `exec`, stdlib imports only). Pass → registered and rated
**%(start)d**; fail → **banned**, with the reason shown. You can add an
**optional picture**, shown beside your bot in the standings and in the cup
bracket — decoration only, and it changes nothing about how your bot is judged.

**3. Tournaments → rating.** The scheduled run fields everybody in one pool, and
every match moves your **rating**: beating a strong bot is worth far more than
beating a weak one. The rating comes with a **deviation** — how sure of it we are
— which shrinks as you play and widens while you sit idle, and the table ranks on
the rating *minus* that, so a hot start has to be backed up before it counts.
There is nothing to click and nothing to enter — your bot is in from the moment it
registers. The reference bots (**rocky**, **copycat**, **brainy**, **randy**)
play too, so how you handle them shows up in the head-to-head grid like any other
result.

**4. Rating → titles.** A peak rating of %(floors)s. The first two come from
rating alone. The top two also need **norms** — %(norms)s — which are qualifying
performances in a *norm event*: a selective tournament whose field has to span
several different submitters and be mostly titled already. That is the difference
between a rating and a title. A rating says how well you did; a norm says who you
did it against, and only the second one is expensive to fake.

Titles are labels you keep for good — a bad run costs rating, never a title — and
they never decide who you play, so nobody is left short of opponents by climbing.

A bot's identity is the hash of its file: edit the code and it's a *new* bot with
a fresh rating; rename the file and it keeps its record.

📄 **Full rules, the exact import allowlist, and example bots:**
[github.com/MuttData/janken-bot-tournament](https://github.com/MuttData/janken-bot-tournament)
"""
            % {
                "start": START_RATING,
                "floors": ", ".join(
                    "**%d** makes you a %s" % (floor, elo.name(title))
                    for title, floor in reversed(elo.RATING_TITLES)),
                "norms": " and ".join(
                    "%d for %s" % (elo.norms_required(title), elo.name(title))
                    for title in ("im", "gm")),
            }
        )


def field_room(db_path, submitter):
    """(bots in the field, limit) for this submitter, or None for nobody.

    Read straight from the registry rather than the cached table: it decides
    what the very next click does, and being one interaction stale would make
    the page promise a slot that isn't there.
    """
    if not (submitter or "").strip():
        return None
    with Registry(db_path) as reg:
        return reg.active_count(submitter), ACTIVE_PER_SUBMITTER


def submit_bot(db_path, user=None):
    with st.expander("📤 Submit a bot", expanded=False):
        if user:
            # Identity comes from the sign-in token, so there's nothing to type
            # and nothing to spoof.
            submitter = user["email"]
            st.caption("Submitting as **%s** — recorded as the bot's owner." % submitter)
        else:
            submitter = st.text_input(
                "Your name", key="sub_by", placeholder="e.g. Yisus")
        room = field_room(db_path, submitter)
        full = room is not None and room[0] >= room[1]
        if full:
            st.warning(
                "**Your field is full — %d of %d bots in play.** This upload is "
                "registered and held back rather than refused: nothing is lost "
                "and nothing is entered. Hold one of the %d from **Your bots**, "
                "then release this one." % (room[0], room[1], room[0]))
        elif room is not None:
            st.caption("**%d of %d** bots in the field. Past %d, further uploads "
                       "register held until you make room."
                       % (room[0], room[1], room[1]))
        up = st.file_uploader(
            "Bot file (.py)", type=["py"], key="sub_file",
            help="Must define play(my_moves, opp_moves) returning rock/paper/scissors.",
        )
        if up is not None:
            default_name = os.path.splitext(os.path.basename(up.name))[0].title()
            bot_name = st.text_input(
                "Bot name", value=default_name, key="sub_name",
                help="Defaults to the uploaded filename — edit to rename your bot.")
            png, image_error = _avatar_field("sub_image")
            # No checkbox when the field is full: the bot is held either way, and
            # a control that cannot change the outcome is worse than none.
            hold = full or st.checkbox(
                "Hold it back — upload now, release later", key="sub_hold",
                help="Registers the bot without entering it in any tournament. "
                     "Its id is the hash of the file and its upload date is "
                     "recorded, so releasing it later proves it is the same file "
                     "you uploaded today. Release it from Your bots.")
            # Nothing is written or registered until the user clicks Submit.
            if st.button("Submit bot", type="primary"):
                name = bot_name.strip() or default_name
                path = _store_upload(db_path, up.getvalue(), name)
                with Registry(db_path) as reg:
                    sync = reg.sync_paths([path], submitter=submitter.strip(),
                                          name=name, held=hold)
                    info = sync.bots[path]
                    # The picture is decoration and the entry is not, so a
                    # picture that would not decode costs the submitter the
                    # picture and nothing else — the bot is already registered
                    # by the time we get here, and stays registered.
                    if png:
                        reg.set_image(info.id, png)
                st.session_state["submission"] = {
                    "path": path, "id": info.id, "image_error": image_error,
                    "auto_held": bool(sync.auto_held)}
                st.cache_data.clear()  # tables reflect the new bot
                st.rerun()

        sub = st.session_state.get("submission")
        if sub:
            _submission_result(db_path, sub["path"], sub["id"],
                               sub.get("image_error", ""),
                               sub.get("auto_held", False))


def _avatar_field(key, label="Bot image (optional)"):
    """An optional picture uploader. Returns (normalised PNG or None, error).

    Normalising here rather than on submit is what makes the preview honest:
    what is shown is the exact square that will be stored, cropping and all, so
    nobody discovers on the standings page that their logo lost its edges. It
    also puts any complaint about the file next to the file picker instead of
    in the result panel further down.
    """
    up = st.file_uploader(
        label, type=avatar.UPLOAD_TYPES, key=key,
        help="Shown beside your bot in the standings and in the cup bracket. "
             "Cropped to a %dpx square — square art fares best. Purely "
             "decorative: it changes nothing about how your bot plays or "
             "what it is worth." % avatar.SIZE,
    )
    if up is None:
        return None, ""
    # Only worth asking about a picture that is not already square — for a
    # square one the two modes do exactly the same thing, and a control whose
    # options are indistinguishable is just a question nobody can answer.
    mode = avatar.FIT_CROP
    if not _is_square(up.getvalue()):
        mode = st.radio(
            "Make it square by", avatar.FIT_MODES, key=key + "_fit",
            horizontal=True,
            format_func=lambda m: ("Cropping the edges" if m == avatar.FIT_CROP
                                   else "Fitting the whole image"),
            help="Crop fills the square and loses the edges — right for a photo. "
                 "Fit shrinks the whole image in and pads with transparency — "
                 "right for a wide logo you don't want cut in half.")
    try:
        png = avatar.normalize(up.getvalue(), mode)
    except avatar.BadImage as exc:
        st.warning("🖼️ That image %s — submitting without a picture." % exc)
        return None, str(exc)
    st.image(png, width=96, caption="How it will appear")
    return png, ""


def _is_square(raw):
    """True for an already-square upload, False for anything else or unreadable.

    Only ever decides whether to offer the crop/fit choice, so an unreadable
    file answering False is right: it falls through to `normalize`, which is
    where a bad image gets its proper error message.
    """
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as img:
            return img.size[0] == img.size[1]
    except Exception:
        return False


def _store_upload(db_path, raw, name):
    """Write uploaded bytes as submissions/<name>-<id>.py; return the path."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", name) or "bot"
    subs = os.path.join(os.path.dirname(os.path.abspath(db_path)), "submissions")
    os.makedirs(subs, exist_ok=True)
    pending = os.path.join(subs, ".pending.py")
    with open(pending, "wb") as fh:
        fh.write(raw)                    # exact bytes, so the id is stable
    bot_id = bot_id_for(pending)
    path = os.path.join(subs, "%s-%s.py" % (safe, bot_id))
    os.replace(pending, path)
    return path


def _submission_result(db_path, path, bot_id, image_error="", auto_held=False):
    with Registry(db_path) as reg:
        info = reg.get(bot_id)
    if info is None:
        return
    if info.banned:
        st.error("⛔ **%s** failed safety screening — it will be banned:\n\n%s"
                 % (info.name, info.ban_reason))
        return
    if image_error:
        st.warning("🖼️ Registered without a picture — the image %s. Add one "
                   "from **Your bots → Details** whenever you like." % image_error)
    who = (" (by %s)" % info.submitter) if info.submitter else ""
    st.success("✓ **%s**%s registered as `%s` — rating **%d**, title **%s**."
               % (info.name, who, short_id(info.id), round(info.rating),
                  elo.name(info.league)))
    # The receipt is the one screen that shows all 64 characters, because it is
    # the screen whose promise depends on them: re-hash the file, compare, and
    # the timestamp is proved. Prose gets the short form and the digest gets a
    # block of its own with a copy button — inline, it was a wall of hex nobody
    # could select cleanly, and it pushed the rating off the end of the sentence.
    with st.expander("Its full SHA-256 id — the receipt for this upload"):
        st.code(info.id, language=None)
        st.caption(
            "This is the SHA-256 of the file itself, so anyone can re-hash your "
            "submission and get the same string. Keep it if you like: it is what "
            "proves this bot is the one you uploaded on %s, unchanged."
            % info.first_seen[:10])
    if info.held and auto_held:
        st.caption(
            "**Held back automatically — your %d slots in the field were already "
            "taken.** It is fully on the record: uploaded %s under the id above, "
            "which is the SHA-256 of the file, so holding it costs it nothing. "
            "Hold one of your bots in play from **Your bots** and this one can "
            "take its place." % (ACTIVE_PER_SUBMITTER, info.first_seen[:10])
        )
        return
    if info.held:
        st.caption(
            "**Held back — it is on the record but not playing.** Uploaded %s "
            "under the id above, which is the SHA-256 of the file itself: release "
            "it in a week or a month and that id still matches, so nobody can "
            "say it was written to answer a bot that came later. Release it from "
            "the **Your bots** tab whenever you want it in the field."
            % info.first_seen[:10]
        )
        return
    st.caption(
        "**Your bot is already entered.** The organizer's scheduled run fields "
        "every registered bot in one pool, so there is nothing to click here: "
        "the next tournament rates it and the standings move. Titles follow the "
        "rating — %s." % elo.ladder()
    )


# --- panels -----------------------------------------------------------------
def _played_at(matches):
    """UTC datetime of the last tournament, the raw stamp if it won't parse, or
    None if none has ever run. Shared by the last-run and next-run captions."""
    played = matches.loc[matches["kind"] == "tournament", "played_at"]
    if played.empty:
        return None
    stamp = played.max()
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:                       # unexpected format — hand it back raw
        return stamp
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def last_tournament(matches):
    """When the tournament last played, absolute + relative; None if it never has.

    Timestamps are stored UTC-aware; the relative half is what tells you at a
    glance whether the periodic run is still alive.
    """
    when = _played_at(matches)
    if when is None:
        return None
    if isinstance(when, str):
        return when[:16].replace("T", " ")
    minutes = int((datetime.now(timezone.utc) - when).total_seconds() // 60)
    if minutes < 1:
        ago = "just now"
    elif minutes < 60:
        ago = "%d min ago" % minutes
    elif minutes < 60 * 24:
        ago = "%dh %dm ago" % divmod(minutes, 60)
    else:
        ago = "%d days ago" % (minutes // (60 * 24))
    return "%s UTC · %s" % (when.strftime("%Y-%m-%d %H:%M"), ago)


def next_tournament(matches):
    """When the next run is due, absolute + relative; None if it can't be said.

    An *estimate*: the timer fires a fixed interval after the last run
    (install.sh's `OnUnitActiveSec`), so this is last run + that interval. A box
    that slept, a stopped timer or an overrunning run all move the real answer,
    which is why the caption marks it approximate. Past due reads "due now"
    rather than naming a time that has already gone by.
    """
    when = _played_at(matches)
    if when is None or isinstance(when, str):
        return None
    due = when + timedelta(minutes=RUN_EVERY_MIN)
    minutes = int((due - datetime.now(timezone.utc)).total_seconds() // 60)
    if minutes < 0:
        soon = "due now"
    elif minutes < 1:
        soon = "in under a minute"
    elif minutes < 60:
        soon = "in %d min" % minutes
    else:
        soon = "in %dh %dm" % divmod(minutes, 60)
    return "%s UTC · %s" % (due.strftime("%Y-%m-%d %H:%M"), soon)


# Tints per title slug, weakest to strongest — brand palette, light enough to
# keep navy text legible (the exception is GM, which goes navy-on-cyan).
_TITLE_TINT = {
    "novice": "background-color: rgba(225,228,237,.65)",
    "cm": "background-color: rgba(136,106,255,.22)",
    "master": "background-color: rgba(0,69,251,.20); font-weight: 500",
    "im": "background-color: rgba(8,5,172,.28); font-weight: 500",
    "gm": "background-color: rgba(0,18,55,.85); color: #00ECFF; font-weight: 500",
}


_SLUG_BY_ABBR = {elo.abbr(t): t for t in LEAGUES}


def _league_colour(title):
    """Tint a title cell. Keyed by the abbreviation the tables actually show."""
    return _TITLE_TINT.get(_SLUG_BY_ABBR.get(title, title), "")


def submitter_options(bots):
    """The sidebar dropdown's entries: everyone, then each submitter by name.

    Bots with no submitter get their own entry rather than being folded into
    one of the real ones — they are the reference bots plus anything synced
    from the CLI without attribution, which is not a person and must not read
    as one.
    """
    named = sorted({s.strip() for s in bots["submitter"] if s.strip()},
                   key=str.lower)
    unnamed = [UNATTRIBUTED] if (bots["submitter"].str.strip() == "").any() else []
    return [EVERYONE] + named + unnamed


def by_submitter(frame, pick):
    """`frame` narrowed to one submitter. Matched the way the registry counts
    them — case-insensitively, ignoring surrounding space — so the dropdown and
    the field limit can never disagree about whose bot is whose."""
    if pick == EVERYONE:
        return frame
    who = frame["submitter"].str.strip()
    if pick == UNATTRIBUTED:
        return frame[who == ""]
    return frame[who.str.lower() == pick.strip().lower()]


# How a bot that is not simply in the field is marked wherever it is named.
# Glyphs rather than colour, because both tables that show opponents already
# spend colour on something else: green and orange mean won and lost rounds in
# the head-to-head, and orange means an unproven rating in the standings. A
# third meaning for the same two hues would make the tables less legible, not
# more. These three are already the app's vocabulary for the same three states
# in Your bots and the bot profile.
STATE_MARK = {"banned": "⛔", "held": "⏸", "disabled": "⊘"}


def bot_state(row):
    """Which of the four states a bot row is in. Banned wins over held, which
    wins over hidden, matching how the standings already resolve it: a banned
    bot is banned whatever else its owner set."""
    if row.get("banned"):
        return "banned"
    if row.get("held"):
        return "held"
    if row.get("disabled"):
        return "disabled"
    return "in field"


def opponent_labels(everyone):
    """{id: label} for naming a bot inside somebody else's record.

    Marked with its state, because an opponent's record is exactly where "why
    is this bot not in the standings?" gets asked. A hidden bot keeps its name
    here — the matches it played are the other bot's history and deleting it
    from them would be rewriting somebody else's record — but it is marked as
    gone, so the row does not read as a bot you could go and look up.
    """
    if everyone.empty:
        return {}
    labels = display_labels(
        zip(everyone["id"], everyone["name"], everyone["submitter"]))
    marked = {}
    for row in everyone.to_dict("records"):
        state = bot_state(row)
        mark = STATE_MARK.get(state, "")
        label = labels.get(row["id"], "")
        marked[row["id"]] = ("%s %s" % (mark, label)).strip() if mark else label
    return marked


def selected_bot_id(pick):
    """The id a dropdown selection refers to, or "" for the all-bots entry.

    Options *are* ids now. They used to be "name · id" strings the label
    function split back apart, which worked only as long as no name contained
    the separator — and the moment a duplicated name earned a " · ivan" suffix,
    the display string and the id were being packed into one field with the same
    delimiter twice over. Streamlit's format_func exists exactly so that a
    dropdown's value and its label do not have to be the same string.
    """
    if not pick or pick == ALL_BOTS:
        return ""
    return pick


def bot_search_options(bots):
    """Ids for the search dropdown, ordered the way people scan the ladder."""
    if bots.empty:
        return [ALL_BOTS]
    ordered = bots.sort_values(
        ["banned", "held", "rating", "name"], ascending=[True, True, False, True])
    return [ALL_BOTS] + list(ordered["id"])


def bot_labeller(bots):
    """A format_func for the dropdowns: bot id in, display label out.

    Ambiguity is a property of the list, so the labels are computed from
    whatever `bots` holds at this point in the render — a table already
    filtered to one submitter mostly stops needing suffixes, which is the
    point.
    """
    labels = display_labels(zip(bots["id"], bots["name"], bots["submitter"])) \
        if not bots.empty else {}

    def label(option):
        if option == ALL_BOTS:
            return ALL_BOTS
        return label_for(labels, option)

    return label


def _with_avatars(table, ids, avatars):
    """Prepend an image column to `table`, if any bot in it has a picture.

    Returns (table, column_config). Avatars are optional and most fields start
    with none, so an always-present column would mostly be an empty stripe down
    the left of the standings. It appears with the first picture and not before.

    The header is blank: a column of faces does not need to be labelled, and the
    tables are already wide.
    """
    uris = [avatars.get(bot_id, "") for bot_id in ids]
    if not any(uris):
        return table, {}
    table.insert(1 if "#" in table.columns else 0, "avatar", uris)
    return table, {"avatar": st.column_config.ImageColumn(label="", width="small")}


# Held and banned bots carry a null rank, which Streamlit draws as an empty
# cell — the Status column beside it already says why the place is blank.
_RANK_COLUMN = st.column_config.NumberColumn("#", format="%d", width="small")


def norm_counts_by_bot(norms_frame):
    """bot id -> {level: count}, the shape `rating.title_earned` wants."""
    counts = {}
    if norms_frame is None or norms_frame.empty:
        return counts
    for (bot_id, title), n in norms_frame.groupby(["bot_id", "title"]).size().items():
        counts.setdefault(bot_id, {})[title] = int(n)
    return counts


def _norm_progress(peak_rating, league, counts):
    """Short text for the Norms column: what the norms say about this rating.

    Three cases worth distinguishing, and the third is the whole point of the
    column — a rating that reaches a rung its owner has not proved it deserves.
    """
    reached = elo.title_for_rating(peak_rating)
    needed = elo.norms_required(reached)
    if needed == 0:
        # No norm-gated rung in play. Show any norms banked anyway, since they
        # count toward one later.
        held = sum(counts.values())
        return "%d banked" % held if held else "—"
    have = elo.norms_toward(reached, counts)
    if have >= needed:
        return "%d/%d ✓" % (have, needed)
    return "%d/%d %s" % (have, needed, elo.abbr(reached))


def unproven(peak_rating, league, counts):
    """Whether this bot's rating outruns the title it has actually earned.

    The cheap collusion signal: a rating high enough for a norm-gated rung, held
    by a bot that has not posted the performances. Newcomers land here too, which
    is why it is a column and not an accusation.
    """
    reached = elo.title_for_rating(peak_rating)
    if elo.norms_required(reached) == 0:
        return False
    return elo.TITLE_RANK.get(league, 0) < elo.TITLE_RANK[reached]


def ranked_bots(bots, norms_frame=None):
    """`bots` plus the derived columns every panel wants: Status, Rank,
    Round +/-, Norms. Built once per render and passed down — it used to be
    rebuilt by each of the three panels that reads it.
    """
    ranked = bots.copy()
    # Assigned in precedence order rather than per row: `iterrows` rebuilt every
    # row as an object Series just to read two booleans. Banned lands last and
    # so wins over held — a banned bot is banned whatever its owner set.
    status = pd.Series("In field", index=ranked.index, dtype=object)
    status[ranked["held"].astype(bool)] = "Held"
    status[ranked["banned"].astype(bool)] = "Banned"
    ranked["Status"] = status
    # Nullable ints, not "—" strings: a held bot genuinely has no rank, and a
    # column of strings sorts 1, 10, 2 the moment somebody clicks the header.
    # Formatting the blank is the table's job, further down.
    ranked["Rank"] = pd.Series(pd.NA, index=ranked.index, dtype="Int64")
    # Ranked on the rating we are confident a bot is *at least*, not on the
    # estimate itself: a bot with four games and a bot with four hundred can share
    # a rating without deserving the same place. The deviation is not shown here —
    # it belongs in the detail view — but it decides the order.
    ranked["_standing"] = ranked["rating"] - glicko.RANK_K * ranked["rd"]
    active = ranked[ranked["Status"] == "In field"].sort_values(
        ["_standing", "wins", "name"], ascending=[False, False, True])
    ranked.loc[active.index, "Rank"] = list(range(1, len(active) + 1))
    ranked["Round +/-"] = ranked["round_wins"] - ranked["round_losses"]

    counts = norm_counts_by_bot(norms_frame)
    per_bot = [counts.get(i, {}) for i in ranked["id"]]
    ranked["norm_total"] = [sum(c.values()) for c in per_bot]
    ranked["Norms"] = [
        _norm_progress(p, t, c)
        for p, t, c in zip(ranked["peak_rating"], ranked["league"], per_bot)]
    ranked["Unproven"] = [
        unproven(p, t, c)
        for p, t, c in zip(ranked["peak_rating"], ranked["league"], per_bot)]
    return ranked


# Spelled out wherever a record is shown. The three numbers are meaningless
# without it, and the head-to-head table printed them in a different order from the
# standings for a while — same format, two meanings, one screen.
RECORD_ORDER = "wins-draws-losses"


def _record(wins, draws, losses):
    """A career record as one cell, always wins-draws-losses.

    Takes its arguments in the order it prints them, which is the point: the
    head-to-head table used to build this string from (wins, losses, draws) while
    every other caller used (wins, draws, losses), so `1-0-1` meant "one win, one
    draw" in one table and "one win, one loss" in the next.
    """
    return "%d-%d-%d" % (wins, draws, losses)


def _match_win_pct(wins, losses, draws):
    """Share of every match played that was won — the thing the ladder scores.

    Kept beside `_round_win_pct` because the two answer different questions and
    routinely disagree by a wide margin. Rounds say how a bot plays; matches say
    what it got for it, and only the second one moves a rating. A bot can win seven
    matches of nine while losing the round count overall, taking each match narrowly
    and losing the rest heavily — so a round percentage alone reads as a bad run
    when the record was a good one.

    Draws count against, the same way they do for rounds: 4-4-1 is 44%, not 50%.
    That is a choice, not the only one — the rating scores that bot 50% — but the two
    percentages on screen have to mean the same thing by the same rule, or the pair
    is worse than either alone.
    """
    total = wins + losses + draws
    return (100 * wins / total) if total else 0


def _round_win_pct(wins, losses, draws):
    """Share of every round played that was won: 40-10-50 is 40%, not 44.4%.

    `draws` has no default on purpose. It used to, and the two callers drifted
    apart — the profile's headline figure left drawn rounds out while the
    per-opponent column below it counted them, so the same bot read two ways on
    one screen. Making it required means a new caller has to go and find the
    number rather than silently get the other definition.
    """
    total = wins + losses + draws
    return (100 * wins / total) if total else 0


def _round_draws(matches, bot_ids):
    """Rounds drawn by `bot_ids`, summed.

    `bots` counts draws per *match* — the W-D-L record — so the only record of a
    drawn round is `matches.draws`, and only tournament matches feed the
    `round_wins`/`round_losses` this pairs with. Counted once per bot per match,
    so two selected bots that played each other contribute that match twice,
    exactly as their round wins and losses already do.
    """
    tour = matches[matches["kind"] == "tournament"]
    if tour.empty:
        return 0
    ids = set(bot_ids)
    return int(tour.loc[tour["bot_a"].isin(ids), "draws"].sum() +
               tour.loc[tour["bot_b"].isin(ids), "draws"].sum())


def _bot_matches(matches, bot_id):
    bot_id = (bot_id or "").strip()
    if not bot_id:
        return matches.iloc[0:0]
    return matches[(matches["bot_a"] == bot_id) | (matches["bot_b"] == bot_id)]


def _h2h_frame(bots, matches, bot_id, everyone=None):
    tour = matches[matches["kind"] == "tournament"]
    rows = _bot_matches(tour, bot_id)
    # Opponents are described from the full roster, not the visible one: a bot
    # this one played and beat does not stop having been beaten because its
    # owner later hid it, and its rating that day is part of what the record
    # means. Only a bot `janken remove` deleted outright is unknown here.
    roster = everyone if everyone is not None else bots
    by_id = roster.set_index("id")
    labels = opponent_labels(roster)
    stats = {}
    for _, m in rows.iterrows():
        if m["bot_a"] == bot_id:
            opp, wins, losses = m["bot_b"], int(m["wins_a"]), int(m["wins_b"])
        else:
            opp, wins, losses = m["bot_a"], int(m["wins_b"]), int(m["wins_a"])
        entry = stats.setdefault(opp, {
            "matches": 0, "match_wins": 0, "match_draws": 0, "match_losses": 0,
            "round_wins": 0, "round_losses": 0, "draws": 0,
        })
        entry["matches"] += 1
        entry["round_wins"] += wins
        entry["round_losses"] += losses
        entry["draws"] += int(m["draws"])
        if wins > losses:
            entry["match_wins"] += 1
        elif wins < losses:
            entry["match_losses"] += 1
        else:
            entry["match_draws"] += 1

    table_rows = []
    for opp_id, s in stats.items():
        opp = by_id.loc[opp_id] if opp_id in by_id.index else None
        rounds = s["round_wins"] + s["round_losses"] + s["draws"]
        opp_rating = float(opp["rating"]) if opp is not None else -1
        table_rows.append({
            "Opponent": label_for(labels, opp_id),
            # pd.NA rather than an em dash: this column is integers, and one
            # string in it makes the whole column an object column, which Arrow
            # then refuses to serialise — so a single unknown opponent took the
            # entire table down. Blank renders the same and sorts properly.
            "Rating": int(round(opp_rating)) if opp is not None else pd.NA,
            "_Rating sort": opp_rating,
            "Title": elo.abbr(opp["league"]) if opp is not None else "—",
            "Matches": s["matches"],
            "Match W-D-L": _record(s["match_wins"], s["match_draws"],
                                   s["match_losses"]),
            "Match win %": _match_win_pct(s["match_wins"], s["match_losses"],
                                          s["match_draws"]),
            "Net rounds": s["round_wins"] - s["round_losses"],
            "Round win %": _round_win_pct(s["round_wins"], s["round_losses"], s["draws"]),
        })
    table = pd.DataFrame(table_rows)
    if not table.empty:
        table = table.sort_values(
            ["_Rating sort", "Opponent"], ascending=[False, True]).drop(
                columns=["_Rating sort"])
    return table


def _format_match_log(rows, bots, everyone=None):
    # Both columns name bots the registry may no longer have a row for — a
    # `janken remove` leaves its matches behind in nobody's log but everyone
    # else's. `label_for` puts a short id in that cell instead of the 64-char
    # digest the old dict-fallback printed.
    labels = opponent_labels(everyone if everyone is not None else bots)
    if rows.empty:
        return pd.DataFrame()
    return pd.DataFrame({
        "When": rows["played_at"].str[:16].str.replace("T", " "),
        "Kind": rows["kind"],
        "Bot A": [label_for(labels, x) for x in rows["bot_a"]],
        "Bot B": [label_for(labels, x) for x in rows["bot_b"]],
        "Score": rows["wins_a"].astype(str) + "–" + rows["wins_b"].astype(str),
        "Draws": rows["draws"],
        "Forfeit": [
            (a or b or "") for a, b in zip(rows["forfeit_a"].fillna(""), rows["forfeit_b"].fillna(""))
        ],
    }).iloc[::-1]


def _status_mark(status):
    """A Status cell with its glyph. Display only — the raw value is what the
    filter above the table matches on."""
    mark = {"Held": STATE_MARK["held"], "Banned": STATE_MARK["banned"],
            "In field": "✓"}.get(status, "")
    return ("%s %s" % (mark, status)).strip() if mark else status


def standings(ranked, avatars=None):
    st.subheader("Standings")
    c1, c2, c3 = st.columns([2, 2, 2])
    with c1:
        titles = st.multiselect("Title", list(LEAGUES), default=[],
                                key="standings_titles",
                                format_func=elo.name,
                                help="Leave empty to include every title.")
    with c2:
        statuses = st.multiselect(
            "Status", ["In field", "Held", "Banned"], default=["In field"],
            key="standings_statuses")
    with c3:
        submitter = st.selectbox("Submitter", submitter_options(ranked),
                                 key="standings_submitter")

    # Banned and held bots sit out the title filter: neither holds a title that
    # means anything while it isn't playing, so filtering them by one would hide
    # rows from the only view that lists them.
    title_filter = titles or list(LEAGUES)
    titled = ranked["league"].isin(title_filter) | (ranked["Status"] != "In field")
    shown = ranked[titled & ranked["Status"].isin(statuses)]
    shown = by_submitter(shown, submitter)
    shown = shown.sort_values(
        ["banned", "held", "_standing", "wins", "name"],
        ascending=[True, True, False, False, True])
    if shown.empty:
        st.info("No bots match those filters.")
        return
    if ranked[ranked["Status"] == "In field"]["rated_games"].sum() == 0:
        st.info("No matches played yet — everyone starts at %d until `janken tournament` runs." %
                START_RATING)

    # Labelled against `shown`, not the whole registry: the suffix answers "which
    # of these two rows is which", so it belongs to the filtered table actually
    # on screen. Filter to one submitter and the duplicates mostly stop being
    # duplicates, and the names go back to being bare.
    names = display_labels(zip(shown["id"], shown["name"], shown["submitter"]),
                           submitter_shown=True)
    table = pd.DataFrame({
        "#": shown["Rank"].values,
        "Bot": [names[i] for i in shown["id"]],
        "Submitter": shown["submitter"].replace("", "—").values,
        # Marked here, not in `ranked_bots`: that column is a filter key, and
        # the multiselect above matches on its exact text.
        "Status": [_status_mark(s) for s in shown["Status"]],
        "Title": [elo.abbr(t) for t in shown["league"]],
        "Norms": shown["Norms"].values,
        "Rating": shown["rating"].round().astype(int).values,
        "Peak": shown["peak_rating"].round().astype(int).values,
        "Matches": shown["rated_games"].astype(int).values,
        "W-D-L": [_record(w, d, l)
                  for w, d, l in zip(shown["wins"], shown["draws"], shown["losses"])],
        "Round +/-": shown["Round +/-"].values,
    })
    table, columns = _with_avatars(table, shown["id"], avatars or {})
    columns["#"] = _RANK_COLUMN
    columns["W-D-L"] = st.column_config.TextColumn(
        "W-D-L",
        help="Match record, in order: %s. A drawn match is one where both bots "
             "won the same number of rounds." % RECORD_ORDER)
    columns["Norms"] = st.column_config.TextColumn(
        "Norms",
        help="Progress toward the norms the top two titles need. `1/2 IM` means "
             "this bot's peak rating reaches International Master but it has one "
             "of the two qualifying performances — the rating is there, the "
             "record backing it is not yet.")
    unproven_rows = shown["Unproven"].values

    styled = table.style.map(_league_colour, subset=["Title"])
    styled = styled.apply(
        lambda col: [("color:%s" % ORANGE) if flag else ""
                     for flag in unproven_rows],
        subset=["Norms"])
    st.dataframe(styled, width="stretch", hide_index=True, column_config=columns)
    if unproven_rows.any():
        st.caption(
            "%d bot(s) hold a rating that reaches a norm-gated title without the "
            "performances to earn it (highlighted above). That is what a newcomer "
            "looks like *and* what an inflated rating looks like — the norms are "
            "how you tell them apart, once enough events have run."
            % int(unproven_rows.sum()))


class _FieldRow:
    """A bot as `norms.composition` wants it: id, rating, submitter, league."""

    def __init__(self, row):
        self.id = row["id"]
        self.name = row["name"]
        self.rating = row["rating"]
        self.submitter = row["submitter"]
        self.league = row["league"]


def norm_event_status(matches, ranked):
    """Whether the field can currently hold a norm event, and when one last ran.

    Worth a line on screen because the answer is "no" for a young field, and
    without saying so the top two titles look broken rather than unearned.
    """
    st.markdown("##### Norm events")
    played = matches[matches["kind"] == "norm"] if "kind" in matches else matches
    last = played["played_at"].max() if not played.empty else None

    field = [_FieldRow(r) for _, r in
             ranked[ranked["Status"] == "In field"].iterrows()]
    chosen, shape = norms.select_field(field)
    if chosen is None:
        st.warning(
            "**No norm event can run yet** — %s.\n\nThe top two titles need "
            "qualifying performances in a selective event, so until the field can "
            "hold one they are out of reach for any new bot. A qualifying field "
            "needs %d+ bots, at most a quarter of them from any one submitter, "
            "distinct submitters numbering at least a third of the field, and half "
            "of it already titled."
            % (shape.why(), norms.MIN_NORM_FIELD))
    else:
        st.success(
            "**A norm event can run**: %d bots would be invited (%s). Events run "
            "about every %g hours, on top of the regular tournament."
            % (len(chosen), shape.why(), norms.norm_interval_hours()))
        st.caption("Invited: " + ", ".join(b.name for b in chosen))
    st.caption("Last norm event: %s" % (last or "never"))


def my_bots(db_path, bots, user, avatars=None):
    """Hold, release, rename and picture controls, for the bots the viewer owns."""
    st.subheader("Your bots")
    if not user:
        st.info("Sign in to manage your own bots.")
        return
    mine = bots[bots["submitter"].str.lower() == user["email"].lower()]
    if mine.empty:
        st.caption("Nothing submitted under **%s** yet." % user["email"])
        return
    playing = int((~mine["banned"] & ~mine["held"]).sum())
    st.caption(
        "**%d of %d** in the field. That is the most one entrant may have "
        "playing at once — hold one to make room for another. Holding costs "
        "a bot nothing: it keeps its rating, its title and its record, and "
        "its id still proves when the file was uploaded."
        % (playing, ACTIVE_PER_SUBMITTER))
    avatars = avatars or {}
    for row in mine.itertuples():
        cols = st.columns([5, 2])
        # Your own banned bot is the one case where the reason is actionable —
        # it says what to fix before resubmitting — so it goes on the row.
        state = (("⛔ banned — %s" % (row.ban_reason or "no reason recorded"))
                 if row.banned
                 else "⏸ held — not playing" if row.held else "✓ in the field")
        cols[0].markdown("`%s` **%s** — %s" % (short_id(row.id), row.name, state))
        label = "Release" if row.held else "Hold"
        if cols[1].button(label, key="hold_%s" % row.id, width="stretch",
                          disabled=bool(row.banned)):
            try:
                with Registry(db_path) as reg:
                    reg.set_held(row.id, not row.held)
            except FieldFull:
                # Said here rather than by grey-ing the button out: a disabled
                # control with no explanation reads as a bug, and the fix — hold
                # one of the others — is one click away on this same list.
                st.error(
                    "**%s stays held — you already have %d bots in the field.** "
                    "Hold one of them above and press Release again."
                    % (row.name, ACTIVE_PER_SUBMITTER))
            else:
                st.cache_data.clear()
                st.rerun()
        _bot_details_controls(db_path, row, avatars.get(row.id, ""))


def _bot_details_controls(db_path, row, picture=""):
    """Rename one bot and change its picture, in place.

    Both are display metadata, unlike the code hash that defines identity, so
    changing either must not require a resubmission or reset the rating
    history. For the picture that is the whole reason this exists: the id *is*
    the hash of the `.py`, so re-submitting to fix a badly cropped image would
    register a different bot and throw the rating away, leaving the picture
    effectively permanent.

    One expander, not two. They are the same kind of thing — everything about a
    bot you can edit without it becoming a new bot — and a second collapsed bar
    on every row turned the list into stripes.
    """
    with st.expander("Details", expanded=False):
        # Both halves are headed the same way. The widget labels underneath are
        # collapsed rather than deleted — screen readers still read them, and
        # "Bot name" twice over is noise for everyone else.
        st.markdown("**Bot name**")
        name = st.text_input("Bot name", value=row.name, key="name_%s" % row.id,
                             label_visibility="collapsed")
        clean = name.strip()
        disabled = not clean or clean == row.name
        if st.button("Save name", key="setname_%s" % row.id,
                     width="stretch", disabled=disabled):
            with Registry(db_path) as reg:
                reg.set_name(row.id, clean)
            st.cache_data.clear()
            st.rerun()

        st.divider()
        st.markdown("**Picture**")
        # "Replace it" / "Add one" stays a visible label: it says what the
        # uploader does to the thumbnail above it, which the heading cannot.
        if picture:
            st.image(picture, width=72)
        else:
            st.caption("No picture yet — bots without one simply show no image.")
        png, _ = _avatar_field("img_%s" % row.id,
                               label="Replace it" if picture else "Add one")
        cols = st.columns([2, 2])
        if cols[0].button("Save picture", key="setimg_%s" % row.id,
                          width="stretch", disabled=png is None):
            with Registry(db_path) as reg:
                reg.set_image(row.id, png)
            st.cache_data.clear()
            st.rerun()
        if cols[1].button("Remove", key="delimg_%s" % row.id, width="stretch",
                          disabled=not picture):
            with Registry(db_path) as reg:
                reg.set_image(row.id, None)
            st.cache_data.clear()
            st.rerun()

        st.divider()
        _hide_control(db_path, row)


def _hide_control(db_path, row):
    """Let an owner take a bot off their own screen for good.

    Deliberately the last thing in Details, deliberately behind typing the
    name. From here it is one way: a hidden bot is not in this list any more,
    so the person who hid it is exactly the person who cannot bring it back —
    only `janken enable` can, which means asking the organizer. A control that
    final should cost more than a click, and should say so before it is used
    rather than after.

    It is not a delete, and the wording works to keep those apart. `janken
    remove` throws away the matches too, which would edit the record of every
    bot this one ever played; hiding leaves all of that standing and only stops
    showing the bot.
    """
    st.markdown("**Hide this bot**")
    st.caption(
        "Takes **%s** off the standings, the search and any cup field — "
        "including this list, so you will not be able to undo it yourself. "
        "Ask the organizer to run `janken enable` if you change your mind. "
        "Nothing is deleted: its rating, its record and its upload date stay "
        "on file, and the matches it played still count for the bots it "
        "played them against." % row.name)
    typed = st.text_input(
        "Type the bot's name to confirm", key="hidename_%s" % row.id,
        label_visibility="collapsed", placeholder="Type “%s” to confirm" % row.name)
    confirmed = typed.strip().casefold() == (row.name or "").strip().casefold()
    if st.button("Hide it", key="hide_%s" % row.id, width="stretch",
                 disabled=not confirmed):
        with Registry(db_path) as reg:
            reg.set_disabled(row.id, True)
        st.cache_data.clear()
        st.rerun()


def _profile_options(bots):
    opts = bot_search_options(bots)
    return opts[1:] if len(opts) > 1 else []


def _bot_history(history, bot_id):
    if history.empty or not bot_id:
        return history.iloc[0:0].copy()
    frame = history[history["bot_id"] == bot_id].copy()
    if frame.empty:
        return frame
    frame["played_at"] = pd.to_datetime(frame["played_at"], errors="coerce")
    return frame.dropna(subset=["played_at"]).sort_values("played_at")


def _norms_table(norms_frame, bot_id):
    """This bot's norms, newest first — the evidence behind its title."""
    if norms_frame is None or norms_frame.empty:
        return pd.DataFrame()
    mine = norms_frame[norms_frame["bot_id"] == bot_id]
    if mine.empty:
        return pd.DataFrame()
    mine = mine.sort_values("played_at", ascending=False)
    return pd.DataFrame({
        "Earned": [str(s)[:16].replace("T", " ") for s in mine["played_at"]],
        "Level": [elo.abbr(t) for t in mine["title"]],
        "Performed at": mine["performance"].round().astype(int).values,
        "Field averaged": mine["field_avg"].round().astype(int).values,
        "Games": mine["games"].astype(int).values,
    })


def bot_norms(norms_frame, row, bot_id):
    """The norms half of a profile: what is banked, and what it still needs."""
    st.markdown("##### Norms")
    table = _norms_table(norms_frame, bot_id)
    reached = elo.title_for_rating(row["peak_rating"])
    needed = elo.norms_required(reached)

    if row["Unproven"]:
        have = 0 if table.empty else len(table)
        st.warning(
            "This bot's peak rating of **%d** reaches **%s**, but that title also "
            "needs **%d** qualifying performances and it has **%d**. The rating is "
            "there; the record backing it is not. A strong newcomer looks exactly "
            "like this until it has played some norm events."
            % (int(round(row["peak_rating"])), elo.name(reached), needed, have))
    elif needed and elo.TITLE_RANK.get(row["league"], 0) >= elo.TITLE_RANK[reached]:
        st.success("**%s**, earned the hard way: the rating *and* the norms."
                   % elo.name(row["league"]))

    if table.empty:
        st.caption("No norms yet. They are earned in norm events — a selective "
                   "tournament whose field has to span several submitters and be "
                   "mostly titled, so a bot cannot stack it with its own.")
        return
    st.dataframe(table, width="stretch", hide_index=True)


def bot_profile(ranked, matches, history, norms_frame=None, avatars=None,
                everyone=None):
    st.subheader("Bot profile")
    options = _profile_options(ranked)
    if not options:
        st.info("No bots registered yet.")
        return
    pick = st.selectbox("Bot", options, format_func=bot_labeller(ranked),
                        key="profile_bot")
    bot_id = selected_bot_id(pick)
    row = ranked.set_index("id").loc[bot_id]

    heading, image_col = st.columns([5, 1])
    heading.markdown("### %s" % row["name"])
    heading.caption("`%s` · %s"
                    % (short_id(bot_id), row["submitter"] or "no submitter"))
    if avatars and avatars.get(bot_id):
        image_col.image(avatars[bot_id], width=80)

    # A status of "Banned" without the reason is the half of the answer nobody
    # needs; this is the only screen that still has room for the whole of it.
    if row["Status"] == "Banned":
        st.error("⛔ **Banned** — %s" % (row["ban_reason"] or "no reason recorded"))
    elif row["Status"] == "Held":
        st.info("⏸ **Held** back by whoever submitted it, so no tournament fields "
                "it. Holding costs a bot nothing: it keeps its rating, its title "
                "and its record, and its id still proves when the file was uploaded.")

    cols = st.columns(4)
    cols[0].metric("Status", row["Status"])
    cols[1].metric("Title", elo.abbr(row["league"]))
    cols[2].metric("Rating", int(round(row["rating"])),
                   help="Glicko-2, scored on who won each match. The standings "
                        "rank on this minus the deviation below, so a rating "
                        "nobody has tested yet does not jump the queue.")
    cols[3].metric("Peak", int(round(row["peak_rating"])),
                   help="The high-water mark titles are earned from. It only "
                        "ever goes up — a bad run costs rating, never a title.")
    cols = st.columns(5)
    cols[0].metric("Rank", "—" if pd.isna(row["Rank"]) else int(row["Rank"]))
    cols[1].metric("Matches", int(row["rated_games"]))
    cols[2].metric("Norms", row["Norms"],
                   help="Qualifying performances toward the top two titles. "
                        "See the Norms section below.")
    cols[3].metric("Match win %", "%.1f%%" % _match_win_pct(
        int(row["wins"]), int(row["losses"]), int(row["draws"])),
        help="Share of matches won — this is what the rating is scored on. "
             "Drawn matches count against it.")
    cols[4].metric("Round win %", "%.1f%%" % _round_win_pct(
        int(row["round_wins"]), int(row["round_losses"]),
        _round_draws(matches, [bot_id])),
        help="Share of every round played that this bot won. It can disagree "
             "sharply with the match figure — winning narrowly and losing heavily "
             "is a good record and a bad round count.")
    st.caption("Matches (%s): %s · Net rounds: %+d" %
               (RECORD_ORDER, _record(row["wins"], row["draws"], row["losses"]),
                row["round_wins"] - row["round_losses"]))

    # The uncertainty numbers live here and nowhere else. On the standings they
    # would be three more columns nobody reads to answer "who is winning"; on the
    # one page about one bot they are exactly the question being asked.
    cols = st.columns(3)
    cols[0].metric("Deviation", "±%d" % round(row["rd"]),
                   help="How sure the rating is. It falls as a bot plays and "
                        "widens again while it sits idle, so a rating that has "
                        "not been tested lately is marked as such rather than "
                        "quietly trusted.")
    cols[1].metric("Ranked as", int(round(row["rating"] - glicko.RANK_K * row["rd"])),
                   help="Rating minus one deviation — the number the standings "
                        "actually sort on. It rises on its own as a bot keeps "
                        "playing, without the rating moving at all.")
    cols[2].metric("Round rating", int(round(row["alt_rating"])),
                   help="The same system scored on the share of rounds instead "
                        "of who won. A finer measure of one bot's edge, but it "
                        "compresses as the field improves — nobody beats a "
                        "random opponent above half the rounds — so no title is "
                        "read off it.")

    bot_norms(norms_frame, row, bot_id)

    hist = _bot_history(history, bot_id)
    c1, c2 = st.columns(2)
    if hist.empty:
        c1.info("No rating history yet.")
        c2.info("No position history yet.")
    else:
        charts = hist.set_index("played_at")
        c1.markdown("##### Rating history")
        c1.line_chart(charts[["rating"]].rename(columns={"rating": "Rating"}))
        c2.markdown("##### Table position")
        # Reverse the axis rather than plot -1, -2, -3: negating put the right
        # shape on screen with the wrong numbers under it, and 1st is the label
        # somebody is looking for.
        c2.altair_chart(
            alt.Chart(hist).mark_line(point=True).encode(
                x=alt.X("played_at:T", title=None),
                y=alt.Y("rank:Q", title="Position",
                        scale=alt.Scale(reverse=True, zero=False),
                        axis=alt.Axis(format="d", tickMinStep=1)),
            ),
            width="stretch",
        )

    h2h = _h2h_frame(ranked, matches, bot_id, everyone)
    st.markdown("##### Head to head")
    if h2h.empty:
        st.info("No tournament head-to-head record yet.")
    else:
        def colour(v):
            if v > 0:
                return "background-color: rgba(96,208,69,.22)"
            if v < 0:
                return "background-color: rgba(255,115,0,.20)"
            return "color:%s" % MIST
        styled = (
            h2h.style
            .map(_league_colour, subset=["Title"])
            .map(colour, subset=["Net rounds"])
            .format({"Net rounds": "{:+d}", "Match win %": "{:.1f}%",
                     "Round win %": "{:.1f}%"})
        )
        st.dataframe(styled, width="stretch", hide_index=True, column_config={
            "Match W-D-L": st.column_config.TextColumn(
                "Match W-D-L",
                help="Record against this opponent, in order: %s. A drawn match "
                     "is one where both bots won the same number of rounds."
                     % RECORD_ORDER),
            "Match win %": st.column_config.NumberColumn(
                "Match win %",
                help="Share of matches against this opponent that were won. "
                     "Drawn matches count against."),
            "Round win %": st.column_config.NumberColumn(
                "Round win %",
                help="Share of individual rounds won. Reads far lower than the "
                     "match figure, because drawn rounds count against it and a "
                     "match is won on rounds *relative* to the opponent."),
        })

    st.markdown("##### Recent matches")
    log = _format_match_log(_bot_matches(matches, bot_id), ranked, everyone)
    if log.empty:
        st.info("No matches recorded for this bot.")
    else:
        st.dataframe(log.head(12), width="stretch", hide_index=True)


def _user_stats(bots, matches):
    total = len(bots)
    active = int((~bots["banned"] & ~bots["held"]).sum())
    held = int(bots["held"].sum())
    banned = int(bots["banned"].sum())
    played = int((bots["wins"] + bots["draws"] + bots["losses"]).sum())
    round_wins = int(bots["round_wins"].sum())
    round_losses = int(bots["round_losses"].sum())
    round_draws = _round_draws(matches, bots["id"])
    return {
        "registered": total,
        "active": active,
        "held": held,
        "banned": banned,
        "best_rating": int(round(bots["rating"].max())) if total else "—",
        "best_peak": int(round(bots["peak_rating"].max())) if total else "—",
        "best_title": elo.abbr(max(bots["league"], key=lambda t: LEAGUE_RANK.get(t, -1))) if total else "—",
        "matches": played,
        "record": _record(int(bots["wins"].sum()), int(bots["draws"].sum()),
                          int(bots["losses"].sum())),
        "match_win_pct": _match_win_pct(int(bots["wins"].sum()),
                                        int(bots["losses"].sum()),
                                        int(bots["draws"].sum())),
        "round_win_pct": _round_win_pct(round_wins, round_losses, round_draws),
        "net_rounds": round_wins - round_losses,
    }


def user_profile(ranked, matches, user=None, avatars=None):
    st.subheader("User profile")
    options = [o for o in submitter_options(ranked) if o != EVERYONE]
    if not options:
        st.info("No submitters found yet.")
        return
    default = 0
    if user:
        lower = [o.strip().lower() for o in options]
        if user["email"].lower() in lower:
            default = lower.index(user["email"].lower())
    pick = st.selectbox("Submitter", options, index=default, key="profile_submitter")
    shown = by_submitter(ranked, pick)
    if shown.empty:
        st.info("No bots found for that submitter.")
        return

    stats = _user_stats(shown, matches)
    cols = st.columns(6)
    cols[0].metric("Bots", stats["registered"])
    cols[1].metric("In field", stats["active"])
    cols[2].metric("Held", stats["held"])
    cols[3].metric("Banned", stats["banned"])
    cols[4].metric("Best rating", stats["best_rating"])
    cols[5].metric("Best title", stats["best_title"])
    cols = st.columns(5)
    cols[0].metric("Best peak", stats["best_peak"])
    cols[1].metric("Matches", stats["matches"])
    cols[2].metric("W-D-L", stats["record"],
                   help="Match record across every bot, in order: %s."
                        % RECORD_ORDER)
    cols[3].metric("Match win %", "%.1f%%" % stats["match_win_pct"])
    cols[4].metric("Round win %", "%.1f%%" % stats["round_win_pct"], "%+d" % stats["net_rounds"])

    # Every row here belongs to the one submitter picked above, so naming them
    # after that submitter would say nothing: a repeat inside this table is
    # someone with two bots under one name, which only an id separates.
    names = display_labels(zip(shown["id"], shown["name"], shown["submitter"]),
                           submitter_shown=True)
    table = pd.DataFrame({
        "#": shown["Rank"].values,
        "Bot": [names[i] for i in shown["id"]],
        "Status": [_status_mark(s) for s in shown["Status"]],
        "Title": [elo.abbr(t) for t in shown["league"]],
        "Rating": shown["rating"].round().astype(int).values,
        "Peak": shown["peak_rating"].round().astype(int).values,
        "Matches": shown["rated_games"].values,
        "W-D-L": [_record(w, d, l)
                  for w, d, l in zip(shown["wins"], shown["draws"], shown["losses"])],
        "Uploaded": shown["first_seen"].str[:10].values,
    })
    table, columns = _with_avatars(table, shown["id"], avatars or {})
    columns["#"] = _RANK_COLUMN
    columns["W-D-L"] = st.column_config.TextColumn(
        "W-D-L", help="Match record, in order: %s." % RECORD_ORDER)
    styled = table.style.map(_league_colour, subset=["Title"])
    st.dataframe(styled, width="stretch", hide_index=True, column_config=columns)


def match_log(bots, matches, everyone=None):
    st.subheader("Match log")
    if matches.empty:
        st.info("No matches recorded yet.")
        return
    # The pickers offer visible bots; the table names whoever actually played,
    # hidden opponents included.
    labels = opponent_labels(everyone if everyone is not None else bots)
    c1, c2, c3 = st.columns(3)
    with c1:
        kinds = ["all"] + sorted(matches["kind"].unique())
        kind = st.selectbox("Kind", kinds, key="log_kind")
    with c2:
        bot_pick = st.selectbox("Bot", bot_search_options(bots),
                                format_func=bot_labeller(bots), key="log_bot")
    with c3:
        submitter = st.selectbox("Submitter", submitter_options(bots), key="log_submitter")
    rows = matches if kind == "all" else matches[matches["kind"] == kind]
    bot_id = selected_bot_id(bot_pick)
    opponent_id = ""
    if bot_id:
        rows = rows[(rows["bot_a"] == bot_id) | (rows["bot_b"] == bot_id)]
        opponent_ids = sorted({
            row.bot_b if row.bot_a == bot_id else row.bot_a
            for row in rows.itertuples()
        }, key=lambda x: label_for(labels, x).lower())
        if opponent_ids:
            opponent_pick = st.selectbox(
                "Against", [ALL_BOTS] + opponent_ids,
                format_func=lambda x: ("All opponents" if x == ALL_BOTS
                                       else label_for(labels, x)),
                key="log_opponent")
            opponent_id = "" if opponent_pick == ALL_BOTS else opponent_pick
            if opponent_id:
                rows = rows[((rows["bot_a"] == bot_id) & (rows["bot_b"] == opponent_id)) |
                            ((rows["bot_b"] == bot_id) & (rows["bot_a"] == opponent_id))]
    if submitter != EVERYONE:
        selected = by_submitter(bots, submitter)
        selected_ids = set(selected["id"])
        rows = rows[rows["bot_a"].isin(selected_ids) | rows["bot_b"].isin(selected_ids)]
    if rows.empty:
        st.info("No matches found with the current filters.")
        return
    st.dataframe(_format_match_log(rows, bots, everyone), width="stretch",
                 hide_index=True)


def dashboard_tabs(user):
    tabs = ["Standings", "Bot profile", "User profile"]
    if user:
        tabs.append("Your bots")
    tabs.append("Match log")
    return tabs


# --- app --------------------------------------------------------------------
def registry_page(db_path, user=None, cup=None, cup_page=None):
    """The registry: a surface you browse. app.py owns the page config, brand
    chrome and sign-in, so this is only the content."""
    header(cup)

    # Opening the registry creates an empty one when there is none, and migrates
    # an older file to the current columns (ratings) — so do it before reading
    # the tables, and before the mtime the cache keys on.
    Registry(db_path).close()

    version = db_version(db_path)
    bots, matches = load_tables(db_path, version)
    # `bots` is what may be listed; `everyone` is what may be *named*. They
    # differ by the disabled bots, which are gone from every panel that lists
    # but still present in the records of the bots they played.
    everyone = load_everyone(db_path, version)
    history = load_history(db_path, version)
    norms_frame = load_norms(db_path, version)
    avatars = load_avatars(db_path, version)
    bots = bots.copy()
    bots["banned"] = bots["banned"].astype(bool)  # sqlite stores 0/1; ~int is a trap
    bots["held"] = bots["held"].astype(bool)

    # Registry vitals: which file, how big the field is, whether the periodic run
    # is still alive. An organizer's questions, asked about once a day — so they
    # belong in the sidebar with the rest of the plumbing rather than across the
    # top of the page, where they were the first thing every visitor read and the
    # last thing most of them wanted.
    active = bots[~bots["banned"] & ~bots["held"]]
    withheld = len(bots) - len(active)
    last_run = last_tournament(matches)
    next_run = next_tournament(matches)

    with st.sidebar:
        if user:
            st.markdown("### Signed in")
            st.caption(user["name"] and "%s · %s" % (user["name"], user["email"])
                       or user["email"])
            if st.button("Log out", width="stretch"):
                st.logout()
        st.markdown("### Registry")
        st.caption("`%s` — re-read on every interaction." % os.path.basename(db_path))
        # One fact per line. The line this replaces joined five parts with " · "
        # while three of them already contained their own " · " — so at page width
        # it read as a run of dates, and at sidebar width it would wrap into one.
        vitals = ["**%d** in field · **%d** held or banned" % (len(active), withheld)]
        if last_run:
            vitals.append("**Last run** %s" % last_run)
        if next_run:
            vitals.append("**Next run** ~%s" % next_run)
        st.caption("  \n".join(vitals))

    # Directly under the header. Not above it: a full-width notice strip sitting on
    # top of the masthead is a shape the web retired twenty years ago, and the
    # header's own CSS already argues the point for the live cup — the navy block is
    # where the eye lands, so an alert belongs *with* it rather than stacked over
    # it. With the registry vitals moved to the sidebar this is now the first thing
    # in the page, which is right: it is the only thing here that nothing else can
    # tell you.
    announcements(db_path)

    if cup is not None and cup.status() in ("live", "ready") and cup_page is not None:
        st.page_link(cup_page, label="Watch the cup", icon=":material/trophy:")

    submit_bot(db_path, user)
    how_it_works()

    if bots.empty:
        st.info("No bots registered yet — upload one above, or run a tournament from the CLI.")
        return

    # Status, Rank and Round +/- are the same for every panel that shows them,
    # so they are derived once here rather than three times over.
    ranked = ranked_bots(bots, norms_frame)
    tab_names = dashboard_tabs(user)
    tabs = dict(zip(tab_names, st.tabs(tab_names)))
    with tabs["Standings"]:
        standings(ranked, avatars)
        norm_event_status(matches, ranked)
    with tabs["Bot profile"]:
        bot_profile(ranked, matches, history, norms_frame, avatars, everyone)
    with tabs["User profile"]:
        user_profile(ranked, matches, user, avatars)
    if user:
        with tabs["Your bots"]:
            my_bots(db_path, bots, user, avatars)
    with tabs["Match log"]:
        match_log(bots, matches, everyone)


if __name__ == "__main__":
    # app.py is the entrypoint now: the nav entry and the browser tab both need
    # the cup's state before the first widget renders, and this module is one of
    # the two pages it serves rather than something to run.
    #
    # Say so rather than quietly forwarding. Importing app from here would load
    # this file a second time — once as __main__, once as `dashboard` — leaving
    # two copies of every cached function for the sake of an alias nobody needs
    # once install.sh and the docs point at the right file.
    st.error("`app.py` is the entrypoint now — run `python3 -m janken dashboard`, "
             "or `streamlit run app.py`.")
