"""The cup page — a broadcast, not a dashboard.

The registry page is a surface you browse: filters, search, sort, four tabs,
a metrics row. Every one of those answers "what do I want to see?", and a
broadcast's whole job is to have answered that already. So this page has none
of them. There are fifteen matches, you watch them in the order the bracket
dictates, and the only control that exists belongs to whoever is running the
show — from a terminal, not from here.

The surface is navy rather than the registry's white. Not a different style:
the brand reserves navy and the gradient blobs for covers and statements, and
white for dense reading. A dashboard is reading; a broadcast is a statement.
"""

import hashlib
import json
import os

import streamlit as st

import cup_view
import dashboard
from janken import avatar
from janken.cupstore import CupStore
from janken.registry import Registry

# Streamlit serves ./static at /app/static when enableStaticServing is on.
# Polling that file is what turns "the operator advanced" into every viewer's
# screen moving; without it the page still renders, it just needs a refresh to
# notice a new round.
#
# The filename is derived from the registry rather than fixed, because the
# database is a parameter and a fixed name is not: point two instances at
# different registries — a rehearsal and the real thing — and they overwrite
# each other's broadcast, so one page serves the other's cup.
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def payload_name(db_path):
    digest = hashlib.sha1(os.path.realpath(db_path).encode()).hexdigest()[:8]
    return "cup-%s.json" % digest


def payload_path(db_path):
    return os.path.join(STATIC_DIR, payload_name(db_path))

HIDE_CHROME = """
<style>
/* A broadcast has nothing to configure. set_page_config is app-wide under
   st.navigation, so the sidebar goes away here with CSS rather than config. */
section[data-testid="stSidebar"] { display: none; }
div[data-testid="stAppViewBlockContainer"] { padding-top: 2rem; max-width: 100%; }
/* Clip the embed to the navy card's own corners. Selected by container rather
   than by the iframe's title, which is Streamlit's to change. */
div[data-testid="stAppViewBlockContainer"] iframe { border-radius: 28px; }
</style>
"""


def _export_avatars(db_path):
    """Materialise the registry's avatars under static/, at most once per change.

    Streamlit reruns the whole script on every interaction and this writes
    files, so the work sits behind a cache keyed on the database file — the
    same version trick the registry page uses to know when results landed. A
    redeploy is what empties static/, and a redeploy also restarts the process
    and so empties the cache, which is why nothing checks the files themselves.
    """
    try:
        stat = os.stat(db_path)
    except OSError:
        return 0
    return _export_avatars_cached(db_path, (stat.st_mtime_ns, stat.st_size))


@st.cache_data(show_spinner=False)
def _export_avatars_cached(db_path, version):
    with Registry(db_path) as reg:
        return avatar.export(reg.images(), STATIC_DIR)


def publish(db_path, payload):
    """Keep the polled document in step with the database.

    `janken cup show` writes this too, but a page load is the other moment we
    know the current state, and refreshing it here means the broadcast works
    even if the operator never passes --payload. Written aside and renamed so
    a viewer polling mid-write never reads half a document.
    """
    path = payload_path(db_path)
    try:
        os.makedirs(STATIC_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
        return True
    except OSError:
        return False        # read-only deployment: the page still renders


def render(db_path, home=None):
    st.markdown(HIDE_CHROME, unsafe_allow_html=True)
    # Hiding the sidebar takes the navigation with it, so the way back has to
    # be on the page itself.
    if home is not None:
        st.page_link(home, label="Registry", icon=":material/arrow_back:")

    # The broadcast carries the banner too. A cup is exactly when the room is
    # looking at this screen and not the registry, so "the final is delayed ten
    # minutes" has to reach it here or it reaches nobody.
    dashboard.announcements(db_path, on_navy=True)

    with CupStore(db_path) as store:
        state = store.current() or store.latest()
        if state is None:
            _nothing_yet()
            return
        payload = store.payload(state.cup_id)

    # `cup run` exports the avatars too, but static/ is a cache and a deploy may
    # have wiped it since. Re-exporting on a page load is cheap next to what it
    # buys: a broadcast never opens with broken pictures because somebody
    # redeployed between the draw and the show.
    _export_avatars(db_path)

    poll = ("/app/static/" + payload_name(db_path)) if publish(db_path, payload) else ""
    # st.iframe, not the deprecated components.v1.html. Beyond the deprecation
    # it settles the question the poll depends on: st.iframe documents that an
    # embedded HTML string gets JavaScript execution *and same-origin access to
    # the app*, so the fetch to /app/static reaches the served payload rather
    # than being blocked as cross-origin.
    st.iframe(cup_view.render(payload, poll), height=cup_view.VIEW_HEIGHT + 16)

    if not poll:
        st.caption(
            "Live updates are off — `static/cup.json` could not be written, so "
            "this page shows the state as of the last refresh."
        )


def _nothing_yet():
    st.markdown(
        '<div class="md-header"><div class="head-main"><div>'
        '<p class="kicker">Janken Cup</p>'
        '<h1>No cup has been run yet</h1>'
        '</div></div></div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        "A cup is a knockout: seeded by rating, decided by sudden death, and "
        "worth no rating at all. The organizer plays one in advance and then "
        "releases it a round at a time:\n\n"
        "```\npython3 -m janken cup run bots/ --name \"Spring Cup\"\n"
        "python3 -m janken cup show --payload static/cup.json\n```"
    )
