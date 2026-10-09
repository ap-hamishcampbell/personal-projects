import json
from pathlib import Path

import pytest

from foniks_event_query.reader import (
    LogRecordError,
    discover_log_files,
    extract_payload,
    iter_event_records,
)


def make_log_record(message: str) -> str:
    return json.dumps(
        {
            "Timestamp": "2026-10-02T13:22:58.0486846+02:00",
            "Level": "Warning",
            "MessageTemplate": message,
            "Properties": {"Category": "MQH-FOENIKS-NOTIFICATION"},
        }
    )


def test_extracts_payload_as_json_without_log_escaping() -> None:
    payload = {
        "_type": "LifeEventEngineNotification",
        "commandId": "command-123",
        "eventId": "event-456",
        "key": "life.commitment.amendCoversSelectionRegistered",
        "secondaryId": "secondary-789",
    }
    message = (
        "Got message: message-001 : MQ-FOENIKS-NOTIFICATION-job : "
        + json.dumps(payload)
    )

    assert extract_payload(message) == payload


def test_extract_payload_preserves_delimiters_inside_json_strings() -> None:
    message = 'Got message: id : job : {"description":"left : right"}'

    assert extract_payload(message) == {"description": "left : right"}


def test_ignores_non_payload_messages() -> None:
    assert extract_payload("Router: done processing the message.") is None


def test_discovers_only_expected_logs_in_numeric_rotation_order(
    tmp_path: Path,
) -> None:
    names = [
        "MQH-FOENIKS-NOTIFICATION.log",
        "MQH-FOENIKS-NOTIFICATION.1.log",
        "MQH-FOENIKS-NOTIFICATION.2.log",
        "MQH-FOENIKS-NOTIFICATION.10.log",
        "MQH-FOENIKS-NOTIFICATION.backup.log",
        "OTHER.log",
    ]
    for name in names:
        (tmp_path / name).touch()

    assert [path.name for path in discover_log_files(tmp_path)] == [
        "MQH-FOENIKS-NOTIFICATION.log",
        "MQH-FOENIKS-NOTIFICATION.1.log",
        "MQH-FOENIKS-NOTIFICATION.2.log",
        "MQH-FOENIKS-NOTIFICATION.10.log",
    ]
    assert [path.name for path in discover_log_files(tmp_path, False)] == [
        "MQH-FOENIKS-NOTIFICATION.log",
    ]


def test_reads_ndjson_and_skips_router_records(tmp_path: Path) -> None:
    path = tmp_path / "MQH-FOENIKS-NOTIFICATION.log"
    payload = {"eventId": "event-456"}
    path.write_text(
        make_log_record("Router: done processing the message.")
        + "\n"
        + make_log_record("Got message: id : job : " + json.dumps(payload))
        + "\n",
        encoding="utf-8",
    )

    records = list(iter_event_records(path))

    assert len(records) == 1
    assert records[0].payload == payload
    assert records[0].timestamp == "2026-10-02T13:22:58.0486846+02:00"
    assert records[0].source_file == path.name
    assert records[0].line_number == 2


def test_reports_malformed_outer_json_with_line_number(tmp_path: Path) -> None:
    path = tmp_path / "broken.log"
    path.write_text("not json\n", encoding="utf-8")

    with pytest.raises(LogRecordError, match="broken.log:1"):
        list(iter_event_records(path))