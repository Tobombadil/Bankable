"""`scripts/check_manifest_licences.py` under the ordinary pytest job (2026-09-18 audit, docs/50 §3.1
"fourteen register-`unknown` sources are recorded `attribution` in the manifest"): the committed
`data/sources.yaml` must be no more permissive than the docs/13 §6 register on any source, and
the script must catch each way it could drift.
"""

from __future__ import annotations

import pathlib
import textwrap

import pytest

from scripts.check_manifest_licences import (
    MANIFEST,
    REGISTER,
    check,
    default_publication,
    load_manifest,
    parse_register,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_committed_manifest_is_no_more_permissive_than_the_register() -> None:
    problems = check(MANIFEST, REGISTER)
    assert problems == [], "\n".join(problems)


def test_every_gated_vocabulary_source_declares_publication_explicitly() -> None:
    entries = [e for e in load_manifest(MANIFEST) if e.get("reuse") is not None]
    assert len(entries) >= 75
    assert all("publication" in e for e in entries)
    assert all(e["publication"] == "none" for e in entries if e["reuse"] in ("restricted", "unknown"))


def test_the_fourteen_audit_sources_are_gated_in_the_manifest() -> None:
    by_id = {e["id"]: e for e in load_manifest(MANIFEST)}
    fourteen = [
        "us.lbnl.queued_up",
        "us.oasis.non_iso_queues",
        "us.state.siting_boards",
        "us.state.puc_dockets",
        "us.utility_rfps",
        "eu.entsoe.tyndp",
        "ie.eirgrid.connections",
        "ca.ieso.connection_status",
        "ca.aeso.connection_list",
        "in.seci_mnre.tenders",
        "br.aneel.leiloes",
        "za.ipp_office.reipppp",
        "mdb.others",
        "global.iea.demo_projects",
    ]
    for sid in fourteen:
        assert by_id[sid]["reuse"] == "unknown", sid
        assert by_id[sid]["publication"] == "none", sid


def test_derived_only_sources_carry_the_explicit_field() -> None:
    by_id = {e["id"]: e for e in load_manifest(MANIFEST)}
    for sid in ("us.iso.caiso.gen_queue", "us.iso.nyiso.gen_queue", "au.aemo.connections_scorecard"):
        assert by_id[sid]["publication"] == "derived_only", sid
    assert by_id["us.iso.ercot.gen_queue"]["publication"] == "raw_ok"


def test_register_parser_takes_the_strictest_class_and_rule() -> None:
    rows = parse_register(REGISTER)
    assert rows["us.iso.pjm.gen_queue"].strictest_class == "restricted"
    assert rows["us.iso.pjm.gen_queue"].strictest_rule == "none"
    assert rows["us.lbnl.queued_up"].strictest_class == "unknown"
    assert rows["news.gdelt.doc"].strictest_class == "open-attribution"
    assert rows["news.gdelt.doc"].strictest_rule == "derived_only"
    assert rows["us.eia.860m"].strictest_class == "public-domain"
    assert rows["us.eia.860m"].strictest_rule == "raw_ok"
    # The reconciliation table under `### 6.1` is not the matrix: it must not overwrite a row.
    assert rows["mdb.others"].class_text.startswith("unknown")


@pytest.mark.parametrize(
    ("reuse", "expected"),
    [
        ("open", "raw_ok"),
        ("attribution", "raw_ok"),
        ("restricted", "none"),
        ("unknown", "none"),
        (None, "none"),
    ],
)
def test_default_publication_per_reuse_class(reuse: str | None, expected: str) -> None:
    assert default_publication(reuse) == expected


_REGISTER = textwrap.dedent(
    """\
    # Register

    ## 6. Per-source publication matrix

    | `source_id` | Class | Publication rule | Evidence | Confidence |
    |---|---|---|---|---|
    | `t.unknown` | unknown | derived-only | not retrieved | low |
    | `t.attr_restricted` | attribution-restricted | derived-only + credit | quoted | high |
    | `t.public` | public-domain | raw-ok | §105 | high |
    | `t.restricted` | restricted | **link-out-only** | quoted | high |
    | `t.mixed` | restricted (DM2) / attribution-restricted | link-out-only now; derived-only later | q | m |
    | `t.noncommercial` | noncommercial | raw-ok while the posture is noncommercial; credit + link | q | m |

    ## 7. Counsel
    """
)


def _write(tmp_path: pathlib.Path, manifest_yaml: str) -> tuple[pathlib.Path, pathlib.Path]:
    manifest = tmp_path / "sources.yaml"
    manifest.write_text("version: 2026-09-18\nsources:\n" + textwrap.dedent(manifest_yaml), encoding="utf-8")
    register = tmp_path / "13.md"
    register.write_text(_REGISTER, encoding="utf-8")
    return manifest, register


def test_register_unknown_recorded_attribution_fails(tmp_path: pathlib.Path) -> None:
    manifest, register = _write(
        tmp_path,
        """\
        - id: t.unknown
          reuse: attribution
          publication: derived_only
        """,
    )
    problems = check(manifest, register)
    assert any("t.unknown" in p and "(R2)" in p for p in problems), problems
    assert any("t.unknown" in p and "(R4)" not in p for p in problems)


def test_register_unknown_recorded_unknown_none_passes(tmp_path: pathlib.Path) -> None:
    manifest, register = _write(
        tmp_path,
        """\
        - id: t.unknown
          reuse: unknown
          publication: none
        - id: t.public
          reuse: open
          publication: raw_ok
        - id: t.mixed
          reuse: restricted
          publication: none
        - id: social.owned
          category: social_channel
        """,
    )
    assert check(manifest, register) == []


def test_raw_ok_under_a_derived_only_rule_fails(tmp_path: pathlib.Path) -> None:
    manifest, register = _write(
        tmp_path,
        """\
        - id: t.attr_restricted
          reuse: attribution
          publication: raw_ok
        """,
    )
    problems = check(manifest, register)
    assert any("(R3)" in p for p in problems), problems
    assert any("(R4)" in p for p in problems), problems


def test_restricted_source_must_be_publication_none(tmp_path: pathlib.Path) -> None:
    manifest, register = _write(
        tmp_path,
        """\
        - id: t.restricted
          reuse: restricted
          publication: derived_only
        """,
    )
    problems = check(manifest, register)
    assert any("(R4)" in p for p in problems), problems


def test_source_missing_from_register_fails(tmp_path: pathlib.Path) -> None:
    manifest, register = _write(
        tmp_path,
        """\
        - id: t.not_registered
          reuse: open
          publication: raw_ok
        """,
    )
    problems = check(manifest, register)
    assert problems == ["t.not_registered: not in the docs/13 §6 register matrix (R1)"]


def test_missing_publication_field_is_reported_and_defaults(tmp_path: pathlib.Path) -> None:
    manifest, register = _write(
        tmp_path,
        """\
        - id: t.public
          reuse: open
        - id: t.unknown
          reuse: unknown
        """,
    )
    problems = check(manifest, register)
    assert [p for p in problems if "no `publication` field" in p and "t.public" in p]
    # `t.unknown` defaults to `none`, so only the missing-field line, no R4.
    assert not [p for p in problems if "t.unknown" in p and "(R4)" in p]


def test_bad_vocabulary_is_reported(tmp_path: pathlib.Path) -> None:
    manifest, register = _write(
        tmp_path,
        """\
        - id: t.public
          reuse: free
          publication: raw_ok
        - id: t.restricted
          reuse: restricted
          publication: sometimes
        """,
    )
    problems = check(manifest, register)
    assert any("reuse='free'" in p for p in problems)
    assert any("publication='sometimes'" in p for p in problems)


# ------------------------------------------------------------------ the noncommercial class (docs/26)
def test_noncommercial_register_class_admits_noncommercial_reuse_raw_ok(tmp_path: pathlib.Path) -> None:
    """The vocabulary row docs/13 §0 gained on 2026-09-25: a `noncommercial` register class supports
    manifest `reuse: noncommercial` with `publication: raw_ok` (the posture, not this check, decides
    whether the class publishes at all)."""
    manifest, register = _write(
        tmp_path,
        """\
        - id: t.noncommercial
          reuse: noncommercial
          publication: raw_ok
        """,
    )
    assert check(manifest, register) == []
    rows = parse_register(register)
    assert rows["t.noncommercial"].strictest_class == "noncommercial"
    assert rows["t.noncommercial"].strictest_rule == "raw_ok"


def test_noncommercial_register_class_refuses_attribution_in_the_manifest(tmp_path: pathlib.Path) -> None:
    """`attribution` ranks above `noncommercial`: recording a CC BY-NC source as `attribution` is the
    drift this check exists for."""
    manifest, register = _write(
        tmp_path,
        """\
        - id: t.noncommercial
          reuse: attribution
          publication: raw_ok
        - id: t.unknown
          reuse: noncommercial
          publication: raw_ok
        """,
    )
    problems = check(manifest, register)
    assert any("t.noncommercial" in p and "(R2)" in p for p in problems), problems
    # ...and an `unknown` register row does not support `noncommercial` either: the terms must be
    # read and the class written into the register before the manifest may claim it.
    assert any("t.unknown" in p and "(R2)" in p for p in problems), problems


def test_noncommercial_defaults_to_raw_ok_publication() -> None:
    assert default_publication("noncommercial") == "raw_ok"


def test_no_committed_source_is_noncommercial_yet() -> None:
    """Measured 2026-09-25: the class exists in the vocabulary but no manifest entry carries it —
    reclassification is per-source work with the terms in front of the reviewer (docs/26 §2). The
    day one does, replace this with the named list, as the fourteen-source test above does."""
    assert [e["id"] for e in load_manifest(MANIFEST) if e.get("reuse") == "noncommercial"] == []
