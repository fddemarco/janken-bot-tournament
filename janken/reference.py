"""Which bots are the organizer's own, and so are not competing.

`rocky`, `copycat`, `brainy` and `randy` ship with the repo as a yardstick: they
give a new submission something to measure against, and they keep the rating pool
anchored to strategies whose strength is known. They play the rated ladder like
anyone else. What they must stay out of is anything with a *prize* attached — the
cup, and norm events — because none of them is trying to win. `randy` is the sharp
case: a coin flip with no strategy at all, which over a single match wins often
enough to take a bracket, and did in fact bank an International Master norm off
the first norm event that ran.

This lives in its own module, awkward as that looks for twenty lines, because both
`cup` and `norms` need it and neither should import the other. It cannot go in
`registry`, which imports `norms` — that way round is a cycle. Everything here is
a plain fact about a bot's identity and depends on nothing.
"""

REFERENCE_BOTS = frozenset({"rocky", "copycat", "brainy", "randy"})


def is_reference(name: str, submitter: str = "") -> bool:
    """True for one of the organizer's baseline bots.

    Two clauses, and the second is what makes the answer unambiguous. The name is
    matched case-insensitively, because the registry title-cases display names
    while a field built from filenames gives stems — `Randy` and `randy` are one
    bot. But a name alone is a claim anybody can make: an entrant who calls their
    bot `randy.py` would be quietly barred from the cup and from norm events,
    which is a strange punishment for a naming coincidence.

    So a bot with a submitter is somebody's entry, whatever it is called. The
    reference bots have none — they are synced from `bots/` with no attribution,
    and every real submission carries an address from sign-in or a name from the
    CLI. `submitter` defaults to empty so that callers holding only a filename
    (a cup fielded straight from a directory, before the registry is consulted)
    get the old name-only behaviour, which is right for unattributed files.
    """
    if submitter and submitter.strip():
        return False
    return name.strip().lower() in REFERENCE_BOTS
