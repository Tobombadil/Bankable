"""The "None" gate (docs/50-audit-2026-09-18.md §3.2: "two templates can publish the literal word
None"). Every template in `services/social/editorial.py` and the digest body in
`services/alerts/evaluate.py` is an f-string over fields that are legitimately `None` when a
source did not record them ("omit a clause if its field is null; never invent a value", docs/32
§3.3); an interpolated `None` is a template bug, and the fix is to refuse to publish the text
rather than trust every template to remember every optional field.

The token is matched on word boundaries, so a project called "Nonesuch Ridge Solar" or a source
named "Nonell County" passes and only a bare `None` trips it.
"""

from __future__ import annotations

import re

BARE_NONE_RE = re.compile(r"\bNone\b")


class BareNoneError(ValueError):
    """Raised by `reject_bare_none` when a rendered text carries the literal token `None`."""

    def __init__(self, template_id: str, text: str) -> None:
        self.template_id = template_id
        self.text = text
        super().__init__(f"template {template_id!r} rendered a bare 'None' token")


def contains_bare_none(text: str) -> bool:
    return BARE_NONE_RE.search(text) is not None


def reject_bare_none(text: str, *, template_id: str) -> str:
    """Returns `text` unchanged when it is clean; raises `BareNoneError` otherwise. Placed at the
    end of every render path so nothing downstream (review queue, publisher, mail port) has to
    re-check."""
    if contains_bare_none(text):
        raise BareNoneError(template_id, text)
    return text


__all__ = ["BARE_NONE_RE", "BareNoneError", "contains_bare_none", "reject_bare_none"]
