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
    still_unread = [
        "us.oasis.non_iso_queues",
        "us.state.siting_boards",
        "us.state.puc_dockets",
        "us.utility_rfps",
        "mdb.others",
        "global.iea.demo_projects",
    ]
    for sid in still_unread:
        assert by_id[sid]["reuse"] == "unknown", sid
        assert by_id[sid]["publication"] == "none", sid
    # Four of the fourteen had their terms read and quoted on 2026-10-23 (docs/13 §2.1, §2.20): each
    # left `unknown` only with the dated quote in its `license`, and only as far as the quote allows.
    read_since = {
        # Its own licence is CC BY 4.0 (register class open-attribution), but its rows are the non-ISO
        # utilities' queues, whose terms are unread, so the manifest records the rows' class (§2.1).
        "us.lbnl.queued_up": ("unknown", "none"),
        "ca.ieso.connection_status": ("attribution", "derived_only"),
        "ca.aeso.connection_list": ("restricted", "none"),
        "ie.eirgrid.connections": ("restricted", "none"),
    }
    for sid, (reuse, publication) in read_since.items():
        assert (by_id[sid]["reuse"], by_id[sid]["publication"]) == (reuse, publication), sid
        assert "retrieved 2026-10-23" in by_id[sid]["license"], sid
    assert by_id["ca.ieso.connection_status"]["status"] == "pending"  # terms read, nothing built
    # Four more read and quoted on 2026-10-24 (docs/07 §1.8): three restricted, ANEEL's ODbL
    # open-attribution, published derived-only (share-alike is docs/07 §5 item 4).
    read_2026_10_24 = {
        "eu.entsoe.tyndp": ("restricted", "none"),
        "in.seci_mnre.tenders": ("restricted", "none"),
        "za.ipp_office.reipppp": ("restricted", "none"),
        "br.aneel.leiloes": ("attribution", "derived_only"),
    }
    for sid, (reuse, publication) in read_2026_10_24.items():
        assert (by_id[sid]["reuse"], by_id[sid]["publication"]) == (reuse, publication), sid
        assert "retrieved 2026-10-24" in by_id[sid]["license"], sid


def test_lbnl_queued_up_is_held_and_the_loader_loads_none_of_it() -> None:
    """Review finding 1 of the national-queues lane (2026-10-23): with `reuse: attribution` and
    `publication: raw_ok` the first load would have made 12,431 rows public whose operators' terms are
    unread. Held: the registry gates the source (scheduled runs refused; a quarantine run needs
    `--allow-restricted`) and the loader's own gate refuses every row, which also keeps the source out
    of the scheduler's resolve pass (`load_refusal`)."""
    from pipeline.connectors.base import GateViolation
    from pipeline.connectors.registry import Registry
    from services.ingest.loader import load_refusal

    registry = Registry()
    entry = registry.get("us.lbnl.queued_up")
    assert entry.implemented and entry.gated
    assert (load_refusal(entry) or "").startswith("us.lbnl.queued_up has reuse='unknown'")
    with pytest.raises(GateViolation):
        registry.instantiate("us.lbnl.queued_up")


def test_derived_only_sources_carry_the_explicit_field() -> None:
    by_id = {e["id"]: e for e in load_manifest(MANIFEST)}
    for sid in ("us.iso.caiso.gen_queue", "us.iso.nyiso.gen_queue", "au.aemo.connections_scorecard"):
        assert by_id[sid]["publication"] == "derived_only", sid
    assert by_id["us.iso.ercot.gen_queue"]["publication"] == "raw_ok"


def test_register_parser_takes_the_strictest_class_and_rule() -> None:
    rows = parse_register(REGISTER)
    assert rows["us.iso.pjm.gen_queue"].strictest_class == "restricted"
    assert rows["us.iso.pjm.gen_queue"].strictest_rule == "none"
    assert rows["us.iso.miso.gen_queue"].strictest_class == "unknown"
    assert rows["us.lbnl.queued_up"].strictest_class == "open-attribution"
    # "link-out-only (held)" outranks the "raw-ok" ceiling the same cell states for when it is lifted.
    assert rows["us.lbnl.queued_up"].strictest_rule == "none"
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
          licence_url: https://example.org/terms
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


