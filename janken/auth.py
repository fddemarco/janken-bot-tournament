"""Who may sign in to the dashboard.

The dashboard puts every panel behind Google sign-in. This module owns the one
security-critical question in that flow — *is this identity allowed in?* — and
is deliberately stdlib-only, with no Streamlit import, so the rule stays covered
by `python3 -m unittest discover tests` like the rest of the engine.

The check has to happen here, on the token, and not only in the sign-in URL:
Google's `hd` parameter merely *hints* which Workspace to offer, and a user is
free to ignore the hint and pick a personal account instead.
"""

import os
from typing import Iterable, Optional, Sequence, Tuple

# Only Mutt Data staff and participants run the championship.
DEFAULT_DOMAINS: Tuple[str, ...] = ("muttdata.ai",)

DOMAINS_ENV = "JANKEN_ALLOWED_DOMAINS"


def parse_domains(raw: Optional[str],
                  default: Sequence[str] = DEFAULT_DOMAINS) -> Tuple[str, ...]:
    """Parse `"muttdata.ai, @example.com"` into a lowercase domain tuple.

    Accepts commas and/or whitespace as separators and tolerates a leading `@`,
    so the value can be written the way people say it out loud. An empty or
    all-junk value falls back to `default` rather than to "allow everyone".
    """
    if not raw:
        return tuple(default)
    parts = (p.strip().lstrip("@").lower() for p in raw.replace(",", " ").split())
    domains = tuple(dict.fromkeys(p for p in parts if "." in p))  # ordered, unique
    return domains or tuple(default)


def allowed_domains() -> Tuple[str, ...]:
    """The allowlist for this deployment ($JANKEN_ALLOWED_DOMAINS, else Mutt Data)."""
    return parse_domains(os.environ.get(DOMAINS_ENV))


def email_domain(email: object) -> Optional[str]:
    """The domain of `email`, lowercased — None if it isn't a single address."""
    if not isinstance(email, str):
        return None
    parts = email.strip().lower().split("@")
    if len(parts) != 2 or not parts[0] or "." not in parts[1]:
        return None
    return parts[1]


def rejection_reason(email: object, verified: object = False,
                     domains: Optional[Iterable[str]] = None) -> Optional[str]:
    """Why this identity may *not* use the dashboard; None means it may.

    `verified` is the provider's `email_verified` claim. Google always sends it
    for Workspace accounts, so a missing or false claim is treated as untrusted
    — this gate fails closed on purpose.
    """
    allowed = tuple(domains) if domains is not None else allowed_domains()
    domain = email_domain(email)
    if domain is None:
        return "the sign-in provider returned no usable email address"
    shown = email.strip().lower() if isinstance(email, str) else ""
    if verified is not True:
        return "%s isn't a verified address with the sign-in provider" % shown
    if domain not in allowed:
        return "only %s accounts may sign in (you used %s)" % (
            " or ".join("@" + d for d in allowed), shown)
    return None
