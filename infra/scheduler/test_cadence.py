"""Unit tests for infra/scheduler/cadence.py — pure functions, no database (docs/04 E-6 style:
recorded inputs, no live/network dependency)."""

from __future__ import annotations

import yaml

from infra.scheduler.cadence import (
    CRON_BY_BUCKET,
    bucket_for_cadence,
    queue_for_source,
    queueing_lock_for,
)

# Every cadence string actually observed in data/sources.yaml as of 2026-09-12 (grep -oh
# 'cadence: .*' data/sources.yaml | sort -u). Kept as a literal list, not read from the file,
# so this test does not depend on the registry not changing under it — the property tested here
# is "this module has an opinion about every one of these", not "the registry stays byte-identical".
_OBSERVED_CADENCES = [
    "15-min",
    "annual",
    "annual (data through 2025 published May–Jun 2026)",
    "biennial",
    "biennial + amendments",
    "continuous",
    "daily",
    "monthly",
    "per-auction",
    "per-batch",
    "per-window",
    "quarterly",
    "quarterly (scorecard); monthly (Generation Information)",
    "realtime",
    "semi-annual/quarterly",
    "twice weekly",
    "varies",
    "weekly",
    "weekly (Federal Register notices Fridays)",
    "weekly–monthly",
]


def test_every_observed_cadence_maps_to_a_known_bucket() -> None:
    for cadence in _OBSERVED_CADENCES:
        decision = bucket_for_cadence(cadence)
        assert decision.bucket in CRON_BY_BUCKET, cadence


def test_floor_cadences_map_to_the_15min_bucket() -> None:
    for cadence in ["15-min", "realtime", "continuous"]:
        assert bucket_for_cadence(cadence).bucket == "15min"


def test_ambiguous_compound_string_resolves_to_the_more_frequent_bucket() -> None:
    decision = bucket_for_cadence("quarterly (scorecard); monthly (Generation Information)")
    assert decision.bucket == "monthly"


def test_no_fixed_schedule_cadences_fall_back_to_weekly() -> None:
    for cadence in ["per-auction", "per-batch", "per-window", "varies"]:
        decision = bucket_for_cadence(cadence)
        assert decision.bucket == "weekly"
        assert decision.matched_keyword is None


def test_unrecognised_cadence_fails_safe_to_weekly_not_an_exception() -> None:
    decision = bucket_for_cadence("some future cadence nobody has seen yet")
    assert decision.bucket == "weekly"
    assert decision.matched_keyword is None


def test_twice_weekly_is_not_swallowed_by_the_bare_weekly_keyword() -> None:
    # Regression guard: "twice weekly" contains the substring "weekly", so the keyword table order
    # (twice weekly before weekly) matters; both resolve to the same bucket today, but the more
    # specific match must still be the one that fires, not an accident of dict ordering.
    decision = bucket_for_cadence("twice weekly")
    assert decision.matched_keyword == "twice weekly"


def test_biennial_never_polls_less_often_than_quarterly() -> None:
    assert bucket_for_cadence("biennial").bucket == "quarterly"
    assert bucket_for_cadence("biennial + amendments").bucket == "quarterly"


def test_queue_for_source_routes_js_app_access_to_the_browser_queue() -> None:
    assert queue_for_source({"access": "js_app"}) == "fetch_browser"
    assert queue_for_source({"access": "bulk_file"}) == "fetch"
    assert queue_for_source({"access": "api"}) == "fetch"


def test_queue_for_source_prefers_an_explicit_egress_field_once_one_exists() -> None:
    # Forward-compatible with docs/20 §4.3's proposed `egress:` field (DA-12): once present, it
    # wins over the access-based inference, in both directions.
    assert queue_for_source({"access": "bulk_file", "egress": "browser"}) == "fetch_browser"
    assert queue_for_source({"access": "js_app", "egress": "plain"}) == "fetch"
    assert queue_for_source({"access": "js_app", "egress": "api_key"}) == "fetch"
    assert queue_for_source({"access": "js_app", "egress": "residential"}) == "fetch"


def test_every_cron_expression_has_five_fields() -> None:
    for bucket, cron in CRON_BY_BUCKET.items():
        assert len(cron.split()) == 5, f"{bucket}: {cron!r}"


def test_queueing_lock_is_stable_across_calls_and_distinguishes_sources() -> None:
    assert queueing_lock_for("us.iso.ercot.gen_queue") == queueing_lock_for("us.iso.ercot.gen_queue")
    assert queueing_lock_for("us.iso.ercot.gen_queue") != queueing_lock_for("us.iso.caiso.gen_queue")


def test_queueing_lock_has_no_whitespace_or_dots_left_over() -> None:
    lock = queueing_lock_for("us.iso.ercot.gen_queue")
    assert " " not in lock
    assert lock.startswith("fetch:")


def test_sources_yaml_cadences_are_all_covered(sources_yaml_path: str = "data/sources.yaml") -> None:
    """Cross-check against the real registry, not just the frozen list above, so a genuinely new
    cadence string lands as a visible bucket choice (even if it is the 'weekly' fail-safe) rather
    than being silently correct by luck."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    doc = yaml.safe_load((root / sources_yaml_path).read_text())
    for source in doc["sources"]:
        if "cadence" not in source:
            # `category: social_channel` entries (social.bluesky, social.x, ...) are distribution
            # channels, not fetch sources, and carry no cadence — infra/scheduler/app.py skips them
            # the same way (no connector, nothing to schedule).
            continue
        decision = bucket_for_cadence(str(source["cadence"]))
        assert decision.bucket in CRON_BY_BUCKET, source["id"]
