"""Entrypoint: two pages that share a brand and almost nothing else.

`st.navigation` rather than a `pages/` directory, because the cup's nav entry
has to change while a cup is on — a directory of pages gets you static titles,
and a title that can say LIVE is one of the three ways someone finds out a cup
is happening. The other two are the browser tab and the registry's header, both
of which also need the cup's state before the first Streamlit widget renders.
So the state is read here, with plain sqlite, before anything else.
"""

import os

import streamlit as st

import cup_page
import dashboard
from janken.cupstore import CupStore

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


def cup_state(db_path):
    """The cup worth anyone's attention right now, or None. Cheap enough to run
    on every rerun of every page — one indexed row."""
    try:
        with CupStore(db_path) as store:
            return store.current()
    except Exception:
        # A dashboard that cannot read the cup tables is still a dashboard.
        return None


def _page_title(state):
    """The browser tab. Someone with this parked in a background tab finds out
    a cup started without looking at it, which is the cheapest attention we
    will ever buy.

    Announcements deliberately stay off it. The tab is the loudest channel here
    and a cup earns it by being a scheduled event people are waiting for; a
    banner that also took it would be shouting from a background window about
    something the page says perfectly well.
    """
    if state is None:
        return "Janken Registry Dashboard"
    if state.status() == "live":
        return "🔴 LIVE — %s" % state.name
    if state.status() == "ready":
        return "● %s — about to start" % state.name
    return "Janken Registry Dashboard"


def _cup_nav_title(state):
    if state is None:
        return "Cup"
    return {"live": "Cup — LIVE", "ready": "Cup — ready"}.get(state.status(), "Cup")


def main():
    db_path = dashboard.get_db_path()
    state = cup_state(db_path)

    icon = os.path.join(ASSETS, dashboard.LOGO_ICON)
    st.set_page_config(page_title=_page_title(state),
                       page_icon=icon if os.path.exists(icon) else "✳️",
                       layout="wide")
    dashboard.brand_chrome()      # st.logo — must be called early in the script
    dashboard.inject_css()
    user = dashboard.current_user()   # stops the script unless admitted

    # Each page links to the other, so the two lambdas close over names defined
    # either side of them. That is fine — closures resolve when they run, which
    # is inside navigation.run(), by which point both are bound.
    cup = st.Page(lambda: cup_page.render(db_path, registry),
                  title=_cup_nav_title(state), icon=":material/trophy:",
                  url_path="cup")
    registry = st.Page(lambda: dashboard.registry_page(db_path, user, state, cup),
                       title="Registry", icon=":material/leaderboard:",
                       url_path="registry", default=True)
    st.navigation([registry, cup]).run()


# Guarded, so `dashboard.py` can import this module and call main() itself
# without the app running twice on the way in.
if __name__ == "__main__":
    main()