def test_only_the_named_sources_are_noncommercial() -> None:
    """2026-09-26: the first sources reclassified into the `noncommercial` register class
    (docs/13 §6.2) — both read from the same RRC Site Policies clause, quoted there and in
    `data/sources.yaml`. Extend this list only after the register row moves first (docs/26 §2).
    2026-10-21: the four AEP operating companies' RFP pages (AEP Terms and Conditions, quoted in
    docs/28 §5 and `data/vendored/rfp_issuers/issuers.yaml`; register rows added from docs/28 §10).
    2026-10-22: SPP's hub prices (owner decision; SPP's grant excludes use "within a commercial
    publication", docs/29 §2.4; the register row, docs/13 §6, moved first).
    2026-10-24: NSTA's carbon storage licences (NSTA User Agreement: "exploit the Information
    non-commercially", docs/07 §1.7; the register row moved first).
    2026-10-24: SPP's interconnection queue (owner decision, the same clause as SPP's prices;
    docs/07 §3.3)."""
    assert sorted(e["id"] for e in load_manifest(MANIFEST) if e.get("reuse") == "noncommercial") == [
        "gb.nsta.carbon_storage_licences",
        "us.iso.spp.gen_queue",
        "us.iso.spp.prices",
        "us.rfp.appalachian_power",
        "us.rfp.indiana_michigan_power",
        "us.rfp.pso",
        "us.rfp.swepco",
        "us.tx.rrc.class_vi",
        "us.tx.rrc.datasets",
    ]


# ------------------------------------------------- R5/R6: mandated credits and licence links (L-2)
_CREDIT_REGISTER = textwrap.dedent(
    """\
    ## 6. Per-source publication matrix

    | `source_id` | Class | Publication rule | Evidence | Confidence |
    |---|---|---|---|---|
    | `t.exact` | open-attribution | raw-ok + exact string "Supported by Test SO Open Data" | q | high |
    | `t.credit` | attribution-restricted | derived-only + credit "Test ISO" | q | high |
    | `t.ccby` | open-attribution | raw-ok + credit "Doe, CC BY 4.0" and a statement of changes | q | h |
    | `t.filing` | public-domain | derived-only: credit "Source: Test Map (<filing>)" | q | high |
    | `t.notice` | noncommercial | raw-ok; credit "Test Agency", notice "Not endorsed by the State." | q | m |
    """
)


def _credit_check(tmp_path: pathlib.Path, manifest_yaml: str) -> list[str]:
    manifest = tmp_path / "sources.yaml"
    manifest.write_text("version: 2026-10-06\nsources:\n" + textwrap.dedent(manifest_yaml), encoding="utf-8")
    register = tmp_path / "13.md"
    register.write_text(_CREDIT_REGISTER, encoding="utf-8")
    return check(manifest, register)


def test_a_mandated_exact_credit_must_be_carried_verbatim(tmp_path: pathlib.Path) -> None:
    """NESO's licence ends automatically unless its exact statement is shown (docs/13 §2.5)."""
    missing = _credit_check(
        tmp_path,
        """\
        - id: t.exact
          reuse: attribution
          publication: raw_ok
          licence_url: https://example.org/licence
          attribution: "Source: Test System Operator"
        """,
    )
    assert any("t.exact" in p and "(R5)" in p for p in missing), missing
    carried = _credit_check(
        tmp_path,
        """\
        - id: t.exact
          reuse: attribution
          publication: raw_ok
          licence_url: https://example.org/licence
          attribution: "Supported by Test SO Open Data"
        """,
    )
    assert carried == []


