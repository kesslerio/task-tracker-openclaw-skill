import json
import sys
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from task_ledger import MalformedLedgerError, read_events, read_events_report


def test_read_events_warns_when_skipping_malformed_jsonl_lines(tmp_path):
    ledger = tmp_path / "events.jsonl"
    ledger.write_text(
        json.dumps({"event_type": "ok", "event_id": "evt_1"}) + "\n"
        '{"event_type":"truncated"\n'
        + json.dumps({"event_type": "ok", "event_id": "evt_2"}) + "\n"
    )

    with pytest.warns(RuntimeWarning, match="malformed ledger JSONL"):
        events = read_events(ledger)

    assert [event["event_id"] for event in events] == ["evt_1", "evt_2"]


def test_read_events_report_returns_malformed_line_details(tmp_path):
    ledger = tmp_path / "events.jsonl"
    ledger.write_text(
        json.dumps({"event_type": "ok", "event_id": "evt_1"}) + "\n"
        '{"event_type":"truncated"\n'
    )

    events, malformed = read_events_report(ledger)

    assert [event["event_id"] for event in events] == ["evt_1"]
    assert len(malformed) == 1
    assert malformed[0].path == str(ledger)
    assert malformed[0].line_number == 2
    assert malformed[0].raw_line == '{"event_type":"truncated"'


def test_read_events_strict_raises_on_malformed_jsonl(tmp_path):
    ledger = tmp_path / "events.jsonl"
    ledger.write_text('{"event_type":"truncated"\n')

    with pytest.raises(MalformedLedgerError) as exc:
        read_events(ledger, strict=True)

    assert exc.value.malformed[0].line_number == 1
