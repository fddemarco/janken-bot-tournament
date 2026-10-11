"""How a bot is named on screen when its own name is not enough.

Nothing stops two people uploading a bot called Rocky II, and nothing should:
a name is a label, not an identity, and the registry has always keyed on the
file's SHA-256 instead. But a list that renders both of them as "Rocky II" is
a list where two rows mean different bots and look like one, which is worse
than either a long id or a rejected upload.

So the discriminator is *conditional*. A name that appears once is printed
alone — which is almost every name, almost always — and only a name that
repeats earns a suffix, and only the smallest suffix that actually separates
the bots sharing it:

    Brainy Boy                     unique, so nothing is added
    Rocky II · ivan                two owners: the people tell them apart
    Rocky II · ivan@muttdata.ai    two locals collide across domains
    Rocky II · a3f9c1d4e07b        one owner, two bots: only the id is left

The ladder matters because the cheap discriminators are the readable ones. A
person's name says something a reader can use ("that's mine, that's Sofia's");
twelve hex characters say only "not that one". Spending the id when a first
name would do makes every duplicate look like a hash collision.

Labels are computed per *list*, not per bot, because ambiguity is a property
of what is on screen together. The same bot is "Rocky II" in a filtered table
where it is alone and "Rocky II · ivan" in the full standings, and that is
correct: the suffix exists to answer a question the reader can only have when
both rows are visible.
"""

import collections
from typing import Dict, Iterable, Sequence, Tuple

from .registry import short_id

# Thin space either side: the suffix is an aside, not part of the name, and a
# bare hyphen reads as one ("Rocky II - ivan" looks like somebody's bot is
# called that).
SEPARATOR = " · "

# One entry to be labelled: the bot's id, its display name, its submitter.
Entry = Tuple[str, str, str]


def person(submitter: str) -> str:
    """The readable short form of a submitter — an email's local part.

    Submitters are full addresses (the dashboard records the signed-in email),
    and a registry is normally one organisation, so the part before the @ is
    both unique and what people actually call each other. Anything that is not
    an address is returned unchanged: a CLI sync can attribute a bot to any
    string, and truncating "Robotics Club" at a nonexistent @ would lose it.
    """
    who = (submitter or "").strip()
    local, at, _ = who.partition("@")
    return local if (at and local) else who


def display_labels(entries: Iterable[Entry],
                   submitter_shown: bool = False) -> Dict[str, str]:
    """Map each bot id to the shortest label that is unambiguous in this list.

    `submitter_shown` is for a table that already has a Submitter column. There,
    two bots called Rocky II owned by different people are *already* told apart
    by the next cell along, so neither needs a suffix — and appending "· ivan"
    beside a cell reading "ivan@muttdata.ai" is the same fact printed twice.
    Only a name repeated *by one owner* is ambiguous on such a screen, and that
    is the case no column can fix, so it goes straight to the id.

    Ids not in `entries` are simply absent from the result, so a caller that
    renders an opponent it has no row for (a removed bot in someone else's
    match log) gets a KeyError rather than a silently wrong name — use
    `label_for` with a fallback if that case is expected.
    """
    rows = [(bot_id, (name or "").strip(), submitter or "")
            for bot_id, name, submitter in entries]

    groups = collections.defaultdict(list)
    for row in rows:
        # Case-folded: "Rocky II" and "rocky ii" are different strings that
        # read as the same name, so they are ambiguous with each other and both
        # need the suffix. Grouping case-sensitively would print them bare and
        # leave the reader to spot the capital.
        key = row[1].casefold()
        if submitter_shown:
            key = (key, row[2].strip().casefold())
        groups[key].append(row)

    labels: Dict[str, str] = {}
    for group in groups.values():
        if len(group) == 1:
            bot_id, name, _ = group[0]
            # A nameless bot is not something the registry produces, but it is
            # one bad `set_name` away, and an empty cell is unclickable.
            labels[bot_id] = name or short_id(bot_id)
            continue
        labels.update(_disambiguate(group, submitter_shown))
    return labels


def _disambiguate(group: Sequence[Entry],
                  submitter_shown: bool = False) -> Dict[str, str]:
    """Label every bot in one ambiguous group, by the cheapest suffix that
    separates all of them."""
    ladder = (
        lambda row: person(row[2]),          # ivan
        lambda row: row[2].strip(),          # ivan@muttdata.ai
        lambda row: short_id(row[0]),        # a3f9c1d4e07b
    )
    if submitter_shown:
        # Everyone in this group shares a submitter — that is what made the
        # group — so the two submitter rungs cannot separate anybody.
        ladder = ladder[2:]
    for suffix_of in ladder:
        suffixes = [suffix_of(row) for row in group]
        # Every bot in the group must get a *distinct, non-empty* suffix for
        # this rung to work. One unattributed bot among three named ones means
        # the submitter rung cannot label the whole group, so the group drops to
        # ids together — a table where two rows are told apart by name and the
        # third by hash is a table with two rules in it.
        if all(suffixes) and len({s.casefold() for s in suffixes}) == len(suffixes):
            return {row[0]: (row[1] or short_id(row[0])) + SEPARATOR + suffix
                    for row, suffix in zip(group, suffixes)}
    # Unreachable: ids are distinct by construction, so the last rung always
    # separates. Spelled out anyway — falling off this loop silently would
    # return no labels at all for the group.
    return {row[0]: (row[1] or short_id(row[0])) + SEPARATOR + short_id(row[0])
            for row in group}


def label_for(labels: Dict[str, str], bot_id: str) -> str:
    """One label, for a bot that may not be in the labelled list at all.

    Falls back to the short id: a match log can name an opponent the registry
    no longer has a row for, and twelve hex characters at least says "a bot
    that is gone" instead of printing a 64-char digest in a table cell.
    """
    return labels.get(bot_id) or short_id(bot_id)