def test_a_named_credit_must_appear_and_a_statement_of_changes_must_exist(tmp_path: pathlib.Path) -> None:
    problems = _credit_check(
        tmp_path,
        """\
        - id: t.credit
          reuse: attribution
          publication: derived_only
          licence_url: https://example.org/terms
        - id: t.ccby
          reuse: attribution
          publication: raw_ok
          licence_url: https://creativecommons.org/licenses/by/4.0/
          attribution: "Doe, CC BY 4.0"
        """,
    )
    assert any(p.startswith("t.credit:") and "(R5)" in p for p in problems), problems
    assert any(p.startswith("t.ccby:") and "statement of changes" in p for p in problems), problems
    fixed = _credit_check(
        tmp_path,
        """\
        - id: t.credit
          reuse: attribution
          publication: derived_only
          licence_url: https://example.org/terms
          attribution: "Source: Test ISO"
        - id: t.ccby
          reuse: attribution
          publication: raw_ok
          licence_url: https://creativecommons.org/licenses/by/4.0/
          attribution: "Doe, CC BY 4.0"
          changes_statement: "Modified: fields renamed."
        - id: t.filing
          reuse: open
          publication: derived_only
          attribution: "Source: Test Map"
        """,
    )
    assert fixed == [], fixed  # a `(<placeholder>)` in the register's credit is optional


def test_an_attribution_source_must_link_its_licence_unless_pending_without_a_connector(
    tmp_path: pathlib.Path,
) -> None:
    problems = _credit_check(
        tmp_path,
        """\
        - id: t.credit
          reuse: attribution
          publication: derived_only
          attribution: "Source: Test ISO"
        """,
    )
    assert any(p.startswith("t.credit:") and "(R6)" in p for p in problems), problems
    pending = _credit_check(
        tmp_path,
        """\
        - id: t.credit
          reuse: attribution
          publication: derived_only
          attribution: "Source: Test ISO"
          licence_url_pending: "terms not yet read"
        """,
    )
    assert pending == []


def test_a_required_notice_must_match_the_register_exactly_and_the_register_moves_first(
    tmp_path: pathlib.Path,
) -> None:
    """R5, `notice "X"` (2026-10-22, docs/13 §6.4): AEP's copyright notice and the Texas state
    policy's no-endorsement statement are required on every copy, so a missing or reworded notice
    fails, and so does a notice the register row does not name."""
    row = (
        "- id: t.notice\n"
        "  reuse: noncommercial\n"
        "  publication: raw_ok\n"
        "  licence_url: https://example.org/site-policies\n"
        '  attribution: "Source: Test Agency"\n'
    )
    missing = _credit_check(tmp_path, row)
    assert any(p.startswith("t.notice:") and "mandates the notice" in p for p in missing), missing
    reworded = _credit_check(tmp_path, row + '  notice: "Not endorsed by the state."\n')
    assert any(p.startswith("t.notice:") and "(R5)" in p for p in reworded), reworded
    assert _credit_check(tmp_path, row + '  notice: "Not endorsed by the State."\n') == []
    unnamed = _credit_check(
        tmp_path,
        """\
        - id: t.credit
          reuse: attribution
          publication: derived_only
          licence_url: https://example.org/terms
          attribution: "Source: Test ISO"
          notice: "Copyright Test ISO."
        """,
    )
    assert any(p.startswith("t.credit:") and "register row moves first" in p for p in unnamed), unnamed


def test_the_six_noncommercial_sources_carry_the_notices_their_terms_require() -> None:
    """The committed rows: AEP's copyright notice on its four operating companies, the Texas
    no-endorsement statement on both RRC sources, and the register names each one (R5 above)."""
    by_id = {e["id"]: e for e in load_manifest(MANIFEST)}
    register = parse_register(REGISTER)
    aep = "Copyright 1996-2026 by American Electric Power Company, Inc., all rights reserved."
    rrc = (
        "Neither Inframation nor this information, as presented on Inframation, is endorsed by the State of "
        "Texas or any state agency."
    )
    expected = dict.fromkeys(
        ("us.rfp.pso", "us.rfp.appalachian_power", "us.rfp.swepco", "us.rfp.indiana_michigan_power"), aep
    ) | dict.fromkeys(("us.tx.rrc.class_vi", "us.tx.rrc.datasets"), rrc)
    for sid, notice in expected.items():
        assert by_id[sid]["notice"] == notice, sid
        assert f'notice "{notice}"' in register[sid].rule_text, sid
        assert by_id[sid]["reuse"] == "noncommercial", sid  # the class is not changed by the notice
    for sid in ("us.tx.rrc.class_vi", "us.tx.rrc.datasets"):
        assert by_id[sid]["attribution"] == "Source: Railroad Commission of Texas, https://www.rrc.texas.gov"
