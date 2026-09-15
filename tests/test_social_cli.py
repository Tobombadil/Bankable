"""services/social/__main__.py: `python -m services.social <command>` (task brief item 5)."""

import pathlib
import sys

import pandas as pd
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from services.social.__main__ import build_parser, main
from services.social.queue import ReviewQueue


@pytest.fixture
def diff_events_parquet(tmp_path: pathlib.Path) -> pathlib.Path:
    events = pd.DataFrame(
        [
            {
                "event_type": "new",
                "record_id": "caiso:1",
                "source_id": "us.iso.caiso.gen_queue",
                "field": "lifecycle_state",
                "before": None,
                "after": "filed",
                "observed_at": "2026-09-10T00:00:00+00:00",
            },
            {
                # below the proposal.new threshold -- should not earn a draft
                "event_type": "new",
                "record_id": "caiso:2",
                "source_id": "us.iso.caiso.gen_queue",
                "field": "lifecycle_state",
                "before": None,
                "after": "filed",
                "observed_at": "2026-09-10T00:00:00+00:00",
            },
        ]
    )
    path = tmp_path / "events.parquet"
    events.to_parquet(path)
    return path


@pytest.fixture
def records_parquet(tmp_path: pathlib.Path) -> pathlib.Path:
    records = pd.DataFrame(
        [
            {
                "record_id": "caiso:1",
                "source_id": "us.iso.caiso.gen_queue",
                "name_canonical": "Solar One",
                "technology": "solar",
                "capacity_mw": 250.0,
                "county": "Kern",
                "state": "CA",
                "iso": "CAISO",
                "queue_id": "Q1234",
                "sponsor_name": "Acme Solar LLC",
                "retrieved_at": "2026-09-10T12:00:00+00:00",
            },
            {
                "record_id": "caiso:2",
                "source_id": "us.iso.caiso.gen_queue",
                "name_canonical": "Tiny Solar",
                "technology": "solar",
                "capacity_mw": 3.0,
                "county": "Kern",
                "state": "CA",
                "iso": "CAISO",
                "queue_id": "Q9999",
                "sponsor_name": "Small Co",
                "retrieved_at": "2026-09-10T12:00:00+00:00",
            },
        ]
    )
    path = tmp_path / "records.parquet"
    records.to_parquet(path)
    return path


class TestDraftCommand:
    def test_draft_reads_diff_shaped_parquet_and_queues_eligible_drafts(
        self,
        tmp_path: pathlib.Path,
        diff_events_parquet: pathlib.Path,
        records_parquet: pathlib.Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        queue_path = tmp_path / "queue.json"
        rc = main(
            [
                "--queue-path",
                str(queue_path),
                "draft",
                "--events",
                str(diff_events_parquet),
                "--records",
                str(records_parquet),
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "3 queued" in out  # caiso:1 -> bluesky, x, linkedin; caiso:2 below threshold

        q = ReviewQueue(queue_path)
        drafts = q.list_drafts()
        assert len(drafts) == 3
        assert {d.channel for d in drafts} == {"bluesky", "x", "linkedin"}
        assert all(d.subject_id == "caiso:1" for d in drafts)
        for d in drafts:
            assert "infraque.com" in d.link_url  # placeholder hostname convention preserved verbatim

    def test_draft_is_idempotent_on_rerun(
        self,
        tmp_path: pathlib.Path,
        diff_events_parquet: pathlib.Path,
        records_parquet: pathlib.Path,
    ) -> None:
        queue_path = tmp_path / "queue.json"
        args = [
            "--queue-path",
            str(queue_path),
            "draft",
            "--events",
            str(diff_events_parquet),
            "--records",
            str(records_parquet),
        ]
        main(args)
        main(args)  # re-running the same events must not double-queue
        q = ReviewQueue(queue_path)
        assert len(q.list_drafts()) == 3


class TestReviewCommands:
    def test_list_approve_reject_round_trip(
        self,
        tmp_path: pathlib.Path,
        diff_events_parquet: pathlib.Path,
        records_parquet: pathlib.Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        queue_path = tmp_path / "queue.json"
        main(
            [
                "--queue-path",
                str(queue_path),
                "draft",
                "--events",
                str(diff_events_parquet),
                "--records",
                str(records_parquet),
            ]
        )
        capsys.readouterr()

        assert main(["--queue-path", str(queue_path), "review", "list"]) == 0
        listing = capsys.readouterr().out
        assert "bluesky" in listing and "OK" in listing

        q = ReviewQueue(queue_path)
        bluesky_draft = next(d for d in q.list_drafts() if d.channel == "bluesky")
        x_draft = next(d for d in q.list_drafts() if d.channel == "x")

        assert (
            main(
                [
                    "--queue-path",
                    str(queue_path),
                    "review",
                    "approve",
                    bluesky_draft.id,
                    "--reviewer",
                    "andrew@example.com",
                ]
            )
            == 0
        )
        assert ReviewQueue(queue_path).get(bluesky_draft.id).status == "approved"

        assert (
            main(
                [
                    "--queue-path",
                    str(queue_path),
                    "review",
                    "reject",
                    x_draft.id,
                    "--reviewer",
                    "andrew@example.com",
                    "--reason",
                    "not_newsworthy",
                ]
            )
            == 0
        )
        assert ReviewQueue(queue_path).get(x_draft.id).status == "rejected"

    def test_reject_rejects_an_unknown_reason_code_at_the_argparse_level(
        self, tmp_path: pathlib.Path
    ) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(
                [
                    "--queue-path",
                    str(tmp_path / "q.json"),
                    "review",
                    "reject",
                    "abc",
                    "--reviewer",
                    "a",
                    "--reason",
                    "nope",
                ]
            )


class TestDryRunCommand:
    def test_dry_run_previews_approved_drafts_without_publishing(
        self,
        tmp_path: pathlib.Path,
        diff_events_parquet: pathlib.Path,
        records_parquet: pathlib.Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        queue_path = tmp_path / "queue.json"
        main(
            [
                "--queue-path",
                str(queue_path),
                "draft",
                "--events",
                str(diff_events_parquet),
                "--records",
                str(records_parquet),
            ]
        )
        q = ReviewQueue(queue_path)
        bluesky_draft = next(d for d in q.list_drafts() if d.channel == "bluesky")
        main(
            [
                "--queue-path",
                str(queue_path),
                "review",
                "approve",
                bluesky_draft.id,
                "--reviewer",
                "a@example.com",
            ]
        )
        capsys.readouterr()

        rc = main(["--queue-path", str(queue_path), "dry-run", "bluesky"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "would send" in out
        # never actually published -- dry-run must not change draft state
        assert ReviewQueue(queue_path).get(bluesky_draft.id).status == "approved"

    def test_dry_run_with_nothing_approved_prints_a_notice(
        self, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        queue_path = tmp_path / "queue.json"
        rc = main(["--queue-path", str(queue_path), "dry-run", "x"])
        assert rc == 0
        assert "no approved" in capsys.readouterr().out


class TestReportCommand:
    def test_report_prints_the_docs_32_template_sections(
        self, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        queue_path = tmp_path / "queue.json"
        rc = main(["--queue-path", str(queue_path), "report"])
        assert rc == 0
        out = capsys.readouterr().out
        for heading in (
            "## Headline",
            "## Per channel",
            "## Review queue",
            "## Budget",
            "## Incidents",
            "## Graduation status",
            "## Recommendations",
        ):
            assert heading in out
        assert "Bluesky" in out and "Linkedin" in out and "| X |" in out
