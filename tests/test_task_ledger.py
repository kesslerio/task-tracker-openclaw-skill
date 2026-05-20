import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from task_ledger import read_events


def test_read_events_skips_malformed_jsonl_lines(tmp_path):
    ledger = tmp_path / "events.jsonl"
    ledger.write_text(
        json.dumps({"event_type": "ok", "event_id": "evt_1"}) + "\n"
        '{"event_type":"truncated"\n'
        + json.dumps({"event_type": "ok", "event_id": "evt_2"}) + "\n"
    )

    events = read_events(ledger)

    assert [event["event_id"] for event in events] == ["evt_1", "evt_2"]
