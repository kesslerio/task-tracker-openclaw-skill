import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import reconcile


def _claim(title: str, **overrides):
    item = {
        "title": title,
        "done": True,
        "raw_line": f"- [x] **{title}**",
    }
    item.update(overrides)
    return item


def _candidate(
    title: str,
    *,
    source: str = "github",
    evidence_hash: str = "sha256:test-1",
    matched_task_id: str | None = None,
    auto_done_eligible: bool = True,
    url: str | None = "https://example.com/repo/pull/101",
):
    return {
        "schema_version": 1,
        "source": source,
        "source_type": source,
        "kind": "activity",
        "provider_id": f"example/repo#{evidence_hash.rsplit('-', 1)[-1]}",
        "provider_state": "state-1",
        "evidence_hash": evidence_hash,
        "occurred_at": "2026-06-23T10:00:00-07:00",
        "match_title": title,
        "title": title,
        "url": url,
        "auto_done_eligible": auto_done_eligible,
        "decision": "evidence-link" if matched_task_id else "needs-review",
        "matched_task_id": matched_task_id,
        "suggested_task_id": matched_task_id,
        "association_status": "auto-associated" if matched_task_id else "needs-review",
        "match": {
            "decision": "evidence-link" if matched_task_id else "needs-review",
            "match_type": "exact-id-or-link" if matched_task_id else "normalized-title",
            "matched_task_id": matched_task_id,
            "suggested_task_id": matched_task_id,
        },
    }


def test_user_done_and_harvested_pr_merge_once_preserving_user_words():
    user_stated = [
        _claim(
            "Closed the payroll sync loop with Finance",
            task_id="tsk_payroll",
            area="Ops",
        )
    ]
    evidence = [
        _candidate(
            "Fix payroll sync edge case [example/repo#101]",
            evidence_hash="sha256:test-pr",
            matched_task_id="tsk_payroll",
        )
    ]

    completed, remaining = reconcile.merge(user_stated, evidence)

    assert remaining == []
    assert len(completed) == 1
    assert completed[0]["title"] == "Closed the payroll sync loop with Finance"
    assert completed[0]["area"] == "Ops"
    assert [entry["source"] for entry in completed[0]["provenance"]] == ["user", "github"]
    assert completed[0]["provenance"][1]["evidence_hash"] == "sha256:test-pr"


def test_user_done_without_matching_evidence_stays_unchanged():
    user_stated = [_claim("Send partner update", area="Sales")]
    evidence = [_candidate("Unrelated PR", evidence_hash="sha256:test-other")]

    completed, remaining = reconcile.merge(user_stated, evidence)

    assert completed == user_stated
    assert remaining == evidence


def test_generic_short_title_does_not_absorb_unrelated_title_candidate():
    candidate = _candidate("Update", evidence_hash="sha256:test-generic-update")

    completed, remaining = reconcile.merge([_claim("Update")], [candidate])

    assert completed == [_claim("Update")]
    assert remaining == [candidate]


def test_specific_multi_word_title_matches_evidence_by_title():
    candidate = _candidate("Ship monthly billing reconcile", evidence_hash="sha256:test-specific-title")

    completed, remaining = reconcile.merge([_claim("Ship monthly billing reconcile")], [candidate])

    assert remaining == []
    assert completed[0]["title"] == "Ship monthly billing reconcile"
    assert completed[0]["provenance"][1]["evidence_hash"] == "sha256:test-specific-title"


def test_short_title_still_matches_by_evidence_hash_or_task_id():
    evidence_hash_candidate = _candidate("Unrelated hash evidence", evidence_hash="sha256:test-short-title")
    task_id_candidate = _candidate(
        "Unrelated task id evidence",
        evidence_hash="sha256:test-short-title-task",
        matched_task_id="tsk_4242424242",
    )

    completed, remaining = reconcile.merge(
        [_claim("Fix", evidence_hash="sha256:test-short-title", task_id="tsk_4242424242")],
        [evidence_hash_candidate, task_id_candidate],
    )

    assert remaining == []
    assert completed[0]["title"] == "Fix"
    assert [entry["evidence_hash"] for entry in completed[0]["provenance"][1:]] == [
        "sha256:test-short-title",
        "sha256:test-short-title-task",
    ]


