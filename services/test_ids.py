"""`services.ids.unique_slug`: slugs for new records are unique by a checked fallback.

The defect (2026-10-07, web e2e setup on the eval fixture): a new proposal's slug was the slugified
title plus the last six digits of its public id. Those six Crockford digits are the uuid's low 30
bits, which for `services.db.types.uuid7` are all `os.urandom` -- not the timestamp, so minting in
the same millisecond is not the cause; two records sharing a title collide with probability
1 in 2**30 per pair, and 1,352 "Untitled" eval-fixture proposals make that about 1 in 1,200 loads.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

from services.db.models import new_uuid
from services.ids import public_id, slugify, unique_slug

TAIL_BITS = (1 << 30) - 1


def test_a_free_slug_is_the_title_plus_the_six_digit_tail_it_always_was() -> None:
    pid = public_id("prop", new_uuid())
    assert unique_slug("Gemini Solar", pid, set()) == f"gemini-solar-{pid[-6:].lower()}"


def test_a_taken_tail_is_lengthened_one_digit_at_a_time() -> None:
    pid = "prop_01JBQ8K2P4ABCDEFGHJKMNPQRS"
    six, seven = "untitled-mnpqrs", "untitled-kmnpqrs"
    assert unique_slug("Untitled", pid, {six}) == seven
    assert unique_slug("Untitled", pid, {six, seven}) == "untitled-jkmnpqrs"


def test_bare_first_uses_the_bare_slug_when_free_and_a_checked_tail_when_not() -> None:
    pid = "org_01JBQ8K2P4ABCDEFGHJKMNPQRS"
    assert unique_slug("Acme Power, LLC", pid, set(), bare_first=True) == "acme-power-llc"
    taken = {"acme-power-llc", "acme-power-llc-mnpqrs"}
    assert unique_slug("Acme Power, LLC", pid, taken, bare_first=True) == "acme-power-llc-kmnpqrs"


def test_the_whole_id_as_tail_is_the_last_resort_and_a_reused_id_is_refused() -> None:
    pid = "prop_0123456789"
    digits = "0123456789"
    every_shorter = {f"x-{digits[-n:]}" for n in range(6, 10)}
    assert unique_slug("x", pid, every_shorter) == "x-0123456789"
    with pytest.raises(ValueError, match="not a new public id"):
        unique_slug("x", pid, every_shorter | {"x-0123456789"})


def test_same_millisecond_mints_with_the_same_random_tail_get_distinct_slugs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact collision: real `new_uuid()` calls in one frozen millisecond whose last four
    random bytes repeat, so every public id ends in the same six digits."""
    real_urandom = os.urandom
    # `uuid7` reads the module-level `time.time` and `os.urandom`; patched for these mints only.
    monkeypatch.setattr(time, "time", lambda: 1_790_000_000.123)
    monkeypatch.setattr(os, "urandom", lambda n: real_urandom(n - 4) + b"\x5a\x5a\x5a\x5a")
    ids = [new_uuid() for _ in range(50)]
    monkeypatch.undo()

    assert len(set(ids)) == 50
    assert len({u.int >> 80 for u in ids}) == 1, "all minted in the same millisecond"
    pids = [public_id("prop", u) for u in ids]
    assert len({p[-6:] for p in pids}) == 1, "and every old-style six-digit tail is identical"

    old_style = {f"{slugify('Untitled')}-{p[-6:].lower()}" for p in pids}
    assert len(old_style) == 1  # the pre-fix expression: 50 records, one slug

    taken: set[str] = set()
    for p in pids:
        slug = unique_slug("Untitled", p, taken)
        assert slug not in taken
        taken.add(slug)
    assert len(taken) == 50


def test_the_six_digit_tail_is_the_uuids_low_random_bits_not_its_timestamp() -> None:
    """Pins the mechanism: the tail encodes exactly the low 30 bits, which uuid7 fills from
    `os.urandom`; the timestamp is in the leading digits."""
    u = new_uuid()
    tail = public_id("prop", u)[-6:]
    assert tail == public_id("prop", uuid.UUID(int=u.int & TAIL_BITS))[-6:]
