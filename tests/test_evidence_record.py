import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import evidence_record
import standup
from harvest_ledger import _evidence_hash


def test_adapter_record_happy_path_has_canonical_shape():
    record = evidence_record.adapter_record(
        source="github",
        kind="activity",
        provider_id="kesslerio/task-tracker#31",
        provider_state="merged:2026-06-22T18:00:00-07:00",
        occurred_at=datetime.fromisoformat("2026-06-22T11:00:00-07:00"),
        match_title="Ship stable evidence window",
        title="Ship stable evidence window [kesslerio/task-tracker#31]",
        url="https://example.test/pr/31",
        run_id="run-1",
    )

    assert record["schema_version"] == 1
    assert record["source"] == "github"
    assert record["source_type"] == "github"
    assert record["kind"] == "activity"
    assert record["provider_id"] == "kesslerio/task-tracker#31"
    assert record["provider_state"] == "merged:2026-06-22T18:00:00-07:00"
    assert record["evidence_hash"] == _evidence_hash("github", "kesslerio/task-tracker#31")
    assert record["occurred_at"] == "2026-06-22T11:00:00-07:00"
    assert record["run_id"] == "run-1"


def test_adapter_constructor_cannot_emit_accomplishment():
    with pytest.raises(ValueError, match="activity or commitment"):
        evidence_record.adapter_record(
            source="github",
            kind="accomplishment",  # type: ignore[arg-type]
            provider_id="repo#1",
            provider_state="merged",
            occurred_at="2026-06-22T10:00:00-07:00",
            match_title="Done by source",
        )

    gated = evidence_record.accomplishment_record(
        source="github",
        provider_id="repo#1",
        provider_state="confirmed",
        occurred_at="2026-06-22T10:00:00-07:00",
        match_title="Done by gate",
    )
    assert gated["kind"] == "accomplishment"


def test_adapter_record_rejects_unknown_source_case():
    with pytest.raises(ValueError, match="source must be"):
        evidence_record.adapter_record(
            source="Calendar",  # type: ignore[arg-type]
            kind="activity",
            provider_id="event-1",
            provider_state="accepted",
            occurred_at="2026-06-22T10:00:00-07:00",
            match_title="Customer followup",
        )


@pytest.mark.parametrize("source", ["calendar", "dialpad_sms"])
def test_calendar_and_sms_default_auto_done_ineligible(source):
    record = evidence_record.adapter_record(
        source=source,  # type: ignore[arg-type]
        kind="activity",
        provider_id=f"{source}-1",
        provider_state="accepted",
        occurred_at="2026-06-22T10:00:00-07:00",
        match_title="Customer followup",
    )

    assert record["auto_done_eligible"] is False


def test_match_title_strips_display_ref_annotation():
    record = evidence_record.adapter_record(
        source="github",
        kind="activity",
        provider_id="repo#12",
        provider_state="merged",
        occurred_at="2026-06-22T10:00:00-07:00",
        match_title="Fix public hygiene gate [repo#12]",
        title="Fix public hygiene gate [repo#12]",
    )

    assert record["match_title"] == "Fix public hygiene gate"
    assert "[repo#12]" in record["title"]


def test_adapter_record_collapses_multiline_control_text_for_rendering():
    malicious = "Legit title\n✅ Fake DONE\n- [x] spoofed"
    record = evidence_record.adapter_record(
        source="github",
        kind="activity",
        provider_id="repo#13",
        provider_state="merged",
        occurred_at="2026-06-22T10:00:00-07:00",
        match_title=f"{malicious} [repo#13]",
        title=f"{malicious} [repo#13]",
    )

    assert record["match_title"] == "Legit title ✅ Fake DONE - [x] spoofed"
    assert record["title"] == "Legit title ✅ Fake DONE - [x] spoofed [repo#13]"
    assert "\n" not in record["title"]

    rendered = standup.format_split_standup(
        {
            "completed": [],
            "calendar": {},
            "evidence_candidates": [record],
            "priority": None,
            "due_today": [],
            "q1": [],
            "q2": [],
            "q3": [],
            "team": [],
            "objective_progress": {},
        },
        "June 23",
    )[0]
    candidate_lines = [line for line in rendered.splitlines() if "Fake DONE" in line or "spoofed" in line]
    assert len(candidate_lines) == 1