def test_calendar_matching_confirmed_item_supplements_but_calendar_alone_stays_candidate():
    confirmed_calendar = _candidate(
        "Met with Finance review",
        source="calendar",
        evidence_hash="sha256:test-calendar-1",
        auto_done_eligible=False,
        url=None,
    )
    unconfirmed_calendar = _candidate(
        "Weekly planning block",
        source="calendar",
        evidence_hash="sha256:test-calendar-2",
        auto_done_eligible=False,
        url=None,
    )

    completed, remaining = reconcile.merge(
        [_claim("Met with Finance review")],
        [confirmed_calendar, unconfirmed_calendar],
    )

    assert len(completed) == 1
    assert completed[0]["title"] == "Met with Finance review"
    assert completed[0]["provenance"][1]["source"] == "calendar"
    assert completed[0]["provenance"][1]["auto_done_eligible"] is False
    assert remaining == [unconfirmed_calendar]


def test_completion_after_claim_dedupes_user_claims():
    completed, remaining = reconcile.merge(
        [
            _claim("Ship onboarding cleanup", completed_date="2026-06-23"),
            _claim("Ship onboarding cleanup", task_id="tsk_onboarding"),
        ],
        [],
    )

    assert remaining == []
    assert len(completed) == 1
    assert completed[0]["title"] == "Ship onboarding cleanup"


def test_explicit_task_ref_without_user_confirmation_stays_candidate():
    candidate = _candidate(
        "Fix payroll sync task_id::tsk_payroll",
        evidence_hash="sha256:test-explicit",
        matched_task_id="tsk_payroll",
    )

    completed, remaining = reconcile.merge([], [candidate])

    assert completed == []
    assert remaining == [candidate]


def test_explicit_task_ref_matches_daily_note_text_claim():
    candidate = _candidate(
        "Fix payroll sync task_id::tsk_payroll",
        evidence_hash="sha256:test-text-ref",
        matched_task_id="tsk_payroll",
    )

    completed, remaining = reconcile.merge(
        [_claim("Closed payroll sync task_id::tsk_payroll")],
        [candidate],
    )

    assert remaining == []
    assert completed[0]["title"] == "Closed payroll sync task_id::tsk_payroll"
    assert completed[0]["provenance"][1]["evidence_hash"] == "sha256:test-text-ref"


def test_url_embedded_task_id_does_not_match_daily_note_text_claim():
    candidate = _candidate(
        "Different github activity",
        evidence_hash="sha256:test-url-id",
        matched_task_id="tsk_4242424242",
    )

    completed, remaining = reconcile.merge(
        [
            _claim(
                "Did unrelated local thing",
                raw_line="- [x] Did unrelated local thing; see https://example.test/t?id::tsk_4242424242",
            )
        ],
        [candidate],
    )

    assert completed == [
        _claim(
            "Did unrelated local thing",
            raw_line="- [x] Did unrelated local thing; see https://example.test/t?id::tsk_4242424242",
        )
    ]
    assert remaining == [candidate]


def test_space_preceded_task_id_matches_daily_note_text_claim():
    candidate = _candidate(
        "Different github activity",
        evidence_hash="sha256:test-space-id",
        matched_task_id="tsk_4242424242",
    )

    completed, remaining = reconcile.merge(
        [_claim("Did the thing", raw_line="- [x] did the thing task_id:: tsk_4242424242")],
        [candidate],
    )

    assert remaining == []
    assert completed[0]["title"] == "Did the thing"
    assert completed[0]["provenance"][1]["matched_task_id"] == "tsk_4242424242"


def test_candidate_with_no_claim_and_no_ref_stays_candidate():
    candidate = _candidate(
        "Investigated a possible issue",
        evidence_hash="sha256:test-no-ref",
        matched_task_id=None,
    )

    completed, remaining = reconcile.merge([], [candidate])

    assert completed == []
    assert remaining == [candidate]


def test_reconcile_is_idempotent_for_completed_and_remaining_sets():
    user_stated = [_claim("Close billing reconciliation", task_id="tsk_billing")]
    evidence = [
        _candidate(
            "Close billing reconciliation",
            evidence_hash="sha256:test-billing",
            matched_task_id="tsk_billing",
        ),
        _candidate("Unclaimed activity", evidence_hash="sha256:test-unclaimed"),
    ]

    first_completed, first_remaining = reconcile.merge(copy.deepcopy(user_stated), copy.deepcopy(evidence))
    second_completed, second_remaining = reconcile.merge(copy.deepcopy(user_stated), copy.deepcopy(evidence))
    rerun_completed, rerun_remaining = reconcile.merge(copy.deepcopy(first_completed), copy.deepcopy(first_remaining))

    assert second_completed == first_completed
    assert second_remaining == first_remaining
    assert rerun_completed == first_completed
    assert rerun_remaining == first_remaining
